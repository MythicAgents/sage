from __future__ import annotations

from copy import deepcopy
import json

from ai.hillclimb import evaluator_evidence, fitness
from ai.hillclimb.range_state import GroundTruth, Milestone


_SECRET = "SAGE_TEST_SECRET_DO_NOT_USE"


def _row() -> dict:
    return {
        "semantic_transaction_count": 2,
        "boundary_violation_count": 0,
        "secret_exposure_count": 0,
        "lab_literal_count": 0,
        "unauthorized_live_request_count": 0,
        "active_runtime_config_mutation_count": 0,
        "verified_hop_count": 3,
        "provider_failure_count": 0,
        "child_task_count": 4,
        "wait_count": 1,
        "retry_count": 0,
        "total_tokens": 100,
        "wall_seconds": 5.5,
    }


def _derived() -> dict:
    return {
        "outcome_source": "independently_observed",
        "objective_proven": True,
        "clean_stop": True,
        "proof_provenance_complete": True,
        "artifact_integrity_passed": True,
        "policy_identity_passed": True,
        "backend_provenance_passed": True,
    }


def _proof() -> dict:
    return {
        "schema": "proof-envelope-v2",
        "scope": "runtime",
        "persistence_state": "admitted",
        "origin": "mythic_task",
        "engagement_id": "eng-1",
        "callback_id": "7",
        "transaction_id": "tx-1",
        "task_id": "task-1",
        "terminal_status": "completed",
        "verifier_id": "verifier-1",
        "verifier_input_sha256": "a" * 64,
        "verifier_result_sha256": "b" * 64,
    }


def _verdict(
    *, row=None, derived=None, proof=None, proofs=None, prm=None, transaction_count=2,
    model_branch=True, row_id="row-1", surface_id="surface-1",
) -> dict:
    return fitness.canonical_row_verdict(
        _row() if row is None else row,
        row_id=row_id,
        policy_arm="hybrid",
        surface_id=surface_id,
        family_id="family-1",
        derived_outcome=_derived() if derived is None else derived,
        proof_envelopes=proofs if proofs is not None else [_proof() if proof is None else proof],
        semantic_transaction_count=transaction_count,
        model_owned_branch_observed=model_branch,
        prm_diagnostic=prm,
    )


def test_canonical_verdict_is_invariant_to_raw_secret_shaped_noise():
    baseline = _verdict()
    row = _row()
    row.update({
        "answer_full": _SECRET,
        "tool_outputs_snippet": _SECRET,
        "messages": [{"metadata": {"tool_use": {"arguments": _SECRET}}}],
        "unknown": {"nested": [_SECRET]},
    })
    derived = {**_derived(), "raw_observation": _SECRET, "unknown": {"nested": _SECRET}}
    proof = {**_proof(), "metadata": {"raw_result": _SECRET}, "unknown": [_SECRET]}

    polluted = _verdict(
        row=row,
        derived=derived,
        proofs=[proof, {"unknown": {"nested": _SECRET}}],
        prm={"judge_explanation": _SECRET, "score": 1.0},
    )

    assert polluted == baseline
    assert _SECRET not in json.dumps(polluted, sort_keys=True)
    assert polluted["dense_reward"]["prm_diagnostic"] == {}


def test_projection_remains_sensitive_to_admitted_causal_fields():
    baseline = _verdict()

    unsafe_row = _row()
    unsafe_row["boundary_violation_count"] = 1
    unsafe = _verdict(row=unsafe_row)
    assert unsafe["row_verdict_hash"] != baseline["row_verdict_hash"]
    assert unsafe["dense_reward"]["hard_gates"]["safety"] is False

    cost_row = {**_row(), "wall_seconds": 6.0}
    assert _verdict(row=cost_row)["row_verdict_hash"] != baseline["row_verdict_hash"]

    failed_proof = {**_proof(), "terminal_status": "failed"}
    failed = _verdict(proof=failed_proof)
    assert failed["row_verdict_hash"] != baseline["row_verdict_hash"]
    assert failed["dense_reward"]["hard_gates"]["proof_provenance"] is False

    off_agent = _verdict(proof={**_proof(), "origin": "sage_local"})
    assert off_agent["dense_reward"]["hard_gates"]["proof_provenance"] is False

    numeric_commitment = _verdict(proof={**_proof(), "verifier_input_sha256": int("1" * 64)})
    assert numeric_commitment["dense_reward"]["hard_gates"]["proof_provenance"] is False

    changed_commitment = {**_proof(), "verifier_result_sha256": "c" * 64}
    changed = _verdict(proof=changed_commitment)
    assert changed["evaluator_evidence_hash"] != baseline["evaluator_evidence_hash"]
    assert changed["row_verdict_hash"] != baseline["row_verdict_hash"]

    failed_outcome = {**_derived(), "objective_proven": False}
    outcome = _verdict(derived=failed_outcome)
    assert outcome["promotion_row_passed"] is False
    assert outcome["row_verdict_hash"] != baseline["row_verdict_hash"]


