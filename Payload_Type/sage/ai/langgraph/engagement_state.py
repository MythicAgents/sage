"""Pure engagement-state model: observed footholds/hops/graph facts, verify-on-record effect
verification, and observed-state phase classification. The STRIPS planner-as-gate (the reactive
precondition gate plus forward-planner hop enumeration) was retired in the engagement-gate retirement;
what remains derives state and verdicts from achieved effects and live signals, never from closed-world
precondition planning."""

from dataclasses import dataclass, field
from typing import Any
import re


@dataclass
class Foothold:
    callback_id: str
    agent: str
    host: str
    forest: str
    identity: str
    integrity: str
    alive: bool
    source: str
    timestamp: str


@dataclass
class Hop:
    id: str
    technique: str
    target: str
    effect: str
    status: str
    evidence: dict
    preconditions: list[str]
    satisfied_effects: list[str]
    source: str
    timestamp: str


@dataclass
class GraphFact:
    predicate: str
    source: str
    timestamp: str
    ttl_seconds: int


@dataclass
class EngagementState:
    objective: str
    footholds: list[Foothold] = field(default_factory=list)
    hops: list[Hop] = field(default_factory=list)
    graph_facts: list[GraphFact] = field(default_factory=list)
    # Effect-prefixes the caller actively READ-PROBED this gate call (e.g. {"creds","krbtgt-hash"} when the
    # credential store was read). Lets the gate distinguish "durable hop's artifact is genuinely GONE
    # (probed, absent → re-run legit)" from "no probe available → trust the ledger, do NOT re-run".
    probed_effect_prefixes: set[str] = field(default_factory=set)

    def achieved_effects(self) -> set[str]:
        """Return normalized effects from achieved hops."""
        effects: set[str] = set()
        for hop in self.hops:
            if _text(getattr(hop, "status", "")).casefold() != "achieved":
                continue
            for effect in getattr(hop, "satisfied_effects", []):
                normalized = _normalize_predicate(effect)
                if normalized:
                    effects.add(normalized)
        return effects

    def satisfied_predicates(self) -> set[str]:
        """Return predicates satisfied by achieved hops AND live footholds/graph facts, plus logical
        implications (e.g. da/ea plus a live Kerberos context implies that domain's replication rights)."""
        predicates = self.achieved_effects() | foothold_predicates(self)
        filtered = _filter_live_kerberos_context_predicates(predicates)
        expanded = _expand_implications(filtered)
        _trace_rights_decision(filtered, expanded)  # DIAGNOSTIC: remove after krbtgt-variance root-caused
        return expanded

    def satisfies_predicate(self, predicate: str) -> bool:
        """Return whether a predicate is satisfied by the current state."""
        return _normalize_predicate(predicate) in self.satisfied_predicates()


def access_context_key(state: "EngagementState", foothold: "Foothold") -> str:
    """A stable key for the collection-relevant authority epoch.

    This is deliberately not a raw session fingerprint. SharpHound should not re-run merely because a callback
    identity string, integrity label, dumped hash, or Kerberos proof changed. A new key is created only when the
    modeled authority that can change graph visibility changes:

    - the foothold's baseline forest scope;
    - active ``da:`` / ``ea:`` authority in that scope;
    - active cross-domain ``da:`` / ``ea:`` authority only after a callback-scoped Kerberos context proves that
      authority is actually in use on this callback.

    Same-domain Kerberos proof does not create a second epoch after DA is already recorded. This keeps the key
    aligned with the reason to collect: a new graph-visible authority, not an authentication artifact. Pure;
    never raises."""
    try:
        scope = (
            _normalize_key(getattr(foothold, "forest", ""))
            or _identity_domain(getattr(foothold, "identity", ""))
            or _normalize_key(getattr(foothold, "host", ""))
        )
        if not scope:
            return ""
        authorities = sorted(_collection_auth_context_effects(state, foothold))
        epoch = ";".join(authorities) or "baseline"
        return _normalize_predicate(f"{scope}|{epoch}")
    except Exception:
        return ""


def collection_target_key(
    state: "EngagementState",
    foothold: "Foothold",
    scope_domain: str = "",
) -> str:
    """Return the durable collect-graph target key for one authority epoch plus one requested scope.

    Targeted domain collections append ``|scope:<domain>`` so the same authority epoch can legitimately collect
    an external trusted domain without overwriting or being suppressed by the current-forest collection."""
    try:
        access_key = access_context_key(state, foothold)
        if not access_key:
            return ""
        scope = _normalize_key(scope_domain)
        if not scope:
            return access_key
        return _normalize_predicate(f"{access_key}|scope:{scope}")
    except Exception:
        return ""


def graph_collection_covers_scope(
    state: "EngagementState",
    foothold: "Foothold",
    scope_domain: str = "",
) -> bool:
    """Return whether verified ingest evidence covers a requested collection scope for this auth context."""
    try:
        target_key = collection_target_key(state, foothold, scope_domain)
        if not target_key:
            return False
        graph_effect = _normalize_predicate(f"graph-built:{target_key}")
        expected_domain = _normalize_key(scope_domain) or _normalize_key(getattr(foothold, "forest", ""))
        matched_verified_hop = False
        for hop in getattr(state, "hops", []) or []:
            if _text(getattr(hop, "status", "")).casefold() != "achieved":
                continue
            effects = {
                _normalize_predicate(effect)
                for effect in getattr(hop, "satisfied_effects", []) or []
            }
            effects.add(_normalize_predicate(getattr(hop, "effect", "")))
            if graph_effect not in effects:
                continue
            evidence = getattr(hop, "evidence", {}) or {}
            if not isinstance(evidence, dict) or evidence.get("graph_verified") is not True:
                continue
            matched_verified_hop = True
            if not expected_domain:
                return True
            covered_domains = evidence.get("covered_domains") or []
            if any(_domains_equivalent(expected_domain, domain) for domain in covered_domains):
                return True
        if not matched_verified_hop or not expected_domain:
            return False
        return any(
            _domains_equivalent(
                expected_domain,
                _text(getattr(fact, "predicate", ""))[len("domain-collected:"):],
            )
            for fact in getattr(state, "graph_facts", []) or []
            if _text(getattr(fact, "predicate", "")).casefold().startswith("domain-collected:")
        )
    except Exception:
        return False


def graph_collection_covers_foothold(state: "EngagementState", foothold: "Foothold") -> bool:
    """Return whether verified ingest evidence covers this foothold's default forest scope."""
    return graph_collection_covers_scope(state, foothold)


def graph_domain_has_verified_collection(state: "EngagementState", domain: str) -> bool:
    """Return whether any verified collection has covered ``domain`` in any authority epoch.

    This distinguishes an initial baseline collection from an optional recollection after authority changes.
    The latter is useful only when the objective still needs more graph data; it should not preempt an already
    visible objective-scoped collection or a grounded executable capability."""
    try:
        expected_domain = _normalize_key(domain)
        if not expected_domain:
            return False
        for hop in getattr(state, "hops", []) or []:
            if _text(getattr(hop, "status", "")).casefold() != "achieved":
                continue
            effects = {
                _normalize_predicate(effect)
                for effect in getattr(hop, "satisfied_effects", []) or []
            }
            effects.add(_normalize_predicate(getattr(hop, "effect", "")))
            if not any(effect.startswith("graph-built:") for effect in effects):
                continue
            evidence = getattr(hop, "evidence", {}) or {}
            if not isinstance(evidence, dict) or evidence.get("graph_verified") is not True:
                continue
            covered_domains = evidence.get("covered_domains") or []
            if any(_domains_equivalent(expected_domain, covered) for covered in covered_domains):
                return True
        return any(
            _domains_equivalent(
                expected_domain,
                _text(getattr(fact, "predicate", ""))[len("domain-collected:"):],
            )
            for fact in getattr(state, "graph_facts", []) or []
            if _text(getattr(fact, "predicate", "")).casefold().startswith("domain-collected:")
        )
    except Exception:
        return False


