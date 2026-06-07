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

Only write-like BloodHound edge labels are emitted. Unknown edge labels,
unknown target kinds, missing keys, and malformed records are skipped.
"""

import asyncio
import json
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
_CYPHER_TOOL = "cypher_query"
_MCP_TIMEOUT_SECONDS = 15.0


def project_graph_predicates(edge_records: list[dict], now: str, ttl_seconds: int) -> list[GraphFact]:
    """Return GraphFacts mapped from BloodHound edge records.

    GPO edges emit one ``generic-write:gpo:{short_host}`` predicate per linked
    computer. Computer edges emit ``generic-write:computer:{short_host}``.
    Domain edges emit ``write-dacl:domain:{fqdn}``.
    """

    facts: list[GraphFact] = []
    safe_records = edge_records if isinstance(edge_records, list) else []
    for edge_record in safe_records:
        if not isinstance(edge_record, dict):
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


async def reconcile_graph_position(
    mcp_manager: Any,
    controlled_principals: list[str],
    objective: str,
    now: str,
    ttl_seconds: int,
) -> list[GraphFact]:
    """Fetch existing BloodHound graph edges and return projected GraphFacts."""

    del objective
    try:
        tool = await asyncio.wait_for(mcp_manager.get_tool_by_name(_CYPHER_TOOL), timeout=_MCP_TIMEOUT_SECONDS)
        if tool is None:
            return []
        principals = _normalized_unique(controlled_principals)
        if not principals:
            return []
        # Read only: this must not run SharpHound or any data collection tool.
        payload = await asyncio.wait_for(
            tool.ainvoke({"query": _read_only_cypher(), "parameters": {"principals": principals}}),
            timeout=_MCP_TIMEOUT_SECONDS,
        )
        edge_records = _edge_records_from_json(payload)
        return project_graph_predicates(edge_records, now, ttl_seconds)
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
