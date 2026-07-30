from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.hillclimb.experiment_contracts import (
    AttemptContract,
    ChildTaskLineage,
    DecisionLineage,
    ForcedIntervention,
    ProofLineage,
    SemanticTransactionLineage,
    TrainingExposure,
    TypedVerdict,
    blocked_action_remains_blocked,
    validate_live_lineage,
)
from ai.hillclimb.fitness import dense_reward_from_trace
from ai.hillclimb.gate_experiment import run_calibration_protocol
from ai.langgraph import policy
from ai.trajectory.dataset import build_dataset_manifest
from ai.trajectory.exporter import export_ledger_artifact
from ai.trajectory.replay import replay_score
from ai.trajectory.schema import (
    EVIDENCE_ROLE_DIAGNOSTIC_ONLY,
    EVIDENCE_ROLE_EMPIRICAL_OUTCOME,
    LABEL_SOURCE_MYTHIC_PROOF,
    OUTCOME_INDEPENDENTLY_OBSERVED,
    TransitionRecord,
    TransitionRepair,
    TransitionVerifier,
)


def _full_verdict(**overrides) -> TypedVerdict:
    values = {
        "artifact_integrity_passed": True,
        "boundary_passed": True,
        "policy_identity_passed": True,
        "causal_model_contribution_passed": True,
        "backend_provenance_passed": True,
        "candidate_efficacy_passed": True,
        "non_regression_passed": True,
        "transfer_passed": True,
    }
    values.update(overrides)
    return TypedVerdict(**values)


def _proof(transaction_id: str = "tx-1") -> dict:
    return {
        "schema": "proof-envelope-v2",
        "scope": "runtime",
        "origin": "mythic_task",
        "engagement_id": "eng-1",
        "callback_id": "7",
        "transaction_id": transaction_id,
        "task_id": "42",
        "terminal_status": "completed",
        "command": "dcsync",
        "verifier_id": "capability:dcsync-account",
        "verifier_version": "v1",
        "verifier_hash": "a" * 64,
        "verifier_input_sha256": "b" * 64,
        "verifier_result_sha256": "c" * 64,
        "captured_at": "2026-07-14T00:00:00+00:00",
        "persistence_state": "admitted",
    }


def _record(
    *,
    run_id: str,
    engagement_id: str,
    topology_family: str,
    repair: TransitionRepair | None,
    proof: dict | None = None,
    evidence_role: str = EVIDENCE_ROLE_EMPIRICAL_OUTCOME,
) -> TransitionRecord:
    proof = dict(proof or {})
    return TransitionRecord(
        run_id=run_id,
        source_files=("fixture",),
        objective="test",
        capability="dcsync-account",
        observations=(),
        verifier=TransitionVerifier(
            status="achieved",
            verifier_id=str(proof.get("verifier_id") or ""),
            proof_ids=("sha256:fixture",) if proof else (),
            admissible_proof=bool(proof),
        ),
        failure_label="ambiguous_account_name",
        repair=repair,
        engagement_id=engagement_id,
        transaction_id=str(proof.get("transaction_id") or ""),
        task_ids=((str(proof.get("task_id")),) if proof.get("task_id") else ()),
        proof_ids=("sha256:fixture",) if proof else (),
        proof_envelope=proof,
        label_source=LABEL_SOURCE_MYTHIC_PROOF if proof else "classifier",
        evidence_role=evidence_role,
        outcome_source=OUTCOME_INDEPENDENTLY_OBSERVED if proof else "diagnostic_only",
        transition_outcome="achieved",
        topology_family=topology_family,
        created_at="2026-07-14T00:00:00+00:00",
    )


def test_typed_verdict_is_conjunction_and_operator_approval_is_separate():
    verdict = _full_verdict()
    assert verdict.promotion_evidence_passed is True
    assert verdict.operator_approved is False
    assert verdict.promotion_applied is False
    assert _full_verdict(transfer_passed=False).promotion_evidence_passed is False
    with pytest.raises(ValueError, match="operator approval"):
        _full_verdict(promotion_applied=True)


def test_exact_lineage_validator_requires_decision_task_verifier_and_proof_join():
    decision = DecisionLineage("decision-1", "candidate-1", "model_branch")
    task = ChildTaskLineage("42", "dcsync", "completed")
    transaction = SemanticTransactionLineage(
        "tx-1",
        "decision-1",
        "dcsync-account",
        "7",
        child_tasks=(task,),
        verifier_ids=("capability:dcsync-account",),
        proof_envelope_ids=("sha256:proof",),
    )
    proof = ProofLineage(
        "sha256:proof",
        "tx-1",
        "42",
        "capability:dcsync-account",
        "a" * 64,
        "runtime",
        "mythic_task",
        True,
    )

    assert validate_live_lineage((decision,), (transaction,), (proof,)).valid is True
    bad = ProofLineage(
        "sha256:proof",
        "tx-1",
        "wrong-task",
        "capability:dcsync-account",
        "a" * 64,
        "runtime",
        "mythic_task",
        True,
    )
    validation = validate_live_lineage((decision,), (transaction,), (bad,))
    assert validation.valid is False
    assert "tx-1:proof_task_mismatch:sha256:proof" in validation.failures


