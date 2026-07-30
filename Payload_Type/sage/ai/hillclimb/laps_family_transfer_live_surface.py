"""Repeated live-frontier preflight for the sealed Phase 6 LAPS holdout.

This module is eval-only. It consumes read-only BloodHound observations after a
clean reset and one collection, reconstructs the current generic capability
frontier, and fails closed unless the same exact two cross-domain LAPS reads are
present on repeated observations.
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

try:
    from . import bare_bloodhound
    from . import frontier_census
    from . import laps_family_transfer_holdout as contract
    from ..langgraph import capabilities, engagement_state, policy
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import bare_bloodhound  # type: ignore
    import frontier_census  # type: ignore
    import laps_family_transfer_holdout as contract  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore
    import policy  # type: ignore


SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 600
SURFACE_NAME = contract.HOLDOUT_NAME
SURFACE_SCENARIO = f"laps-family-transfer-{contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants[0].name}"
SURFACE_RANGE_ID = contract.LAPS_FAMILY_TRANSFER_HOLDOUT.range_id
SURFACE_SNAPSHOT = contract.LAPS_FAMILY_TRANSFER_HOLDOUT.live_reset_snapshot
SURFACE_RETAINED_CALLBACK_CONFIG = contract.LAPS_FAMILY_TRANSFER_HOLDOUT.retained_callback_config
SURFACE_RESET_PATH = (
    "rollback the Ludus range to the declared snapshot",
    "power on all range VMs and synchronize clocks",
    "reset Mythic, restart Sage, and wipe BloodHound",
    f"import the retained {contract.FOOTHOLD_HOST} Apollo callback config and launch the staged callback",
    "run one controller cycle with SAGE_CONTROLLER_MAX_CYCLES=1 to collect and ingest the graph only",
)


class LapsFamilyTransferLiveSurfaceError(ValueError):
    """Raised when Phase 6 live-surface evidence is missing or malformed."""


@dataclass(frozen=True)
class LapsFamilyTransferLiveSurfaceSpec:
    name: str
    scenario: str
    range_id: str
    snapshot: str
    retained_callback_config: str
    objective: str
    foothold_host: str
    foothold_identity: str
    required_domains: tuple[str, ...]
    expected_targets: tuple[str, ...]
    reset_path: tuple[str, ...]


LIVE_SURFACE = LapsFamilyTransferLiveSurfaceSpec(
    name=SURFACE_NAME,
    scenario=SURFACE_SCENARIO,
    range_id=SURFACE_RANGE_ID,
    snapshot=SURFACE_SNAPSHOT,
    retained_callback_config=SURFACE_RETAINED_CALLBACK_CONFIG,
    objective=contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants[0].objective,
    foothold_host=contract.LAPS_FAMILY_TRANSFER_HOLDOUT.foothold_host,
    foothold_identity=contract.LAPS_FAMILY_TRANSFER_HOLDOUT.foothold_identity,
    required_domains=(
        contract.ROOT_DOMAIN,
        *contract.LAPS_FAMILY_TRANSFER_HOLDOUT.child_domains,
    ),
    expected_targets=(
        *tuple(
            (
                f"account=user1;account_domain={contract.ROOT_DOMAIN};"
                f"target={host.casefold()};target_domain={domain};callback={contract.CANONICAL_CALLBACK_ID}"
            )
            for host, domain in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.targets
        ),
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
    return {key: value for key, value in observation.items() if key != "observation_hash"}


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
    return sorted(
        [payload for payload in (_fact_payload(fact) for fact in list(facts or [])) if payload["predicate"]],
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
            callback_id=contract.CANONICAL_CALLBACK_ID,
            agent="apollo",
            host=LIVE_SURFACE.foothold_host,
            forest=contract.ROOT_DOMAIN,
            identity=LIVE_SURFACE.foothold_identity,
            integrity="medium",
            alive=True,
            source="laps_family_transfer_live_surface",
            timestamp=now,
        )],
    )


def _state_from_observation(observation: dict[str, Any]) -> engagement_state.EngagementState:
    state = _base_state()
    return engagement_state.EngagementState(
        objective=state.objective,
        footholds=list(state.footholds),
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
        "kind": "laps_family_transfer_live_surface_evidence",
        "generated_at": _now(),
        "spec": asdict(LIVE_SURFACE),
        "sealed_manifest_hash": contract.sealed_manifest()["manifest_hash"],
        "topology_hash": contract.topology_hash(),
        "observations": list(observations or []),
    }


def _action_payload(action: Any, index: int | None = None) -> dict[str, Any]:
    return contract._action_payload(action, index)  # type: ignore[attr-defined]


def _reconstruct_observation(observation: dict[str, Any]) -> dict[str, Any]:
    state = _state_from_observation(observation)
    actions = list(capabilities.actions_from_state(state))
    frontier = [_action_payload(action, index) for index, action in enumerate(actions)]
    targets = [str(item.get("target") or "") for item in frontier]
    families = sorted({str(item.get("family") or "") for item in frontier})
    checks = {
        "observation_hash_valid": observation.get("observation_hash") == observation_hash(observation),
        "required_domains_collected": set(LIVE_SURFACE.required_domains) <= set(observation.get("collected_domains") or []),
        "exact_two_laps_candidates": (
            len(frontier) == 2
            and targets == list(LIVE_SURFACE.expected_targets)
            and all(item["name"] == "read-managed-local-admin-secret" for item in frontier)
        ),
        "managed_local_admin_is_only_initial_family": families == ["managed-local-admin"],
    }
    return {
        "label": observation.get("label"),
        "observation_hash": observation.get("observation_hash"),
        "collected_domains": list(observation.get("collected_domains") or []),
        "graph_fact_count": len(list(observation.get("graph_facts") or [])),
        "frontier": frontier,
        "candidate_set_hash": contract.canonical_candidate_set_hash(actions),
        "ordered_frontier_hash": contract.canonical_ordered_frontier_hash(actions),
        "families": families,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def validate_live_surface_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    observations = list(evidence.get("observations") or [])
    reports = [_reconstruct_observation(observation) for observation in observations if isinstance(observation, dict)]
    candidate_set_hashes = [str(report.get("candidate_set_hash") or "") for report in reports if report.get("candidate_set_hash")]
    ordered_frontier_hashes = [str(report.get("ordered_frontier_hash") or "") for report in reports if report.get("ordered_frontier_hash")]
    labels = [str(report.get("label") or "") for report in reports]
    metadata = evidence.get("spec") if isinstance(evidence.get("spec"), dict) else {}
    checks = {
        "schema_version_supported": evidence.get("schema_version") == SCHEMA_VERSION,
        "sealed_manifest_hash_matches": evidence.get("sealed_manifest_hash") == contract.sealed_manifest()["manifest_hash"],
        "topology_hash_matches": evidence.get("topology_hash") == contract.topology_hash(),
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
        "repeated_candidate_set_hash_is_stable": len(candidate_set_hashes) >= 2 and len(set(candidate_set_hashes)) == 1,
        "repeated_ordered_frontier_hash_is_stable": len(ordered_frontier_hashes) >= 2 and len(set(ordered_frontier_hashes)) == 1,
    }
    passes_gate = all(checks.values())
    return {
        "kind": "laps_family_transfer_live_surface_validation",
        "generated_at": _now(),
        "spec": asdict(LIVE_SURFACE),
        "sealed_manifest_hash": contract.sealed_manifest()["manifest_hash"],
        "topology_hash": contract.topology_hash(),
        "observations": reports,
        "candidate_set_hashes": candidate_set_hashes,
        "ordered_frontier_hashes": ordered_frontier_hashes,
        "checks": checks,
        "passes_gate": passes_gate,
        "authorization": {
            "mechanics_canaries_authorized": passes_gate,
            "reason": (
                "Repeated clean resets reproduce the sealed two-target LAPS frontier."
                if passes_gate
                else "The live LAPS frontier is not stable enough to release mechanics canaries."
            ),
        },
    }


async def _capture_live_observation_async(label: str, registry: dict[str, Any]) -> dict[str, Any]:
    cypher_tool = registry.get("cypher_query")
    if cypher_tool is None:
        raise LapsFamilyTransferLiveSurfaceError("BloodHound cypher_query tool is unavailable")
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
        raise LapsFamilyTransferLiveSurfaceError(f"evidence file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LapsFamilyTransferLiveSurfaceError(f"evidence file is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise LapsFamilyTransferLiveSurfaceError("evidence payload must be a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def add_cli(subparsers: Any) -> None:
    capture = subparsers.add_parser(
        "laps-family-transfer-live-surface-capture",
        help="capture one read-only BloodHound observation for the Phase 6 LAPS holdout",
    )
    capture.add_argument("--label", required=True, help="stable observation label such as clean-reset-1")
    capture.add_argument("--evidence", required=True, help="JSON evidence file to create or append")
    capture.add_argument("--replace", action="store_true", help="replace any existing evidence file before appending")
    capture.set_defaults(func=_cmd_laps_family_transfer_live_surface_capture)

    validate = subparsers.add_parser(
        "laps-family-transfer-live-surface-validate",
        help="validate repeated live graph observations against the sealed Phase 6 LAPS contract",
    )
    validate.add_argument("--evidence", required=True, help="JSON evidence file containing repeated observations")
    validate.add_argument("--output", default=None, help="optional JSON report path")
    validate.set_defaults(func=_cmd_laps_family_transfer_live_surface_validate)


def _cmd_laps_family_transfer_live_surface_capture(args: Any) -> int:
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
    except LapsFamilyTransferLiveSurfaceError as exc:
        print(f"laps-family-transfer-live-surface-capture: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(observation, indent=2, sort_keys=True, ensure_ascii=True))
    print(f"\nCAPTURED: {observation['label']}  (observation_hash={observation['observation_hash']})", flush=True)
    return 0


def _cmd_laps_family_transfer_live_surface_validate(args: Any) -> int:
    try:
        evidence = _load_json(Path(args.evidence))
        report = validate_live_surface_evidence(evidence)
    except LapsFamilyTransferLiveSurfaceError as exc:
        print(f"laps-family-transfer-live-surface-validate: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        _write_json(Path(args.output), report)
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(mechanics_canaries_authorized={report['authorization']['mechanics_canaries_authorized']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
