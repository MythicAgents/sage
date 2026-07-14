"""Live-surface validation for the same-domain GPO DC-scope benchmark.

This module is eval-only. It does not task Mythic, mutate BloodHound, or alter
runtime policy behavior. It consumes graph observations captured after a clean
range reset and one collection, reconstructs the authorized late-blocker
frontier through the current generic capability model, and checks that repeated
observations preserve the same decisive frontier.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

try:  # package import
    from . import bare_bloodhound
    from . import frontier_census
    from . import gpo_dc_scope_late_blocker_contract as contract
    from . import purpose_range
    from ..langgraph import capabilities, engagement_state, graph_reconciler, policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import bare_bloodhound  # type: ignore
    import frontier_census  # type: ignore
    import gpo_dc_scope_late_blocker_contract as contract  # type: ignore
    import purpose_range  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore
    import graph_reconciler  # type: ignore
    import policy  # type: ignore


SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 600
SURFACE_NAME = contract.GPO_DC_SCOPE_LATE_BLOCKER.name
SURFACE_SCENARIO = contract.GPO_DC_SCOPE_LATE_BLOCKER.scenario
SURFACE_RANGE_ID = "SAGEPOLICY20260712"
SURFACE_SNAPSHOT = "sage-policy-range-gpo-dc-scope-base-v1"
SURFACE_RETAINED_CALLBACK_CONFIG = "skills/sage-callback-bootstrap/apollo_policy_range_ws01_callback_config.json"
SURFACE_RESET_PATH = (
    "rollback the Ludus range to the declared snapshot",
    "power on all range VMs and synchronize clocks",
    "reset Mythic, restart Sage, and wipe BloodHound",
    "import the retained WS01 Apollo callback config and launch the staged callback",
    "run one controller cycle with SAGE_CONTROLLER_MAX_CYCLES=1 to collect and ingest the graph only",
)


class GpoDcScopeLiveSurfaceError(ValueError):
    """Raised when live-surface evidence cannot be loaded or reconstructed."""


@dataclass(frozen=True)
class GpoDcScopeLiveSurfaceSpec:
    name: str
    scenario: str
    range_id: str
    snapshot: str
    retained_callback_config: str
    objective: str
    foothold_host: str
    foothold_identity: str
    target_domain: str
    expected_targets: tuple[str, ...]
    reset_path: tuple[str, ...]


LIVE_SURFACE = GpoDcScopeLiveSurfaceSpec(
    name=SURFACE_NAME,
    scenario=SURFACE_SCENARIO,
    range_id=SURFACE_RANGE_ID,
    snapshot=SURFACE_SNAPSHOT,
    retained_callback_config=SURFACE_RETAINED_CALLBACK_CONFIG,
    objective=purpose_range.PURPOSE_RANGE.objective,
    foothold_host=purpose_range.PURPOSE_RANGE.foothold_host,
    foothold_identity=purpose_range.PURPOSE_RANGE.foothold_identity,
    target_domain=purpose_range.PURPOSE_RANGE.domain,
    expected_targets=(
        f"gpo={contract.GPO_DC_SCOPE_LATE_BLOCKER.non_dc_gpo};domain={purpose_range.PURPOSE_RANGE.domain}",
        f"gpo={contract.GPO_DC_SCOPE_LATE_BLOCKER.dc_scoped_gpo};domain={purpose_range.PURPOSE_RANGE.domain}",
    ),
    reset_path=SURFACE_RESET_PATH,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _observation_payload(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in observation.items()
        if key != "observation_hash"
    }


def observation_hash(observation: dict[str, Any]) -> str:
    return _sha256(_observation_payload(observation))


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
    normalized = [_fact_payload(fact) for fact in list(facts or [])]
    return sorted(
        [fact for fact in normalized if fact["predicate"]],
        key=lambda item: (item["predicate"], item["source"], item["timestamp"], item["ttl_seconds"]),
    )


def _fact_from_payload(payload: dict[str, Any]) -> engagement_state.GraphFact:
    return engagement_state.GraphFact(
        predicate=str(payload.get("predicate") or ""),
        source=str(payload.get("source") or "bloodhound:cypher"),
        timestamp=str(payload.get("timestamp") or _now()),
        ttl_seconds=int(payload.get("ttl_seconds") or DEFAULT_TTL_SECONDS),
    )


def _base_state() -> engagement_state.EngagementState:
    now = _now()
    return engagement_state.EngagementState(
        objective=LIVE_SURFACE.objective,
        footholds=[engagement_state.Foothold(
            callback_id="purpose-range-live-surface",
            agent="apollo",
            host=LIVE_SURFACE.foothold_host,
            forest=LIVE_SURFACE.target_domain,
            identity=LIVE_SURFACE.foothold_identity,
            integrity="medium",
            alive=True,
            source="gpo_dc_scope_live_surface",
            timestamp=now,
        )],
    )


def _state_from_observation(observation: dict[str, Any]) -> engagement_state.EngagementState:
    state = _base_state()
    return engagement_state.EngagementState(
        objective=state.objective,
        footholds=list(state.footholds),
        hops=[],
        graph_facts=[
            _fact_from_payload(fact)
            for fact in _normalize_graph_facts(list(observation.get("graph_facts") or []))
        ],
    )


def make_observation(
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


def new_evidence_manifest(observations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "gpo_dc_scope_live_surface_evidence",
        "generated_at": _now(),
        "spec": asdict(LIVE_SURFACE),
        "observations": list(observations or []),
    }


def _action_payload(action: Any, index: int | None = None) -> dict[str, Any]:
    payload = contract._action_payload(action, index)  # type: ignore[attr-defined]
    return payload


def _reconstruct_observation(observation: dict[str, Any]) -> dict[str, Any]:
    state = _state_from_observation(observation)
    prefix_state, shared_path, shared_reason = contract._replay_achieved(  # type: ignore[attr-defined]
        state,
        contract.GPO_DC_SCOPE_LATE_BLOCKER.shared_prefix,
    )
    blocked_action = contract._select_action(prefix_state, contract.BLOCKED_ACTION)  # type: ignore[attr-defined]
    if blocked_action is None:
        return {
            "label": observation.get("label"),
            "observation_hash": observation.get("observation_hash"),
            "observation_hash_valid": observation.get("observation_hash") == observation_hash(observation),
            "shared_path": shared_path,
            "shared_path_reason": shared_reason,
            "blocked_action": None,
            "post_blocker_frontier": [],
            "candidate_hash": "",
            "reachability": [],
            "modeled_transaction_costs": [],
            "best_indices": [],
            "checks": {
                "observation_hash_valid": observation.get("observation_hash") == observation_hash(observation),
                "collected_domain_present": LIVE_SURFACE.target_domain in set(observation.get("collected_domains") or []),
                "shared_prefix_reconstructs": False,
                "blocked_action_reconstructs": False,
                "post_blocker_frontier_matches_contract": False,
                "one_dc_scoped_and_one_non_dc_scoped_target": False,
                "modeled_costs_match_contract": False,
            },
            "passes_gate": False,
        }
    blocked_state, blocked_effect = contract._blocked_state(prefix_state, blocked_action)  # type: ignore[attr-defined]
    actions = list(capabilities.actions_from_state(blocked_state))
    frontier = [_action_payload(action, index) for index, action in enumerate(actions)]
    reachability = []
    for index, action in enumerate(actions):
        branch = contract._modeled_reachability(blocked_state, action)  # type: ignore[attr-defined]
        reachability.append({
            "index": index,
            "target": frontier[index]["target"],
            "reachable": branch["reachable"],
            "modeled_transactions": branch["modeled_transactions"],
            "path_names": [str(item.get("name") or "") for item in list(branch.get("path") or [])],
            "reason": branch["reason"],
        })
    reachable_costs = [
        int(item["modeled_transactions"])
        for item in reachability
        if item["reachable"] and item["modeled_transactions"] is not None
    ]
    best_cost = min(reachable_costs) if reachable_costs else None
    best_indices = [
        int(item["index"])
        for item in reachability
        if best_cost is not None and item["modeled_transactions"] == best_cost
    ]
    expected_contract = contract.validate_gpo_dc_scope_late_blocker_contract()
    expected_hash = str(expected_contract["decision_packet"]["candidate_hash"])
    frontier_targets = [str(item.get("target") or "") for item in frontier]
    dc_scoped = [
        item for item in frontier
        if any(str(fact).startswith("gpo-affects-dc:") for fact in list(item.get("source_facts") or []))
    ]
    non_dc_scoped = [
        item for item in frontier
        if not any(str(fact).startswith("gpo-affects-dc:") for fact in list(item.get("source_facts") or []))
    ]
    checks = {
        "observation_hash_valid": observation.get("observation_hash") == observation_hash(observation),
        "collected_domain_present": LIVE_SURFACE.target_domain in set(observation.get("collected_domains") or []),
        "shared_prefix_reconstructs": (
            [item["name"] for item in shared_path]
            == list(contract.GPO_DC_SCOPE_LATE_BLOCKER.shared_prefix)
        ),
        "blocked_action_reconstructs": (
            str(getattr(blocked_action, "name", "") or "") == contract.BLOCKED_ACTION
            and blocked_effect.startswith(contract.BLOCKED_EFFECT_PREFIX)
        ),
        "post_blocker_frontier_matches_contract": (
            frontier_targets == list(LIVE_SURFACE.expected_targets)
            and policy.candidate_hash(actions) == expected_hash
        ),
        "one_dc_scoped_and_one_non_dc_scoped_target": (
            len(dc_scoped) == 1
            and len(non_dc_scoped) == 1
            and dc_scoped[0]["target"] == LIVE_SURFACE.expected_targets[1]
        ),
        "modeled_costs_match_contract": (
            reachable_costs == list(expected_contract["modeled_transaction_costs"])
            and best_indices == list(expected_contract["best_indices"])
        ),
    }
    return {
        "label": observation.get("label"),
        "observation_hash": observation.get("observation_hash"),
        "observation_hash_valid": checks["observation_hash_valid"],
        "collected_domains": list(observation.get("collected_domains") or []),
        "graph_fact_count": len(list(observation.get("graph_facts") or [])),
        "shared_path": shared_path,
        "shared_path_reason": shared_reason,
        "blocked_action": _action_payload(blocked_action),
        "blocked_effect": blocked_effect,
        "post_blocker_frontier": frontier,
        "candidate_hash": policy.candidate_hash(actions),
        "expected_candidate_hash": expected_hash,
        "reachability": reachability,
        "modeled_transaction_costs": reachable_costs,
        "best_indices": best_indices,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def validate_live_surface_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    observations = list(evidence.get("observations") or [])
    reports = [_reconstruct_observation(observation) for observation in observations if isinstance(observation, dict)]
    candidate_hashes = [str(report.get("candidate_hash") or "") for report in reports if report.get("candidate_hash")]
    labels = [str(report.get("label") or "") for report in reports]
    metadata = evidence.get("spec") if isinstance(evidence.get("spec"), dict) else {}
    checks = {
        "schema_version_supported": evidence.get("schema_version") == SCHEMA_VERSION,
        "surface_metadata_matches_spec": (
            metadata.get("name") == LIVE_SURFACE.name
            and metadata.get("scenario") == LIVE_SURFACE.scenario
            and metadata.get("range_id") == LIVE_SURFACE.range_id
            and metadata.get("snapshot") == LIVE_SURFACE.snapshot
            and metadata.get("retained_callback_config") == LIVE_SURFACE.retained_callback_config
            and tuple(metadata.get("reset_path") or ()) == LIVE_SURFACE.reset_path
        ),
        "two_clean_reset_observations_present": len(reports) >= 2 and len(set(labels)) == len(labels),
        "all_observations_reconstruct_contract": bool(reports) and all(report["passes_gate"] for report in reports),
        "repeated_frontier_hash_is_stable": len(candidate_hashes) >= 2 and len(set(candidate_hashes)) == 1,
    }
    return {
        "kind": "gpo_dc_scope_live_surface_validation",
        "generated_at": _now(),
        "spec": asdict(LIVE_SURFACE),
        "observations": reports,
        "candidate_hashes": candidate_hashes,
        "checks": checks,
        "passes_gate": all(checks.values()),
        "authorization": {
            "live_canary_authorized": all(checks.values()),
            "reason": (
                "The live range reproduces the authorized two-GPO late-blocker frontier across repeated clean resets."
                if all(checks.values())
                else "The live surface is not yet stable enough to release a canary."
            ),
        },
    }


async def _capture_live_observation_async(label: str, registry: dict[str, Any]) -> dict[str, Any]:
    cypher_tool = registry.get("cypher_query")
    if cypher_tool is None:
        raise GpoDcScopeLiveSurfaceError("BloodHound cypher_query tool is unavailable")
    domain_tool = registry.get("domain_info")
    now = _now()
    manager = frontier_census._SingleToolMCPManager(cypher_tool)  # type: ignore[attr-defined]
    refresh = frontier_census._live_refresh_fn(manager, now, DEFAULT_TTL_SECONDS)  # type: ignore[attr-defined]
    state = await refresh(_base_state())
    collected_domains = await frontier_census._live_collected_domains(domain_tool)  # type: ignore[attr-defined]
    return make_observation(
        label=label,
        graph_facts=list(getattr(state, "graph_facts", []) or []),
        collected_domains=list(collected_domains or []),
        captured_at=now,
    )


def capture_live_observation(label: str) -> dict[str, Any]:
    _, registry = bare_bloodhound.load_bloodhound_mcp_tools()
    return asyncio.run(_capture_live_observation_async(label, registry))


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GpoDcScopeLiveSurfaceError(f"evidence file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GpoDcScopeLiveSurfaceError(f"evidence file is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise GpoDcScopeLiveSurfaceError("evidence payload must be a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def add_cli(subparsers: Any) -> None:
    capture = subparsers.add_parser(
        "gpo-dc-scope-live-surface-capture",
        help="capture one read-only BloodHound observation for the GPO DC-scope live surface",
    )
    capture.add_argument("--label", required=True, help="stable observation label such as clean-reset-1")
    capture.add_argument("--evidence", required=True, help="JSON evidence file to create or append")
    capture.add_argument("--replace", action="store_true", help="replace any existing evidence file before appending")
    capture.set_defaults(func=_cmd_gpo_dc_scope_live_surface_capture)

    validate = subparsers.add_parser(
        "gpo-dc-scope-live-surface-validate",
        help="validate repeated live graph observations against the authorized GPO DC-scope contract",
    )
    validate.add_argument("--evidence", required=True, help="JSON evidence file containing repeated observations")
    validate.add_argument("--output", default=None, help="optional JSON report path")
    validate.set_defaults(func=_cmd_gpo_dc_scope_live_surface_validate)


def _cmd_gpo_dc_scope_live_surface_capture(args: Any) -> int:
    path = Path(args.evidence)
    try:
        observation = capture_live_observation(args.label)
        if args.replace or not path.exists():
            evidence = new_evidence_manifest()
        else:
            evidence = _load_json(path)
        observations = list(evidence.get("observations") or [])
        observations.append(observation)
        evidence["observations"] = observations
        evidence["generated_at"] = _now()
        _write_json(path, evidence)
    except GpoDcScopeLiveSurfaceError as exc:
        print(f"gpo-dc-scope-live-surface-capture: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(observation, indent=2, sort_keys=True, ensure_ascii=True))
    print(f"\nCAPTURED: {observation['label']}  (observation_hash={observation['observation_hash']})", flush=True)
    return 0


def _cmd_gpo_dc_scope_live_surface_validate(args: Any) -> int:
    try:
        evidence = _load_json(Path(args.evidence))
        report = validate_live_surface_evidence(evidence)
    except GpoDcScopeLiveSurfaceError as exc:
        print(f"gpo-dc-scope-live-surface-validate: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        _write_json(Path(args.output), report)
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(live_canary_authorized={report['authorization']['live_canary_authorized']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