def trusted_uncollected_domains(state: "EngagementState") -> list[str]:
    """Return trusted domains visible in the graph but not yet fully collected.

    Only trust edges whose source domain is already collected are considered. This avoids chasing stub domains
    from incomplete graph data. Objective-target domains sort first; remaining candidates are stable-sorted."""
    try:
        collected = {
            _normalize_key(_text(getattr(fact, "predicate", ""))[len("domain-collected:"):])
            for fact in getattr(state, "graph_facts", []) or []
            if _text(getattr(fact, "predicate", "")).casefold().startswith("domain-collected:")
        }
        candidates: set[str] = set()
        for fact in getattr(state, "graph_facts", []) or []:
            predicate = _normalize_predicate(getattr(fact, "predicate", ""))
            if not predicate.startswith("trust-reachable:"):
                continue
            source, sep, target = predicate[len("trust-reachable:"):].partition(":")
            source = _normalize_key(source)
            target = _normalize_key(target)
            if not sep or not source or not target:
                continue
            if not any(_domains_equivalent(source, domain) for domain in collected):
                continue
            if any(_domains_equivalent(target, domain) for domain in collected):
                continue
            candidates.add(target)
        objective_targets = {
            _normalize_key(domain)
            for domain in _objective_target_domains(getattr(state, "objective", ""))
            if _normalize_key(domain)
        }
        return sorted(
            candidates,
            key=lambda domain: (0 if any(_domains_equivalent(domain, target) for target in objective_targets) else 1, domain),
        )
    except Exception:
        return []


def _collection_auth_context_effects(state: "EngagementState", foothold: "Foothold") -> set[str]:
    """Canonical active DA/EA authority effects that can change SharpHound graph visibility.

    A Kerberos context is only an activation witness for authority in another domain. It is not itself a reason
    to recollect, and an account-context proof without modeled DA/EA authority is intentionally ignored."""
    callback_id = _normalize_key(getattr(foothold, "callback_id", ""))
    foothold_domain = _normalize_key(getattr(foothold, "forest", ""))
    active_domains: set[str] = {foothold_domain} if foothold_domain else set()
    achieved = state.achieved_effects()
    for effect in achieved:
        parsed = _parse_kerberos_context_effect(effect) or _parse_kerberos_account_context_effect(effect)
        if not parsed:
            continue
        context_domain, context_callback_id = parsed
        if callback_id and context_callback_id == callback_id:
            active_domains.add(context_domain)

    effects: set[str] = set()
    for effect in achieved:
        if effect.startswith(("da:", "ea:")):
            authority, _, domain = effect.partition(":")
            canonical_domain = _canonical_active_collection_domain(domain, active_domains)
            if canonical_domain:
                effects.add(f"{authority}:{canonical_domain}")
    return effects


def foothold_predicates(state: "EngagementState") -> set[str]:
    """Predicates derived ONLY from live footholds and graph facts — NOT from hops.

    This is the independent-evidence set used to corroborate a durable (loaded-from-disk) achieved hop:
    a hop must not corroborate itself, so corroboration consults live signal only."""
    predicates: set[str] = set()
    for foothold in getattr(state, "footholds", []) or []:
        if not _is_live_target_foothold(foothold):
            continue
        predicates.add("live-foothold:*")
        callback_id = _normalize_key(getattr(foothold, "callback_id", ""))
        host = _normalize_key(getattr(foothold, "host", ""))
        forest = _normalize_key(getattr(foothold, "forest", ""))
        identity_domain = _identity_domain(getattr(foothold, "identity", ""))
        integrity = _normalize_key(getattr(foothold, "integrity", ""))
        if callback_id:
            predicates.add(f"live-callback:{callback_id}")
        if forest:
            predicates.add(f"live-foothold:{forest}")
            predicates.add(f"authenticated:{forest}")
        if identity_domain:
            predicates.add(f"authenticated:{identity_domain}")
        if host:
            predicates.add(f"live-host:{host}")
        if host and integrity in _ADMIN_INTEGRITY:
            predicates.add(f"system-or-admin:{host}")
            predicates.add(f"admin:{host}")
        if host and integrity == "system":
            predicates.add(f"system:{host}")
    for graph_fact in getattr(state, "graph_facts", []) or []:
        predicate = _normalize_predicate(getattr(graph_fact, "predicate", ""))
        if predicate:
            predicates.add(predicate)
    return predicates


# Effect implications: holding one effect can logically grant others, so a precondition can be satisfied
# without a separate hop. DA/EA only unlocks remote replication when that authorization is live in a callback's
# Kerberos context; a durable ticket proof on a dead callback is not enough to run a new DCSync.
_DA_EFFECT_PREFIXES = ("da:", "ea:")
_KERBEROS_CONTEXT_PREFIX = "kerberos-context:"
_KERBEROS_CONTEXT_CALLBACK_MARKER = "@callback:"


_RIGHTS_TRACE_SEEN: set = set()