def test_forced_interventions_are_exact_target_labels_not_policy_wins():
    intervention = ForcedIntervention("forced-1", "dcsync-account", "domain=lab.local;account=krbtgt")
    assert intervention.matches("dcsync-account", "domain=lab.local;account=krbtgt") is True
    assert intervention.matches("dcsync-account", "domain=other.local;account=krbtgt") is False
    attempt = AttemptContract(
        attempt_id="attempt-1",
        experiment_id="experiment-1",
        treatment_id="treatment-1",
        outcome_source=OUTCOME_INDEPENDENTLY_OBSERVED,
        typed_verdict=_full_verdict(),
        forced_intervention=intervention,
    )
    assert attempt.policy_win_eligible is False
    assert attempt.retain_artifact_for_review is False

    candidate = SimpleNamespace(
        name="dcsync-account",
        target="domain=lab.local;account=krbtgt",
        preconditions=[],
        effects=["krbtgt-hash:lab.local"],
        intent={
            "eval_intervention": {
                "forced": True,
                "intervention_id": "forced-1",
                "exact_target": "domain=lab.local;account=krbtgt",
                "credit_policy_win": False,
            }
        },
    )
    state = SimpleNamespace(achieved_effects=lambda: set())
    decision = asyncio.run(policy.SymbolicPolicy().select(
        episode_id="episode-1",
        objective="test",
        state=state,
        candidates=[candidate],
        history=[],
    ))
    assert decision.decision_owner == "forced_intervention"
    assert decision.forced_intervention is True
    assert decision.forced_policy_win_credit is False
    assert decision.model_owned_decision_count == 0


def test_blocked_actions_stay_blocked_until_a_transition_removes_the_blocker():
    assert blocked_action_remains_blocked(prior_status="blocked", transition_status="blocked") is True
    assert blocked_action_remains_blocked(prior_status="blocked", transition_status="achieved") is False
    assert blocked_action_remains_blocked(
        prior_status="blocked",
        transition_status="achieved",
        removes_blocker=True,
    ) is True


def test_v1_and_proposed_only_repairs_stay_diagnostic_while_proven_repairs_train():
    legacy = TransitionRecord.from_dict({
        "run_id": "legacy",
        "source_files": ["old.log"],
        "objective": "test",
        "capability": "dcsync-account",
        "observations": [],
        "verifier": {"status": "failed"},
        "failure_label": "ambiguous_account_name",
        "repair": {"kind": "qualify_principal_with_target_netbios"},
    })
    proposed = _record(
        run_id="proposed",
        engagement_id="eng-proposed",
        topology_family="family-a",
        repair=TransitionRepair(kind="qualify_principal_with_target_netbios"),
        proof=_proof("tx-proposed"),
    )
    proven = _record(
        run_id="proven",
        engagement_id="eng-proven",
        topology_family="family-a",
        repair=TransitionRepair(
            kind="qualify_principal_with_target_netbios",
            applied=True,
            independently_verified_outcome=True,
        ),
        proof=_proof("tx-proven"),
    )

    assert legacy.schema_version == 1
    assert legacy.is_diagnostic_only is True
    assert proposed.positive_repair_evidence is False
    assert proven.positive_repair_evidence is True
    result = replay_score([legacy, proposed, proven], [legacy, proposed, proven])
    assert result.total == 1
    assert result.exact_repair_matches == 1
    assert result.diagnostic_records_skipped == 1
    assert result.proposed_only_records_skipped == 1


def test_dataset_manifest_is_content_addressed_and_split_by_topology_family():
    first = _record(
        run_id="one",
        engagement_id="eng-one",
        topology_family="family-a",
        repair=None,
        proof=_proof("tx-one"),
    )
    second = _record(
        run_id="two",
        engagement_id="eng-two",
        topology_family="family-a",
        repair=None,
        proof=_proof("tx-two"),
    )
    third = _record(
        run_id="three",
        engagement_id="eng-three",
        topology_family="family-b",
        repair=None,
        proof=_proof("tx-three"),
    )
    diagnostic = _record(
        run_id="diagnostic",
        engagement_id="eng-diagnostic",
        topology_family="family-c",
        repair=None,
        evidence_role=EVIDENCE_ROLE_DIAGNOSTIC_ONLY,
    )

    manifest = build_dataset_manifest(
        [first, second, third, diagnostic],
        topology_commitments={"family-a": "train", "family-b": "sealed"},
    )

    assert manifest.valid is True
    assert manifest.dataset_hash.startswith("sha256:")
    assert {entry.topology_family for entry in manifest.splits["train"]} == {"family-a"}
    assert {entry.topology_family for entry in manifest.splits["sealed"]} == {"family-b"}
    assert [entry.run_id for entry in manifest.diagnostic_entries] == ["diagnostic"]


