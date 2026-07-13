"""Project existing BloodHound graph edges into engagement-state predicates.

Expected edge-record shape for pure projection:

{
    "principal": str,
    "type": str,
    "target_kind": str,
    "gpo": str,
    "linked_computers": [str],
    "computer": str,
    "domain": str,
}

Only modeled BloodHound edge labels are emitted: write-like ACL control,
managed-secret read authority, and direct DCSync authority on a domain.
Unknown edge labels, unknown target kinds, missing keys, and malformed records
are skipped.
"""

import asyncio
import json
import re
from typing import Any

try:
    from . import access_reconciler
    from . import engagement_state
except ImportError:  # allow running directly / under pytest sys.path injection
    import access_reconciler
    import engagement_state

EngagementState = engagement_state.EngagementState
GraphFact = engagement_state.GraphFact

_SOURCE = "bloodhound:cypher"
_WRITE_EDGE_TYPES = {"genericwrite", "genericall", "writedacl", "writeowner", "owns"}
_MANAGED_SECRET_EDGE_TYPES = {"readlapspassword", "readlaps", "readmslapspassword"}
_REPLICATION_EDGE_TYPES = {"dcsync"}
_CYPHER_TOOL = "cypher_query"
_MCP_TIMEOUT_SECONDS = 15.0


def project_graph_predicates(edge_records: list[dict], now: str, ttl_seconds: int) -> list[GraphFact]:
    """Return GraphFacts mapped from BloodHound edge records.

    GPO edges emit one ``generic-write:gpo:{short_host}`` predicate per linked
    computer. Computer edges emit ``generic-write:computer:{short_host}``.
    Domain write edges emit ``write-dacl:domain:{fqdn}``. Direct DCSync edges
    to domains emit ``ds-replication-rights:{fqdn}``.
    """

    facts: list[GraphFact] = []
    safe_records = edge_records if isinstance(edge_records, list) else []
    for edge_record in safe_records:
        if not isinstance(edge_record, dict):
            continue
        if _is_managed_secret_edge(edge_record):
            fact = _managed_secret_fact(edge_record, now, ttl_seconds)
            if fact is not None:
                facts.append(fact)
            continue
        if _is_replication_edge(edge_record):
            fact = _replication_fact(edge_record, now, ttl_seconds)
            if fact is not None:
                facts.append(fact)
            continue
        if not _is_write_edge(edge_record):
            continue
        target_kind = _text(edge_record.get("target_kind")).casefold()
        if target_kind == "gpo":
            facts.extend(_gpo_facts(edge_record, now, ttl_seconds))
        elif target_kind == "computer":
            fact = _computer_fact(edge_record, now, ttl_seconds)
            if fact is not None:
                facts.append(fact)
        elif target_kind == "domain":
            fact = _domain_fact(edge_record, now, ttl_seconds)
            if fact is not None:
                facts.append(fact)
    return facts


def project_graph_position(edge_records: list[dict], controlled_principals: list[str], now: str) -> dict:
    """Return a compact graph-position view for DECIDE prompts."""

    del now
    principals = _normalized_unique(controlled_principals)
    candidate_edges: list[dict] = []
    safe_records = edge_records if isinstance(edge_records, list) else []
    for edge_record in safe_records:
        if not isinstance(edge_record, dict):
            continue
        edge = _candidate_edge(edge_record)
        if edge:
            candidate_edges.append(edge)
    return {"current_principals": principals, "candidate_edges": candidate_edges}


