from __future__ import annotations

from copy import deepcopy

import pytest

from ai.hillclimb import fitness
from ai.hillclimb import phase13_canonical_promotion as phase13
from ai.hillclimb.experiment_contracts import (
    NOT_ESTIMABLE,
    POLICY_APPLICATION_SCOPE_EXPLICIT_AUTHORIZED_HARNESS,
    POLICY_DEFAULT_RECOMMENDATION_INVALIDATED,
    POLICY_DEFAULT_RECOMMENDATION_PENDING_APPROVAL,
    POLICY_EVIDENCE_SCOPE_AUTHORIZED_LAB_HARNESS,
    SCOPE_GOVERNANCE_NOT_EVALUATED,
)


def _proof(index: int) -> dict:
    return {
        "schema": "proof-envelope-v2",
        "scope": "runtime",
        "origin": "mythic_task",
        "engagement_id": "eng-1",
        "callback_id": "7",
        "transaction_id": f"tx-{index}",
        "task_id": f"task-{index}",
        "terminal_status": "completed",
        "verifier_id": f"verifier-{index}",
        "verifier_input_sha256": "a" * 64,
        "verifier_result_sha256": "b" * 64,
        "persistence_state": "admitted",
    }


def _row_verdict(
    row_id: str,
    *,
    policy_arm: str,
    family_id: str,
    cost: int,
    model_owned_branch_observed: bool,
) -> dict:
    row = {
        "semantic_transaction_count": cost,
        "boundary_violation_count": 0,
        "secret_exposure_count": 0,
        "lab_literal_count": 0,
        "unauthorized_live_request_count": 0,
        "active_runtime_config_mutation_count": 0,
        "verified_effects": [f"effect:{row_id}"],
    }
    return fitness.canonical_row_verdict(
        row,
        row_id=row_id,
        policy_arm=policy_arm,
        surface_id="structural-surface",
        family_id=family_id,
        derived_outcome={
            "outcome_source": "independently_observed",
            "objective_proven": True,
            "clean_stop": True,
            "proof_provenance_complete": True,
            "artifact_integrity_passed": True,
            "policy_identity_passed": True,
            "backend_provenance_passed": True,
        },
        proof_envelopes=[_proof(cost)],
        semantic_transaction_count=cost,
        model_owned_branch_observed=model_owned_branch_observed,
    )


def _positive_inputs() -> dict:
    symbolic = _row_verdict(
        "symbolic-primary",
        policy_arm="symbolic",
        family_id="family-a",
        cost=2,
        model_owned_branch_observed=False,
    )
    hybrid = _row_verdict(
        "hybrid-primary",
        policy_arm="hybrid",
        family_id="family-a",
        cost=1,
        model_owned_branch_observed=True,
    )
    transfer = _row_verdict(
        "hybrid-transfer",
        policy_arm="hybrid",
        family_id="family-b",
        cost=1,
        model_owned_branch_observed=True,
    )
    return {
        "primary": [symbolic, hybrid],
        "causal": [hybrid],
        "transfer": [transfer],
        "transfer_evidence": {
            "evaluated_policy": "hybrid",
            "surface_id": "structural-surface",
            "held_out_family_id": "family-b",
            "source_family_ids": ["family-a"],
            "row_verdict_hashes": [transfer["row_verdict_hash"]],
            "separately_held_out": True,
        },
    }


def _aggregate(inputs: dict, *, evaluated="hybrid", baseline="symbolic", claimed=None) -> dict:
    return fitness.canonical_aggregate_verdict(
        inputs["primary"],
        evaluated_policy=evaluated,
        baseline_policy=baseline,
        causal_row_verdicts=inputs["causal"],
        transfer_row_verdicts=inputs["transfer"],
        transfer_evidence=inputs["transfer_evidence"],
        claimed_hard_gates=claimed,
        implementation_status="not_applied",
    )


def _positive_aggregate(*, claimed_hard_gates=None, transfer_evidence=None) -> dict:
    inputs = _positive_inputs()
    inputs["transfer_evidence"] = transfer_evidence or inputs["transfer_evidence"]
    return _aggregate(inputs, claimed=claimed_hard_gates)


def _unscorable_row() -> dict:
    return fitness.canonical_row_verdict(
        {"semantic_transaction_count": 1},
        row_id="unscorable-row",
        policy_arm="hybrid",
        surface_id="structural-surface",
        family_id="family-a",
        derived_outcome=None,
        proof_envelopes=(),
        semantic_transaction_count=1,
        model_owned_branch_observed=True,
    )


def _transfer_evidence(transfer: list[dict], source_families: list[str], held_out: str) -> dict:
    return {
        "evaluated_policy": "hybrid",
        "surface_id": "structural-surface",
        "held_out_family_id": held_out,
        "source_family_ids": source_families,
        "row_verdict_hashes": [item["row_verdict_hash"] for item in transfer],
        "separately_held_out": True,
    }


