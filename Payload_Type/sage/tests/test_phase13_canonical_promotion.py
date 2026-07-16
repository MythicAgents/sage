from __future__ import annotations

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


def _positive_aggregate(*, claimed_hard_gates=None, transfer_evidence=None) -> dict:
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
    evidence = transfer_evidence or {
        "evaluated_policy": "hybrid",
        "surface_id": "structural-surface",
        "held_out_family_id": "family-b",
        "source_family_ids": ["family-a"],
        "row_verdict_hashes": [transfer["row_verdict_hash"]],
        "separately_held_out": True,
    }
    return fitness.canonical_aggregate_verdict(
        [symbolic, hybrid],
        evaluated_policy="hybrid",
        baseline_policy="symbolic",
        causal_row_verdicts=[hybrid],
        transfer_row_verdicts=[transfer],
        transfer_evidence=evidence,
        claimed_hard_gates=claimed_hard_gates,
        implementation_status="not_applied",
    )


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
