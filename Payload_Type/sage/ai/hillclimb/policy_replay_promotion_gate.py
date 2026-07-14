"""Held-out transfer and promotion gate for the first replay hill-climb candidate.

The kept candidate from the first iteration is still development-surface evidence only.
This module evaluates it on one structurally different census surface outside the packet
training corpus, tracks that held-out budget, and records the live ground-truth checks
that would still be required before runtime promotion.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

try:  # package import
    from . import policy_replay_hillclimb_iteration as iteration
    from . import policy_replay_selector_experiment as selector_experiment
    from . import target_value_census
    from ..langgraph import policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import policy_replay_hillclimb_iteration as iteration  # type: ignore
    import policy_replay_selector_experiment as selector_experiment  # type: ignore
    import target_value_census  # type: ignore
    import policy  # type: ignore


LIVE_PROMOTION_REQUIRED_CHECKS = (
    "objective_proven",
    "clean_stop",
    "policy_identity_valid",
    "backend_provenance_complete",
)


class PromotionGateError(ValueError):
    """Raised when held-out transfer evidence cannot be evaluated."""


def _surface_specs() -> dict[str, Any]:
    return {spec.id: spec for spec in target_value_census.SURFACES}


def _training_families(iteration_report: dict[str, Any]) -> list[str]:
    families = {
        policy.capability_family((case.get("candidate") or {}).get("selected_capability"))
        for case in list(iteration_report.get("paired_cases") or [])
        if isinstance(case, dict) and case.get("relation") == "improved"
    }
    return sorted(family for family in families if family)


def _training_case_ids(iteration_report: dict[str, Any]) -> list[str]:
    return sorted(
        str(case.get("id") or "")
        for case in list(iteration_report.get("paired_cases") or [])
        if isinstance(case, dict) and str(case.get("id") or "")
    )


def _select_held_out_surface(
    census_report: dict[str, Any],
    training_families: list[str],
    *,
    held_out_surface_id: str | None = None,
) -> dict[str, Any]:
    surfaces = [
        surface
        for surface in list(census_report.get("surfaces") or [])
        if isinstance(surface, dict)
        and (surface.get("diagnosis") or {}).get("classification") == target_value_census.NATURALLY_ASYMMETRIC
    ]
    if held_out_surface_id is not None:
        matches = [surface for surface in surfaces if str(surface.get("id") or "") == held_out_surface_id]
        if len(matches) != 1:
            raise PromotionGateError(f"unknown held-out surface: {held_out_surface_id}")
        return matches[0]
    eligible = [
        surface
        for surface in surfaces
        if str((surface.get("spec") or {}).get("family") or "") not in set(training_families)
    ]
    if not eligible:
        raise PromotionGateError("no structurally different naturally asymmetric held-out surface is available")
    return sorted(eligible, key=lambda surface: str(surface.get("id") or ""))[0]


def _held_out_report(surface: dict[str, Any], training_families: list[str], training_case_ids: list[str]) -> dict[str, Any]:
    surface_id = str(surface.get("id") or "")
    spec = _surface_specs().get(surface_id)
    if spec is None:
        raise PromotionGateError(f"missing surface spec for held-out surface: {surface_id}")
    state = spec.state_factory()
    actions = target_value_census._same_capability_actions(state, spec.capability_name)  # type: ignore[attr-defined]
    packet = policy._decision_packet(  # type: ignore[attr-defined]
        objective=state.objective,
        state=state,
        candidates=actions,
        history=[],
        budgets={},
        selection_contract=selector_experiment.SUPPORTED_SELECTION_CONTRACT,
    )
    reachability = [
        target_value_census._modeled_reachability(state, action)  # type: ignore[attr-defined]
        for action in actions
    ]
    modeled_transactions_by_index = {
        index: (
            int(branch["modeled_transactions"])
            if branch.get("modeled_transactions") is not None
            else None
        )
        for index, branch in enumerate(reachability)
    }
    baseline_index, baseline_ranking = selector_experiment.select_blocked_effect_aware_visible_cost(packet)
    candidate_index, candidate_ranking = iteration.select_modeled_reachability_aware_visible_cost(
        packet,
        modeled_transactions_by_index=modeled_transactions_by_index,
    )
    best_indices = [int(index) for index in list(surface.get("best_indices") or [])]
    baseline_cost = modeled_transactions_by_index.get(baseline_index)
    candidate_cost = modeled_transactions_by_index.get(candidate_index)
    relation = (
        "improved"
        if baseline_cost is not None and candidate_cost is not None and candidate_cost < baseline_cost
        else "regressed"
        if baseline_cost is not None and candidate_cost is not None and candidate_cost > baseline_cost
        else "tie"
        if baseline_cost is not None and candidate_cost is not None
        else "unscored"
    )
    family = str((surface.get("spec") or {}).get("family") or "")
    checks = {
        "held_out_surface_is_naturally_asymmetric": (
            (surface.get("diagnosis") or {}).get("classification") == target_value_census.NATURALLY_ASYMMETRIC
        ),
        "held_out_family_differs_from_training_family": bool(family) and family not in set(training_families),
        "held_out_surface_is_outside_packet_training_cases": surface_id not in set(training_case_ids),
        "held_out_surface_is_not_a_renamed_training_copy": bool(family) and family not in set(training_families),
        "candidate_selects_modeled_best_target": candidate_index in best_indices,
        "candidate_does_not_regress_vs_baseline": relation in {"improved", "tie"},
    }
    return {
        "id": surface_id,
        "family": family,
        "purpose": (surface.get("spec") or {}).get("purpose"),
        "best_indices": best_indices,
        "modeled_transaction_costs": list(surface.get("modeled_transaction_costs") or []),
        "baseline": {
            "selected_index": baseline_index,
            "selected_target": getattr(actions[baseline_index], "target", "") if 0 <= baseline_index < len(actions) else "",
            "modeled_transactions": baseline_cost,
            "ranking": baseline_ranking,
        },
        "candidate": {
            "selected_index": candidate_index,
            "selected_target": getattr(actions[candidate_index], "target", "") if 0 <= candidate_index < len(actions) else "",
            "modeled_transactions": candidate_cost,
            "ranking": candidate_ranking,
        },
        "relation": relation,
        "checks": checks,
        "transfer_passes": all(checks.values()),
    }


def run_promotion_gate(
    *,
    iteration_report: dict[str, Any] | None = None,
    census_report: dict[str, Any] | None = None,
    held_out_surface_id: str | None = None,
) -> dict[str, Any]:
    replay_iteration = iteration_report or iteration.run_hillclimb_iteration()
    census = census_report or target_value_census.run_target_value_census()
    if not isinstance(replay_iteration, dict) or not isinstance(census, dict):
        raise PromotionGateError("promotion gate inputs must be report dictionaries")
    training_families = _training_families(replay_iteration)
    training_case_ids = _training_case_ids(replay_iteration)
    held_out_surface = _select_held_out_surface(
        census,
        training_families,
        held_out_surface_id=held_out_surface_id,
    )
    held_out = _held_out_report(held_out_surface, training_families, training_case_ids)
    held_out_budget = {
        "kind": "structurally_different_known_holdout",
        "sealed_before_iteration": False,
        "total_cases": 1,
        "consumed_cases": 1,
        "remaining_cases": 0,
        "case_ids": [held_out["id"]],
        "note": (
            "This holdout was predeclared by the target-value census and is structurally different from the "
            "packet training corpus; it is not a secret sealed benchmark."
        ),
    }
    live_promotion_gate = {
        "runtime_promotion_authorized": False,
        "held_out_transfer_passes": held_out["transfer_passes"],
        "required_ground_truth_checks": list(LIVE_PROMOTION_REQUIRED_CHECKS),
        "current_live_check_status": {
            check: "not_run"
            for check in LIVE_PROMOTION_REQUIRED_CHECKS
        },
        "reason": (
            "Synthetic held-out transfer passes, but runtime promotion still requires a live ground-truth gate."
            if held_out["transfer_passes"]
            else "Held-out transfer did not pass; runtime promotion remains blocked before any live spend."
        ),
    }
    checks = {
        "iteration_passes_and_keeps_candidate": (
            replay_iteration.get("passes_gate") is True
            and (replay_iteration.get("decision") or {}).get("keep_candidate") is True
        ),
        "census_passes": census.get("passes_gate") is True,
        "training_family_is_known": bool(training_families),
        "held_out_budget_is_tracked": (
            held_out_budget["consumed_cases"] == 1
            and held_out_budget["remaining_cases"] == 0
        ),
        "held_out_surface_is_structurally_different": held_out["checks"]["held_out_family_differs_from_training_family"],
        "held_out_surface_is_outside_training_corpus": held_out["checks"]["held_out_surface_is_outside_packet_training_cases"],
        "renamed_copy_is_not_counted_as_transfer": held_out["checks"]["held_out_surface_is_not_a_renamed_training_copy"],
        "transfer_and_non_regression_are_reported": (
            held_out["relation"] in {"improved", "tie", "regressed"}
            and "candidate_does_not_regress_vs_baseline" in held_out["checks"]
        ),
        "live_promotion_gate_declares_ground_truth_checks": set(LIVE_PROMOTION_REQUIRED_CHECKS)
        == set(live_promotion_gate["required_ground_truth_checks"]),
        "runtime_promotion_not_claimed_without_live_ground_truth": live_promotion_gate["runtime_promotion_authorized"] is False,
    }
    return {
        "kind": "policy_replay_promotion_gate",
        "training": {
            "iteration_id": (replay_iteration.get("iteration") or {}).get("id"),
            "training_case_ids": training_case_ids,
            "training_families": training_families,
            "verifier_hash": (replay_iteration.get("iteration") or {}).get("verifier_hash"),
            "candidate_kept": (replay_iteration.get("decision") or {}).get("keep_candidate"),
            "training_non_regression": (replay_iteration.get("aggregate") or {}).get("regressed_case_ids") == [],
        },
        "held_out_budget": held_out_budget,
        "held_out": held_out,
        "live_promotion_gate": live_promotion_gate,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "policy-replay-promotion-gate",
        help="evaluate the kept replay candidate on a structurally different held-out surface",
    )
    parser.add_argument("--held-out-surface-id", default=None, help="optional explicit census surface ID")
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.set_defaults(func=_cmd_policy_replay_promotion_gate)


def _cmd_policy_replay_promotion_gate(args: Any) -> int:
    try:
        report = run_promotion_gate(held_out_surface_id=args.held_out_surface_id)
    except PromotionGateError as exc:
        print(f"policy-replay-promotion-gate: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(held_out_transfer_passes={report['live_promotion_gate']['held_out_transfer_passes']}, "
        f"runtime_promotion_authorized={report['live_promotion_gate']['runtime_promotion_authorized']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1

