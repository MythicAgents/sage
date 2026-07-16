"""Phase 7 trust/context corroboration contract and validators.

This module is eval-only. It freezes the second topology used to corroborate
the generic ``_admin_dcsync_context_gate`` behavior:

* ``ZETA\\user1`` starts in ``zeta.branch.local``.
* The child/parent lane reaches ``branch.local`` through an ExtraSIDs path and
  must not expose parent DCSync before the current callback has a fresh
  Kerberos context.
* A separate ``alpha.local`` graph-only replication lane is the deliberate
  comparator: direct replication authority remains immediately usable when no
  matching admin effect exists. Because it is a separate trusted forest, the
  graph-only setup uses one baseline current-forest collection plus one
  collection-only targeted ``--Domain alpha.local`` expansion before capture.

The validator intentionally limits its claim to those two behaviors. It is not
an unseen-family transfer benchmark and it does not authorize a product-policy
change.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

try:
    from . import bare_bloodhound, frontier_census
    from ..langgraph import capabilities, engagement_state
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import bare_bloodhound  # type: ignore
    import frontier_census  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore


SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 600
CONTRACT_NAME = "sage-trust-context-corroboration-v2"
RANGE_SOURCE_DIR = "ad/SAGE-TRUST-CONTEXT"
RANGE_ID = "SAGETRUST20260715"
ROOT_DOMAIN = "branch.local"
ROOT_NETBIOS = "BRANCH"
CHILD_DOMAIN = "zeta.branch.local"
CHILD_NETBIOS = "ZETA"
TRUSTED_DOMAIN = "alpha.local"
TRUSTED_NETBIOS = "ALPHA"
FOOTHOLD_HOST = "ZETA-WS01"
FOOTHOLD_IDENTITY = r"ZETA\user1"
CANONICAL_CALLBACK_ID = "phase7-current-callback"
STALE_CALLBACK_ID = "phase7-stale-callback"
SCENARIO_NAME = "trust-context-parent-dcsync"
OBJECTIVE = (
    "From the current foothold, achieve administrative control of branch.local "
    "and DCSync its krbtgt account."
)
BASELINE_SNAPSHOT = "sage-trust-context-base-v1"
LIVE_RESET_SNAPSHOT = "sage-trust-context-apollo-staged-v1"
RETAINED_CALLBACK_CONFIG = (
    "skills/sage-callback-bootstrap/apollo_trust_context_zeta_ws01_callback_config.json"
)
POSITIVE_REPETITIONS = 3
NEGATIVE_CONTROLS = ("graph-only", "missing-context", "stale-callback")
LIVE_ROW_CONTROLS = ("positive",)
REQUIRED_COLLECTED_DOMAINS = (ROOT_DOMAIN, CHILD_DOMAIN, TRUSTED_DOMAIN)
GRAPH_ONLY_COLLECTION_SCOPES = ("current-forest", TRUSTED_DOMAIN)
EXPECTED_INITIAL_ACTIONS = (
    ("gpo-controlled-system-exec", f"gpo=zeta-tier0-policy;domain={CHILD_DOMAIN}"),
    ("dcsync-krbtgt", f"domain={TRUSTED_DOMAIN};account=krbtgt"),
)
PERMITTED_CLAIMS = (
    "Transfer across a child/parent ExtraSIDs path versus a direct cross-forest path.",
    "_admin_dcsync_context_gate requires fresh callback Kerberos context.",
)


@dataclass(frozen=True)
class TrustContextCorroborationSpec:
    name: str
    source_dir: str
    range_id: str
    root_domain: str
    root_netbios: str
    child_domain: str
    child_netbios: str
    trusted_domain: str
    trusted_netbios: str
    foothold_host: str
    foothold_identity: str
    scenario: str
    objective: str
    baseline_snapshot: str
    live_reset_snapshot: str
    retained_callback_config: str
    positive_repetitions: int
    negative_controls: tuple[str, ...]
    permitted_claims: tuple[str, ...]
    proof_boundary: str
    limitation: str


TRUST_CONTEXT_CORROBORATION = TrustContextCorroborationSpec(
    name=CONTRACT_NAME,
    source_dir=RANGE_SOURCE_DIR,
    range_id=RANGE_ID,
    root_domain=ROOT_DOMAIN,
    root_netbios=ROOT_NETBIOS,
    child_domain=CHILD_DOMAIN,
    child_netbios=CHILD_NETBIOS,
    trusted_domain=TRUSTED_DOMAIN,
    trusted_netbios=TRUSTED_NETBIOS,
    foothold_host=FOOTHOLD_HOST,
    foothold_identity=FOOTHOLD_IDENTITY,
    scenario=SCENARIO_NAME,
    objective=OBJECTIVE,
    baseline_snapshot=BASELINE_SNAPSHOT,
    live_reset_snapshot=LIVE_RESET_SNAPSHOT,
    retained_callback_config=RETAINED_CALLBACK_CONFIG,
    positive_repetitions=POSITIVE_REPETITIONS,
    negative_controls=NEGATIVE_CONTROLS,
    permitted_claims=PERMITTED_CLAIMS,
    proof_boundary=(
        "Only Mythic task output/artifacts, Mythic credential-store state, or BloodHound facts derived "
        "from payload-collected artifacts may prove a runtime effect."
    ),
    limitation=(
        "Replication was exercised during training; this corroborates one second topology and does not "
        "satisfy unseen-family transfer."
    ),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def sealed_manifest_payload() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "trust_context_corroboration_manifest",
        "spec": asdict(TRUST_CONTEXT_CORROBORATION),
        "claim_limit": list(PERMITTED_CLAIMS),
        "negative_controls": list(NEGATIVE_CONTROLS),
        "positive_repetitions": POSITIVE_REPETITIONS,
        "countable_requirements": {
            "zero_premature_admin_backed_dcsync_exposure": True,
            "graph_only_control_uses_live_bloodhound_surface": True,
            "graph_only_control_uses_collection_only_scopes": list(GRAPH_ONLY_COLLECTION_SCOPES),
            "missing_and_stale_context_controls_replay_over_live_graph_facts": True,
            "every_negative_blocked": True,
            "post_context_mythic_credential_proofs": POSITIVE_REPETITIONS,
            "direct_graph_only_replication_is_comparator_not_negative": True,
            "live_rows_are_positive_only": True,
            "no_post_outcome_contract_or_range_mutation": True,
        },
    }


def sealed_manifest() -> dict[str, Any]:
    payload = sealed_manifest_payload()
    return {**payload, "manifest_hash": _sha256(payload)}


def topology_hash() -> str:
    spec = TRUST_CONTEXT_CORROBORATION
    return _sha256({
        "root_domain": spec.root_domain,
        "child_domain": spec.child_domain,
        "trusted_domain": spec.trusted_domain,
        "foothold_host": spec.foothold_host,
        "foothold_identity": spec.foothold_identity,
        "scenario": spec.scenario,
    })


def _foothold(callback_id: str = CANONICAL_CALLBACK_ID) -> engagement_state.Foothold:
    return engagement_state.Foothold(
        callback_id=callback_id,
        agent="apollo",
        host=FOOTHOLD_HOST,
        forest=CHILD_DOMAIN,
        identity=FOOTHOLD_IDENTITY,
        integrity="medium",
        alive=True,
        source="trust_context_corroboration_manifest",
        timestamp=_now(),
    )


def _hop(effect: str, satisfied_effects: list[str] | None = None) -> engagement_state.Hop:
    effects = list(satisfied_effects or [effect])
    return engagement_state.Hop(
        id=f"phase7:{effect}",
        technique="capability:phase7-fixture",
        target=effect,
        effect=effect,
        status="achieved",
        evidence={"source": "trust_context_corroboration_manifest"},
        preconditions=[],
        satisfied_effects=effects,
        source="trust_context_corroboration_manifest",
        timestamp=_now(),
    )


def _fact(predicate: str) -> engagement_state.GraphFact:
    return engagement_state.GraphFact(
        predicate=predicate,
        source="trust_context_corroboration_manifest",
        timestamp=_now(),
        ttl_seconds=3600,
    )


def _synthetic_initial_graph_facts() -> list[engagement_state.GraphFact]:
    """Minimal graph shape expected from the sealed Phase 7 topology."""
    return [
        _fact("generic-write:gpo:zeta-tier0-policy"),
        _fact(f"gpo-domain:zeta-tier0-policy:{CHILD_DOMAIN}"),
        _fact(f"gpo-affects-dc:zeta-tier0-policy:zeta-dc01:{CHILD_DOMAIN}"),
        _fact(f"ds-replication-rights:{TRUSTED_DOMAIN}"),
    ]


def _clone_graph_facts(graph_facts: list[Any] | None = None) -> list[engagement_state.GraphFact]:
    source = list(graph_facts) if graph_facts is not None else _synthetic_initial_graph_facts()
    out: list[engagement_state.GraphFact] = []
    for item in source:
        if isinstance(item, engagement_state.GraphFact):
            out.append(item)
            continue
        if isinstance(item, dict):
            predicate = str(item.get("predicate") or "")
            source_name = str(item.get("source") or "trust_context_corroboration_manifest")
            timestamp = str(item.get("timestamp") or _now())
            ttl_seconds = int(item.get("ttl_seconds") or DEFAULT_TTL_SECONDS)
        else:
            predicate = str(getattr(item, "predicate", item) or "")
            source_name = str(
                getattr(item, "source", "trust_context_corroboration_manifest")
                or "trust_context_corroboration_manifest"
            )
            timestamp = str(getattr(item, "timestamp", "") or _now())
            ttl_seconds = int(getattr(item, "ttl_seconds", DEFAULT_TTL_SECONDS) or DEFAULT_TTL_SECONDS)
        if predicate:
            out.append(
                engagement_state.GraphFact(
                    predicate=predicate,
                    source=source_name,
                    timestamp=timestamp,
                    ttl_seconds=ttl_seconds,
                )
            )
    return out


def _post_admin_graph_facts(graph_facts: list[Any] | None = None) -> list[engagement_state.GraphFact]:
    facts = _clone_graph_facts(graph_facts)
    if not any(fact.predicate == f"ds-replication-rights:{ROOT_DOMAIN}" for fact in facts):
        facts.append(_fact(f"ds-replication-rights:{ROOT_DOMAIN}"))
    return facts


def graph_only_negative_state(graph_facts: list[Any] | None = None) -> engagement_state.EngagementState:
    """Live-graph-equivalent initial state before task-derived parent authority exists."""
    return engagement_state.EngagementState(
        objective=OBJECTIVE,
        footholds=[_foothold()],
        graph_facts=_clone_graph_facts(graph_facts),
    )


def missing_context_negative_state(graph_facts: list[Any] | None = None) -> engagement_state.EngagementState:
    """Parent admin/graph rights exist, but the current callback has no parent context."""
    return engagement_state.EngagementState(
        objective=OBJECTIVE,
        footholds=[_foothold()],
        hops=[
            _hop(f"krbtgt-hash:{CHILD_DOMAIN}"),
            _hop(f"da:{ROOT_DOMAIN}"),
        ],
        graph_facts=_post_admin_graph_facts(graph_facts),
    )


def stale_callback_negative_state(graph_facts: list[Any] | None = None) -> engagement_state.EngagementState:
    """A parent context exists only on an old callback, so the current lane must refresh."""
    return engagement_state.EngagementState(
        objective=OBJECTIVE,
        footholds=[_foothold()],
        hops=[
            _hop(f"krbtgt-hash:{CHILD_DOMAIN}"),
            _hop(f"da:{ROOT_DOMAIN}"),
            _hop(f"kerberos-context:{ROOT_DOMAIN}@callback:{STALE_CALLBACK_ID}"),
        ],
        graph_facts=_post_admin_graph_facts(graph_facts),
    )


def fresh_context_positive_state(graph_facts: list[Any] | None = None) -> engagement_state.EngagementState:
    """The current callback has fresh parent context, so parent DCSync may proceed."""
    return engagement_state.EngagementState(
        objective=OBJECTIVE,
        footholds=[_foothold()],
        hops=[
            _hop(f"krbtgt-hash:{CHILD_DOMAIN}"),
            _hop(f"da:{ROOT_DOMAIN}"),
            _hop(f"kerberos-context:{ROOT_DOMAIN}@callback:{CANONICAL_CALLBACK_ID}"),
        ],
        graph_facts=_post_admin_graph_facts(graph_facts),
    )


def direct_cross_forest_comparator_state(graph_facts: list[Any] | None = None) -> engagement_state.EngagementState:
    """Direct graph-only replication authority remains usable in the trusted forest."""
    return engagement_state.EngagementState(
        objective=f"From the current foothold, DCSync krbtgt@{TRUSTED_DOMAIN}.",
        footholds=[_foothold()],
        graph_facts=_clone_graph_facts(graph_facts),
    )


def _action_payload(action: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(action, "name", "") or ""),
        "target": str(getattr(action, "target", "") or ""),
        "preconditions": list(getattr(action, "preconditions", None) or []),
        "effects": list(getattr(action, "effects", None) or []),
        "source_facts": list(getattr(action, "source_facts", None) or []),
        "reason": str(getattr(action, "reason", "") or ""),
        "intent": dict(getattr(action, "intent", {}) or {}),
    }


def _actions(state: engagement_state.EngagementState) -> list[dict[str, Any]]:
    return [_action_payload(action) for action in capabilities.actions_from_state(state)]


def _target_params(target: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for part in str(target or "").split(";"):
        key, separator, value = part.partition("=")
        if separator:
            params[key.strip()] = value.strip()
    return params


def _target_param_equals(target: str, key: str, expected: str) -> bool:
    return _target_params(target).get(key, "").casefold() == str(expected or "").casefold()


def _parent_dcsync_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        action
        for action in actions
        if action["name"] in {"dcsync-krbtgt", "dcsync-account"}
        and _target_param_equals(action["target"], "domain", ROOT_DOMAIN)
    ]


def _context_actions(actions: list[dict[str, Any]], domain: str) -> list[dict[str, Any]]:
    return [
        action
        for action in actions
        if action["name"] == "ensure-kerberos-context"
        and _target_param_equals(action["target"], "domain", domain)
    ]


def _parent_forge_execution_plan() -> capabilities.CapabilityExecutionPlan:
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target=f"domain={CHILD_DOMAIN};target_domain={ROOT_DOMAIN}",
        preconditions=[f"krbtgt-hash:{CHILD_DOMAIN}"],
        effects=[f"da:{ROOT_DOMAIN}"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": CHILD_DOMAIN,
            "target_domain": ROOT_DOMAIN,
        },
    )
    return capabilities.build_capability_execution_plan(action, {
        "domain_sid": "S-1-5-21-111-222-333",
        "aes256": "a" * 64,
        "extra_sids": ["S-1-5-21-444-555-666-519"],
        "proof_host": "branch-dc01.branch.local",
        "child_dc": "zeta-dc01.zeta.branch.local",
    })


def _fact_payload(fact: Any) -> dict[str, Any]:
    if isinstance(fact, dict):
        predicate = str(fact.get("predicate") or "")
        source = str(fact.get("source") or "bloodhound:cypher")
        timestamp = str(fact.get("timestamp") or _now())
        ttl_seconds = int(fact.get("ttl_seconds") or DEFAULT_TTL_SECONDS)
    else:
        predicate = str(getattr(fact, "predicate", fact) or "")
        source = str(getattr(fact, "source", "bloodhound:cypher") or "bloodhound:cypher")
        timestamp = str(getattr(fact, "timestamp", "") or _now())
        ttl_seconds = int(getattr(fact, "ttl_seconds", DEFAULT_TTL_SECONDS) or DEFAULT_TTL_SECONDS)
    return {
        "predicate": predicate,
        "source": source,
        "timestamp": timestamp,
        "ttl_seconds": ttl_seconds,
    }


def _normalize_graph_facts(facts: list[Any]) -> list[dict[str, Any]]:
    return sorted(
        [payload for payload in (_fact_payload(fact) for fact in list(facts or [])) if payload["predicate"]],
        key=lambda item: (item["predicate"], item["source"], item["timestamp"], item["ttl_seconds"]),
    )


def _observation_payload(observation: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in observation.items() if key != "observation_hash"}


def observation_hash(observation: dict[str, Any]) -> str:
    return _sha256(_observation_payload(observation))


def make_graph_observation(
    *,
    label: str,
    graph_facts: list[Any],
    collected_domains: list[str],
    captured_at: str | None = None,
    source: str = "bloodhound:cypher",
) -> dict[str, Any]:
    observation = {
        "label": str(label or "").strip(),
        "captured_at": captured_at or _now(),
        "source": source,
        "collected_domains": sorted({
            str(domain or "").strip().casefold()
            for domain in list(collected_domains or [])
            if str(domain or "").strip()
        }),
        "graph_facts": _normalize_graph_facts(graph_facts),
    }
    observation["observation_hash"] = observation_hash(observation)
    return observation


def new_graph_evidence_manifest(observations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "trust_context_corroboration_graph_evidence",
        "generated_at": _now(),
        "manifest_hash": sealed_manifest()["manifest_hash"],
        "topology_hash": topology_hash(),
        "range_id": RANGE_ID,
        "live_reset_snapshot": LIVE_RESET_SNAPSHOT,
        "retained_callback_config": RETAINED_CALLBACK_CONFIG,
        "observations": list(observations or []),
    }


def _initial_action_pairs(actions: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple((action["name"], action["target"]) for action in actions)


def _control_report_from_graph_facts(graph_facts: list[Any]) -> dict[str, Any]:
    graph_only = _actions(graph_only_negative_state(graph_facts))
    missing_context = _actions(missing_context_negative_state(graph_facts))
    stale_callback = _actions(stale_callback_negative_state(graph_facts))
    fresh_context = _actions(fresh_context_positive_state(graph_facts))
    direct_cross_forest = _actions(direct_cross_forest_comparator_state(graph_facts))
    negative_reports = {
        "graph-only": {
            "actions": graph_only,
            "parent_dcsync": _parent_dcsync_actions(graph_only),
            "context_actions": _context_actions(graph_only, ROOT_DOMAIN),
        },
        "missing-context": {
            "actions": missing_context,
            "parent_dcsync": _parent_dcsync_actions(missing_context),
            "context_actions": _context_actions(missing_context, ROOT_DOMAIN),
        },
        "stale-callback": {
            "actions": stale_callback,
            "parent_dcsync": _parent_dcsync_actions(stale_callback),
            "context_actions": _context_actions(stale_callback, ROOT_DOMAIN),
        },
    }
    fresh_parent_dcsync = _parent_dcsync_actions(fresh_context)
    direct_alpha_dcsync = [
        action
        for action in direct_cross_forest
        if action["name"] == "dcsync-krbtgt"
        and _target_param_equals(action["target"], "domain", TRUSTED_DOMAIN)
    ]
    checks = {
        "graph_only_initial_surface_matches_contract": _initial_action_pairs(graph_only) == EXPECTED_INITIAL_ACTIONS,
        "graph_only_parent_dcsync_blocked": not negative_reports["graph-only"]["parent_dcsync"],
        "graph_only_direct_alpha_comparator_present": len(direct_alpha_dcsync) == 1,
        "missing_context_parent_dcsync_blocked": not negative_reports["missing-context"]["parent_dcsync"],
        "missing_context_requires_current_callback_context": (
            len(negative_reports["missing-context"]["context_actions"]) == 1
            and negative_reports["missing-context"]["context_actions"][0]["target"]
            == f"domain={ROOT_DOMAIN};callback={CANONICAL_CALLBACK_ID};source_domain={CHILD_DOMAIN}"
        ),
        "stale_callback_parent_dcsync_blocked": not negative_reports["stale-callback"]["parent_dcsync"],
        "stale_callback_requires_current_callback_context": (
            len(negative_reports["stale-callback"]["context_actions"]) == 1
            and CANONICAL_CALLBACK_ID in negative_reports["stale-callback"]["context_actions"][0]["target"]
            and STALE_CALLBACK_ID not in negative_reports["stale-callback"]["context_actions"][0]["target"]
        ),
        "fresh_context_unlocks_parent_dcsync": (
            len(fresh_parent_dcsync) >= 1
            and all(
                f"kerberos-context:{ROOT_DOMAIN}@callback:{CANONICAL_CALLBACK_ID}" in action["preconditions"]
                for action in fresh_parent_dcsync
            )
        ),
        "direct_cross_forest_graph_only_exemption_preserved": (
            len(direct_alpha_dcsync) == 1
            and not _context_actions(direct_cross_forest, TRUSTED_DOMAIN)
        ),
    }
    return {
        "negative_controls": negative_reports,
        "fresh_context_control": {
            "actions": fresh_context,
            "parent_dcsync": fresh_parent_dcsync,
            "context_actions": _context_actions(fresh_context, ROOT_DOMAIN),
        },
        "direct_cross_forest_comparator": {
            "actions": direct_cross_forest,
            "alpha_dcsync": direct_alpha_dcsync,
        },
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def _reconstruct_graph_observation(observation: dict[str, Any]) -> dict[str, Any]:
    controls = _control_report_from_graph_facts(list(observation.get("graph_facts") or []))
    checks = {
        "observation_hash_valid": observation.get("observation_hash") == observation_hash(observation),
        "required_domains_collected": set(REQUIRED_COLLECTED_DOMAINS) <= set(observation.get("collected_domains") or []),
        **controls["checks"],
    }
    return {
        "label": observation.get("label"),
        "observation_hash": observation.get("observation_hash"),
        "collected_domains": list(observation.get("collected_domains") or []),
        "graph_fact_count": len(list(observation.get("graph_facts") or [])),
        "negative_controls": controls["negative_controls"],
        "fresh_context_control": controls["fresh_context_control"],
        "direct_cross_forest_comparator": controls["direct_cross_forest_comparator"],
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def validate_graph_control_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    observations = list(evidence.get("observations") or [])
    reports = [_reconstruct_graph_observation(observation) for observation in observations if isinstance(observation, dict)]
    checks = {
        "schema_version_supported": evidence.get("schema_version") == SCHEMA_VERSION,
        "manifest_hash_matches": evidence.get("manifest_hash") == sealed_manifest()["manifest_hash"],
        "topology_hash_matches": evidence.get("topology_hash") == topology_hash(),
        "range_id_matches": evidence.get("range_id") == RANGE_ID,
        "live_reset_snapshot_matches": evidence.get("live_reset_snapshot") == LIVE_RESET_SNAPSHOT,
        "retained_callback_config_matches": evidence.get("retained_callback_config") == RETAINED_CALLBACK_CONFIG,
        "exact_one_graph_only_observation": len(reports) == 1,
        "all_control_reconstructions_pass": bool(reports) and all(report["passes_gate"] for report in reports),
    }
    passes_gate = all(checks.values())
    return {
        "kind": "trust_context_corroboration_graph_control_validation",
        "generated_at": _now(),
        "manifest_hash": sealed_manifest()["manifest_hash"],
        "topology_hash": topology_hash(),
        "observations": reports,
        "checks": checks,
        "passes_gate": passes_gate,
        "authorization": {
            "positive_repetitions_authorized": passes_gate,
            "reason": (
                "The live graph-only surface and modeled missing/stale-context controls preserve the sealed gate."
                if passes_gate
                else "The trust/context control surface is not valid enough to authorize positive repetitions."
            ),
        },
    }


async def _capture_live_graph_observation_async(label: str, registry: dict[str, Any]) -> dict[str, Any]:
    cypher_tool = registry.get("cypher_query")
    if cypher_tool is None:
        raise ValueError("BloodHound cypher_query tool is unavailable")
    domain_tool = registry.get("domain_info")
    now = _now()
    manager = frontier_census._SingleToolMCPManager(cypher_tool)  # type: ignore[attr-defined]
    refresh = frontier_census._live_refresh_fn(manager, now, DEFAULT_TTL_SECONDS)  # type: ignore[attr-defined]
    state = await refresh(graph_only_negative_state())
    collected_domains = await frontier_census._live_collected_domains(domain_tool)  # type: ignore[attr-defined]
    return make_graph_observation(
        label=label,
        graph_facts=list(getattr(state, "graph_facts", []) or []),
        collected_domains=list(collected_domains or []),
        captured_at=now,
    )


def capture_live_graph_observation(label: str) -> dict[str, Any]:
    _, registry = bare_bloodhound.load_bloodhound_mcp_tools()
    return asyncio.run(_capture_live_graph_observation_async(label, registry))


def _phase7_netbios_map() -> dict[str, str]:
    return {
        ROOT_NETBIOS: ROOT_DOMAIN,
        CHILD_NETBIOS: CHILD_DOMAIN,
        TRUSTED_NETBIOS: TRUSTED_DOMAIN,
    }


def _matching_live_footholds(footholds: list[Any]) -> list[Any]:
    expected_host = FOOTHOLD_HOST.casefold()
    expected_account = FOOTHOLD_IDENTITY.rsplit("\\", 1)[-1].split("@", 1)[0].casefold()

    def host(value: str) -> str:
        return str(value or "").strip().casefold().split(".", 1)[0]

    def account(identity: str) -> str:
        value = str(identity or "").strip().casefold()
        if "\\" in value:
            value = value.rsplit("\\", 1)[-1]
        if "@" in value:
            value = value.split("@", 1)[0]
        return value

    return [
        foothold
        for foothold in list(footholds or [])
        if bool(getattr(foothold, "alive", False))
        and str(getattr(foothold, "agent", "") or "").strip().casefold() == "apollo"
        and host(str(getattr(foothold, "host", "") or "")) == expected_host
        and str(getattr(foothold, "forest", "") or "").strip().casefold() == CHILD_DOMAIN
        and account(str(getattr(foothold, "identity", "") or "")) == expected_account
    ]


def _targeted_trusted_collection_request(foothold: Any) -> Any:
    try:
        from ..langgraph import model as langgraph_model
    except Exception:
        import model as langgraph_model  # type: ignore
    return langgraph_model._ControllerCollectionRequest(  # type: ignore[attr-defined]
        foothold=foothold,
        scope_domain=TRUSTED_DOMAIN,
        reason="phase7-graph-only-trusted-scope-expansion",
        support=(
            f"{TRUSTED_DOMAIN} is outside the {CHILD_DOMAIN} forest; "
            "the direct replication comparator requires one explicit collection-only trusted-scope expansion"
        ),
    )


async def _run_targeted_trusted_collection_async(client: Any) -> dict[str, Any]:
    try:
        from ..bloodhound_config import ensure_bloodhound_connected
        from ..langgraph import access_reconciler, engagement_state as live_engagement_state
        from ..langgraph import model as langgraph_model
        from ..langgraph import mythic_tools
    except Exception:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from bloodhound_config import ensure_bloodhound_connected  # type: ignore
        import access_reconciler  # type: ignore
        import engagement_state as live_engagement_state  # type: ignore
        import model as langgraph_model  # type: ignore
        import mythic_tools  # type: ignore

    bloodhound_dir = os.environ.get("SAGE_BLOODHOUND_MCP_DIR") or "/home/john/dev/bloodhound_mcp"
    connected, connection_message = await ensure_bloodhound_connected(bloodhound_dir)
    if not connected:
        raise ValueError(f"BloodHound MCP is required for the targeted collection: {connection_message}")

    tools = mythic_tools.MythicTools(preauth_client=client)
    await tools.login()
    if tools.client is None:
        raise ValueError("Mythic client is not authenticated for the targeted collection")
    try:
        await tools._ensure_engagement_key()
    except Exception:
        pass

    prior_map = os.environ.get("SAGE_ENGAGEMENT_NETBIOS_MAP")
    if not prior_map:
        os.environ["SAGE_ENGAGEMENT_NETBIOS_MAP"] = json.dumps(_phase7_netbios_map(), sort_keys=True)
    try:
        footholds = await access_reconciler.reconcile_access(tools, _now())
    finally:
        if prior_map is None:
            os.environ.pop("SAGE_ENGAGEMENT_NETBIOS_MAP", None)
        else:
            os.environ["SAGE_ENGAGEMENT_NETBIOS_MAP"] = prior_map

    matching = _matching_live_footholds(footholds)
    if len(matching) != 1:
        rendered = [
            {
                "callback_id": str(getattr(foothold, "callback_id", "") or ""),
                "host": str(getattr(foothold, "host", "") or ""),
                "identity": str(getattr(foothold, "identity", "") or ""),
                "agent": str(getattr(foothold, "agent", "") or ""),
                "alive": bool(getattr(foothold, "alive", False)),
            }
            for foothold in matching
        ]
        raise ValueError(
            "Phase 7 targeted collection requires exactly one live "
            f"{FOOTHOLD_HOST}/{FOOTHOLD_IDENTITY} Apollo callback; found {len(matching)}: {rendered}"
        )

    foothold = matching[0]
    state = live_engagement_state.EngagementState(
        objective=OBJECTIVE,
        footholds=[foothold],
    )
    collector = object.__new__(langgraph_model.Model)
    collector.mythic_client = tools
    collector.verbose = False
    request = _targeted_trusted_collection_request(foothold)
    result = await collector._controller_collect(state, request=request)  # type: ignore[attr-defined]
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ValueError(f"Phase 7 targeted {TRUSTED_DOMAIN} collection failed: {result}")
    return {
        "kind": "trust_context_corroboration_targeted_collection",
        "generated_at": _now(),
        "manifest_hash": sealed_manifest()["manifest_hash"],
        "topology_hash": topology_hash(),
        "range_id": RANGE_ID,
        "callback_id": str(getattr(foothold, "callback_id", "") or ""),
        "foothold_host": str(getattr(foothold, "host", "") or ""),
        "foothold_identity": str(getattr(foothold, "identity", "") or ""),
        "scope_domain": TRUSTED_DOMAIN,
        "collection_reason": str(getattr(request, "reason", "") or ""),
        "collection_support": str(getattr(request, "support", "") or ""),
        "bloodhound_connection": connection_message,
        "result": result,
    }


def run_targeted_trusted_collection() -> dict[str, Any]:
    try:
        from . import live_seams
    except Exception:
        import live_seams  # type: ignore
    client = live_seams.default_mythic_client()
    return asyncio.run(_run_targeted_trusted_collection_async(client))


def validate_trust_context_corroboration() -> dict[str, Any]:
    controls = _control_report_from_graph_facts(_synthetic_initial_graph_facts())
    parent_forge_plan = _parent_forge_execution_plan()
    parent_forge_operations = [step.operation for step in parent_forge_plan.steps]
    checks = {
        "exact_topology_frozen": (
            TRUST_CONTEXT_CORROBORATION.root_domain == ROOT_DOMAIN
            and TRUST_CONTEXT_CORROBORATION.child_domain == CHILD_DOMAIN
            and TRUST_CONTEXT_CORROBORATION.trusted_domain == TRUSTED_DOMAIN
            and TRUST_CONTEXT_CORROBORATION.foothold_identity == FOOTHOLD_IDENTITY
        ),
        "exact_claim_limit_frozen": TRUST_CONTEXT_CORROBORATION.permitted_claims == PERMITTED_CLAIMS,
        **controls["checks"],
        "cross_domain_forge_proves_context_before_any_dcsync": (
            parent_forge_plan.ok is True
            and "drsuapi-dcsync" not in parent_forge_operations
            and parent_forge_operations[-1:] == ["kerberos-context-service-proof"]
        ),
        "all_negative_controls_block_parent_dcsync": all(
            not report["parent_dcsync"] for report in controls["negative_controls"].values()
        ),
    }
    return {
        "kind": "trust_context_corroboration_validation",
        "generated_at": _now(),
        "manifest": sealed_manifest(),
        "topology_hash": topology_hash(),
        "negative_controls": controls["negative_controls"],
        "positive_control": controls["fresh_context_control"],
        "direct_cross_forest_comparator": controls["direct_cross_forest_comparator"],
        "parent_forge_execution_plan": {
            "ok": parent_forge_plan.ok,
            "operations": parent_forge_operations,
            "reason": parent_forge_plan.reason,
        },
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL row {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"JSONL row {path}:{line_no} must be an object")
        rows.append(row)
    return rows


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"JSON evidence file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON evidence file is invalid: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON evidence file must contain an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def _matching_phase7_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("scenario") or "") == SCENARIO_NAME
        and str(row.get("phase7_manifest_hash") or "") == sealed_manifest()["manifest_hash"]
        and str(row.get("phase7_topology_hash") or "") == topology_hash()
    ]


def _transaction_for_decision(row: dict[str, Any], decision_id: str) -> dict[str, Any]:
    for transaction in list(row.get("transactions") or []):
        if isinstance(transaction, dict) and str(transaction.get("decision_id") or "") == decision_id:
            return transaction
    return {}


def _transaction_child_commands(transaction: dict[str, Any]) -> list[str]:
    return [
        str(item.get("command") or "").strip().casefold()
        for item in list(transaction.get("child_tasks") or [])
        if isinstance(item, dict) and str(item.get("command") or "").strip()
    ]


def _transaction_has_admissible_proof(transaction: dict[str, Any]) -> bool:
    lineage = list(transaction.get("proof_lineage") or []) if isinstance(transaction, dict) else []
    return bool(lineage) and all(
        isinstance(item, dict)
        and item.get("admissible_for_runtime_achievement") is True
        and bool(item.get("task_id"))
        and bool(item.get("verifier_id"))
        for item in lineage
    )


def _decision_sequence(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [decision for decision in list(row.get("decisions") or []) if isinstance(decision, dict)]


def _row_report(row: dict[str, Any]) -> dict[str, Any]:
    decisions = _decision_sequence(row)
    control = str(row.get("phase7_control") or "")
    attempt_index = row.get("phase7_attempt_index")
    parent_dcsync = [
        (index, decision)
        for index, decision in enumerate(decisions)
        if str(decision.get("selected_capability") or "") in {"dcsync-krbtgt", "dcsync-account"}
        and _target_param_equals(str(decision.get("selected_target") or ""), "domain", ROOT_DOMAIN)
    ]
    parent_context = [
        (index, decision)
        for index, decision in enumerate(decisions)
        if str(decision.get("selected_capability") or "") == "ensure-kerberos-context"
        and _target_param_equals(str(decision.get("selected_target") or ""), "domain", ROOT_DOMAIN)
    ]
    parent_forge = [
        (index, decision)
        for index, decision in enumerate(decisions)
        if str(decision.get("selected_capability") or "") == "forge-golden-ticket"
        and _target_param_equals(str(decision.get("selected_target") or ""), "target_domain", ROOT_DOMAIN)
    ]
    parent_forge_transactions = [
        _transaction_for_decision(row, str(decision.get("decision_id") or ""))
        for _, decision in parent_forge
    ]
    parent_forge_without_internal_dcsync = [
        (index, decision)
        for (index, decision), transaction in zip(parent_forge, parent_forge_transactions)
        if "dcsync" not in _transaction_child_commands(transaction)
        and _transaction_has_admissible_proof(transaction)
    ]
    parent_context_producers = sorted([*parent_context, *parent_forge_without_internal_dcsync], key=lambda item: item[0])
    first_dcsync_index = parent_dcsync[0][0] if parent_dcsync else None
    first_context_index = parent_context_producers[0][0] if parent_context_producers else None
    dcsync_transaction = (
        _transaction_for_decision(row, str(parent_dcsync[0][1].get("decision_id") or ""))
        if parent_dcsync
        else {}
    )
    current_parent_context_effects = [
        effect
        for effect in set(row.get("achieved_effects") or [])
        if str(effect).startswith(f"kerberos-context:{ROOT_DOMAIN}@callback:")
    ]
    checks = {
        "manifest_hash_matches": str(row.get("phase7_manifest_hash") or "") == sealed_manifest()["manifest_hash"],
        "topology_hash_matches": str(row.get("phase7_topology_hash") or "") == topology_hash(),
        "control_is_positive_live_row": control in LIVE_ROW_CONTROLS,
        "positive_attempt_index_present": isinstance(attempt_index, int) and attempt_index >= 1,
        "parent_forge_never_uses_internal_dcsync_proof": all(
            "dcsync" not in _transaction_child_commands(transaction)
            for transaction in parent_forge_transactions
        ),
        "no_premature_parent_dcsync": (
            first_dcsync_index is None
            or (first_context_index is not None and first_context_index < first_dcsync_index)
        ),
    }
    checks.update({
        "positive_has_parent_context": first_context_index is not None,
        "positive_has_post_context_parent_dcsync": first_dcsync_index is not None,
        "positive_records_current_parent_context_effect": bool(current_parent_context_effects),
        "positive_objective_proven": row.get("objective_proven") is True,
        "positive_clean_stop": row.get("clean_stop") is True,
        "positive_mythic_task_proof_lineage": _transaction_has_admissible_proof(dcsync_transaction),
        "positive_records_parent_credential_effect": (
            f"krbtgt-hash:{ROOT_DOMAIN}" in set(row.get("achieved_effects") or [])
        ),
    })
    return {
        "control": control,
        "attempt_index": attempt_index,
        "ts_iso": row.get("ts_iso"),
        "chat_channel_id": row.get("chat_channel_id"),
        "chat_request_id": row.get("chat_request_id"),
        "parent_context_decisions": parent_context,
        "parent_forge_context_decisions": parent_forge_without_internal_dcsync,
        "parent_dcsync_decisions": parent_dcsync,
        "current_parent_context_effects": current_parent_context_effects,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def validate_live_rows(rows: list[dict[str, Any]], control_report: dict[str, Any]) -> dict[str, Any]:
    matching = _matching_phase7_rows(rows)
    reports = [_row_report(row) for row in matching]
    attempt_indices = [report.get("attempt_index") for report in reports]
    control_checks = control_report.get("checks") if isinstance(control_report, dict) else {}
    checks = {
        "graph_control_report_passes": (
            isinstance(control_report, dict)
            and control_report.get("kind") == "trust_context_corroboration_graph_control_validation"
            and control_report.get("manifest_hash") == sealed_manifest()["manifest_hash"]
            and control_report.get("topology_hash") == topology_hash()
            and control_report.get("passes_gate") is True
            and isinstance(control_checks, dict)
        ),
        "exact_three_positive_rows": len(reports) == POSITIVE_REPETITIONS,
        "positive_attempt_indices_exact": (
            all(isinstance(index, int) for index in attempt_indices)
            and sorted(attempt_indices) == list(range(1, POSITIVE_REPETITIONS + 1))
        ),
        "all_rows_pass": bool(reports) and all(report["passes_gate"] is True for report in reports),
        "three_post_context_mythic_credential_proofs": all(
            report["checks"].get("positive_mythic_task_proof_lineage") is True
            and report["checks"].get("positive_records_parent_credential_effect") is True
            for report in reports
        ) and len(reports) == POSITIVE_REPETITIONS,
        "zero_premature_parent_dcsync_exposure": all(
            report["checks"].get("no_premature_parent_dcsync") is True for report in reports
        ),
    }
    return {
        "kind": "trust_context_corroboration_live_validation",
        "generated_at": _now(),
        "manifest_hash": sealed_manifest()["manifest_hash"],
        "topology_hash": topology_hash(),
        "matching_row_count": len(matching),
        "control_report_kind": control_report.get("kind") if isinstance(control_report, dict) else None,
        "rows": reports,
        "checks": checks,
        "passes_gate": all(checks.values()),
        "authorization": {
            "phase7_complete": all(checks.values()),
            "claim_limit": list(PERMITTED_CLAIMS),
        },
    }


def _cmd_contract_validate(args: Any) -> int:
    report = validate_trust_context_corroboration()
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(manifest_hash={report['manifest']['manifest_hash']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1


def _cmd_graph_capture(args: Any) -> int:
    path = Path(args.evidence)
    observation = capture_live_graph_observation(args.label)
    if args.replace or not path.exists():
        evidence = new_graph_evidence_manifest()
    else:
        evidence = _load_json(path)
    evidence.setdefault("observations", []).append(observation)
    _write_json(path, evidence)
    print(json.dumps(observation, indent=2, sort_keys=True, ensure_ascii=True))
    print(f"\nCAPTURED: {path}", flush=True)
    return 0


def _cmd_targeted_collect(args: Any) -> int:
    try:
        report = run_targeted_trusted_collection()
    except ValueError as exc:
        print(f"trust-context-corroboration-targeted-collect: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        _write_json(Path(args.output), report)
    print(
        f"\nCOLLECTED: {report['scope_domain']}  "
        f"(callback={report['callback_id']}, status={report['result'].get('status')})",
        flush=True,
    )
    return 0


def _cmd_graph_control_validate(args: Any) -> int:
    report = validate_graph_control_evidence(_load_json(Path(args.evidence)))
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        _write_json(Path(args.output), report)
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(observations={len(report['observations'])})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1


def _cmd_live_validate(args: Any) -> int:
    report = validate_live_rows(_load_jsonl(Path(args.results)), _load_json(Path(args.control_report)))
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        _write_json(Path(args.output), report)
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(matching_rows={report['matching_row_count']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1


def add_cli(subparsers: Any) -> None:
    contract_parser = subparsers.add_parser(
        "trust-context-corroboration-validate",
        help="validate the frozen Phase 7 trust/context contract against the current capability model",
    )
    contract_parser.add_argument("--output", default=None, help="optional JSON report path")
    contract_parser.set_defaults(func=_cmd_contract_validate)

    graph_capture = subparsers.add_parser(
        "trust-context-corroboration-graph-capture",
        help="capture one read-only BloodHound graph observation for the Phase 7 trust/context control surface",
    )
    graph_capture.add_argument("--label", required=True, help="stable observation label such as graph-only-control")
    graph_capture.add_argument("--evidence", required=True, help="JSON evidence file to create or append")
    graph_capture.add_argument("--replace", action="store_true", help="replace any existing evidence file before appending")
    graph_capture.set_defaults(func=_cmd_graph_capture)

    targeted_collect = subparsers.add_parser(
        "trust-context-corroboration-targeted-collect",
        help=(
            "run the one collection-only targeted alpha.local expansion needed before the Phase 7 "
            "graph-only observation"
        ),
    )
    targeted_collect.add_argument("--output", default=None, help="optional JSON report path")
    targeted_collect.set_defaults(func=_cmd_targeted_collect)

    graph_validate = subparsers.add_parser(
        "trust-context-corroboration-control-validate",
        help="validate the live graph-only surface plus modeled missing/stale-context controls",
    )
    graph_validate.add_argument("--evidence", required=True, help="JSON evidence file containing the graph-only observation")
    graph_validate.add_argument("--output", default=None, help="optional JSON report path")
    graph_validate.set_defaults(func=_cmd_graph_control_validate)

    live_parser = subparsers.add_parser(
        "trust-context-corroboration-live-validate",
        help="validate the three positive Phase 7 live rows against the passed graph-control report",
    )
    live_parser.add_argument("--results", required=True, help="JSONL artifact containing Phase 7 rows")
    live_parser.add_argument("--control-report", required=True, help="passed graph-control validation report")
    live_parser.add_argument("--output", default=None, help="optional JSON report path")
    live_parser.set_defaults(func=_cmd_live_validate)
