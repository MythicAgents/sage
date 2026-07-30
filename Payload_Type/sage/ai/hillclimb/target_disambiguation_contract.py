"""Offline audit for target-disambiguated benchmark contracts.

The next live benchmark is supposed to test target choice, not just capability
family choice or frontier ordering. This module checks whether the currently
proposed ADCS late-blocker surface can express that distinction under the
existing capability model, then compares it with a control shape that does have
same-capability, equal-visible-cost candidates with different modeled downstream
cost.

This is eval-only. It does not mutate runtime policy, capability generation, or
live orchestration.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

try:  # package import
    from . import frontier_census
    from . import policy_replay_selector_experiment as selector_experiment
    from ..langgraph import capabilities, engagement_state, policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import frontier_census  # type: ignore
    import policy_replay_selector_experiment as selector_experiment  # type: ignore
    import capabilities  # type: ignore
    import engagement_state  # type: ignore
    import policy  # type: ignore


ADCS_PROPOSAL_ID = "adcs-enrollment-target-disambiguation"
CONTROL_ID = "multi-domain-replication-target-control"
ADCS_CAPABILITY = "adcs-esc-certificate-enroll"
CONTROL_CAPABILITY = "dcsync-krbtgt"
ADCS_DOMAIN = "lab.local"
ADCS_CA_HOST = "ca01"
CONTROL_OBJECTIVE_DOMAIN = "target.local"
CONTROL_AUXILIARY_DOMAIN = "decoy.local"
DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_NODES = 80


class TargetDisambiguationContractError(ValueError):
    """Raised when the audit cannot construct or score a declared surface."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fact(predicate: str) -> engagement_state.GraphFact:
    return engagement_state.GraphFact(
        predicate=predicate,
        source="target_disambiguation_contract",
        timestamp=_now(),
        ttl_seconds=3600,
    )


def _foothold(domain: str, *, callback_id: str = "1") -> engagement_state.Foothold:
    return engagement_state.Foothold(
        callback_id=callback_id,
        agent="apollo",
        host="ws01",
        forest=domain,
        identity=f"{domain.split('.', 1)[0].upper()}\\user1",
        integrity="medium",
        alive=True,
        source="target_disambiguation_contract",
        timestamp=_now(),
    )


def _blocked_ca_export_hop() -> engagement_state.Hop:
    effect = f"adcs-ca-private-key:{ADCS_CA_HOST}@{ADCS_DOMAIN}"
    return engagement_state.Hop(
        id="target-disambiguation-blocked-ca-export",
        technique="capability:adcs-ca-private-key-export",
        target=f"target={ADCS_CA_HOST};target_domain={ADCS_DOMAIN};callback=1",
        effect=effect,
        status="blocked",
        evidence={
            "source": "target_disambiguation_contract",
            "verify_reason": "key not exportable",
            "key_not_exportable": True,
            "target_host": ADCS_CA_HOST,
            "target_domain": ADCS_DOMAIN,
            "terminal_failure": True,
        },
        preconditions=[],
        satisfied_effects=[],
        source="target_disambiguation_contract",
        timestamp=_now(),
    )


def _adcs_proposal_state() -> engagement_state.EngagementState:
    return engagement_state.EngagementState(
        objective=f"From the current foothold, achieve administrative control of {ADCS_DOMAIN}.",
        footholds=[_foothold(ADCS_DOMAIN)],
        hops=[_blocked_ca_export_hop()],
        graph_facts=[
            _fact(f"certificate-auth-target:administrator@{ADCS_DOMAIN}"),
            _fact(f"certificate-auth-target:helpdesk@{ADCS_DOMAIN}"),
        ],
    )


def _control_state() -> engagement_state.EngagementState:
    return engagement_state.EngagementState(
        objective=(
            "From the current foothold, achieve administrative control of "
            f"{CONTROL_OBJECTIVE_DOMAIN}."
        ),
        footholds=[_foothold(CONTROL_OBJECTIVE_DOMAIN)],
        graph_facts=[
            _fact(f"ds-replication-rights:{CONTROL_AUXILIARY_DOMAIN}"),
            _fact(f"ds-replication-rights:{CONTROL_OBJECTIVE_DOMAIN}"),
        ],
    )


def _action_payload(action: Any, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "name": str(getattr(action, "name", "") or ""),
        "target": str(getattr(action, "target", "") or ""),
        "family": policy.capability_family(getattr(action, "name", "")),
        "preconditions": list(getattr(action, "preconditions", None) or []),
        "effects": list(getattr(action, "effects", None) or []),
        "operational_cost": capabilities.operational_cost_for_action(action),
        "reason": str(getattr(action, "reason", "") or ""),
    }


def _same_capability_actions(
    state: engagement_state.EngagementState,
    capability_name: str,
) -> list[Any]:
    return [
        action
        for action in capabilities.actions_from_state(state)
        if str(getattr(action, "name", "") or "") == capability_name
    ]


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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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


