"""Pure engagement-state gate for STRIPS preconditions and effects."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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
        implications (e.g. da/ea on a domain implies that domain's replication rights)."""
        return _expand_implications(self.achieved_effects() | foothold_predicates(self))

    def satisfies_predicate(self, predicate: str) -> bool:
        """Return whether a predicate is satisfied by the current state."""
        return _normalize_predicate(predicate) in self.satisfied_predicates()


def foothold_predicates(state: "EngagementState") -> set[str]:
    """Predicates derived ONLY from live footholds and graph facts — NOT from hops.

    This is the independent-evidence set used to corroborate a durable (loaded-from-disk) achieved hop:
    a hop must not corroborate itself, so corroboration consults live signal only."""
    predicates: set[str] = set()
    for foothold in getattr(state, "footholds", []) or []:
        if not getattr(foothold, "alive", False):
            continue
        predicates.add("live-foothold:*")
        host = _normalize_key(getattr(foothold, "host", ""))
        forest = _normalize_key(getattr(foothold, "forest", ""))
        identity_domain = _identity_domain(getattr(foothold, "identity", ""))
        integrity = _normalize_key(getattr(foothold, "integrity", ""))
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


# Effect implications: holding one effect logically grants others, so a precondition can be satisfied without
# a separate hop. A Domain/Enterprise Admin can replicate the directory (DCSync), so da:/ea: on a domain
# satisfies that domain's ds-replication-rights precondition — the over-DEFER fix for the parent DCSync after a
# SID-history climb (you are DA on the parent via a forged ticket, with no separate replication grant).
_DA_EFFECT_PREFIXES = ("da:", "ea:")


def _expand_implications(predicates: set[str]) -> set[str]:
    """Augment a satisfied-predicate set with logically-implied predicates. Pure; never raises."""
    expanded = set(predicates)
    for predicate in predicates:
        for prefix in _DA_EFFECT_PREFIXES:
            if predicate.startswith(prefix):
                domain = predicate[len(prefix):].strip()
                if domain:
                    expanded.add(f"ds-replication-rights:{domain}")
    return expanded


def corroborate_effect(effect: str, state: "EngagementState") -> bool:
    """Best-effort live corroboration of a durable hop's effect: True iff an INDEPENDENT live signal
    (a foothold-derived or graph-derived predicate) supports the effect. This is the deterministic,
    no-network verifier shipped now. The per-technique read-probe path (engagement_state.verify_effect
    fed by a live query) is the documented follow-up that plugs into the same seam in gate_decision."""
    return _normalize_predicate(effect) in foothold_predicates(state)


class GateDecision(str, Enum):
    SKIP = "skip"
    DEFER = "defer"
    PROCEED = "proceed"