def test_canonical_row_verdict_refuses_to_score_before_derived_outcome_and_proof_fields_exist():
    verdict = fitness.canonical_row_verdict(
        {"semantic_transaction_count": 1},
        row_id="legacy-row",
        policy_arm="hybrid",
        surface_id="surface",
        family_id="family",
        derived_outcome=None,
        proof_envelopes=(),
        semantic_transaction_count=1,
        model_owned_branch_observed=True,
    )

    assert verdict["row_status"] == "unscorable"
    assert verdict["dense_reward"] is None
    assert verdict["promotion_row_passed"] is False
    assert "missing_derived_row_fields" in verdict["reason_codes"][0]


def test_canonical_aggregate_consumes_row_verdicts_and_keeps_status_axes_separate():
    aggregate = _positive_aggregate()
    typed = aggregate["typed_verdict"]

    assert aggregate["aggregate_admission_passed"] is True
    assert aggregate["promotion_evidence_passed"] is True
    assert typed["product_policy_status"] == POLICY_DEFAULT_RECOMMENDATION_PENDING_APPROVAL
    assert typed["descriptive_status"] == "retained_descriptive_evidence"
    assert typed["within_family_causal_status"] == "supported"
    assert typed["transfer_status"] == "supported"
    assert typed["research_claim_status"] == "supported"
    assert typed["approval_status"] == "pending_operator_approval"
    assert typed["implementation_status"] == "not_applied"
    assert typed["policy_evidence_scope"] == POLICY_EVIDENCE_SCOPE_AUTHORIZED_LAB_HARNESS
    assert typed["policy_application_scope"] == POLICY_APPLICATION_SCOPE_EXPLICIT_AUTHORIZED_HARNESS
    assert typed["scope_governance_status"] == SCOPE_GOVERNANCE_NOT_EVALUATED


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("invalid_extra", "primary_contains_noncanonical_row"),
        ("unscorable_extra", "primary_contains_unscorable_row"),
        ("nonmapping_primary", "primary_contains_non_mapping_row"),
        ("nonmapping_causal", "causal_contains_non_mapping_row"),
        ("nonmapping_transfer", "transfer_contains_non_mapping_row"),
        ("unknown_arm", "primary_contains_unknown_policy_arm"),
        ("same_arm", "policy_arms_not_distinct"),
        ("duplicate_primary", "primary_contains_duplicate_row_id"),
        ("duplicate_causal", "causal_contains_duplicate_row_id"),
        ("duplicate_transfer", "transfer_contains_duplicate_row_id"),
        ("unbalanced_primary", "primary_policy_strata_imbalanced"),
        ("cross_family", "primary_policy_strata_imbalanced"),
        ("cross_role_conflict", "cross_role_row_identity_conflict"),
        ("wrong_causal_arm", "causal_contains_wrong_policy_arm"),
        ("wrong_transfer_arm", "transfer_contains_wrong_policy_arm"),
        ("invalid_evaluated_type", "invalid_evaluated_policy"),
        ("invalid_evaluated_empty", "invalid_evaluated_policy"),
        ("invalid_baseline_type", "invalid_baseline_policy"),
        ("invalid_baseline_empty", "invalid_baseline_policy"),
    ),
)
def test_aggregate_admission_failure_class_is_dispositive(case, reason):
    inputs = _positive_inputs()
    evaluated, baseline = "hybrid", "symbolic"
    if case == "invalid_extra":
        forged = deepcopy(inputs["primary"][0])
        forged["row_id"] = "forged"
        forged.pop("row_verdict_hash")
        forged["row_verdict_hash"] = fitness._canonical_sha256(forged)
        inputs["primary"].append(forged)
    elif case == "unscorable_extra":
        inputs["primary"].append(_unscorable_row())
    elif case.startswith("nonmapping_"):
        inputs[case.removeprefix("nonmapping_")].append("noise")
    elif case == "unknown_arm":
        inputs["primary"].append(_row_verdict("llm-row", policy_arm="llm", family_id="family-a", cost=1, model_owned_branch_observed=True))
    elif case == "same_arm":
        evaluated = baseline = "hybrid"
    elif case.startswith("duplicate_"):
        role = case.removeprefix("duplicate_")
        inputs[role].append(inputs[role][0])
    elif case == "unbalanced_primary":
        inputs["primary"].append(_row_verdict("symbolic-extra", policy_arm="symbolic", family_id="family-a", cost=1, model_owned_branch_observed=False))
    elif case == "cross_family":
        hybrid = _row_verdict("hybrid-primary", policy_arm="hybrid", family_id="family-c", cost=1, model_owned_branch_observed=True)
        inputs["primary"][1] = inputs["causal"][0] = hybrid
    elif case == "cross_role_conflict":
        inputs["causal"][0] = _row_verdict("hybrid-primary", policy_arm="hybrid", family_id="family-c", cost=1, model_owned_branch_observed=True)
    elif case == "wrong_causal_arm":
        inputs["causal"][0] = inputs["primary"][0]
    elif case == "wrong_transfer_arm":
        inputs["transfer"][0] = _row_verdict("symbolic-transfer", policy_arm="symbolic", family_id="family-b", cost=1, model_owned_branch_observed=False)
    elif case == "invalid_evaluated_type":
        evaluated = 1
    elif case == "invalid_evaluated_empty":
        evaluated = " "
    elif case == "invalid_baseline_type":
        baseline = False
    elif case == "invalid_baseline_empty":
        baseline = " "

    aggregate = _aggregate(inputs, evaluated=evaluated, baseline=baseline)
    typed = aggregate["typed_verdict"]

    assert aggregate["aggregate_admission_passed"] is False
    assert aggregate["promotion_evidence_passed"] is False
    assert reason in aggregate["reason_codes"]
    assert typed["artifact_integrity_passed"] is False
    assert typed["research_claim_status"] != "supported"
    assert typed["product_policy_status"] == POLICY_DEFAULT_RECOMMENDATION_INVALIDATED
    assert typed["approval_status"] == "not_approved"
    if case.startswith("duplicate_"):
        assert f"{case.removeprefix('duplicate_')}_contains_duplicate_row_hash" in aggregate["reason_codes"]