def controlled_principals_from_state(state: EngagementState) -> list[str]:
    """Derive the BloodHound principal names we control, for the reconcile cypher's
    ``toLower(principal.name) IN $principals`` match. Built from LIVE foothold identities
    (``DOMAIN\\user`` or ``user@domain``) projected to the UPN form ``user@forest`` that BloodHound
    stores as a node ``.name``, any ``creds:user@domain`` effects already achieved (a DCSynced /
    dumped principal we can now act as), and live domain-admin authority projected to the privileged
    group principals BloodHound stores in the graph. Pure; never raises."""
    principals: set[str] = set()
    try:
        for foothold in getattr(state, "footholds", []) or []:
            if not getattr(foothold, "alive", False):
                continue
            identity = _text(getattr(foothold, "identity", ""))
            forest = _text(getattr(foothold, "forest", "")).casefold()
            if "\\" in identity:
                user = identity.split("\\", 1)[1]
            elif "@" in identity:
                user = identity.split("@", 1)[0]
            else:
                user = identity
            user = user.casefold().strip()
            if "@" in identity:
                principals.add(identity.casefold())
            if user and forest:
                principals.add(f"{user}@{forest}")
        for effect in state.achieved_effects():
            if effect.startswith("creds:"):
                value = effect[len("creds:"):].strip()
                if "@" in value:
                    principals.add(value.casefold())
        predicates = state.satisfied_predicates()
        for predicate in sorted(predicates):
            if predicate.startswith("da:"):
                domain = predicate[len("da:"):].strip()
                if _has_live_domain_authority(predicates, domain):
                    principals.add(f"domain admins@{domain}".casefold())
                    principals.add(f"administrators@{domain}".casefold())
            elif predicate.startswith("ea:"):
                domain = predicate[len("ea:"):].strip()
                if _has_live_domain_authority(predicates, domain):
                    principals.add(f"enterprise admins@{domain}".casefold())
    except Exception:
        pass
    return sorted(principal for principal in principals if principal)


def prune_stale_graph_facts(state: EngagementState, now: str) -> EngagementState:
    """Return a new state with stale graph facts removed."""

    current = access_reconciler._parse_iso_datetime(now)
    pruned: list[GraphFact] = []
    for graph_fact in getattr(state, "graph_facts", []):
        observed_at = access_reconciler._parse_iso_datetime(_text(getattr(graph_fact, "timestamp", "")))
        ttl_seconds = _int_or_none(getattr(graph_fact, "ttl_seconds", None))
        if observed_at is None or current is None or ttl_seconds is None:
            continue
        if (current - observed_at).total_seconds() <= ttl_seconds:
            pruned.append(graph_fact)
    return EngagementState(
        objective=state.objective,
        footholds=list(state.footholds),
        hops=list(state.hops),
        graph_facts=pruned,
    )


_WRITE_LABELS_CYPHER = "['GenericWrite', 'GenericAll', 'WriteDacl', 'WriteOwner', 'Owns']"
_MANAGED_SECRET_LABELS_CYPHER = "['ReadLAPSPassword', 'ReadLAPS', 'ReadMSLAPSPassword']"
_REPLICATION_LABELS_CYPHER = "['DCSync']"


