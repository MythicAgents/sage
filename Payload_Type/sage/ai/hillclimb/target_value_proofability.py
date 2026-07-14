"""Offline proofability screen for naturally asymmetric target surfaces.

The target-value census found two current capability families that already
express same-capability, equal-visible-cost target asymmetry. This module asks
the next narrower question before any live spend: which shape is the cheaper
and more defensible contract to build next?

The screen stays eval-only. It reuses current modeled frontier behavior,
generic graph-fact projection, current verifier/execution-plan support, and
existing purpose-range validation artifacts. It does not claim a live benchmark
exists yet, and it does not modify runtime policy or capability selection.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

try:  # package import
    from . import policy_replay_selector_experiment as selector_experiment
    from . import purpose_range
    from . import replanning_benchmark
    from . import replication_purpose_range
    from . import target_value_census
    from ..langgraph import capabilities, graph_reconciler, policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import policy_replay_selector_experiment as selector_experiment  # type: ignore
    import purpose_range  # type: ignore
    import replanning_benchmark  # type: ignore
    import replication_purpose_range  # type: ignore
    import target_value_census  # type: ignore
    import capabilities  # type: ignore
    import graph_reconciler  # type: ignore
    import policy  # type: ignore


GPO_CANDIDATE_ID = "same-domain-gpo-dc-scope"
DCSYNC_CANDIDATE_ID = "multi-domain-dcsync-krbtgt"


class TargetValueProofabilityError(ValueError):
    """Raised when a declared proofability candidate cannot be screened."""


@dataclass(frozen=True)
class CandidateSpec:
    id: str
    fact_probe: str
    proof_probe: str
    substrate_probe: str
    required_fact_prefixes: tuple[str, ...]
    closest_existing_range_family: str
    reset_burden: str
    shape_compatible_with_existing_substrate: bool
    can_reuse_existing_late_blocker_substrate: bool
    new_topology_family_required: bool
    recommendation_rank: int
    construction_note: str


CANDIDATE_SPECS = (
    CandidateSpec(
        id=GPO_CANDIDATE_ID,
        fact_probe="gpo_dc_scope",
        proof_probe="gpo_dc_scope",
        substrate_probe="purpose_range_replanning",
        required_fact_prefixes=("gpo-affects-dc:",),
        closest_existing_range_family="purpose-range",
        reset_burden="extend_existing_single_domain_late_blocker_range",
        shape_compatible_with_existing_substrate=True,
        can_reuse_existing_late_blocker_substrate=True,
        new_topology_family_required=False,
        recommendation_rank=0,
        construction_note=(
            "Add a second controlled GPO target to the existing single-domain late-blocker purpose-range so "
            "one GPO is DC-scoped and the other is not."
        ),
    ),
    CandidateSpec(
        id=DCSYNC_CANDIDATE_ID,
        fact_probe="direct_dcsync",
        proof_probe="direct_dcsync",
        substrate_probe="replication_range",
        required_fact_prefixes=("ds-replication-rights:",),
        closest_existing_range_family="replication-purpose-range",
        reset_burden="new_multi_domain_topology_or_major_range_extension",
        shape_compatible_with_existing_substrate=False,
        can_reuse_existing_late_blocker_substrate=False,
        new_topology_family_required=True,
        recommendation_rank=1,
        construction_note=(
            "The current replication purpose range proves one direct DCSync target only; a true multi-domain "
            "target split needs a new topology or a major extension before it can be late-blocker-ready."
        ),
    ),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _surface_specs() -> dict[str, Any]:
    return {spec.id: spec for spec in target_value_census.SURFACES}


def _has_prefix(values: list[str], prefix: str) -> bool:
    return any(str(value).startswith(prefix) for value in values)


def _action_payload(action: Any, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "name": str(getattr(action, "name", "") or ""),
        "target": str(getattr(action, "target", "") or ""),
        "family": policy.capability_family(getattr(action, "name", "")),
        "effects": list(getattr(action, "effects", None) or []),
        "source_facts": list(getattr(action, "source_facts", None) or []),
        "operational_cost": capabilities.operational_cost_for_action(action),
    }


def _selector_scores(state: Any, actions: list[Any], best_indices: list[int]) -> list[dict[str, Any]]:
    packet = policy._decision_packet(  # type: ignore[attr-defined]
        objective=state.objective,
        state=state,
        candidates=actions,
        history=[],
        budgets={},
        selection_contract=selector_experiment.SUPPORTED_SELECTION_CONTRACT,
    )
    candidates = [_action_payload(action, index) for index, action in enumerate(actions)]
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


def _gpo_fact_projection_probe() -> list[str]:
    return graph_reconciler._gpo_scope_facts_from_scalar(  # type: ignore[attr-defined]
        "candidate-policy@target.local|dc01.target.local|target.local|1"
    )


def _dcsync_fact_projection_probe() -> list[str]:
    facts = graph_reconciler.project_graph_predicates(
        [{
            "principal": "user1@target.local",
            "type": "DCSync",
            "target_kind": "Domain",
            "domain": "target.local",
        }],
        _now(),
        3600,
    )
    return [str(getattr(fact, "predicate", "") or "") for fact in facts]


def _fact_support_report(spec: CandidateSpec, best_action: Any) -> dict[str, Any]:
    projected_predicates = (
        _gpo_fact_projection_probe()
        if spec.fact_probe == "gpo_dc_scope"
        else _dcsync_fact_projection_probe()
        if spec.fact_probe == "direct_dcsync"
        else []
    )
    source_facts = list(getattr(best_action, "source_facts", None) or [])
    projected_prefixes_present = {
        prefix: _has_prefix(projected_predicates, prefix)
        for prefix in spec.required_fact_prefixes
    }
    source_prefixes_present = {
        prefix: _has_prefix(source_facts, prefix)
        for prefix in spec.required_fact_prefixes
    }
    checks = {
        "required_fact_projects_from_generic_input": all(projected_prefixes_present.values()),
        "best_action_consumes_required_fact": all(source_prefixes_present.values()),
    }
    return {
        "probe": spec.fact_probe,
        "required_fact_prefixes": list(spec.required_fact_prefixes),
        "projected_predicates": projected_predicates,
        "best_action_source_facts": source_facts,
        "projected_prefixes_present": projected_prefixes_present,
        "source_prefixes_present": source_prefixes_present,
        "checks": checks,
        "supported": all(checks.values()),
    }


def _gpo_proof_support_report(action: Any) -> dict[str, Any]:
    plan = capabilities.build_capability_execution_plan(
        action,
        {
            "controlled_principal": r"TARGET\user1",
            "wait_seconds": 0,
        },
    )
    operations = [step.operation for step in plan.steps]
    verification = capabilities.verify_capability(
        str(getattr(action, "name", "") or ""),
        {"system_command_succeeded": True},
    )
    checks = {
        "execution_plan_builds": plan.ok is True,
        "declares_objective_admin_effect": any(
            str(effect).startswith("da:")
            for effect in list(getattr(action, "effects", None) or [])
        ),
        "membership_proof_step_present": "gpo-domain-admin-membership-proof" in operations,
        "capability_verifier_accepts_proof": verification.verdict == "achieved",
    }
    return {
        "probe": "gpo_dc_scope",
        "plan_ok": plan.ok,
        "plan_reason": plan.reason,
        "plan_operations": operations,
        "verification_verdict": verification.verdict,
        "verification_reason": verification.reason,
        "checks": checks,
        "supported": all(checks.values()),
    }


def _dcsync_proof_support_report(action: Any) -> dict[str, Any]:
    plan = capabilities.build_capability_execution_plan(action, {})
    operations = [step.operation for step in plan.steps]
    verification = capabilities.verify_capability(
        str(getattr(action, "name", "") or ""),
        {"krbtgt_hash_present": True},
    )
    checks = {
        "execution_plan_builds": plan.ok is True,
        "dcsync_step_present": "drsuapi-dcsync" in operations,
        "declares_krbtgt_effect": any(
            str(effect).startswith("krbtgt-hash:")
            for effect in list(getattr(action, "effects", None) or [])
        ),
        "capability_verifier_accepts_proof": verification.verdict == "achieved",
    }
    return {
        "probe": "direct_dcsync",
        "plan_ok": plan.ok,
        "plan_reason": plan.reason,
        "plan_operations": operations,
        "verification_verdict": verification.verdict,
        "verification_reason": verification.reason,
        "checks": checks,
        "supported": all(checks.values()),
    }


def _proof_support_report(spec: CandidateSpec, best_action: Any) -> dict[str, Any]:
    if spec.proof_probe == "gpo_dc_scope":
        return _gpo_proof_support_report(best_action)
    if spec.proof_probe == "direct_dcsync":
        return _dcsync_proof_support_report(best_action)
    return {
        "probe": spec.proof_probe,
        "checks": {"known_proof_probe": False},
        "supported": False,
    }


def _purpose_range_replanning_substrate_report(spec: CandidateSpec) -> dict[str, Any]:
    purpose_report = purpose_range.validate_purpose_range()
    replanning_report = replanning_benchmark.validate_replanning_benchmark()
    checks = {
        "closest_existing_substrate_validates": purpose_report["passes_gate"] is True,
        "existing_late_blocker_contract_validates": replanning_report["passes_gate"] is True,
        "shape_compatible_with_existing_substrate": spec.shape_compatible_with_existing_substrate,
        "can_reuse_existing_late_blocker_substrate": spec.can_reuse_existing_late_blocker_substrate,
        "does_not_require_new_topology_family": spec.new_topology_family_required is False,
    }
    return {
        "probe": spec.substrate_probe,
        "closest_existing_range_family": spec.closest_existing_range_family,
        "validated_contracts": [
            "purpose-range-validate",
            "replanning-benchmark-validate",
        ],
        "validator_results": {
            "purpose_range": purpose_report["passes_gate"],
            "replanning_benchmark": replanning_report["passes_gate"],
        },
        "reset_burden": spec.reset_burden,
        "construction_note": spec.construction_note,
        "checks": checks,
        "supported": all(checks.values()),
    }


def _replication_range_substrate_report(spec: CandidateSpec) -> dict[str, Any]:
    replication_report = replication_purpose_range.validate_replication_purpose_range()
    checks = {
        "closest_existing_substrate_validates": replication_report["passes_gate"] is True,
        "existing_late_blocker_contract_validates": False,
        "shape_compatible_with_existing_substrate": spec.shape_compatible_with_existing_substrate,
        "can_reuse_existing_late_blocker_substrate": spec.can_reuse_existing_late_blocker_substrate,
        "does_not_require_new_topology_family": spec.new_topology_family_required is False,
    }
    return {
        "probe": spec.substrate_probe,
        "closest_existing_range_family": spec.closest_existing_range_family,
        "validated_contracts": ["replication-purpose-range-validate"],
        "validator_results": {
            "replication_purpose_range": replication_report["passes_gate"],
        },
        "reset_burden": spec.reset_burden,
        "construction_note": spec.construction_note,
        "checks": checks,
        "supported": all(checks.values()),
    }


def _substrate_report(spec: CandidateSpec) -> dict[str, Any]:
    if spec.substrate_probe == "purpose_range_replanning":
        return _purpose_range_replanning_substrate_report(spec)
    if spec.substrate_probe == "replication_range":
        return _replication_range_substrate_report(spec)
    return {
        "probe": spec.substrate_probe,
        "checks": {"known_substrate_probe": False},
        "supported": False,
    }


def _candidate_report(spec: CandidateSpec, census_surface: dict[str, Any]) -> dict[str, Any]:
    surface_spec = _surface_specs().get(spec.id)
    if surface_spec is None:
        raise TargetValueProofabilityError(f"{spec.id}: no matching target-value census surface exists")
    state = surface_spec.state_factory()
    actions = target_value_census._same_capability_actions(  # type: ignore[attr-defined]
        state,
        surface_spec.capability_name,
    )
    best_indices = list(census_surface.get("best_indices") or [])
    if len(best_indices) != 1 or best_indices[0] >= len(actions):
        raise TargetValueProofabilityError(f"{spec.id}: expected exactly one modeled best action")
    best_action = actions[best_indices[0]]
    selector_scores = _selector_scores(state, actions, best_indices)
    fact_support = _fact_support_report(spec, best_action)
    proof_support = _proof_support_report(spec, best_action)
    substrate = _substrate_report(spec)
    checks = {
        "census_surface_is_naturally_asymmetric": (
            census_surface["diagnosis"]["classification"] == target_value_census.NATURALLY_ASYMMETRIC
        ),
        "same_capability_multi_target_frontier": (
            census_surface["checks"]["same_capability_multi_target_frontier"] is True
        ),
        "equal_visible_cost": census_surface["checks"]["equal_visible_cost"] is True,
        "all_targets_reach_objective": census_surface["checks"]["all_targets_reach_objective"] is True,
        "unique_best_target": len(best_indices) == 1,
        "current_selectors_choose_worse_target": all(
            item["selected_is_best"] is False
            for item in selector_scores
        ),
        "generic_fact_support_exists": fact_support["supported"] is True,
        "current_proof_path_exists": proof_support["supported"] is True,
        "existing_substrate_can_be_reused": substrate["supported"] is True,
    }
    ready_for_contract_build = all(checks.values())
    blockers = [
        key
        for key, value in checks.items()
        if value is not True
    ]
    return {
        "id": spec.id,
        "candidate": {
            "closest_existing_range_family": spec.closest_existing_range_family,
            "reset_burden": spec.reset_burden,
            "construction_note": spec.construction_note,
            "new_topology_family_required": spec.new_topology_family_required,
        },
        "modeled_surface": {
            "capability_name": census_surface["spec"]["capability_name"],
            "family": census_surface["spec"]["family"],
            "modeled_transaction_costs": census_surface["modeled_transaction_costs"],
            "best_indices": best_indices,
            "best_target": str(getattr(best_action, "target", "") or ""),
        },
        "selector_scores": selector_scores,
        "fact_support": fact_support,
        "proof_support": proof_support,
        "substrate": substrate,
        "checks": checks,
        "blockers": blockers,
        "ready_for_contract_build": ready_for_contract_build,
        "live_contract_ready": False,
        "live_contract_ready_reason": (
            "This screen only selects a contract-building direction; the dedicated post-blocker contract "
            "and live frontier still need to be built and audited."
        ),
    }


def run_target_value_proofability_screen(
    candidate_specs: tuple[CandidateSpec, ...] = CANDIDATE_SPECS,
) -> dict[str, Any]:
    """Return the bounded offline screen for the current natural-asymmetry candidates."""

    census_report = target_value_census.run_target_value_census()
    census_surfaces = {
        surface["id"]: surface
        for surface in census_report["surfaces"]
    }
    candidates = [
        _candidate_report(spec, census_surfaces[spec.id])
        for spec in candidate_specs
    ]
    ready_candidates = [
        candidate
        for candidate in candidates
        if candidate["ready_for_contract_build"] is True
    ]
    if len(ready_candidates) == 1:
        recommended = sorted(
            ready_candidates,
            key=lambda item: next(
                spec.recommendation_rank
                for spec in candidate_specs
                if spec.id == item["id"]
            ),
        )[0]
        recommended_candidate_id = recommended["id"]
        recommended_next_move = "build_same_domain_gpo_dc_scope_late_blocker_contract"
        recommendation_reason = (
            "Both natural-asymmetry shapes have real modeled signal and current proof support, but only the "
            "same-domain GPO DC-scope shape can reuse the existing single-domain late-blocker purpose-range "
            "without introducing a new topology family."
            if recommended_candidate_id == GPO_CANDIDATE_ID
            else (
                "Exactly one candidate clears the current modeled, proof, and substrate reuse checks."
            )
        )
    else:
        recommended_candidate_id = ""
        recommended_next_move = "resolve_candidate_proofability_gap"
        recommendation_reason = (
            "No single candidate currently clears the modeled, proof, and substrate-reuse checks. Build more "
            "offline evidence before authorizing a live benchmark contract."
        )
    live_benchmark_authorized = False
    checks = {
        "target_value_census_passes": census_report["passes_gate"] is True,
        "screens_only_natural_asymmetry_candidates": all(
            census_surfaces[spec.id]["diagnosis"]["classification"]
            == target_value_census.NATURALLY_ASYMMETRIC
            for spec in candidate_specs
        ),
        "recommendation_matches_ready_candidate_count": (
            (len(ready_candidates) == 1 and bool(recommended_candidate_id))
            or (len(ready_candidates) != 1 and not recommended_candidate_id)
        ),
        "live_benchmark_not_authorized_before_dedicated_contract": live_benchmark_authorized is False,
    }
    return {
        "kind": "target_value_proofability_screen",
        "generated_at": _now(),
        "evidence_scope": (
            "offline modeled frontier behavior, generic fact projection, current proof/execution support, "
            "and existing purpose-range validation artifacts only"
        ),
        "candidates": candidates,
        "recommendation": {
            "recommended_candidate_id": recommended_candidate_id,
            "recommended_next_move": recommended_next_move,
            "ready_candidate_ids": [candidate["id"] for candidate in ready_candidates],
            "live_benchmark_authorized": live_benchmark_authorized,
            "reason": recommendation_reason,
            "next_requirement": (
                "Build and audit a dedicated post-blocker contract for the recommended target shape before "
                "spending on another live matrix."
            ),
        },
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "target-value-proofability-screen",
        help="screen natural target-value surfaces for the next proofable live contract direction",
    )
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.set_defaults(func=_cmd_target_value_proofability_screen)


def _cmd_target_value_proofability_screen(args: Any) -> int:
    try:
        report = run_target_value_proofability_screen()
    except TargetValueProofabilityError as exc:
        print(f"target-value-proofability-screen: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(recommended_candidate_id={report['recommendation']['recommended_candidate_id'] or 'none'})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