TECHNIQUE_MODEL: dict[str, dict] = {
    "gpo-abuse": {
        "target_type": "host",
        "effect": "system:{host}",
        "preconditions": ["generic-write:gpo:{host}", "live-foothold:{domain}"],
        "verify": {
            "achieved_all": ["scheduled_task_present"],
            "partial_any": ["gpo_modified", "task_xml_present", "callback_alive"],
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
        # explicit grant (dcsync-rights-grant) OR implied by da:/ea: on the domain (see _expand_implications).
        "effect": "krbtgt-hash:{domain}",
        "preconditions": ["ds-replication-rights:{domain}", "live-foothold:*"],
        "verify": {
            "achieved_all": ["krbtgt_hash_present"],
            "partial_any": ["domain_hashes_dumped", "secretsdump_connected"],
        },
    },
    "golden-ticket": {
        "target_type": "domain",
        "effect": "da:{domain}",
        "preconditions": ["krbtgt-hash:{domain}"],
        "verify": {
            "achieved_any": ["domain_admin", "ticket_valid"],
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
            "achieved_any": ["domain_admin", "ticket_valid"],
            "member_of_contains": ["domain admins", "enterprise admins"],
            "partial_any": ["ticket_forged", "tgt_present"],
        },
    },
}

_ADMIN_INTEGRITY = {"admin", "administrator", "elevated", "high", "system"}
_HOP_STATUSES = {"achieved", "failed", "blocked", "pending"}
# Predicate prefixes that can ONLY be asserted from BloodHound graph data. When no graph data has
# been reconciled into the state, these are UNKNOWN (not false) and must not block a hop.
_GRAPH_DERIVED_PREFIXES = ("generic-write", "generic-all", "write-dacl", "write-owner", "can-add-member")


def gate_decision(technique: str, target: str, state: EngagementState) -> tuple[GateDecision, str]:
    """Return whether a hop should skip, defer, or proceed."""
    try:
        model = TECHNIQUE_MODEL.get(technique)
        if model is None:
            return GateDecision.PROCEED, "technique not modeled — fail-open"

        effect = _technique_effect(technique, target)
        achieved_hop = _effect_hop(state, effect)
        durable_unconfirmed = False
        if achieved_hop is not None:
            provenance = _hop_provenance(achieved_hop)
            # A run-provenance hop (achieved THIS process) is trustworthy → hard-SKIP (preserves the
            # within-run 604→0 loop fix). A durable hop (loaded from disk) is only trusted when an
            # INDEPENDENT live signal corroborates it; otherwise we must NOT silently skip a possibly
            # stale belief (the redeploy footgun) — fall through and let the operator re-verify by doing.
            if provenance != "durable" or corroborate_effect(effect, state):
                tag = "durable+corroborated" if provenance == "durable" else "run"
                return GateDecision.SKIP, (
                    f"effect already achieved ({tag}): {effect}; evidence={dict(getattr(achieved_hop, 'evidence', {}))}"
                )
            durable_unconfirmed = True

        preconditions = _technique_preconditions(technique, target)
        # Belief: unknown ≠ false. Only DEFER on a precondition the state can AFFIRMATIVELY
        # determine is unmet. A graph-derived ACL precondition with no graph data reconciled,
        # or an empty/unresolved-value predicate, is UNKNOWN — it must not block the hop.
        missing = [
            predicate for predicate in preconditions
            if not state.satisfies_predicate(predicate)
            and _is_enforceable_precondition(predicate, state)
        ]
        if missing:
            return GateDecision.DEFER, f"missing precondition(s): {', '.join(missing)}"

        proceed_reason = f"preconditions met for {technique} on {target}"
        if durable_unconfirmed:
            proceed_reason += (
                " [durable belief NOT corroborated by live signal — proceeding to re-verify by execution "
                "rather than silently skip a possibly-stale hop]"
            )
        return GateDecision.PROCEED, proceed_reason
    except Exception as exc:
        return GateDecision.PROCEED, f"gate failed — fail-open: {exc}"


def _is_enforceable_precondition(predicate: str, state: EngagementState) -> bool:
    """Whether the current state can affirmatively determine this precondition is UNMET.

    Returns False (do not block) when the precondition is unknowable from the available data:
    - an empty / unresolved value (predicate ending in ':' or with no value), or
    - a graph-derived ACL predicate when no graph data has been reconciled into the state.
    Returns True only when the state can genuinely assert-or-deny the predicate.
    """
    norm = _normalize_predicate(predicate)
    if ":" not in norm:
        return False
    _, _, tail = norm.partition(":")
    if not tail.strip():
        return False
    if any(norm.startswith(prefix) for prefix in _GRAPH_DERIVED_PREFIXES):
        if not getattr(state, "graph_facts", None):
            return False
    return True


def record_hop_result(
    state: EngagementState,
    technique: str,
    target: str,
    status: str,
    evidence: dict,
    now: str,
) -> EngagementState:
    """Return state with a hop result appended or updated by technique and target."""
    normalized_status = _normalize_key(status)
    if normalized_status not in _HOP_STATUSES:
        raise ValueError(f"invalid hop status: {status!r}")

    effect = _technique_effect(technique, target)
    preconditions = _technique_preconditions(technique, target)
    source = _text(evidence.get("source")) if isinstance(evidence, dict) else ""
    hop = Hop(
        id=_hop_id(technique, target),
        technique=technique,
        target=target,
        effect=effect,
        status=normalized_status,
        evidence=dict(evidence) if isinstance(evidence, dict) else {},
        preconditions=preconditions,
        satisfied_effects=[effect],
        source=source or "record_hop_result",
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
    """Return a compact observed-state block for prompt injection."""
    try:
        lines = [_RENDER_HEADER]
        objective = _render_value(getattr(state, "objective", ""))
        if objective:
            lines.append(f"Objective: {objective}")
        elif state is None:
            lines.append("Objective: (state unavailable)")

        live_footholds = []
        for foothold in _render_items(getattr(state, "footholds", [])):
            if getattr(foothold, "alive", False) is not True:
                continue
            host = _render_value(getattr(foothold, "host", "")) or "(unknown host)"
            forest = _render_value(getattr(foothold, "forest", "")) or "(unknown forest)"
            identity = _render_value(getattr(foothold, "identity", "")) or "(unknown identity)"
            integrity = _render_value(getattr(foothold, "integrity", "")) or "(unknown integrity)"
            callback_id = _render_value(getattr(foothold, "callback_id", ""))
            live_footholds.append((
                (_normalize_key(host), _normalize_key(forest), _normalize_key(callback_id)),
                f"- {host} | forest={forest} | identity={identity} | integrity={integrity}",
            ))

        achieved_hops = []
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
            elif status in {"failed", "blocked"}:
                blocked_hops.append((sort_key, f"- {status}: {technique} → {target}: {effect}"))

        if live_footholds:
            lines.append("Live footholds:")
            lines.extend(item for _, item in sorted(live_footholds, key=lambda item: item[0]))
        if achieved_hops:
            lines.append("Achieved hops:")
            lines.extend(item for _, item in sorted(achieved_hops, key=lambda item: item[0]))
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