async def reconcile_graph_position(
    mcp_manager: Any,
    controlled_principals: list[str],
    objective: str,
    now: str,
    ttl_seconds: int,
    credential_domains: list[str] | None = None,
) -> list[GraphFact]:
    """Project modeled BloodHound edges for controlled principals into engagement
    predicates via the BloodHound MCP ``cypher_query`` tool.

    The real BloodHound CE cypher API (1) requires ``info_type="run"``, (2) does NOT support query
    parameters, and (3) returns scalar RETURNs under ``data.literals`` (a flat ``{value,key}`` list), NOT
    a record/edge list. So we inline the principal allowlist, run one focused single-column query per
    target kind, and read names out of ``literals``. GPO control is keyed by GPO NAME (matching
    intent_classifier's ``gpo-abuse`` target = ``--gponame``), computers by short host, domains by FQDN.
    Read-only; never runs collection. Fail-open -> []."""
    del objective
    try:
        tool = await asyncio.wait_for(mcp_manager.get_tool_by_name(_CYPHER_TOOL), timeout=_MCP_TIMEOUT_SECONDS)
        if tool is None:
            return []
        principals = _normalized_unique(controlled_principals)
        domains = _normalized_unique(credential_domains or [])
        if not principals and not domains:
            return []
        principals = await _resolve_principal_aliases(tool, principals)
        inlist = _principal_in_list(principals) if principals else "[]"
        facts: list[GraphFact] = []
        seen: set[str] = set()

        async def _collect(target_label: str, predicate_prefix: str, key_fn) -> None:
            if not principals:
                return
            query = (
                f"MATCH (p)-[e]->(t:{target_label}) WHERE toLower(p.name) IN {inlist} "
                f"AND type(e) IN {_WRITE_LABELS_CYPHER} RETURN DISTINCT t.name AS name"
            )
            for raw_name in await _run_scalar_names(tool, query):
                key = key_fn(raw_name)
                if not key:
                    continue
                predicate = f"{predicate_prefix}:{key}"
                if predicate not in seen:
                    seen.add(predicate)
                    facts.append(_graph_fact(predicate, now, ttl_seconds))

        # GPO control: emit the control fact (keyed by NAME, matching SharpGPOAbuse --gponame) AND the
        # GPO->domain link, so the forward planner can effect-chain gpo-abuse -> dcsync on the domain whose
        # DC the GPO governs (the SharpGPOAbuse SYSTEM task grants DS-Replication there).
        if principals:
            gpo_query = (
                f"MATCH (p)-[e]->(t:GPO) WHERE toLower(p.name) IN {inlist} "
                f"AND type(e) IN {_WRITE_LABELS_CYPHER} "
                "RETURN DISTINCT t.name + '|' + coalesce(t.distinguishedname, '') + '|' "
                "+ coalesce(t.gpcpath, '') + '|' + coalesce(t.objectid, '') AS name"
            )
            for raw_name in await _run_scalar_names(tool, gpo_query):
                raw_name, gpo_guid = _gpo_scalar_parts(raw_name)
                gpo = _gpo_name_key(raw_name)
                if not gpo:
                    continue
                for predicate in (
                    f"generic-write:gpo:{gpo}",
                    _gpo_domain_fact(raw_name, gpo),
                    _gpo_guid_fact(gpo, gpo_guid),
                ):
                    if predicate and predicate not in seen:
                        seen.add(predicate)
                        facts.append(_graph_fact(predicate, now, ttl_seconds))
            # BloodHound CE's cypher API rejects `WITH ... any(...) AS isDc` + `CASE WHEN` (observed live: the
            # old combined scope query silently returned nothing, so `gpo-affects-dc` was never produced and a
            # controlled GPO that governs a DC never gained its `da:` effect -> the deterministic controller
            # could not escalate via the GPO). Split into two CE-compatible queries: all governed computers
            # (|0), and DC-governed only (DC filtered in WHERE, |1). The `seen` set dedupes the
            # `gpo-affects-computer` predicate a DC yields from both passes.
            gpo_scope_queries = (
                (f"MATCH (p)-[e]->(g:GPO) WHERE toLower(p.name) IN {inlist} "
                 f"AND type(e) IN {_WRITE_LABELS_CYPHER} "
                 "MATCH (g)-[:GPLink]->(container)-[:Contains*1..4]->(comp:Computer) "
                 "RETURN DISTINCT g.name + '|' + comp.name + '|' + coalesce(comp.domain, '') + '|0' AS name"),
                (f"MATCH (p)-[e]->(g:GPO) WHERE toLower(p.name) IN {inlist} "
                 f"AND type(e) IN {_WRITE_LABELS_CYPHER} "
                 "MATCH (g)-[:GPLink]->(container)-[:Contains*1..4]->(comp:Computer)-[:MemberOf*1..]->(dcg:Group) "
                 "WHERE coalesce(dcg.objectid, '') ENDS WITH '-516' "
                 "RETURN DISTINCT g.name + '|' + comp.name + '|' + coalesce(comp.domain, '') + '|1' AS name"),
            )
            for gpo_scope_query in gpo_scope_queries:
                for raw_name in await _run_scalar_names(tool, gpo_scope_query):
                    for predicate in _gpo_scope_facts_from_scalar(raw_name):
                        if predicate and predicate not in seen:
                            seen.add(predicate)
                            facts.append(_graph_fact(predicate, now, ttl_seconds))
        await _collect("Computer", "generic-write:computer", _short_host)
        await _collect("Domain", "write-dacl:domain", lambda n: access_reconciler.normalize_forest(_text(n)))
        if principals:
            replication_query = (
                f"MATCH (p)-[e]->(t:Domain) WHERE toLower(p.name) IN {inlist} "
                f"AND type(e) IN {_REPLICATION_LABELS_CYPHER} RETURN DISTINCT t.name AS name"
            )
            for raw_name in await _run_scalar_names(tool, replication_query):
                domain = access_reconciler.normalize_forest(_text(raw_name))
                predicate = f"ds-replication-rights:{domain}" if domain else ""
                if predicate and predicate not in seen:
                    seen.add(predicate)
                    facts.append(_graph_fact(predicate, now, ttl_seconds))
        if principals:
            laps_query = (
                f"MATCH (p)-[e]->(c:Computer) WHERE toLower(p.name) IN {inlist} "
                f"AND type(e) IN {_MANAGED_SECRET_LABELS_CYPHER} "
                "RETURN DISTINCT p.name + '|' + c.name + '|' + coalesce(c.domain, '') AS name"
            )
            for raw_name in await _run_scalar_names(tool, laps_query):
                fact = _managed_secret_fact_from_scalar(raw_name, now, ttl_seconds)
                if fact is not None and fact.predicate not in seen:
                    seen.add(fact.predicate)
                    facts.append(fact)
        if principals and domains:
            domain_filter = _user_domain_filter("u.name", domains)
            controlled_group_member_query = (
                "MATCH (p)-[e]->(g:Group) "
                f"WHERE toLower(p.name) IN {inlist} AND type(e) IN {_WRITE_LABELS_CYPHER} "
                "MATCH (u:User)-[:MemberOf*1..4]->(g) "
                f"WHERE ({domain_filter}) "
                "RETURN DISTINCT u.name AS name"
            )
            for raw_name in await _run_scalar_names(tool, controlled_group_member_query):
                fact = _credential_target_fact_from_scalar(raw_name, now, ttl_seconds)
                if fact is not None and fact.predicate not in seen:
                    seen.add(fact.predicate)
                    facts.append(fact)
        if domains:
            domain_filter = _user_domain_filter("u.name", domains)
            cross_forest_laps_query = (
                "MATCH (u:User)-[:MemberOf*0..4]->(p)-[e]->(c:Computer) "
                f"WHERE ({domain_filter}) AND type(e) IN {_MANAGED_SECRET_LABELS_CYPHER} "
                "RETURN DISTINCT u.name + '|' + c.name + '|' + coalesce(c.domain, '') AS name"
            )
            for raw_name in await _run_scalar_names(tool, cross_forest_laps_query):
                for fact in (
                    _credential_target_fact_from_scalar(raw_name, now, ttl_seconds),
                    _managed_secret_fact_from_scalar(raw_name, now, ttl_seconds),
                ):
                    if fact is not None and fact.predicate not in seen:
                        seen.add(fact.predicate)
                        facts.append(fact)
        trust_query = (
            "MATCH (source:Domain)-[edge]->(target:Domain) "
            "WHERE type(edge) CONTAINS 'Trust' "
            "RETURN DISTINCT source.name + '|' + target.name AS name"
        )
        for raw_name in await _run_scalar_names(tool, trust_query):
            predicate = _trust_reachable_fact_from_scalar(raw_name)
            if predicate and predicate not in seen:
                seen.add(predicate)
                facts.append(_graph_fact(predicate, now, ttl_seconds))
        return facts
    except Exception:
        return []