def test_policy_identity_normalization_preserves_a_valid_near_match():
    aggregate = _aggregate(_positive_inputs(), evaluated=" HYBRID ", baseline="SYMBOLIC")

    assert aggregate["aggregate_admission_passed"] is True
    assert aggregate["promotion_evidence_passed"] is True


def test_non_regression_is_required_in_every_balanced_primary_stratum():
    symbolic_a = _row_verdict("symbolic-a", policy_arm="symbolic", family_id="family-a", cost=4, model_owned_branch_observed=False)
    hybrid_a = _row_verdict("hybrid-a", policy_arm="hybrid", family_id="family-a", cost=1, model_owned_branch_observed=True)
    symbolic_b = _row_verdict("symbolic-b", policy_arm="symbolic", family_id="family-b", cost=1, model_owned_branch_observed=False)
    hybrid_b = _row_verdict("hybrid-b", policy_arm="hybrid", family_id="family-b", cost=4, model_owned_branch_observed=True)
    transfer = _row_verdict("hybrid-transfer-c", policy_arm="hybrid", family_id="family-c", cost=1, model_owned_branch_observed=True)
    inputs = {
        "primary": [symbolic_a, hybrid_a, symbolic_b, hybrid_b],
        "causal": [hybrid_a, hybrid_b],
        "transfer": [transfer],
        "transfer_evidence": _transfer_evidence([transfer], ["family-a", "family-b"], "family-c"),
    }

    aggregate = _aggregate(inputs)

    assert aggregate["aggregate_admission_passed"] is True
    assert aggregate["typed_verdict"]["candidate_efficacy_passed"] is True
    assert aggregate["typed_verdict"]["non_regression_passed"] is False
    assert aggregate["promotion_evidence_passed"] is False


def test_canonical_aggregate_fails_closed_on_row_aggregate_disagreement():
    aggregate = _positive_aggregate(claimed_hard_gates={"artifact_integrity": False})

    assert aggregate["row_aggregate_disagreement"] is True
    assert aggregate["promotion_evidence_passed"] is False
    assert aggregate["typed_verdict"]["product_policy_status"] == POLICY_DEFAULT_RECOMMENDATION_INVALIDATED
    assert "row_aggregate_hard_gate_disagreement" in aggregate["reason_codes"]


def test_symbolic_only_surrogate_cannot_satisfy_hybrid_transfer():
    aggregate = _positive_aggregate(
        transfer_evidence={
            "evaluated_policy": "symbolic",
            "surface_id": "structural-surface",
            "held_out_family_id": "family-b",
            "source_family_ids": ["family-a"],
            "row_verdict_hashes": [],
            "separately_held_out": True,
        }
    )

    assert aggregate["typed_verdict"]["transfer_passed"] is False
    assert aggregate["promotion_evidence_passed"] is False
    assert "transfer_not_independently_observed_for_evaluated_policy" in aggregate["reason_codes"]


def test_missing_metrics_are_reported_as_not_estimable():
    report = fitness.canonical_metric_report()

    assert report == {
        "rank_correlation": NOT_ESTIMABLE,
        "achieved_power": NOT_ESTIMABLE,
        "mde": NOT_ESTIMABLE,
    }


def test_phase13_replay_invalidates_retained_phase8_recommendation_without_retrofit():
    report = phase13.build_phase13_status_map()

    assert report["validation"]["passes_gate"] is True
    assert report["phase6_replay"]["row_count"] == 12
    assert report["phase6_replay"]["status_counts"] == {"unscorable": 12}
    assert report["phase8_replay"]["row_count"] == 10
    assert report["phase8_replay"]["status_counts"] == {"unscorable": 10}
    assert report["phase8_replay"]["canonical_aggregate"]["row_aggregate_disagreement"] is True
    assert (
        report["superseding_status_map"]["former_phase8_recommendation"]
        == POLICY_DEFAULT_RECOMMENDATION_INVALIDATED
    )
    assert report["superseding_status_map"]["transfer_status"] == "not_established"