def _trace_rights_decision(input_preds: set, expanded: set) -> None:
    """DIAGNOSTIC (temporary; remove after the krbtgt-variance root-cause is confirmed).

    When DA/EA is held, snapshot whether `_expand_implications` granted ds-replication-rights, plus the
    Fix-A-relevant predicates — captured from the RUNTIME set (incl. live-callback/authenticated footholds
    that the stored ledger does NOT persist as hops, which is why offline ledger replay couldn't reproduce
    Fix A's behavior). Deduped per-process on the (DA, rights, relevant-preds) tuple; fail-safe; never
    affects behavior. Writes `.sage_engagement/rights_trace.jsonl`."""
    try:
        import os
        # Test-pollution guard: pytest exercises satisfied_predicates() with synthetic predicates, which would
        # otherwise append junk (empty-eid, fixture preds) to the real .sage_engagement/rights_trace.jsonl.
        # PYTEST_CURRENT_TEST is set by pytest for the duration of every test; skip the write entirely there.
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        da = sorted(p for p in input_preds if p.startswith(("da:", "ea:")))
        if not da:
            return
        rights = sorted(p for p in expanded if p.startswith("ds-replication-rights:"))
        rel = sorted(p for p in input_preds if p.startswith((
            "da:", "ea:", "system:", "gpo-domain:", "kerberos-context:", "kerberos-account-context:",
            "authenticated:", "live-foothold:", "live-callback:", "krbtgt-hash:", "graph-built:")))
        key = (tuple(da), tuple(rights), tuple(rel))
        if key in _RIGHTS_TRACE_SEEN:
            return
        _RIGHTS_TRACE_SEEN.add(key)
        import json
        from datetime import datetime, timezone
        eid = ""
        try:
            try:
                from . import engagement_ledger as _el
            except ImportError:  # ai/langgraph on sys.path directly (tests, some runtimes)
                import engagement_ledger as _el
            d = _el.state_dir()
            # The live Sage process publishes its frozen engagement key here; the SAGE_ENGAGEMENT_ID env lives
            # on the harness, NOT this process, so prefer the published value (fall back to env, then "").
            eid = _el.active_engagement_id() or os.environ.get("SAGE_ENGAGEMENT_ID", "")
        except Exception:
            d = os.path.join(os.getcwd(), ".sage_engagement")
            eid = os.environ.get("SAGE_ENGAGEMENT_ID", "")
        os.makedirs(d, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "eid": eid,
            "da": da,
            "rights_granted": bool(rights),
            "ds_replication_rights": rights,
            "predicates": rel,
        }
        with open(os.path.join(d, "rights_trace.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception:
        pass


def _expand_implications(predicates: set[str]) -> set[str]:
    """Augment a satisfied-predicate set with logically-implied predicates. Pure; never raises.

    Three implications:
    1. kerberos-context:{domain}@callback:{id} + live-callback:{id} -> kerberos-context:{domain}.
    2. da:/ea: on a domain -> that domain's ds-replication-rights, when the right is USABLE: either a live
       kerberos-context:{domain} (the cross-forest path — you hold a usable ticket for it), OR a live IN-DOMAIN
       foothold in that domain (you can DCSync your own DA domain directly; DA-group membership grants
       DS-Replication unless explicitly removed). The in-domain arm unblocks in-forest DA (e.g. GPO->DA) where
       no separate kerberos-context was minted; cross-forest keeps the context requirement (no in-domain
       foothold there until cert-auth), so it cannot DCSync prematurely.
    3. gpo-abuse (effect system:{gpo}) on a GPO that governs a domain (gpo-domain:{gpo}:{D} graph fact)
       -> ds-replication-rights:{D}. The SharpGPOAbuse SYSTEM task grants/holds DS-Replication on the DC
       the GPO governs, so controlling the GPO chains forward to dcsync on that domain (the effect-chained
       hop the forward planner needs to name after the first graph-derived hop)."""
    expanded = set(predicates)
    gpo_domain: dict[str, str] = {}
    live_context_domains = _live_kerberos_context_domains(predicates)
    for domain in live_context_domains:
        expanded.add(f"kerberos-context:{domain}")
    # Domains where we hold a live, in-domain foothold (from foothold_predicates: authenticated:/live-foothold:).
    # FQDN-ONLY: a bare NetBIOS label (e.g. "child", from authenticated:{identity_domain}) collides across
    # unrelated forests (child.root.example.local vs child.other.example.local), so a same-label foothold in a
    # DIFFERENT forest + a separately-held da: could wrongly grant replication rights and trigger a wrong-forest
    # DCSync. Trust only dotted forest FQDNs (canonicalized via access_reconciler's NetBIOS->FQDN map), mirroring
    # _in_domain_live_callback's forest-only matching. Degrades safely (no grant -> stall detector recovers).
    live_foothold_domains = {
        v for v in (
            p.split(":", 1)[1].strip()
            for p in predicates
            if p.startswith(("authenticated:", "live-foothold:")) and ":" in p and not p.endswith(":*")
        )
        if "." in v
    }
    for predicate in predicates:
        if predicate.startswith("gpo-domain:"):
            gpo, _, domain = predicate[len("gpo-domain:"):].partition(":")
            if gpo.strip() and domain.strip():
                gpo_domain[gpo.strip()] = domain.strip()
    for predicate in predicates:
        for prefix in _DA_EFFECT_PREFIXES:
            if predicate.startswith(prefix):
                domain = predicate[len(prefix):].strip()
                context_matches = {
                    candidate
                    for candidate in live_context_domains
                    if _domains_equivalent(domain, candidate)
                }
                foothold_matches = {
                    candidate
                    for candidate in live_foothold_domains
                    if _domains_equivalent(domain, candidate)
                }
                usable_domains = context_matches or foothold_matches
                if domain and usable_domains:
                    for usable_domain in usable_domains:
                        expanded.add(f"ds-replication-rights:{usable_domain}")
        if predicate.startswith("system:"):
            domain = gpo_domain.get(predicate[len("system:"):].strip())
            if domain:
                expanded.add(f"ds-replication-rights:{domain}")
    return expanded


def _live_kerberos_context_domains(predicates: set[str]) -> set[str]:
    live_callbacks = {
        predicate[len("live-callback:"):].strip()
        for predicate in predicates
        if predicate.startswith("live-callback:")
    }
    domains: set[str] = set()
    for predicate in predicates:
        parsed = _parse_kerberos_context_effect(predicate)
        if not parsed:
            continue
        domain, callback_id = parsed
        if domain and callback_id in live_callbacks:
            domains.add(domain)
    return domains


def _filter_live_kerberos_context_predicates(predicates: set[str]) -> set[str]:
    live_callbacks = {
        predicate[len("live-callback:"):].strip()
        for predicate in predicates
        if predicate.startswith("live-callback:")
    }
    filtered: set[str] = set()
    for predicate in predicates:
        parsed = _parse_kerberos_context_effect(predicate)
        if parsed:
            _domain, callback_id = parsed
            if callback_id in live_callbacks:
                filtered.add(predicate)
            continue
        filtered.add(predicate)
    return filtered


def _parse_kerberos_context_effect(predicate: str) -> tuple[str, str] | None:
    normalized = _normalize_predicate(predicate)
    if not normalized.startswith(_KERBEROS_CONTEXT_PREFIX):
        return None
    tail = normalized[len(_KERBEROS_CONTEXT_PREFIX):]
    domain, sep, callback_id = tail.partition(_KERBEROS_CONTEXT_CALLBACK_MARKER)
    if not sep:
        return None
    domain = domain.strip()
    callback_id = callback_id.strip()
    if not domain or not callback_id:
        return None
    return domain, callback_id


def _parse_kerberos_account_context_effect(predicate: str) -> tuple[str, str] | None:
    normalized = _normalize_predicate(predicate)
    prefix = "kerberos-account-context:"
    if not normalized.startswith(prefix):
        return None
    tail = normalized[len(prefix):]
    account_domain, sep, callback_id = tail.partition(_KERBEROS_CONTEXT_CALLBACK_MARKER)
    if not sep:
        return None
    _account, domain_sep, domain = account_domain.rpartition("@")
    domain = domain.strip()
    callback_id = callback_id.strip()
    if not domain_sep or not domain or not callback_id:
        return None
    return domain, callback_id


def _canonical_active_collection_domain(domain: str, active_domains: set[str]) -> str:
    """Return the active canonical spelling for one authority domain, or ``""`` when inactive."""
    normalized_domain = _normalize_key(domain)
    if not normalized_domain:
        return ""
    # Prefer an FQDN witness over a NetBIOS label so equivalent da:north / da:north.example.local effects do
    # not create separate collection epochs.
    for active_domain in sorted(active_domains, key=lambda item: ("." not in item, item)):
        if _domains_equivalent(normalized_domain, active_domain):
            return active_domain
    return ""


def corroborate_effect(effect: str, state: "EngagementState") -> bool:
    """Best-effort live corroboration of a durable hop's effect: True iff an INDEPENDENT live signal
    (a foothold-derived or graph-derived predicate) supports the effect. This is the deterministic,
    no-network verifier shipped now. The per-technique read-probe path (engagement_state.verify_effect
    fed by a live query) is the documented follow-up that plugs into the same verify-on-record seam."""
    return _normalize_predicate(effect) in foothold_predicates(state)


TECHNIQUE_MODEL: dict[str, dict] = {
    # Collection modeled as a STRIPS action so re-collection at the SAME authority epoch is deterministically
    # SKIPped by the gate. The target is a collection-authority key (forest|active-da/ea-authority); a modeled
    # authority change yields a new key, while raw identity/ticket churn does not. Effect is verified by
    # ingest_collection's graph_verified.
    "collect-graph": {
        "target_type": "access",
        "effect": "graph-built:{target}",
        "preconditions": ["live-foothold:*"],
        "verify": {"achieved_all": ["graph_verified"]},
    },
    "gpo-abuse": {
        "target_type": "host",
        "effect": "system:{host}",
        "preconditions": ["generic-write:gpo:{host}", "live-foothold:{domain}"],
        "verify": {
            "achieved_any": ["system_command_succeeded", "system_callback_observed"],
            "partial_any": ["scheduled_task_present", "gpo_modified", "task_xml_present", "callback_alive"],
        },
    },
    "lsass-dump": {
        "target_type": "host",
        "effect": "creds:{host}",
        "preconditions": ["system-or-admin:{host}"],
        "verify": {
            "achieved_all": ["credentials_dumped"],
            "partial_any": ["dump_file_present", "lsass_handle_opened"],
        },
    },
    "rbcd-standin": {
        "target_type": "host",
        "effect": "rbcd:{host}",
        "preconditions": ["generic-write:computer:{host}", "live-foothold:{domain}"],
        "verify": {
            "achieved_all": ["rbcd_configured"],
            "partial_any": ["computer_created", "delegation_acl_present"],
        },
    },
    "dcsync-rights-grant": {
        "target_type": "domain",
        "effect": "ds-replication-rights:{domain}",
        "preconditions": ["write-dacl:domain:{domain}", "live-foothold:{domain}"],
        "verify": {
            "achieved_all": ["ds_replication_rights"],
            "partial_any": ["get_changes", "get_changes_all", "get_changes_in_filtered_set", "ace_present"],
        },
    },
    # DCSync of a SPECIFIC user's secret (e.g. a SMALL COUNCIL member, to forge their REAL TGT for the
    # cross-forest LAPS read). Distinct from the domain krbtgt DCSync so the gate does not SKIP it once the
    # krbtgt is dumped. Target is "user@domain"; effect is that user's creds; needs replication rights
    # (which da:/ea: on the domain imply).
    "dcsync-user": {
        "target_type": "user",
        "effect": "creds:{target}",
        "preconditions": ["ds-replication-rights:{domain}"],
        "verify": {
            "achieved_all": ["credentials_dumped"],
            "partial_any": ["domain_hashes_dumped", "secretsdump_connected", "user_hash_present"],
        },
    },
    "dcsync": {
        "target_type": "domain",
        # DCSync is a REMOTE replication request to the target DC — it needs replication rights + a network
        # position (a live foothold ANYWHERE), NOT a foothold inside the target domain. Requiring
        # live-foothold:{domain} false-DEFERred the parent DCSync after a SID-history climb (you hold DA on the
        # parent via a forged ticket but have no callback in it). Replication rights are satisfied either by an
        # explicit grant (dcsync-rights-grant) OR implied by da:/ea: paired with a live callback-scoped
        # Kerberos context for the domain (see _expand_implications).
        "effect": "krbtgt-hash:{domain}",
        "preconditions": ["ds-replication-rights:{domain}", "live-foothold:*"],
        "verify": {
            "achieved_all": ["krbtgt_hash_present"],
            "partial_any": ["domain_hashes_dumped", "secretsdump_connected"],
        },
    },
    "domain-admin-membership-check": {
        "target_type": "domain",
        # Read-proof that the current controlled principal is listed in Domain Admins. This is not tied to
        # a specific add mechanism; GPO task, ACL abuse, ticket use, or any other path can prove the same
        # durable effect when the group output contains the issuing identity.
        "effect": "da:{domain}",
        "preconditions": ["live-foothold:*"],
        "verify": {
            "achieved_any": ["domain_admin"],
            "member_of_contains": ["domain admins"],
            "partial_any": ["group_query_succeeded"],
        },
    },
    "golden-ticket": {
        "target_type": "domain",
        "effect": "da:{domain}",
        "preconditions": ["krbtgt-hash:{domain}"],
        "verify": {
            "achieved_any": ["domain_admin", "ticket_valid", "service_access_proven"],
            "member_of_contains": ["domain admins"],
            "partial_any": ["ticket_forged", "tgt_present"],
        },
    },
    # Intra-forest child→parent (forest-root) escalation via an ExtraSIDs / SID-history golden ticket.
    # Forged from the CHILD krbtgt we already hold (precondition krbtgt-hash:{domain} = the child domain),
    # injecting the root Enterprise Admins SID; it yields DA over the forest ROOT (effect da:{parent}).
    # This MUST be a distinct technique+effect from a plain child golden ticket: otherwise the classifier
    # labels a SID-history climb "golden-ticket" on the child with effect da:{child} — which is already
    # achieved — and the gate SKIPs it, silently blocking the child→parent hop (the essos-solve bug, 2026-06-07).
    # No SID filtering applies WITHIN a forest, so there is no graph-ACL precondition here.
    "sid-history-escalation": {
        "target_type": "domain",
        "effect": "da:{parent}",
        "preconditions": ["krbtgt-hash:{domain}"],
        "verify": {
            "achieved_any": ["domain_admin", "ticket_valid", "service_access_proven"],
            "member_of_contains": ["domain admins", "enterprise admins"],
            "partial_any": ["ticket_forged", "tgt_present"],
        },
    },
}

_ADMIN_INTEGRITY = {"admin", "administrator", "elevated", "high", "system"}
_HOP_STATUSES = {"achieved", "failed", "blocked", "pending"}


_OBJECTIVE_DOMAIN_RE = r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}"


def _objective_target_domains(objective) -> set:
    """Target domain(s) the objective is about — parsed GENERICALLY from goal phrases ('Domain Admin on X',
    'administrative control of X', 'reach/target/compromise X', 'X forest'). No hardcoded names. Intermediate
    domains merely named in an attack-path description are not goal-phrase-adjacent and are excluded, so
    reaching them is a milestone, not completion. Returns a (possibly empty) set."""
    o = str(objective or "").casefold()
    targets: set = set()
    for m in re.finditer(
        r"(?:domain admin(?:istrator)?s?\s+(?:on|of|over)|administrative control (?:of|over)|"
        r"reach(?:ing)?|target(?:ing)?|compromise|pwn|own)\s+(?:the\s+)?(" + _OBJECTIVE_DOMAIN_RE + r")", o):
        targets.add(m.group(1).strip(" .'\""))
    for m in re.finditer(r"(" + _OBJECTIVE_DOMAIN_RE + r")\s+forest", o):
        targets.add(m.group(1).strip(" .'\""))
    return targets


def _objective_required_effects(objective, domain) -> set[str]:
    """Effects the objective EXPLICITLY demands for `domain`, beyond admin control itself — parsed
    generically from the objective text, with no hardcoded scenario names. Today: an objective that calls for
    dumping/DCSyncing krbtgt requires `krbtgt-hash:{domain}` as an INDEPENDENT witness, so completion is not
    declared on DA alone before the krbtgt material is actually extracted (an under-reach). Returns a
    (possibly empty) set; empty means admin control of the target domain is itself sufficient."""
    o = str(objective or "").casefold()
    domain = _normalize_key(domain)
    required: set[str] = set()
    if not domain:
        return required
    if "krbtgt" in o:
        required.add(f"krbtgt-hash:{domain}")
    return required


def _effect_satisfied(required_effect: str, achieved: set[str]) -> bool:
    """Whether `required_effect` (a `prefix:domain` predicate) is met by `achieved`, tolerating domain-form
    differences (NetBIOS vs FQDN) between the demand and the recorded effect — otherwise a consistent objective
    is silently never-completed when two record paths spell the same domain differently (an under-reach that
    re-creates exactly the klist-loop this change kills)."""
    if required_effect in achieved:
        return True
    prefix, sep, dom = required_effect.partition(":")
    if not sep or not dom:
        return False
    for eff in achieved:
        p, s, d = eff.partition(":")
        if s and p == prefix and _domains_equivalent(d, dom):
            return True
    return False


def _objective_is_complete(state: "EngagementState", has_next: bool) -> bool:
    """The objective's administrative-control proof is recorded AND terminal. Terminal when a proven
    admin-control domain matches the objective's parsed TARGET domain; if no target is parseable, fall back to
    'no further grounded hop advances' only for real human-readable objectives. Opaque ledger IDs are not
    objectives, so they must not turn "no modeled next hop" into mission completion. A proven INTERMEDIATE
    domain with a further hop available is a MILESTONE, not completion — so the climb continues."""
    candidates = objective_completion_candidates(state)
    if not candidates:
        return False
    objective = getattr(state, "objective", "")
    try:
        achieved = state.achieved_effects()
    except Exception:
        achieved = set()
    # Declarative per-objective witness set: a candidate is completion-worthy only when EVERY effect the
    # objective explicitly demands for that candidate's domain is recorded. This stops a relaxed access proof
    # (e.g. an in-domain live callback) from declaring victory BEFORE the objective's required artifacts (the
    # krbtgt dump) exist — which would be an under-reach. Objectives with no extra demands are unaffected.
    qualified = [
        c for c in candidates
        if all(
            _effect_satisfied(req, achieved)
            for req in _objective_required_effects(objective, str(c.get("domain", "")).strip())
        )
    ]
    if not qualified:
        return False
    cand_domains = [str(c.get("domain", "")).strip() for c in qualified]
    targets = _objective_target_domains(objective)
    if targets:
        return any(_domains_equivalent(cd, t) for cd in cand_domains for t in targets)
    if _objective_is_opaque_engagement_id(objective):
        return False
    return not has_next


def _objective_is_opaque_engagement_id(objective: Any) -> bool:
    return str(objective or "").strip().casefold().startswith("sage-engagement:")


def engagement_phase(state: "EngagementState") -> str:
    """Deterministic phase of the engagement, so the operator stops asking 'should I collect more?':
    FOOTHOLD (no live access) -> COMPLETE-CANDIDATE (recorded admin/control proof) -> EXPLOITATION
    (a grounded hop is already available) -> RECON (no graph and no grounded hop yet) -> BLOCKED (graph but
    nothing modeled is available). Pure; never raises."""
    try:
        alive = any(getattr(f, "alive", False) for f in getattr(state, "footholds", []) or [])
        if not alive:
            return "FOOTHOLD — establish access"
        has_next = bool(_capability_actions_available(state))
        # COMPLETE-CANDIDATE is TERMINAL only when the OBJECTIVE's target domain is under admin control (or, if
        # no target is parseable, when no further hop advances). Admin control of an INTERMEDIATE domain with a
        # further hop available is a MILESTONE — keep climbing (EXPLOITATION) instead of halting on the first
        # domain reached.
        if _objective_is_complete(state, has_next):
            return "COMPLETE-CANDIDATE — administrative-control proof for the objective's target is recorded; report the proof chain before executing a new action"
        if has_next:
            return "EXPLOITATION — execute a NEXT GROUNDED ACTION below; collection is COMPLETE"
        if current_access_collection_missing(state):
            return "RECON — current access has not been collected; run one BloodHound collection for this access context, then analyze"
        trusted_missing = trusted_uncollected_domains(state)
        if trusted_missing:
            return (
                "RECON — a trusted domain is visible but not collected; run one targeted BloodHound collection "
                f"for {trusted_missing[0]}, then analyze"
            )
        if not getattr(state, "graph_facts", None):
            return "RECON — collect the graph ONCE, then analyze (do NOT re-collect)"
        return "BLOCKED — no modeled hop available; route to BloodHound for graph coverage/path analysis or await an access change"
    except Exception:
        return ""


def current_access_collection_missing(state: "EngagementState") -> bool:
    """Return whether a live non-Sage callback lacks a collection for its current authority epoch.

    The key changes only for modeled active DA/EA authority, not for dumped hashes, raw identity churn, or
    same-domain Kerberos proof. This is the deterministic form of "re-collect when graph-visible permissions
    changed and more data is needed"; callers should consult it only after checking that no grounded next action
    is available.
    """
    try:
        for foothold in getattr(state, "footholds", []) or []:
            if not _is_live_target_foothold(foothold):
                continue
            if not graph_collection_covers_foothold(state, foothold):
                return True
    except Exception:
        return False
    return False


def _capability_actions_available(state: "EngagementState") -> bool:
    try:
        try:
            from . import capabilities
        except ImportError:
            import capabilities
        return bool(capabilities.actions_from_state(state))
    except Exception:
        return False


def _gpo_domain_has_downstream_progress(state: "EngagementState", gpo: str) -> bool:
    gpo_key = _normalize_key(gpo)
    if not gpo_key:
        return False
    domains: set[str] = set()
    try:
        for predicate in foothold_predicates(state):
            if not predicate.startswith("gpo-domain:"):
                continue
            mapped_gpo, _, domain = predicate[len("gpo-domain:"):].partition(":")
            if _normalize_key(mapped_gpo) == gpo_key and domain.strip():
                domains.add(_normalize_key(domain))
    except Exception:
        domains = set()
    if not domains:
        return False
    achieved = state.achieved_effects()
    return any(
        effect in achieved
        for domain in domains
        for effect in (
            f"ds-replication-rights:{domain}",
            f"krbtgt-hash:{domain}",
            f"da:{domain}",
            f"ea:{domain}",
        )
    )


def _pending_hop_superseded(state: "EngagementState", hop: Hop) -> bool:
    technique = _normalize_key(getattr(hop, "technique", ""))
    if technique != "gpo-abuse":
        return False
    target = _normalize_key(getattr(hop, "target", ""))
    effect = _normalize_key(getattr(hop, "effect", ""))
    gpo = target
    if effect.startswith("system:") and effect[len("system:"):].strip():
        gpo = effect[len("system:"):].strip()
    if not gpo:
        return False
    achieved = state.achieved_effects()
    if any(item.startswith(f"system-exec:gpo:{gpo}@") for item in achieved):
        return True
    return _gpo_domain_has_downstream_progress(state, gpo)


def record_hop_result(
    state: EngagementState,
    technique: str,
    target: str,
    status: str,
    evidence: dict,
    now: str,
) -> EngagementState:
    """Return state with a hop result appended or updated by technique and target."""
    effect = _technique_effect(technique, target)
    preconditions = _technique_preconditions(technique, target)
    return record_effect_result(
        state,
        technique,
        target,
        effect,
        status,
        evidence,
        now,
        preconditions=preconditions,
        satisfied_effects=[effect],
    )


def record_effect_result(
    state: EngagementState,
    technique: str,
    target: str,
    effect: str,
    status: str,
    evidence: dict,
    now: str,
    preconditions: list[str] | None = None,
    satisfied_effects: list[str] | None = None,
) -> EngagementState:
    """Return state with an explicit effect recorded.

    This is the generic counterpart to ``record_hop_result``. It lets capability
    code record effects that are not part of the legacy STRIPS technique model
    while preserving the same hop ledger/update semantics.
    """
    normalized_status = _normalize_key(status)
    if normalized_status not in _HOP_STATUSES:
        raise ValueError(f"invalid hop status: {status!r}")

    normalized_effect = _normalize_predicate(effect)
    normalized_effects = [
        _normalize_predicate(item)
        for item in (satisfied_effects if satisfied_effects is not None else [effect])
        if _normalize_predicate(item)
    ]
    if normalized_effect and normalized_effect not in normalized_effects:
        normalized_effects.insert(0, normalized_effect)
    if not normalized_effect and normalized_effects:
        normalized_effect = normalized_effects[0]
    normalized_preconditions = [
        _normalize_predicate(item)
        for item in (preconditions or [])
        if _normalize_predicate(item)
    ]
    source = _text(evidence.get("source")) if isinstance(evidence, dict) else ""
    hop = Hop(
        id=_hop_id(technique, target),
        technique=technique,
        target=target,
        effect=normalized_effect,
        status=normalized_status,
        evidence=dict(evidence) if isinstance(evidence, dict) else {},
        preconditions=normalized_preconditions,
        satisfied_effects=normalized_effects,
        source=source or "record_effect_result",
        timestamp=now,
    )

    updated = False
    hops: list[Hop] = []
    for existing in state.hops:
        if _same_hop(existing, technique, target):
            hops.append(hop if not getattr(existing, "id", "") else _replace_hop_id(hop, existing.id))
            updated = True
        else:
            hops.append(existing)
    if not updated:
        hops.append(hop)
    return EngagementState(
        objective=state.objective,
        footholds=list(state.footholds),
        hops=hops,
        graph_facts=list(state.graph_facts),
        probed_effect_prefixes=set(getattr(state, "probed_effect_prefixes", set()) or set()),
    )


def verify_effect(technique: str, target: str, probe_result: dict) -> str:
    """Return the post-state verdict from caller-supplied structured probe data.

    The caller runs the read query and supplies structured post-state. This module
    only interprets that structure and never classifies free-form error strings.
    """
    del target
    if not isinstance(probe_result, dict):
        return "failed"

    model = TECHNIQUE_MODEL.get(technique)
    if model is None:
        return "failed"

    verify = model.get("verify", {})
    if _probe_all_true(probe_result, verify.get("achieved_all", [])):
        return "achieved"
    if _probe_any_true(probe_result, verify.get("achieved_any", [])):
        return "achieved"
    if _member_contains(probe_result, verify.get("member_of_contains", [])):
        return "achieved"
    if _probe_any_true(probe_result, verify.get("partial_any", [])):
        return "partial"
    return "failed"


def _effect_hop(state: EngagementState, effect: str) -> "Hop | None":
    """Return the achieved Hop whose satisfied_effects include `effect`, else None.
    Like _effect_evidence but returns the hop so the caller can read provenance."""
    normalized_effect = _normalize_predicate(effect)
    for hop in state.hops:
        if _text(getattr(hop, "status", "")).casefold() != "achieved":
            continue
        effects = {_normalize_predicate(item) for item in getattr(hop, "satisfied_effects", [])}
        if normalized_effect in effects:
            return hop
    return None


def _hop_provenance(hop: "Hop") -> str:
    """'durable' if the hop was loaded from the cross-run ledger, else 'run' (achieved this process).
    Default 'run' when unset so pre-existing/in-run hops keep their trustworthy hard-SKIP behavior."""
    evidence = getattr(hop, "evidence", {}) or {}
    if isinstance(evidence, dict) and _normalize_key(evidence.get("provenance")) == "durable":
        return "durable"
    return "run"


def _parse_iso(value: Any):
    """Parse an ISO-8601 timestamp to an aware datetime (assume UTC if naive). None on failure."""
    from datetime import datetime, timezone
    text = _text(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def filter_hops_by_ttl(hops: Any, now: str, ttl_hours: float) -> tuple[list, int]:
    """Drop hops older than ttl_hours relative to `now`. Returns (kept_hops, dropped_count).
    ttl_hours<=0 (or unparseable now) disables expiry — all hops kept. Never raises."""
    hops_list = list(hops or [])
    try:
        if not ttl_hours or float(ttl_hours) <= 0:
            return hops_list, 0
        now_dt = _parse_iso(now)
        if now_dt is None:
            return hops_list, 0
        from datetime import timedelta
        cutoff = now_dt - timedelta(hours=float(ttl_hours))
        kept = []
        dropped = 0
        for hop in hops_list:
            ts = _parse_iso(getattr(hop, "timestamp", ""))
            if ts is not None and ts < cutoff:
                dropped += 1
            else:
                kept.append(hop)
        return kept, dropped
    except Exception:
        return hops_list, 0


def _effect_evidence(state: EngagementState, effect: str) -> dict | None:
    normalized_effect = _normalize_predicate(effect)
    for hop in state.hops:
        if _text(getattr(hop, "status", "")).casefold() != "achieved":
            continue
        effects = {
            _normalize_predicate(item)
            for item in getattr(hop, "satisfied_effects", [])
        }
        if normalized_effect in effects:
            return dict(getattr(hop, "evidence", {}))
    return None


_IN_DOMAIN_CALLBACK_PREFIX = "in-domain-callback:"


def _domain_label(domain: str) -> str:
    """Leading DNS label of a domain (north.sevenkingdoms.local -> north); a bare label returns itself."""
    d = _normalize_key(domain)
    return d.split(".", 1)[0] if d else ""


def _domains_equivalent(a: str, b: str) -> bool:
    """Whether two domain spellings denote the SAME domain: exact match (FQDN==FQDN or label==label), or a
    FQDN against its own single-label NetBIOS form (north.sevenkingdoms.local <-> north). Deliberately does
    NOT treat a child FQDN as equal to its parent (north.sevenkingdoms.local != sevenkingdoms.local): an
    in-domain callback must prove access to THIS domain, not merely the forest root."""
    a = _normalize_key(a)
    b = _normalize_key(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if "." not in a and "." in b:
        return a == _domain_label(b)
    if "." not in b and "." in a:
        return b == _domain_label(a)
    return False


def _in_domain_live_callback(state: EngagementState, domain: str) -> str:
    """A live, non-Sage callback whose foothold's DNS domain (forest) IS `domain`. Returns the callback id, or
    "" when no in-domain live foothold exists. This is the access witness for an in-forest objective; a
    cross-forest objective has no in-domain foothold until cert-auth, so it returns "" and the strict
    cert-auth/kerberos-context gate still governs.

    Matching is on the foothold FOREST only — deliberately NOT the identity's NetBIOS domain — because a bare
    NetBIOS label (e.g. NORTH) can collide across unrelated forests (north.sevenkingdoms.local vs
    north.otherforest.local) and would leak a cross-forest foothold in as a false in-domain witness. `forest`
    is already canonicalized to an FQDN via the engagement NetBIOS→FQDN map in access_reconciler when one is
    known, so exact-FQDN equality is the common path; the residual bare-label match in `_domains_equivalent`
    only applies in a degraded environment with no FQDN mapping (and no same-label sibling forest)."""
    for foothold in getattr(state, "footholds", []) or []:
        if not _is_live_target_foothold(foothold):
            continue
        callback_id = _normalize_key(getattr(foothold, "callback_id", "")).lstrip("#")
        if not callback_id:
            continue
        if _domains_equivalent(domain, getattr(foothold, "forest", "")):
            return callback_id
    return ""


def objective_completion_candidates(state: EngagementState) -> list[dict[str, str]]:
    """Return generic objective-satisfaction candidates proven by achieved effects and live access.

    This intentionally does not parse GOAD names or objective prose. It names reusable proof facts:
    domain administrative control is a completion candidate when the ledger contains `da:`/`ea:` for a
    domain and a currently-live callback-scoped Kerberos context proves usable access in that domain.
    """
    try:
        achieved = set(getattr(state, "achieved_effects")())
    except Exception:
        return []
    live_callbacks = _live_callback_ids_from_state(state)
    if not achieved or not live_callbacks:
        return []

    candidates: list[dict[str, str]] = []
    for admin_effect in sorted(achieved):
        if not admin_effect.startswith(_DA_EFFECT_PREFIXES):
            continue
        domain = admin_effect.split(":", 1)[1].strip()
        if not domain:
            continue
        context_effect, callback_id = _live_context_effect_for_domain(domain, achieved, live_callbacks)
        cert_effect = _certificate_auth_effect_for_domain(domain, achieved)
        if not context_effect and cert_effect:
            context_effect, callback_id = _live_certificate_auth_effect_for_domain(
                state,
                cert_effect,
                live_callbacks,
            )
        if not context_effect:
            # In-domain access witness (generic, but reachable ONLY because admin_effect — da:/ea:, which is
            # verifier-gated — is already held): a live non-Sage callback whose foothold sits in `domain`
            # itself proves usable access there, so the existing Kerberos session stands in for a separately
            # recorded kerberos-context effect. A cross-forest objective has no in-domain foothold until
            # cert-auth, so this never fires for it and its strict cert-auth gate is preserved.
            in_domain_cb = _in_domain_live_callback(state, domain)
            if in_domain_cb:
                context_effect = (
                    f"{_IN_DOMAIN_CALLBACK_PREFIX}{domain}{_KERBEROS_CONTEXT_CALLBACK_MARKER}{in_domain_cb}"
                )
                callback_id = in_domain_cb
        if not context_effect:
            continue
        candidate = {
            "kind": "administrative-control",
            "domain": domain,
            "admin_effect": admin_effect,
            "admin_task_id": _hop_task_id(_effect_hop(state, admin_effect)),
            "access_effect": context_effect,
            "access_task_id": _hop_task_id(_effect_hop(state, context_effect)),
            "callback_id": callback_id,
        }
        if cert_effect:
            candidate["auth_effect"] = cert_effect
            candidate["auth_task_id"] = _hop_task_id(_effect_hop(state, cert_effect))
        key_effect = f"krbtgt-hash:{domain}"
        if key_effect in achieved:
            candidate["key_effect"] = key_effect
            candidate["key_task_id"] = _hop_task_id(_effect_hop(state, key_effect))
        candidates.append(candidate)

    live_domains = _live_foothold_domain_set(state)
    candidates.sort(key=lambda item: (item.get("domain", "") in live_domains, item.get("domain", "")))
    return candidates


def _live_callback_ids_from_state(state: EngagementState) -> set[str]:
    callback_ids: set[str] = set()
    for foothold in getattr(state, "footholds", []) or []:
        if getattr(foothold, "alive", False) is not True:
            continue
        callback_id = _normalize_key(getattr(foothold, "callback_id", "")).lstrip("#")
        if callback_id:
            callback_ids.add(callback_id)
    return callback_ids


def _live_foothold_domain_set(state: EngagementState) -> set[str]:
    domains: set[str] = set()
    for foothold in getattr(state, "footholds", []) or []:
        if getattr(foothold, "alive", False) is not True:
            continue
        forest = _normalize_key(getattr(foothold, "forest", ""))
        identity_domain = _identity_domain(getattr(foothold, "identity", ""))
        if forest:
            domains.add(forest)
        if identity_domain:
            domains.add(identity_domain)
    return domains


def _live_context_effect_for_domain(
    domain: str,
    achieved: set[str],
    live_callbacks: set[str],
) -> tuple[str, str]:
    for effect in sorted(achieved):
        parsed = _parse_kerberos_context_effect(effect)
        if not parsed:
            continue
        context_domain, callback_id = parsed
        if context_domain == domain and callback_id in live_callbacks:
            return effect, callback_id
    return "", ""


def _certificate_auth_effect_for_domain(domain: str, achieved: set[str]) -> str:
    suffix = f"@{_normalize_key(domain)}"
    for effect in sorted(achieved):
        if effect.startswith("certificate-auth:") and effect.endswith(suffix):
            return effect
    return ""


def _live_certificate_auth_effect_for_domain(
    state: EngagementState,
    cert_effect: str,
    live_callbacks: set[str],
) -> tuple[str, str]:
    evidence = _effect_evidence(state, cert_effect) or {}
    for key in ("callback_id", "callback", "callback_display_id"):
        callback_id = _normalize_key(evidence.get(key)).lstrip("#")
        if callback_id and callback_id in live_callbacks:
            return cert_effect, callback_id
    return "", ""


def _hop_task_id(hop: "Hop | None") -> str:
    if hop is None:
        return ""
    evidence = getattr(hop, "evidence", {}) or {}
    if not isinstance(evidence, dict):
        return ""
    for key in ("mythic_task_id", "task_id", "task", "display_id"):
        value = _text(evidence.get(key)).strip()
        if value:
            return value
    return ""


def _technique_effect(technique: str, target: str) -> str:
    model = TECHNIQUE_MODEL.get(technique)
    if model is None:
        return _normalize_predicate(f"{technique}:{target}")
    return _instantiate(model["effect"], target, model)


def _technique_preconditions(technique: str, target: str) -> list[str]:
    model = TECHNIQUE_MODEL.get(technique)
    if model is None:
        return []
    return [_instantiate(item, target, model) for item in model.get("preconditions", [])]


def _instantiate(template: str, target: str, model: dict) -> str:
    target_text = _normalize_key(target)
    target_type = model.get("target_type")
    host = target_text
    if target_type == "domain":
        domain = target_text
    elif target_type == "user":
        # target is "user@domain"; the domain is what backs replication-rights preconditions.
        domain = target_text.split("@", 1)[1] if "@" in target_text else "*"
    else:
        domain = _domain_from_host(target_text)
    values = {
        "target": target_text,
        "host": host,
        "forest": domain,
        "domain": domain,
        "parent": _parent_domain(domain),
        "principal": target_text,
    }
    return _normalize_predicate(template.format(**values))


def _domain_from_host(target: str) -> str:
    parts = [part for part in target.split(".") if part]
    if len(parts) > 2:
        return ".".join(parts[1:])
    return "*"


def _parent_domain(target: str) -> str:
    """The parent (forest-root-ward) domain of an FQDN: strip the leftmost label when there are >2 labels
    (north.sevenkingdoms.local → sevenkingdoms.local); a 2-label root returns itself. Backs the {parent}
    template var for the SID-history child→parent escalation effect (da:{parent})."""
    parts = [part for part in _normalize_key(target).split(".") if part]
    if len(parts) > 2:
        return ".".join(parts[1:])
    return _normalize_key(target)


def _same_hop(hop: Hop, technique: str, target: str) -> bool:
    return (
        _normalize_key(getattr(hop, "technique", "")) == _normalize_key(technique)
        and _normalize_key(getattr(hop, "target", "")) == _normalize_key(target)
    )


def _replace_hop_id(hop: Hop, hop_id: str) -> Hop:
    return Hop(
        id=hop_id,
        technique=hop.technique,
        target=hop.target,
        effect=hop.effect,
        status=hop.status,
        evidence=hop.evidence,
        preconditions=hop.preconditions,
        satisfied_effects=hop.satisfied_effects,
        source=hop.source,
        timestamp=hop.timestamp,
    )


def _hop_id(technique: str, target: str) -> str:
    return f"{_normalize_key(technique)}:{_normalize_key(target)}"


def _identity_domain(identity: Any) -> str:
    text = _text(identity).strip()
    if "\\" in text:
        return _normalize_key(text.split("\\", 1)[0])
    if "@" in text:
        return _normalize_key(text.split("@", 1)[1])
    return ""


def _probe_all_true(probe_result: dict, keys: list[str]) -> bool:
    return bool(keys) and all(probe_result.get(key) is True for key in keys)


def _probe_any_true(probe_result: dict, keys: list[str]) -> bool:
    return any(probe_result.get(key) is True for key in keys)


def _member_contains(probe_result: dict, needles: list[str]) -> bool:
    member_of = probe_result.get("member_of")
    if not isinstance(member_of, list):
        return False
    lowered = [_text(item).casefold() for item in member_of]
    return any(needle.casefold() in item for needle in needles for item in lowered)


def _normalize_predicate(value: Any) -> str:
    return " ".join(_text(value).strip().casefold().split())


def _normalize_key(value: Any) -> str:
    return _text(value).strip().casefold()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


_RENDER_HEADER = "=== ENGAGEMENT STATE (observed; trust this over the plan) ==="
_RENDER_LIMIT = 1500
_TRUNCATED_MARKER = "\n… (truncated)"


def render_engagement_state(state: EngagementState) -> str:
    """Return a compact observed-state block for prompt injection.

    The renderer is observed-state only: objective, live footholds, and durable hop ledger.
    """
    try:
        lines = [_RENDER_HEADER]
        objective = _render_value(getattr(state, "objective", ""))
        if objective:
            lines.append(f"Objective: {objective}")
        elif state is None:
            lines.append("Objective: (state unavailable)")

        live_footholds = []
        for foothold in _render_items(getattr(state, "footholds", [])):
            if not _is_live_target_foothold(foothold):
                continue
            host = _render_value(getattr(foothold, "host", "")) or "(unknown host)"
            forest = _render_value(getattr(foothold, "forest", "")) or "(unknown forest)"
            identity = _render_value(getattr(foothold, "identity", "")) or "(unknown identity)"
            integrity = _render_value(getattr(foothold, "integrity", "")) or "(unknown integrity)"
            callback_id = _render_value(getattr(foothold, "callback_id", ""))
            cb_text = f" | cb={callback_id}" if callback_id else ""
            live_footholds.append((
                (_normalize_key(host), _normalize_key(forest), _normalize_key(callback_id)),
                f"- {host} | forest={forest} | identity={identity} | integrity={integrity}{cb_text}",
            ))

        achieved_hops = []
        pending_hops = []
        blocked_hops = []
        for hop in _render_items(getattr(state, "hops", [])):
            status = _normalize_key(getattr(hop, "status", ""))
            technique = _render_value(getattr(hop, "technique", "")) or "(unknown technique)"
            target = _render_value(getattr(hop, "target", "")) or "(unknown target)"
            effect = _render_value(getattr(hop, "effect", "")) or "(no effect recorded)"
            sort_key = (_normalize_key(technique), _normalize_key(target), _normalize_key(effect))
            if status == "achieved":
                # Mark a durable (loaded-from-disk) belief that no live signal corroborates, so the
                # operator treats it as a hint to re-check rather than ground truth after a redeploy.
                suffix = ""
                try:
                    if _hop_provenance(hop) == "durable" and not corroborate_effect(
                        getattr(hop, "effect", ""), state
                    ):
                        suffix = " (durable, unverified — re-check if AD changed)"
                except Exception:
                    suffix = ""
                achieved_hops.append((sort_key, f"- {technique} → {target}: {effect}{suffix}"))
            elif status == "pending":
                if _pending_hop_superseded(state, hop):
                    continue
                pending_hops.append((sort_key, f"- pending: {technique} → {target}: {effect}"))
            elif status in {"failed", "blocked"}:
                blocked_hops.append((sort_key, f"- {status}: {technique} → {target}: {effect}"))

        if live_footholds:
            lines.append("Live footholds:")
            lines.extend(item for _, item in sorted(live_footholds, key=lambda item: item[0]))

        if achieved_hops:
            lines.append("Achieved hops:")
            lines.extend(item for _, item in sorted(achieved_hops, key=lambda item: item[0]))
        if pending_hops:
            lines.append("Pending hops:")
            lines.extend(item for _, item in sorted(pending_hops, key=lambda item: item[0]))
        if blocked_hops:
            lines.append("Failed/blocked hops:")
            lines.extend(item for _, item in sorted(blocked_hops, key=lambda item: item[0]))

        if not live_footholds and not achieved_hops and not blocked_hops:
            lines.append("(no observed state yet)")

        return _render_bounded("\n".join(lines))
    except Exception:
        return _render_bounded(
            "\n".join([
                _RENDER_HEADER,
                "Objective: (state unavailable)",
                "(no observed state yet)",
            ])
        )


def _is_live_target_foothold(foothold: Any) -> bool:
    if getattr(foothold, "alive", False) is not True:
        return False
    agent = _normalize_key(getattr(foothold, "agent", ""))
    return agent != "sage"


def _render_objective_completion_candidates(state: EngagementState, limit: int = 3, terminal: bool = True) -> list[str]:
    candidates = objective_completion_candidates(state)
    if not candidates:
        return []
    if terminal:
        header = ("OBJECTIVE SATISFIED CANDIDATES (if the current objective asks for admin/control of one of "
                  "these domains, STOP and report this proof; do not execute another capability):")
    else:
        header = ("ADMIN-CONTROL MILESTONES (intermediate domain control already proven — do NOT redo these; a "
                  "further hop advances the engagement, so CONTINUE toward the objective via the NEXT actions below):")
    lines = [header]
    for candidate in candidates[:max(1, int(limit or 1))]:
        pieces = [
            f"- {candidate.get('kind', 'objective')}:{candidate.get('domain', '')}",
            "admin=" + _effect_with_task(candidate.get("admin_effect", ""), candidate.get("admin_task_id", "")),
            "access=" + _effect_with_task(candidate.get("access_effect", ""), candidate.get("access_task_id", "")),
            f"callback={candidate.get('callback_id', '')}",
        ]
        if candidate.get("auth_effect"):
            pieces.append("auth=" + _effect_with_task(candidate.get("auth_effect", ""), candidate.get("auth_task_id", "")))
        if candidate.get("key_effect"):
            pieces.append("key=" + _effect_with_task(candidate.get("key_effect", ""), candidate.get("key_task_id", "")))
        lines.append(" | ".join(piece for piece in pieces if piece and not piece.endswith("=")))
    return lines


def _effect_with_task(effect: str, task_id: str) -> str:
    if not effect:
        return ""
    return f"{effect} task={task_id}" if task_id else effect


def hops_to_dicts(hops: Any) -> list[dict]:
    """Serialize a list of Hop dataclasses to plain JSON-safe dicts. Never raises — skips anything
    that is not a dataclass-shaped Hop. Used by the durable per-engagement ledger (write-through)."""
    import dataclasses
    out: list[dict] = []
    for hop in _render_items(hops):
        try:
            if dataclasses.is_dataclass(hop) and not isinstance(hop, type):
                out.append(dataclasses.asdict(hop))
        except Exception:
            continue
    return out


def hops_from_dicts(items: Any) -> list["Hop"]:
    """Deserialize plain dicts (from the durable ledger) back into Hop dataclasses. Never raises —
    skips malformed entries. Mirrors hops_to_dicts; round-trips losslessly for well-formed hops."""
    out: list[Hop] = []
    for d in _render_items(items):
        if not isinstance(d, dict):
            continue
        try:
            out.append(Hop(
                id=_text(d.get("id")),
                technique=_text(d.get("technique")),
                target=_text(d.get("target")),
                effect=_text(d.get("effect")),
                status=_normalize_key(d.get("status")),
                evidence=dict(d["evidence"]) if isinstance(d.get("evidence"), dict) else {},
                preconditions=list(d.get("preconditions") or []),
                satisfied_effects=list(d.get("satisfied_effects") or []),
                source=_text(d.get("source")),
                timestamp=_text(d.get("timestamp")),
            ))
        except Exception:
            continue
    return out


def graph_facts_to_dicts(graph_facts: Any) -> list[dict]:
    """Serialize GraphFact dataclasses to JSON-safe dicts. Never raises."""
    import dataclasses
    out: list[dict] = []
    for fact in _render_items(graph_facts):
        try:
            if dataclasses.is_dataclass(fact) and not isinstance(fact, type):
                out.append(dataclasses.asdict(fact))
        except Exception:
            continue
    return out


def graph_facts_from_dicts(items: Any) -> list["GraphFact"]:
    """Deserialize ledger graph facts back into GraphFact dataclasses. Never raises."""
    out: list[GraphFact] = []
    for d in _render_items(items):
        if not isinstance(d, dict):
            continue
        try:
            predicate = _normalize_predicate(d.get("predicate"))
            if not predicate:
                continue
            out.append(GraphFact(
                predicate=predicate,
                source=_text(d.get("source")),
                timestamp=_text(d.get("timestamp")),
                ttl_seconds=int(d.get("ttl_seconds") or 0),
            ))
        except Exception:
            continue
    return out


def _render_items(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _render_value(value: Any) -> str:
    text = " ".join(_text(value).split())
    if len(text) > 160:
        return text[:157].rstrip() + "..."
    return text


def _render_bounded(text: str) -> str:
    if len(text) <= _RENDER_LIMIT:
        return text
    limit = _RENDER_LIMIT - len(_TRUNCATED_MARKER)
    return text[:limit].rstrip() + _TRUNCATED_MARKER