def _has_live_domain_authority(predicates: set[str], domain: str) -> bool:
    domain = access_reconciler.normalize_forest(_text(domain))
    return bool(
        domain
        and (
            f"kerberos-context:{domain}" in predicates
            or f"ds-replication-rights:{domain}" in predicates
        )
    )


def credential_target_domains_from_state(state: EngagementState) -> list[str]:
    """Domains where the current state has replication rights and can extract real user keys."""
    try:
        domains = {
            predicate[len("ds-replication-rights:"):].strip()
            for predicate in state.satisfied_predicates()
            if predicate.startswith("ds-replication-rights:")
        }
        return sorted(domain for domain in domains if domain)
    except Exception:
        return []


def _gpo_domain_fact(raw_name: Any, gpo_key: str) -> str:
    """The ``gpo-domain:{gpo}:{domain}`` link fact (domain parsed from the GPO name's ``@suffix``), or ''.
    Lets the planner chain gpo-abuse -> dcsync on the GPO's domain — the SharpGPOAbuse SYSTEM task grants
    DS-Replication on the DC the GPO governs, so controlling the GPO implies eventual replication rights."""
    text = _text(raw_name)
    if "@" not in text:
        return ""
    domain = access_reconciler.normalize_forest(text.split("@", 1)[1])
    return f"gpo-domain:{gpo_key}:{domain}" if domain else ""


def _gpo_guid_fact(gpo_key: str, gpo_guid: Any) -> str:
    guid = _normalize_guid(gpo_guid)
    return f"gpo-guid:{gpo_key}:{guid}" if gpo_key and guid else ""