def test_invalid_authoritative_types_fail_closed():
    row = _row()
    row["secret_exposure_count"] = "0"
    verdict = _verdict(row=row)
    assert verdict["row_status"] == "unscorable"
    assert "secret_exposure_count" in verdict["reason_codes"][0]

    derived = {**_derived(), "objective_proven": "true"}
    verdict = _verdict(derived=derived)
    assert verdict["row_status"] == "unscorable"
    assert "objective_proven" in verdict["reason_codes"][0]

    for invalid_count in ("2", -1):
        verdict = _verdict(transaction_count=invalid_count)
        assert verdict["row_status"] == "unscorable"
        assert "semantic_transaction_count" in verdict["reason_codes"][0]

    for kwargs, field in (
        ({"row_id": {"secret": _SECRET}}, "row_id"),
        ({"surface_id": False}, "surface_id"),
        ({"model_branch": "false"}, "model_owned_branch_observed"),
    ):
        verdict = _verdict(**kwargs)
        assert verdict["row_status"] == "unscorable"
        assert field in verdict["reason_codes"][0]


def test_arbitrary_verified_effect_elements_cannot_inflate_progress():
    row = _row()
    row.pop("verified_hop_count")
    baseline = _verdict(row=row)
    polluted = _verdict(row={**row, "verified_effects": [{"raw": _SECRET}]})

    assert polluted == baseline
    assert baseline["dense_reward"]["components"]["verified_hop_progress"]["value"] == 0


def test_aggregate_rejects_self_hashed_rows_without_canonical_authority_fields():
    valid = _verdict()
    assert fitness._promotion_row_passes(valid) is True

    fabricated = {"row_status": "scored", "promotion_row_passed": True}
    fabricated["row_verdict_hash"] = fitness._canonical_sha256(fabricated)
    assert fitness._promotion_row_passes(fabricated) is False

    for field in ("schema", "authority_version", "evaluator_evidence_schema", "evaluator_evidence_hash"):
        mutated = deepcopy(valid)
        mutated.pop("row_verdict_hash")
        mutated.pop(field)
        mutated["row_verdict_hash"] = fitness._canonical_sha256(mutated)
        assert fitness._promotion_row_passes(mutated) is False

    contradictory = deepcopy(valid)
    contradictory["dense_reward"]["components"]["verified_hop_progress"]["value"] = 999
    contradictory.pop("row_verdict_hash")
    contradictory["row_verdict_hash"] = fitness._canonical_sha256(contradictory)
    assert fitness._promotion_row_passes(contradictory) is False


def test_runtime_eval_view_keeps_counts_and_drops_raw_decision_and_proof_content():
    telemetry = {
        "policy_mode": "hybrid",
        "configured_policy_mode": "hybrid",
        "policy_identity_valid": True,
        "model_calls": 1,
        "semantic_transaction_count": 2,
        "authorized_transaction_count": 2,
        "semantic_policy_coverage": 1.0,
        "branch_opportunity_count": 1,
        "model_owned_decision_count": 1,
        "kernel_singleton_count": 1,
        "causally_decisive_decision_count": 1,
        "decisions": [{"raw_response": _SECRET, "rationale": _SECRET, "target": _SECRET}],
        "transactions": [{"target": _SECRET, "raw_response": _SECRET}],
        "proof_lineage": [{"metadata": {"raw_result": _SECRET}}],
        "effective_backend_requests": [{"response_metadata": {"raw": _SECRET}}],
        "effective_backends": _SECRET,
        "messages": [_SECRET],
    }

    projected = evaluator_evidence.project_runtime_telemetry(telemetry)
    summary = projected["runtime_telemetry"]

    assert summary["model_owned_decision_count"] == 1
    assert summary["decision_record_count"] == 1
    assert summary["transaction_record_count"] == 1
    assert summary["proof_lineage_record_count"] == 1
    assert _SECRET not in json.dumps(projected, sort_keys=True)

    changed = deepcopy(telemetry)
    changed["decisions"][0]["raw_response"] = _SECRET + "-changed"
    assert evaluator_evidence.project_runtime_telemetry(changed) == projected


def test_substring_proxy_changes_alarm_but_not_optimizer_scalar():
    ground_truth = GroundTruth(
        "scenario",
        {Milestone.FOOTHOLD: True, Milestone.GRAPH_COLLECTED: True},
        Milestone.GRAPH_COLLECTED,
    )
    common = {"status": "pass", "tool_calls": 1, "model_calls": 1, "errors": []}
    low_proxy = fitness.score({**common, "score": 0.0}, ground_truth)
    high_proxy = fitness.score({**common, "score": 1.0, "answer_full": _SECRET}, ground_truth)

    assert low_proxy.metric_capability_gap != high_proxy.metric_capability_gap
    assert low_proxy.capability == high_proxy.capability
    assert fitness.to_scalar(low_proxy) == fitness.to_scalar(high_proxy)
    assert low_proxy.dense_reward == high_proxy.dense_reward