def test_ledger_export_requires_exact_transaction_lineage_for_empirical_outcome(tmp_path):
    proof = _proof("tx-complete")
    ledger = {
        "engagement_id": "eng-1",
        "hops": [
            {
                "technique": "capability:dcsync-account",
                "target": "domain=lab.local;account=krbtgt",
                "status": "achieved",
                "effect": "krbtgt-hash:lab.local",
                "proof_envelope": proof,
                "evidence": {"proof_envelope": proof, "proof_persistence_state": "admitted"},
            },
            {
                "technique": "capability:dcsync-account",
                "target": "domain=other.local;account=krbtgt",
                "status": "achieved",
                "effect": "krbtgt-hash:other.local",
                "proof_envelope": {**proof, "transaction_id": ""},
                "evidence": {"proof_envelope": {**proof, "transaction_id": ""}, "proof_persistence_state": "admitted"},
            },
        ],
    }
    path = tmp_path / "state_fixture.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")

    records = export_ledger_artifact(path)

    assert records[0].evidence_role == EVIDENCE_ROLE_EMPIRICAL_OUTCOME
    assert records[0].transaction_id == "tx-complete"
    assert records[0].proof_envelope_ref.startswith("sha256:")
    assert records[1].evidence_role == EVIDENCE_ROLE_DIAGNOSTIC_ONLY


def test_dense_reward_uses_typed_proof_fields_and_keeps_prm_diagnostic_separate():
    trace = {
        "objective_proven": True,
        "clean_stop": True,
        "verified_effects": ["krbtgt-hash:lab.local"],
        "child_task_count": 1,
        "secret_exposure_count": 0,
    }
    without_proof = dense_reward_from_trace(
        trace,
        typed_verdict=_full_verdict(),
        prm_diagnostic={"score": 1.0},
    )
    with_proof = dense_reward_from_trace(
        trace,
        typed_verdict=_full_verdict(),
        proof_envelopes=[_proof()],
        prm_diagnostic={"score": 1.0},
    )

    assert without_proof.hard_gates["boundary"] is False
    assert without_proof.hard_gates_passed is False
    assert with_proof.hard_gates_passed is True
    assert with_proof.prm_diagnostic == {"score": 1.0}
    assert "prm" not in with_proof.hard_gates


def test_calibration_protocol_blocks_ranking_until_power_correlation_and_inversions_pass():
    ready = run_calibration_protocol(
        candidate_surface="retrieval-v1",
        cheap_scores=list(range(20)),
        t2_scores=list(range(20)),
        t0_disposition="survived_triage",
        t1_substrate_status="verified",
        t2_anchor_present=True,
        smallest_relevant_effect=0.2,
        target_power=0.8,
        achieved_power=0.9,
        measured_noise=0.05,
        mde=0.1,
        bootstrap_samples=200,
    )
    live_recovered_from_unscorable_t0 = run_calibration_protocol(
        candidate_surface="retrieval-v1",
        cheap_scores=list(range(20)),
        t2_scores=list(range(20)),
        t0_disposition="unscorable_new_behavior",
        t1_substrate_status="verified",
        t2_anchor_present=True,
        smallest_relevant_effect=0.2,
        target_power=0.8,
        achieved_power=0.9,
        measured_noise=0.05,
        mde=0.1,
        bootstrap_samples=200,
    )
    blocked = run_calibration_protocol(
        candidate_surface="retrieval-v1",
        cheap_scores=[1, 2, 3, 4],
        t2_scores=[4, 3, 2, 1],
        t0_disposition="unscorable_new_behavior",
        t1_substrate_status="unavailable",
        t2_anchor_present=False,
        smallest_relevant_effect=0.2,
        target_power=0.5,
        achieved_power=0.2,
        measured_noise=0.4,
        mde=0.5,
        bootstrap_samples=50,
    )

    assert ready.readiness_decision == "eligible_for_supervised_artifact_campaign"
    assert ready.ranking_authorized is True
    assert ready.spearman_rho == 1.0
    assert ready.rank_correlation_ci95["lower"] > 0.0
    assert live_recovered_from_unscorable_t0.ranking_authorized is True
    assert blocked.readiness_decision == "auto_harness_not_ready"
    assert blocked.ranking_authorized is False
    assert "unscorable_new_behavior" in blocked.failed_gates
    assert "insufficient_statistical_power" in blocked.failed_gates
    assert "high_cheap_low_live_inversion" in blocked.failed_gates