def _gpo_scope_facts_from_scalar(value: Any) -> list[str]:
    text = _text(value)
    if "|" not in text:
        return []
    raw_gpo, raw_computer, raw_domain, raw_is_dc, *_ = text.split("|")
    gpo = _gpo_name_key(raw_gpo)
    host = _short_host(raw_computer)
    domain = access_reconciler.normalize_forest(_text(raw_domain)) or _domain_from_computer_name(raw_computer)
    if not gpo or not host or not domain:
        return []
    facts = [f"gpo-affects-computer:{gpo}:{host}:{domain}"]
    if _text(raw_is_dc).casefold() in {"1", "true", "yes", "dc"}:
        facts.append(f"gpo-affects-dc:{gpo}:{host}:{domain}")
    return facts


def _trust_reachable_fact_from_scalar(value: Any) -> str:
    text = _text(value)
    if "|" not in text:
        return ""
    raw_source, raw_target, *_ = text.split("|")
    source = access_reconciler.normalize_forest(raw_source)
    target = access_reconciler.normalize_forest(raw_target)
    if not source or not target or source == target:
        return ""
    return f"trust-reachable:{source}:{target}"


def _gpo_scalar_parts(value: Any) -> tuple[str, str]:
    text = _text(value)
    if "|" not in text:
        return text, ""
    name, *parts = text.split("|")
    return name, _extract_policy_guid(parts)


def _extract_policy_guid(parts: list[str]) -> str:
    """Return the GPO policy container GUID, not the BloodHound objectid.

    BloodHound exposes ``objectid`` for GPO nodes, but SharpGPOAbuse/GPP file writes need the LDAP/SYSVOL
    policy container name: ``CN={guid},CN=Policies,...`` or ``\\SYSVOL\\...\\Policies\\{guid}``.
    """
    for part in parts:
        text = _text(part)
        match = re.search(r"\{([0-9a-fA-F-]{36})\}", text)
        if match:
            return _normalize_guid(match.group(1))
    return ""


def _normalize_guid(value: Any) -> str:
    text = _text(value).strip().strip("{}")
    if not text:
        return ""
    if len(text) != 36:
        return ""
    return text.casefold()


def _domain_from_computer_name(value: Any) -> str:
    text = _text(value).strip().strip("@").casefold()
    if "." not in text:
        return ""
    return access_reconciler.normalize_forest(text.split(".", 1)[1])


def _principal_in_list(principals: list[str]) -> str:
    """Inline a principal allowlist as a Cypher list literal (the BloodHound cypher API takes no params)."""
    safe = [p.replace("\\", "").replace("'", "") for p in principals if p]
    return "[" + ", ".join(f"'{p}'" for p in safe) + "]"


async def _resolve_principal_aliases(tool: Any, principals: list[str]) -> list[str]:
    """Resolve ``user@NETBIOS`` names to BloodHound's FQDN UPN when exactly one node matches."""
    resolved: list[str] = []
    for principal in _normalized_unique(principals):
        account, separator, domain = principal.partition("@")
        if not separator or not account or not domain or "." in domain:
            resolved.append(principal)
            continue
        safe_prefix = f"{account}@{domain}".replace("\\", "").replace("'", "")
        query = (
            "MATCH (u:User) "
            f"WHERE toLower(u.name) = '{safe_prefix}' "
            f"OR toLower(u.name) STARTS WITH '{safe_prefix}.' "
            "RETURN DISTINCT u.name AS name"
        )
        matches = [
            name.casefold()
            for name in await _run_scalar_names(tool, query)
            if name.casefold() == safe_prefix or name.casefold().startswith(f"{safe_prefix}.")
        ]
        aliases = _normalized_unique(matches)
        resolved.append(aliases[0] if len(aliases) == 1 else principal)
    return _normalized_unique(resolved)


def _user_domain_filter(property_name: str, domains: list[str]) -> str:
    clauses = []
    for domain in _normalized_unique(domains):
        safe = domain.replace("\\", "").replace("'", "")
        if safe:
            clauses.append(f"toLower({property_name}) ENDS WITH '@{safe}'")
    return " OR ".join(clauses) if clauses else "false"


def _gpo_name_key(name: Any) -> str:
    """GPO predicate key = the display name before '@domain', lowercased (matches SharpGPOAbuse --gponame)."""
    text = _text(name)
    return text.split("@", 1)[0].strip().casefold() if text else ""


async def _run_scalar_names(tool: Any, query: str) -> list[str]:
    """Run a single-column Cypher RETURN via the MCP cypher_query tool and read the values from the
    BloodHound ``data.literals`` response shape. Fail-open -> []."""
    try:
        payload = await asyncio.wait_for(
            tool.ainvoke({"info_type": "run", "query": query, "include_properties": False}),
            timeout=_MCP_TIMEOUT_SECONDS,
        )
    except Exception:
        return []
    return _names_from_payload(payload)


