"""Offline contract for same-domain GPO DC-scope late-blocker recovery.

This module is eval-only. It extends the existing purpose-range replanning
substrate with a second controlled GPO, replays the current LAPS/ADCS lane
through verified CA export, records one terminal certificate-auth blocker, and
proves that the resulting full recovery frontier is a two-target GPO choice.

The contract does not add runtime target-value logic. It reuses the current
generic capability frontier, operational-cost metadata, modeled reachability,
and offline selector probes to show whether the existing state model already
contains a real target-choice signal.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

try:  # package import
    from . import frontier_census
    from . import policy_replay_selector_experiment as selector_experiment
    from . import purpose_range
    from . import replanning_benchmark
    from ..langgraph import capabilities, engagement_state, policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import frontier_census  # type: ignore
    import policy_replay_selector_experiment as selector_experiment  # type: ignore
    import purpose_range  # type: ignore
    import replanning_benchmark  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore
    import policy  # type: ignore


TARGET_CAPABILITY = "gpo-controlled-system-exec"
BLOCKED_ACTION = "adcs-certificate-auth"
BLOCKED_EFFECT_PREFIX = "certificate-auth:"
SHARED_PREFIX = (*replanning_benchmark.SHARED_PREFIX, "adcs-ca-private-key-export")
DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_NODES = 80


class GpoDcScopeLateBlockerContractError(ValueError):
    """Raised when the declared GPO late-blocker contract cannot be constructed."""


@dataclass(frozen=True)
class GpoDcScopeLateBlockerSpec:
    name: str
    scenario: str
    objective: str
    foothold_host: str
    foothold_identity: str
    target_domain: str
    shared_prefix: tuple[str, ...]
    blocked_action: str
    blocker_reason: str
    blocker_failure_class: str
    blocker_probe: tuple[tuple[str, Any], ...]
    non_dc_gpo: str
    dc_scoped_gpo: str
    dc_host: str
    proof_path: str


GPO_DC_SCOPE_LATE_BLOCKER = GpoDcScopeLateBlockerSpec(
    name="same-domain-gpo-dc-scope-late-blocker",
    scenario="purpose-range-gpo-dc-scope-late-blocker",
    objective=purpose_range.PURPOSE_RANGE.objective,
    foothold_host=purpose_range.PURPOSE_RANGE.foothold_host,
    foothold_identity=purpose_range.PURPOSE_RANGE.foothold_identity,
    target_domain=purpose_range.PURPOSE_RANGE.domain,
    shared_prefix=SHARED_PREFIX,
    blocked_action=BLOCKED_ACTION,
    blocker_reason="certificate authentication failed after verified CA export on ca01",
    blocker_failure_class="terminal",
    blocker_probe=(
        ("pkinit_failed", True),
        ("target_host", "ca01"),
        ("target_domain", "range.local"),
        ("account", "administrator"),
        ("callback_id", "purpose-range-1"),
    ),
    non_dc_gpo="srv02-policy",
    dc_scoped_gpo="tier0-policy",
    dc_host="dc01",
    proof_path="current generic GPO scope facts plus modeled objective reachability",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fact(predicate: str) -> engagement_state.GraphFact:
    return engagement_state.GraphFact(
        predicate=predicate,
        source="gpo_dc_scope_late_blocker_contract",
        timestamp=_now(),
        ttl_seconds=3600,
    )


def synthetic_collected_state() -> engagement_state.EngagementState:
    """Return the purpose-range state extended with one DC-scoped controlled GPO."""
    base = purpose_range.synthetic_collected_state()
    gpo = GPO_DC_SCOPE_LATE_BLOCKER.dc_scoped_gpo
    domain = GPO_DC_SCOPE_LATE_BLOCKER.target_domain
    dc_host = GPO_DC_SCOPE_LATE_BLOCKER.dc_host
    return engagement_state.EngagementState(
        objective=base.objective,
        footholds=list(base.footholds),
        hops=list(base.hops),
        graph_facts=[
            *list(base.graph_facts),
            _fact(f"generic-write:gpo:{gpo}"),
            _fact(f"gpo-domain:{gpo}:{domain}"),
            _fact(f"gpo-guid:{gpo}:22222222-2222-2222-2222-222222222222"),
            _fact(f"gpo-affects-dc:{gpo}:{dc_host}:{domain}"),
        ],
        probed_effect_prefixes=set(getattr(base, "probed_effect_prefixes", set()) or set()),
    )


def _action_payload(action: Any, index: int | None = None) -> dict[str, Any]:
    payload = {
        "name": str(getattr(action, "name", "") or ""),
        "target": str(getattr(action, "target", "") or ""),
        "family": policy.capability_family(getattr(action, "name", "")),
        "preconditions": list(getattr(action, "preconditions", None) or []),
        "effects": list(getattr(action, "effects", None) or []),
        "source_facts": list(getattr(action, "source_facts", None) or []),
        "operational_cost": capabilities.operational_cost_for_action(action),
        "reason": str(getattr(action, "reason", "") or ""),
    }
    if index is not None:
        payload["index"] = index
    return payload


def _select_action(state: engagement_state.EngagementState, expected_name: str) -> Any | None:
    return next(
        (
            action
            for action in capabilities.actions_from_state(state)
            if str(getattr(action, "name", "") or "") == expected_name
        ),
        None,
    )


def _replay_achieved(
    state: engagement_state.EngagementState,
    expected_names: tuple[str, ...],
) -> tuple[engagement_state.EngagementState, list[dict[str, Any]], str]:
    current = state
    path: list[dict[str, Any]] = []
    for expected_name in expected_names:
        action = _select_action(current, expected_name)
        if action is None:
            return current, path, f"expected capability {expected_name!r} was not admissible"
        path.append(_action_payload(action))
        current = frontier_census._apply_modeled_action(current, action, _now())  # type: ignore[attr-defined]
    return current, path, "declared capability effects replayed"


def _blocked_effect(action: Any) -> str:
    effects = [str(effect) for effect in list(getattr(action, "effects", None) or []) if str(effect)]
    matches = [effect for effect in effects if effect.startswith(BLOCKED_EFFECT_PREFIX)]
    if len(matches) != 1:
        raise GpoDcScopeLateBlockerContractError(
            f"{BLOCKED_ACTION}: expected exactly one {BLOCKED_EFFECT_PREFIX!r} effect, got {effects!r}"
        )
    return matches[0]


def _blocked_state(
    state: engagement_state.EngagementState,
    action: Any,
) -> tuple[engagement_state.EngagementState, str]:
    effect = _blocked_effect(action)
    evidence = {
        "source": "gpo_dc_scope_late_blocker_contract",
        "verify_reason": GPO_DC_SCOPE_LATE_BLOCKER.blocker_reason,
        "terminal_failure": True,
        "failure_class": GPO_DC_SCOPE_LATE_BLOCKER.blocker_failure_class,
        **dict(GPO_DC_SCOPE_LATE_BLOCKER.blocker_probe),
    }
    return (
        engagement_state.record_effect_result(
            state,
            f"gpo-dc-scope-late-blocker:{getattr(action, 'name', 'action')}",
            str(getattr(action, "target", "") or ""),
            effect,
            "blocked",
            evidence,
            _now(),
            preconditions=list(getattr(action, "preconditions", None) or []),
            satisfied_effects=[effect],
        ),
        effect,
    )


def _decision_packet(
    state: engagement_state.EngagementState,
    actions: list[Any],
) -> dict[str, Any]:
    return policy._decision_packet(  # type: ignore[attr-defined]
        objective=state.objective,
        state=state,
        candidates=actions,
        history=[],
        budgets={},
        selection_contract=selector_experiment.SUPPORTED_SELECTION_CONTRACT,
    )


def _modeled_reachability(
    state: engagement_state.EngagementState,
    action: Any,
) -> dict[str, Any]:
    return asyncio.run(
        frontier_census.modeled_branch_reachability(
            state,
            action,
            max_depth=DEFAULT_MAX_DEPTH,
            max_nodes=DEFAULT_MAX_NODES,
        )
    )


def _selector_scores(
    packet: dict[str, Any],
    candidates: list[dict[str, Any]],
    best_indices: list[int],
) -> list[dict[str, Any]]:
    scores: list[dict[str, Any]] = []
    for selector_name, selector_fn in selector_experiment.SELECTORS.items():
        index, ranking = selector_fn(packet)
        selected = candidates[index] if 0 <= index < len(candidates) else {}
        scores.append({
            "selector": selector_name,
            "selected_index": index,
            "selected_target": selected.get("target"),
            "selected_is_best": index in best_indices,
            "ranking": ranking,
        })
    return scores


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _has_dc_scope(candidate: dict[str, Any]) -> bool:
    return any(str(fact).startswith("gpo-affects-dc:") for fact in candidate.get("source_facts", []))


def validate_gpo_dc_scope_late_blocker_contract() -> dict[str, Any]:
    """Return the offline acceptance report for the dedicated GPO late-blocker contract."""
    state = synthetic_collected_state()
    prefix_state, shared_path, shared_reason = _replay_achieved(state, GPO_DC_SCOPE_LATE_BLOCKER.shared_prefix)
    blocked_action = _select_action(prefix_state, GPO_DC_SCOPE_LATE_BLOCKER.blocked_action)
    if blocked_action is None:
        raise GpoDcScopeLateBlockerContractError(
            f"expected blocked capability {GPO_DC_SCOPE_LATE_BLOCKER.blocked_action!r} was not admissible"
        )
    blocker_probe = dict(GPO_DC_SCOPE_LATE_BLOCKER.blocker_probe)
    blocker_verification = capabilities.verify_capability(GPO_DC_SCOPE_LATE_BLOCKER.blocked_action, blocker_probe)
    blocked_state, blocked_effect = _blocked_state(prefix_state, blocked_action)
    post_blocker_actions = list(capabilities.actions_from_state(blocked_state))
    post_blocker_frontier = [
        _action_payload(action, index)
        for index, action in enumerate(post_blocker_actions)
    ]
    packet = _decision_packet(blocked_state, post_blocker_actions)
    reachability: list[dict[str, Any]] = []
    for index, action in enumerate(post_blocker_actions):
        branch = _modeled_reachability(blocked_state, action)
        reachability.append({
            "index": index,
            "target": post_blocker_frontier[index]["target"],
            "reachable": branch["reachable"],
            "modeled_transactions": branch["modeled_transactions"],
            "path": branch["path"],
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
    selector_scores = _selector_scores(packet, post_blocker_frontier, best_indices)
    cost_profiles = {
        _canonical_json(candidate["operational_cost"])
        for candidate in post_blocker_frontier
    }
    best_candidates = [
        post_blocker_frontier[index]
        for index in best_indices
        if 0 <= index < len(post_blocker_frontier)
    ]
    blocked_hops = [
        hop
        for hop in blocked_state.hops
        if getattr(hop, "status", "") == "blocked"
        and getattr(hop, "technique", "") == f"gpo-dc-scope-late-blocker:{GPO_DC_SCOPE_LATE_BLOCKER.blocked_action}"
    ]
    catalog_names = {item["name"] for item in capabilities.capability_catalog()}
    declared_actions = {
        *GPO_DC_SCOPE_LATE_BLOCKER.shared_prefix,
        GPO_DC_SCOPE_LATE_BLOCKER.blocked_action,
        TARGET_CAPABILITY,
    }
    checks = {
        "shared_prefix_extends_existing_replanning_lane": (
            tuple(GPO_DC_SCOPE_LATE_BLOCKER.shared_prefix[:-1]) == tuple(replanning_benchmark.SHARED_PREFIX)
            and [item["name"] for item in shared_path] == list(GPO_DC_SCOPE_LATE_BLOCKER.shared_prefix)
        ),
        "blocker_is_verifier_backed_and_terminal": (
            blocker_verification.verdict == "blocked"
            and len(blocked_hops) == 1
            and blocked_hops[0].effect == blocked_effect
            and blocked_hops[0].satisfied_effects == [blocked_effect]
            and blocked_hops[0].evidence.get("terminal_failure") is True
        ),
        "post_blocker_frontier_is_exact_two_gpo_targets": (
            len(post_blocker_frontier) == 2
            and {candidate["name"] for candidate in post_blocker_frontier} == {TARGET_CAPABILITY}
            and len({candidate["target"] for candidate in post_blocker_frontier}) == 2
        ),
        "equal_visible_operational_cost": len(cost_profiles) == 1,
        "all_targets_reach_objective": bool(reachability) and all(item["reachable"] for item in reachability),
        "asymmetric_modeled_downstream_cost": len(set(reachable_costs)) > 1,
        "unique_best_target": len(best_indices) == 1,
        "unique_best_target_is_dc_scoped": len(best_candidates) == 1 and _has_dc_scope(best_candidates[0]),
        "current_selectors_choose_worse_target": bool(selector_scores) and all(
            score["selected_is_best"] is False
            for score in selector_scores
        ),
        "current_capability_only": declared_actions <= catalog_names,
    }
    return {
        "kind": "gpo_dc_scope_late_blocker_contract_validation",
        "generated_at": _now(),
        "evidence_scope": (
            "offline purpose-range state extension, current capability frontier, current operational-cost "
            "metadata, current modeled reachability, and current offline selector probes only"
        ),
        "spec": asdict(GPO_DC_SCOPE_LATE_BLOCKER),
        "shared_path": shared_path,
        "shared_path_reason": shared_reason,
        "blocker": {
            "action": _action_payload(blocked_action),
            "blocked_effect": blocked_effect,
            "reason": GPO_DC_SCOPE_LATE_BLOCKER.blocker_reason,
            "failure_class": GPO_DC_SCOPE_LATE_BLOCKER.blocker_failure_class,
            "probe": blocker_probe,
            "verification": {
                "verdict": blocker_verification.verdict,
                "reason": blocker_verification.reason,
            },
        },
        "post_blocker_frontier": post_blocker_frontier,
        "decision_packet": packet,
        "reachability": reachability,
        "modeled_transaction_costs": reachable_costs,
        "best_modeled_transaction_cost": best_cost,
        "best_indices": best_indices,
        "selector_scores": selector_scores,
        "authorization": {
            "live_benchmark_authorized": False,
            "reason": (
                "This contract proves the offline target-choice shape only. A separate offline authorization "
                "audit still needs to confirm the live surface is proofable before any canary."
            ),
            "next_requirement": "Run the dedicated offline authorization audit from roadmap Step 2.",
        },
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "gpo-dc-scope-late-blocker-contract-validate",
        help="validate the dedicated same-domain GPO DC-scope late-blocker contract",
    )
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.set_defaults(func=_cmd_gpo_dc_scope_late_blocker_contract_validate)


def _cmd_gpo_dc_scope_late_blocker_contract_validate(args: Any) -> int:
    try:
        report = validate_gpo_dc_scope_late_blocker_contract()
    except GpoDcScopeLateBlockerContractError as exc:
        print(f"gpo-dc-scope-late-blocker-contract-validate: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(live_benchmark_authorized={report['authorization']['live_benchmark_authorized']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