def _surface_report(
    *,
    surface_id: str,
    purpose: str,
    state: engagement_state.EngagementState,
    capability_name: str,
    expected_target_count: int,
) -> dict[str, Any]:
    actions = _same_capability_actions(state, capability_name)
    if not actions:
        raise TargetDisambiguationContractError(
            f"{surface_id}: no {capability_name!r} actions were generated from the declared state"
        )
    candidates = [_action_payload(action, index) for index, action in enumerate(actions)]
    packet = _decision_packet(state, actions)
    reachability: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        branch = _modeled_reachability(state, action)
        reachability.append({
            "index": index,
            "target": candidates[index]["target"],
            "reachable": branch["reachable"],
            "modeled_transactions": branch["modeled_transactions"],
            "path": branch["path"],
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
    cost_profiles = {
        _canonical_json(candidate["operational_cost"])
        for candidate in candidates
    }
    target_names = {candidate["target"] for candidate in candidates}
    capability_names = {candidate["name"] for candidate in candidates}
    checks = {
        "same_capability_multi_target_frontier": (
            len(candidates) == expected_target_count
            and len(capability_names) == 1
            and capability_name in capability_names
            and len(target_names) == len(candidates)
        ),
        "equal_visible_cost": len(cost_profiles) == 1,
        "all_targets_reach_objective": bool(reachability) and all(item["reachable"] for item in reachability),
        "asymmetric_downstream_value": len(set(reachable_costs)) > 1,
        "unique_best_target": len(best_indices) == 1,
    }
    return {
        "id": surface_id,
        "purpose": purpose,
        "capability": capability_name,
        "objective": state.objective,
        "decision_packet": packet,
        "candidates": candidates,
        "reachability": reachability,
        "modeled_transaction_costs": reachable_costs,
        "best_modeled_transaction_cost": best_cost,
        "best_indices": best_indices,
        "selector_scores": _selector_scores(packet, candidates, best_indices),
        "checks": checks,
    }


def validate_target_disambiguation_contract() -> dict[str, Any]:
    """Return an offline report for the proposed and control target-choice surfaces."""

    adcs = _surface_report(
        surface_id=ADCS_PROPOSAL_ID,
        purpose=(
            "Audit the proposed late CA-export fallback surface with multiple "
            "same-capability enrollment targets."
        ),
        state=_adcs_proposal_state(),
        capability_name=ADCS_CAPABILITY,
        expected_target_count=2,
    )
    control = _surface_report(
        surface_id=CONTROL_ID,
        purpose=(
            "Control shape proving the audit can detect equal-visible-cost "
            "same-capability targets with unequal downstream cost."
        ),
        state=_control_state(),
        capability_name=CONTROL_CAPABILITY,
        expected_target_count=2,
    )
    control_selector_failures = [
        item["selector"]
        for item in control["selector_scores"]
        if item["selected_is_best"] is not True
    ]
    live_benchmark_authorized = bool(
        adcs["checks"]["same_capability_multi_target_frontier"]
        and adcs["checks"]["equal_visible_cost"]
        and adcs["checks"]["all_targets_reach_objective"]
        and adcs["checks"]["asymmetric_downstream_value"]
        and adcs["checks"]["unique_best_target"]
    )
    authorization_reason = (
        "The ADCS enrollment targets are same-capability and equal-visible-cost, and they now "
        "have a unique modeled best target."
        if live_benchmark_authorized
        else (
            "The current ADCS enrollment targets are same-capability and equal-visible-cost, "
            "but they collapse to the same modeled two-transaction objective path. A live "
            "matrix here would measure target serialization, not downstream decision quality."
        )
    )
    next_requirement = (
        "Review whether the modeled asymmetry is grounded in live-proofable facts before authorizing live spend."
        if live_benchmark_authorized
        else (
            "Use a surface with same-capability equal-visible-cost targets that have distinct "
            "modeled objective reachability or transaction cost before authorizing live spend."
        )
    )
    checks = {
        "adcs_proposal_has_same_capability_multi_target_frontier": (
            adcs["checks"]["same_capability_multi_target_frontier"]
        ),
        "adcs_proposal_has_equal_visible_cost": adcs["checks"]["equal_visible_cost"],
        "adcs_proposal_collapses_to_equal_downstream_value": (
            adcs["checks"]["all_targets_reach_objective"]
            and adcs["checks"]["asymmetric_downstream_value"] is False
            and adcs["checks"]["unique_best_target"] is False
        ),
        "control_shape_has_real_target_discriminator": (
            control["checks"]["same_capability_multi_target_frontier"]
            and control["checks"]["equal_visible_cost"]
            and control["checks"]["all_targets_reach_objective"]
            and control["checks"]["asymmetric_downstream_value"]
            and control["checks"]["unique_best_target"]
        ),
        "current_selectors_collapse_on_control_shape": (
            set(control_selector_failures) == set(selector_experiment.SELECTORS)
        ),
        "live_adcs_benchmark_not_authorized": live_benchmark_authorized is False,
    }
    return {
        "kind": "target_disambiguation_contract_audit",
        "generated_at": _now(),
        "proposed_surface": adcs,
        "control_surface": control,
        "authorization": {
            "live_benchmark_authorized": live_benchmark_authorized,
            "candidate_surface": ADCS_PROPOSAL_ID,
            "reason": authorization_reason,
            "next_requirement": next_requirement,
        },
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "target-disambiguation-contract-audit",
        help="audit whether the next target-disambiguated benchmark contract is real under current capabilities",
    )
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.set_defaults(func=_cmd_target_disambiguation_contract_audit)


def _cmd_target_disambiguation_contract_audit(args: Any) -> int:
    try:
        report = validate_target_disambiguation_contract()
    except TargetDisambiguationContractError as exc:
        print(f"target-disambiguation-contract-audit: {exc}", file=sys.stderr)
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