def _names_from_payload(payload: Any) -> list[str]:
    """Extract scalar values from a BloodHound MCP cypher_query 'run' response (``data.literals``).
    Handles MCP content-list wrapping plus a raw dict / JSON string. Never raises."""
    try:
        text = payload
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            text = payload[0].get("text", "")
        data = json.loads(text) if isinstance(text, str) else (text or {})
        literals = (((data or {}).get("data") or {}).get("literals")) or []
        names: list[str] = []
        for literal in literals:
            if isinstance(literal, dict):
                value = literal.get("value")
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
        return names
    except Exception:
        return []


def _gpo_facts(edge_record: dict, now: str, ttl_seconds: int) -> list[GraphFact]:
    if not _text(edge_record.get("gpo")):
        return []
    linked_computers = edge_record.get("linked_computers")
    if not isinstance(linked_computers, list):
        return []
    facts: list[GraphFact] = []
    for linked_computer in linked_computers:
        host = _short_host(linked_computer)
        if not host:
            continue
        facts.append(_graph_fact(f"generic-write:gpo:{host}", now, ttl_seconds))
    return facts


def _computer_fact(edge_record: dict, now: str, ttl_seconds: int) -> GraphFact | None:
    host = _short_host(edge_record.get("computer"))
    if not host:
        return None
    return _graph_fact(f"generic-write:computer:{host}", now, ttl_seconds)


def _domain_fact(edge_record: dict, now: str, ttl_seconds: int) -> GraphFact | None:
    domain = access_reconciler.normalize_forest(_text(edge_record.get("domain")))
    if not domain:
        return None
    return _graph_fact(f"write-dacl:domain:{domain}", now, ttl_seconds)


def _replication_fact(edge_record: dict, now: str, ttl_seconds: int) -> GraphFact | None:
    if _text(edge_record.get("target_kind")).casefold() != "domain":
        return None
    domain = access_reconciler.normalize_forest(_text(edge_record.get("domain")))
    if not domain:
        return None
    return _graph_fact(f"ds-replication-rights:{domain}", now, ttl_seconds)


def _graph_fact(predicate: str, now: str, ttl_seconds: int) -> GraphFact:
    return GraphFact(predicate=predicate, source=_SOURCE, timestamp=now, ttl_seconds=ttl_seconds)


def _candidate_edge(edge_record: dict) -> dict:
    principal = _text(edge_record.get("principal"))
    edge_type = _text(edge_record.get("type"))
    target_kind = _text(edge_record.get("target_kind")).casefold()
    target = _candidate_target(edge_record, target_kind)
    if not principal or not edge_type or not target_kind or not target:
        return {}
    return {"principal": principal.casefold(), "type": edge_type, "target_kind": target_kind, "target": target}


def _candidate_target(edge_record: dict, target_kind: str) -> str:
    if target_kind == "gpo":
        gpo = _text(edge_record.get("gpo"))
        if gpo:
            return gpo
        linked_computers = edge_record.get("linked_computers")
        if isinstance(linked_computers, list):
            hosts = [_short_host(item) for item in linked_computers]
            return ",".join(host for host in hosts if host)
        return ""
    if target_kind == "computer":
        return _short_host(edge_record.get("computer"))
    if target_kind == "domain":
        return access_reconciler.normalize_forest(_text(edge_record.get("domain")))
    return ""


def _is_write_edge(edge_record: dict) -> bool:
    return _text(edge_record.get("type")).casefold() in _WRITE_EDGE_TYPES


def _is_managed_secret_edge(edge_record: dict) -> bool:
    return _text(edge_record.get("type")).casefold() in _MANAGED_SECRET_EDGE_TYPES


def _is_replication_edge(edge_record: dict) -> bool:
    return _text(edge_record.get("type")).casefold() in _REPLICATION_EDGE_TYPES


def _managed_secret_fact(edge_record: dict, now: str, ttl_seconds: int) -> GraphFact | None:
    principal = _text(edge_record.get("principal"))
    account, account_domain = _principal_account_domain(principal)
    target = (
        edge_record.get("computer")
        or edge_record.get("target")
        or edge_record.get("target_name")
        or edge_record.get("name")
    )
    target_host, target_domain = _host_domain(target)
    if not target_domain:
        target_domain = access_reconciler.normalize_forest(_text(edge_record.get("domain")))
    if not (account and account_domain and target_host and target_domain):
        return None
    predicate = (
        "can-read-managed-local-admin-secret:"
        f"account={account};account_domain={account_domain};target={target_host};target_domain={target_domain}"
    )
    return _graph_fact(predicate, now, ttl_seconds)


def _managed_secret_fact_from_scalar(value: Any, now: str, ttl_seconds: int) -> GraphFact | None:
    parts = [_text(part) for part in _text(value).split("|")]
    if len(parts) < 2:
        return None
    principal = parts[0]
    computer = parts[1]
    domain = parts[2] if len(parts) > 2 else ""
    return _managed_secret_fact(
        {
            "principal": principal,
            "type": "ReadLAPSPassword",
            "target_kind": "computer",
            "computer": computer,
            "domain": domain,
        },
        now,
        ttl_seconds,
    )


def _credential_target_fact_from_scalar(value: Any, now: str, ttl_seconds: int) -> GraphFact | None:
    parts = [_text(part) for part in _text(value).split("|")]
    if not parts:
        return None
    account, domain = _principal_account_domain(parts[0])
    if not (account and domain) or account == "krbtgt" or account.endswith("$"):
        return None
    return _graph_fact(f"credential-target:{account}@{domain}", now, ttl_seconds)


def _principal_account_domain(value: Any) -> tuple[str, str]:
    text = _text(value).strip().casefold()
    if not text:
        return "", ""
    if "\\" in text:
        domain, _, account = text.partition("\\")
        return account.strip(), access_reconciler.normalize_forest(domain)
    if "@" in text:
        account, _, domain = text.partition("@")
        return account.strip(), access_reconciler.normalize_forest(domain)
    return "", ""


def _host_domain(value: Any) -> tuple[str, str]:
    text = _text(value).strip().strip("\\/").casefold()
    if not text:
        return "", ""
    if "@" in text:
        text = text.split("@", 1)[0]
    if text.endswith("$"):
        text = text[:-1]
    parts = [part for part in text.split(".") if part]
    if len(parts) >= 3:
        return parts[0], ".".join(parts[1:])
    return _short_host(text), ""


def _short_host(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    return text.split(".", 1)[0].strip().casefold()


def _normalized_unique(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    safe_values = values if isinstance(values, list) else []
    for value in safe_values:
        text = _text(value).casefold()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _edge_records_from_json(raw_payload: Any) -> list[dict]:
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except (TypeError, json.JSONDecodeError):
        return []
    return _edge_records_from_payload(payload)


def _edge_records_from_payload(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return _edge_records_from_items(payload)
    if not isinstance(payload, dict):
        return []
    records = payload.get("records")
    if isinstance(records, list):
        return _edge_records_from_items(records)
    for key in ("edges", "data", "result", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return _edge_records_from_items(value)
        if isinstance(value, dict):
            nested = _edge_records_from_payload(value)
            if nested:
                return nested
    edge_record = _edge_record_from_item(payload)
    return [edge_record] if edge_record else []


def _edge_records_from_items(items: list[Any]) -> list[dict]:
    edge_records: list[dict] = []
    for item in items:
        edge_record = _edge_record_from_item(item)
        if edge_record:
            edge_records.append(edge_record)
    return edge_records


def _edge_record_from_item(item: Any) -> dict:
    if not isinstance(item, dict):
        return {}
    if "row" in item and isinstance(item["row"], dict):
        return _edge_record_from_item(item["row"])
    if "record" in item and isinstance(item["record"], dict):
        return _edge_record_from_item(item["record"])
    return dict(item)


def _read_only_cypher() -> str:
    return (
        "MATCH (principal)-[edge]->(target) "
        "WHERE toLower(principal.name) IN $principals "
        "AND type(edge) IN ['GenericWrite', 'GenericAll', 'WriteDacl', 'WriteOwner', 'Owns'] "
        "AND (target:GPO OR target:Computer OR target:Domain) "
        "RETURN principal.name AS principal, type(edge) AS type, "
        "CASE WHEN target:GPO THEN 'gpo' WHEN target:Computer THEN 'computer' "
        "WHEN target:Domain THEN 'domain' ELSE '' END AS target_kind, "
        "target.name AS gpo, target.name AS computer, target.name AS domain, "
        "[(target)-[:GPLink|:Contains*1..2]->(linked:Computer) | linked.name] AS linked_computers"
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
