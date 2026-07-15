from __future__ import annotations

from pathlib import Path

from ai.hillclimb.evaluation_foundation import (
    FrozenArtifactConfiguration,
    PairedIdentityValidation,
    bind_candidate_surface,
    build_fixture_known_different_artifacts,
    build_phase4_provisional_report,
    build_provisional_readiness_report,
    build_t2_anchor_report,
    byte_identity_matches,
    compose_official_reset_workflow,
    lint_candidate_text,
    run_integrity_audit,
    run_t0_triage,
    snapshot_byte_hashes,
    validate_known_different_artifacts,
    validate_paired_identity,
)
from ai.hillclimb.experiment_contracts import (
    AuthorizationBoundary,
    CandidateArtifact,
    EvalResult,
    FrontierIdentity,
    ImmutableHashes,
    LineageValidation,
    Measurement,
    ModelIdentity,
    ObservedOutcome,
    OperationalRisk,
    PolicyIdentity,
    ProviderFailure,
    RangeIdentity,
    ResetIdentity,
    SURFACE_CLASS_LEXICAL_IDENTITY_SECRET,
    SURFACE_CLASS_STRUCTURAL,
    T0_UNSCORABLE_NEW_BEHAVIOR,
    T1_LEXICAL_UNAVAILABLE,
    T1_STRUCTURAL_UNAVAILABLE,
    TypedVerdict,
    AttemptContract,
    CostMeasurement,
    append_attempt_record,
    content_hash,
    load_attempt_records,
    validate_attempt_contract,
    validate_attempt_records,
)
from ai.hillclimb import fitness
from ai.hillclimb.fitness import dense_reward_from_trace
from ai.hillclimb.gate_experiment import paired_sensitivity_report
from ai.hillclimb.proposer_canary import (
    ProposerOutcome,
    ProviderRoute,
    run_proposer_canary,
)
from ai.trajectory.schema import (
    EVIDENCE_ROLE_EMPIRICAL_OUTCOME,
    OUTCOME_INDEPENDENTLY_OBSERVED,
    TransitionRecord,
    TransitionVerifier,
)


def _full_verdict() -> TypedVerdict:
    return TypedVerdict(
        artifact_integrity_passed=True,
        boundary_passed=True,
        policy_identity_passed=True,
        causal_model_contribution_passed=True,
        backend_provenance_passed=True,
        candidate_efficacy_passed=True,
        non_regression_passed=True,
        transfer_passed=True,
    )


def _proof() -> dict:
    return {
        "scope": "runtime",
        "origin": "mythic_task",
        "callback_id": "7",
        "transaction_id": "tx-1",
        "task_id": "42",
        "terminal_status": "completed",
        "verifier_id": "capability:test",
        "persistence_state": "admitted",
    }


def _transition(run_id: str = "run-1") -> TransitionRecord:
    return TransitionRecord(
        run_id=run_id,
        source_files=("fixture",),
        objective="test",
        capability="collect-graph",
        observations=(),
        verifier=TransitionVerifier(
            status="achieved",
            verifier_id="capability:test",
            proof_ids=("sha256:proof",),
            admissible_proof=True,
        ),
        failure_label="",
        engagement_id="eng-1",
        transaction_id="tx-1",
        callback_id="7",
        task_ids=("42",),
        proof_ids=("sha256:proof",),
        proof_envelope=_proof(),
        evidence_role=EVIDENCE_ROLE_EMPIRICAL_OUTCOME,
        outcome_source=OUTCOME_INDEPENDENTLY_OBSERVED,
        transition_outcome="achieved",
        topology_family="family-a",
    )


def _candidate(surface: str = "prompt-overlay", surface_class: str = SURFACE_CLASS_LEXICAL_IDENTITY_SECRET) -> CandidateArtifact:
    return CandidateArtifact(
        candidate_id="candidate-1",
        parent_id="baseline",
        surface=surface,
        surface_class=surface_class,
        single_variable="one-field",
        target_subsystem="policy",
        artifact_hash=content_hash({"surface": surface}),
        hypothesis="one hypothesis",
        mechanism="one mechanism",
    )


def _eval_result(
    *,
    candidate_id: str,
    ready_hash: str = "sha256:ready",
    effective_backend: str = "provider:model",
    outcome: bool = True,
) -> EvalResult:
    return EvalResult(
        candidate_id=candidate_id,
        tier="T1",
        mode="clean_live",
        disposition="survived_triage",
        outcome_source=OUTCOME_INDEPENDENTLY_OBSERVED if outcome else "diagnostic_only",
        measurement=Measurement(reward_version="dense-reward-v1"),
        typed_verdict=_full_verdict(),
        range_identity=RangeIdentity(
            tier="T1",
            substrate_class="dreadgoad_sage_variant",
            range_id="range-1",
            topology_family="family-a",
            objective_id="objective-1",
            seed="7",
            validity_envelope=("lexical", "identity", "secret"),
            paired_instance_id="pair-1",
            presentation_order="candidate-first",
            budget_hash="sha256:budget",
        ),
        reset_identity=ResetIdentity(
            workflow_id="sage-official-reset-and-ready-v1",
            snapshot_id="snapshot-1",
            ready_attestation_hash=ready_hash,
            operator_invoked=True,
            unattended_after_invocation=True,
        ),
        model_identity=ModelIdentity(
            configured_backend="provider:model",
            effective_backend=effective_backend,
            request_id="request-1",
            prompt_hash="sha256:prompt",
            request_schema_hash="sha256:schema",
        ),
        authorization_boundary=AuthorizationBoundary(),
        independently_observed_outcome=(
            ObservedOutcome(True, True, ("sha256:proof",)) if outcome else None
        ),
        lineage_validation=LineageValidation(valid=outcome),
    )


def _hashes() -> ImmutableHashes:
    return ImmutableHashes(
        evaluator_hash="a" * 64,
        reward_hash="b" * 64,
        test_hash="c" * 64,
        dataset_hash="d" * 64,
        sealed_hash="e" * 64,
        prompt_hash="f" * 64,
        policy_hash="1" * 64,
        active_sage_db_hash_before="2" * 64,
        active_phoenix_db_hash_before="3" * 64,
        active_ledger_hash_before="4" * 64,
        active_sage_db_hash_after="2" * 64,
        active_phoenix_db_hash_after="3" * 64,
        active_ledger_hash_after="4" * 64,
    )


def test_attempt_contract_extends_frozen_schema_and_appends_without_rewrite(tmp_path):
    started = AttemptContract(
        attempt_id="attempt-1",
        experiment_id="experiment-1",
        treatment_id="treatment-1",
        candidate=_candidate(),
        immutable_hashes=_hashes(),
        started_at="2026-07-14T00:00:00+00:00",
        status="started",
        cost=CostMeasurement(child_task_count=1, wall_time_seconds=1.5),
    )
    interrupted = AttemptContract(
        attempt_id="attempt-2",
        supersedes_attempt_id="attempt-1",
        experiment_id="experiment-1",
        treatment_id="treatment-1",
        candidate=_candidate(),
        immutable_hashes=_hashes(),
        started_at="2026-07-14T00:00:00+00:00",
        completed_at="2026-07-14T00:01:00+00:00",
        status="failed",
        disposition="failed_infra",
        cost=CostMeasurement(child_task_count=1, wall_time_seconds=60.0, termination_reason="interrupted"),
    )
    path = tmp_path / "attempts.jsonl"
    append_attempt_record(path, started)
    append_attempt_record(path, interrupted)
    rows = load_attempt_records(path)
    assert len(rows) == 2
    assert rows[0]["schema_version"] == 1
    assert rows[0]["candidate"]["candidate_id"] == "candidate-1"
    assert rows[0]["immutable_hashes"]["active_ledger_hash_before"] == "4" * 64
    assert rows[0]["record_hash"] == started.content_hash
    assert rows[1]["supersedes_attempt_id"] == "attempt-1"
    assert rows[1]["status"] == "failed"
    assert rows[1]["cost"]["termination_reason"] == "interrupted"
    assert validate_attempt_contract(started) == ()
    assert validate_attempt_records(rows) == ()


def test_verifier_hash_changes_when_dense_reward_version_changes(monkeypatch):
    before = fitness.verifier_hash()
    monkeypatch.setattr(fitness, "DENSE_REWARD_VERSION", "dense-reward-fixture-v2")
    assert fitness.verifier_hash() != before


def test_attempt_contract_carries_phase4_identity_measurement_provider_and_risk_fields():
    attempt = AttemptContract(
        attempt_id="attempt-1",
        experiment_id="experiment-1",
        treatment_id="treatment-1",
        candidate=_candidate(),
        immutable_hashes=_hashes(),
        range_identity=_eval_result(candidate_id="candidate").range_identity,
        reset_identity=_eval_result(candidate_id="candidate").reset_identity,
        model_identity=_eval_result(candidate_id="candidate").model_identity,
        policy_identity=PolicyIdentity(
            mode="hybrid",
            version="hybrid-full-frontier-v2",
            selection_contract_hash="a" * 64,
            decision_owner="model_branch",
        ),
        frontier_identity=FrontierIdentity(
            raw_candidate_count=2,
            admissible_candidate_count=2,
            candidate_ids=("candidate-a", "candidate-b"),
            candidate_set_hash="b" * 64,
            ordered_frontier_hash="c" * 64,
            selected_candidate_id="candidate-a",
            symbolic_counterfactual_candidate_id="candidate-b",
        ),
        measurement=Measurement(
            reward_version="dense-reward-v1",
            paired_instance_count=20,
            smallest_relevant_effect=0.1,
            target_power=0.8,
            achieved_power=0.9,
            measured_noise=0.02,
            mde=0.03,
        ),
        provider_failures=(
            ProviderFailure("attempt-1", "primary", "primary:model", "timeout", True, False, retry_index=1),
        ),
        operational_risk=OperationalRisk(unauthorized_live_request_count=1),
    )
    row = attempt.to_dict()
    assert row["range_identity"]["paired_instance_id"] == "pair-1"
    assert row["policy_identity"]["version"] == "hybrid-full-frontier-v2"
    assert row["frontier_identity"]["candidate_ids"] == ("candidate-a", "candidate-b")
    assert row["measurement"]["achieved_power"] == 0.9
    assert row["provider_failures"][0]["retry_index"] == 1
    assert row["operational_risk"]["risk_level"] == "low"


def test_dense_reward_keeps_distinct_typed_components_and_prm_cannot_open_hard_gate():
    trace = {
        "objective_proven": True,
        "clean_stop": True,
        "correct_replan": True,
        "verified_effects": ["effect:a", "effect:b"],
        "repeated_ineffective_work_count": 3,
        "provider_failure_count": 2,
        "child_task_count": 4,
        "wait_count": 1,
        "retry_count": 1,
        "total_tokens": 200,
        "wall_seconds": 10.5,
        "monetary_cost_usd": 0.25,
        "boundary_violation_count": 1,
        "secret_exposure_count": 0,
    }
    vector = dense_reward_from_trace(
        trace,
        typed_verdict=_full_verdict(),
        proof_envelopes=[_proof()],
        prm_diagnostic={"score": 0.99},
    )
    assert vector.components["verified_hop_progress"].value == 2
    assert vector.components["clean_stop"].value is True
    assert vector.components["repeated_ineffective_work"].value == 3
    assert vector.components["provider_failure_count"].value == 2
    assert vector.components["task_count"].value == 4
    assert vector.components["operational_risk"].value == 1
    assert vector.hard_gates["safety"] is False
    assert vector.hard_gates_passed is False
    assert vector.prm_diagnostic == {"score": 0.99}
    assert "prm" not in vector.hard_gates


def test_t0_triage_is_v2_only_and_unseen_behavior_never_retains():
    report = run_t0_triage([_transition()], claimed_mechanism_requires_unseen_behavior=True)
    assert report.disposition == T0_UNSCORABLE_NEW_BEHAVIOR
    assert report.scorable_records == 1
    assert report.retain_artifact_for_review is False
    assert report.promotion_evidence_passed is False


def test_surface_binding_keeps_lexical_and_structural_substrates_separate():
    lexical = bind_candidate_surface(
        "prompt-overlay",
        surface_class=SURFACE_CLASS_LEXICAL_IDENTITY_SECRET,
    )
    structural = bind_candidate_surface(
        "retrieval-ranking",
        surface_class=SURFACE_CLASS_STRUCTURAL,
    )
    verified_lexical = bind_candidate_surface(
        "prompt-overlay",
        surface_class=SURFACE_CLASS_LEXICAL_IDENTITY_SECRET,
        dreadgoad_authorized=True,
        dreadgoad_seeded_identity_verified=True,
        dreadgoad_reset_verified=True,
        dreadgoad_power_verified=True,
        dreadgoad_manifest_hash="a" * 64,
        dreadgoad_entrypoint="official-dreadgoad-entrypoint",
        dreadgoad_snapshot_id="snapshot-1",
    )
    assert lexical.reason_code == T1_LEXICAL_UNAVAILABLE
    assert structural.reason_code == T1_STRUCTURAL_UNAVAILABLE
    assert structural.validity_envelope != lexical.validity_envelope
    assert verified_lexical.verified is True
    assert verified_lexical.substrate_manifest_hash == "a" * 64


def test_paired_identity_requires_same_reset_backend_budget_and_proof_join():
    candidate = _eval_result(candidate_id="candidate")
    incumbent = _eval_result(candidate_id="incumbent")
    valid = validate_paired_identity(candidate, incumbent)
    assert valid.valid is True
    bad = validate_paired_identity(
        _eval_result(candidate_id="candidate", effective_backend="other:model"),
        _eval_result(candidate_id="incumbent", outcome=False),
    )
    assert bad.valid is False
    assert "paired_model_effective_backend_mismatch" in bad.failures
    assert "incumbent_outcome_not_independently_observed" in bad.failures


def test_known_different_artifacts_require_same_surface_and_fixture_only_is_not_live_evidence():
    artifacts = (
        FrozenArtifactConfiguration("inc", "a" * 40, "prompt-overlay", "1" * 64, "incumbent", "live", True),
        FrozenArtifactConfiguration("deg", "a" * 40, "prompt-overlay", "2" * 64, "deliberate_degradation", "live", True),
        FrozenArtifactConfiguration("hist", "a" * 40, "prompt-overlay", "3" * 64, "historical", "fixture_only", True),
    )
    report = validate_known_different_artifacts(artifacts)
    assert report.valid_for_instrument_plumbing is True
    assert report.valid_for_live_ranking is False
    assert report.live_ranking_evidence_count == 2
    scoped = validate_known_different_artifacts(artifacts, expected_surface="retrieval-ranking")
    assert "candidate_surface_scope_mismatch" in scoped.failures


def test_phase4_fixture_controls_are_same_head_same_surface_and_never_live_ranking_evidence():
    artifacts = build_fixture_known_different_artifacts(
        source_head="a" * 40,
        candidate_surface="prompt-overlay",
    )
    report = validate_known_different_artifacts(artifacts)
    assert len(artifacts) == 3
    assert report.valid_for_instrument_plumbing is True
    assert report.valid_for_live_ranking is False
    assert {artifact.role for artifact in artifacts} == {"incumbent", "deliberate_degradation"}


def test_paired_sensitivity_calculates_noise_mde_power_and_effect_interval():
    report = paired_sensitivity_report(
        [0.21, 0.22, 0.23, 0.24],
        [0.0, 0.0, 0.0, 0.0],
        smallest_relevant_effect=0.1,
        target_power=0.8,
    )
    assert report.paired_instance_count == 4
    assert report.measured_noise is not None
    assert report.mde is not None
    assert report.achieved_power is not None
    assert report.achieved_power >= 0.8
    assert report.paired_effect_ci95 is not None
    assert report.powered is True


def test_t2_anchor_wires_paired_sensitivity_and_rejects_score_artifact_count_drift():
    artifacts = (
        FrozenArtifactConfiguration("inc", "a" * 40, "prompt-overlay", "1" * 64, "incumbent", "live", True),
        FrozenArtifactConfiguration("deg", "a" * 40, "prompt-overlay", "2" * 64, "deliberate_degradation", "live", True),
        FrozenArtifactConfiguration("hist", "a" * 40, "prompt-overlay", "3" * 64, "historical", "live", True),
    )
    pair = validate_paired_identity(_eval_result(candidate_id="candidate"), _eval_result(candidate_id="incumbent"))
    report = build_t2_anchor_report(
        candidate_surface="prompt-overlay",
        artifacts=artifacts,
        paired_identity=pair,
        cheap_scores=(1.0, 0.5, 0.75),
        t2_scores=(1.0, 0.5, 0.75),
        t0_disposition="survived_triage",
        t1_substrate_status="verified",
        smallest_relevant_effect=0.1,
        target_power=0.8,
        measured_noise=None,
        mde=None,
        paired_candidate_scores=(0.21, 0.22, 0.23, 0.24),
        paired_incumbent_scores=(0.0, 0.0, 0.0, 0.0),
        bootstrap_samples=50,
    )
    assert report.calibration.paired_instance_count == 4
    assert report.calibration.achieved_power is not None
    assert report.calibration.mde is not None
    drift = build_t2_anchor_report(
        candidate_surface="prompt-overlay",
        artifacts=artifacts,
        paired_identity=pair,
        cheap_scores=(1.0, 0.5),
        t2_scores=(1.0, 0.5),
        t0_disposition="survived_triage",
        t1_substrate_status="verified",
        smallest_relevant_effect=0.1,
        target_power=0.8,
        achieved_power=0.9,
        measured_noise=0.01,
        mde=0.02,
    )
    assert "anchor_score_artifact_count_mismatch" in drift.artifact_validation.failures
    assert drift.ranking_authorized is False


def test_reset_workflow_hashes_ready_attestation_and_fails_when_human_steps_remain():
    blocked = compose_official_reset_workflow(
        ready_payload={"ready": True, "foothold": "present"},
        operator_invoked=True,
        human_steps_remaining=("open interactive session",),
    )
    ready = compose_official_reset_workflow(
        ready_payload={"ready": True, "foothold": "present"},
        operator_invoked=True,
        human_steps_remaining=(),
    )
    assert blocked.ready_attestation_hash.startswith("sha256:")
    assert blocked.ahi22_passed is False
    assert ready.ahi22_passed is True
    assert ready.steps[-1].step_id == "runtime_readiness"
    assert ready.steps[-1].entrypoint.endswith("bootstrap_payloads.py")


def test_integrity_audit_catches_lab_literals_leakage_hash_drift_and_overlay_cleanup(tmp_path):
    db = tmp_path / "sage.db"
    db.write_bytes(b"same")
    before = snapshot_byte_hashes([db])
    after = snapshot_byte_hashes([db])
    assert byte_identity_matches(before, after) is True
    assert lint_candidate_text(
        "connect to 10.1.2.3 callback 9 /home/alice/tmp domain.example host-a user-a",
        forbidden_literals=("domain.example", "host-a", "user-a"),
    ) != ()
    report = run_integrity_audit(
        candidate_payload="password=Secret123 host=range-host",
        forbidden_literals=("range-host",),
        immutable_hashes=ImmutableHashes(
            evaluator_hash="a" * 64,
            reward_hash="b" * 64,
            test_hash="c" * 64,
            dataset_hash="d" * 64,
            sealed_hash="e" * 64,
            prompt_hash="f" * 64,
            policy_hash="1" * 64,
            active_sage_db_hash_before="2" * 64,
            active_sage_db_hash_after="9" * 64,
        ),
        authorization_boundary=AuthorizationBoundary(
            candidate_mythic_tasks_launched=1,
            candidate_target_connections_opened=1,
        ),
        disposable_overlay_exists_after=True,
        sealed_feedback_fragments=("sealed-outcome",),
        sealed_feedback_accessed=True,
    )
    assert report.passed is False
    assert "no_lab_literals" in report.failures
    assert "leakage_detected" in report.failures
    assert "holdout_leakage_detected" in report.failures
    assert "active_database_or_ledger_hash_drift" in report.failures
    assert "offline_candidate_side_effect_detected" in report.failures
    assert "disposable_overlay_not_cleaned" in report.failures


class _FakeProcessController:
    def __init__(self, orphans=()):
        self.killed = []
        self.orphans = tuple(orphans)

    def kill_process_group(self, process_group_id: int) -> None:
        self.killed.append(process_group_id)

    def detect_orphans(self, child_process_ids):
        return self.orphans


def test_proposer_canary_preserves_transient_attempts_retries_and_never_scores_failures():
    controller = _FakeProcessController()
    calls = []

    def executor(_payload, route, iteration, retry_index):
        calls.append((route.provider, iteration, retry_index))
        if iteration == 0 and retry_index == 0:
            return ProposerOutcome(
                status="proxy_failure",
                effective_backend=route.effective_backend,
                process_group_id=10,
                child_process_ids=(101,),
            )
        return ProposerOutcome(status="success", output="{}", effective_backend=route.effective_backend)

    report = run_proposer_canary(
        redacted_input="state=<password:redacted>",
        routes=(ProviderRoute("primary", "primary:model"), ProviderRoute("backup", "backup:model")),
        executor=executor,
        iterations=3,
        max_retries_per_route=1,
        process_controller=controller,
    )
    assert report.passed is True
    assert report.iterations_completed == 3
    assert len(report.attempts) == 4
    assert report.attempts[0].status == "proxy_failure"
    assert report.attempts[0].candidate_score is None
    assert controller.killed == [10]


def test_proposer_canary_uses_preregistered_failover_after_primary_retries_exhaust():
    controller = _FakeProcessController()
    calls = []

    def executor(_payload, route, _iteration, retry_index):
        calls.append((route.provider, retry_index))
        if route.provider == "primary":
            return ProposerOutcome(
                status="provider_error",
                effective_backend=route.effective_backend,
                process_group_id=30 + retry_index,
            )
        return ProposerOutcome(status="success", output="{}", effective_backend=route.effective_backend)

    report = run_proposer_canary(
        redacted_input="state=<password:redacted>",
        routes=(ProviderRoute("primary", "primary:model"), ProviderRoute("backup", "backup:model")),
        executor=executor,
        iterations=1,
        max_retries_per_route=1,
        process_controller=controller,
    )
    assert report.passed is True
    assert calls == [("primary", 0), ("primary", 1), ("backup", 0)]
    assert [attempt.status for attempt in report.attempts] == ["provider_error", "provider_error", "success"]
    assert controller.killed == [30, 31]


def test_proposer_canary_rejects_sealed_feedback_before_any_provider_call():
    report = run_proposer_canary(
        redacted_input="state=sealed-outcome",
        sealed_feedback_fragments=("sealed-outcome",),
        routes=(ProviderRoute("primary", "primary:model"),),
        executor=lambda *_args: ProposerOutcome(status="success", output="{}", effective_backend="primary:model"),
        iterations=1,
    )
    assert report.passed is False
    assert report.attempts == ()
    assert report.failures == ("sealed_feedback_exposed",)


def test_proposer_canary_fails_closed_on_backend_mismatch_and_orphans():
    controller = _FakeProcessController(orphans=(222,))
    report = run_proposer_canary(
        redacted_input="state=<password:redacted>",
        routes=(ProviderRoute("primary", "primary:model"),),
        executor=lambda *_args: ProposerOutcome(
            status="success",
            output="{}",
            effective_backend="wrong:model",
            process_group_id=20,
            child_process_ids=(222,),
        ),
        iterations=1,
        process_controller=controller,
    )
    assert report.passed is False
    assert "backend_mismatch" in report.failures
    assert "orphan_process_detected" in report.failures
    assert report.terminal_disposition == "orphan_process_detected"


def test_t2_anchor_and_provisional_report_remain_not_ready_without_live_substrate_reset_and_canary():
    candidate = _candidate("retrieval-ranking", SURFACE_CLASS_STRUCTURAL)
    t0 = run_t0_triage([_transition()], claimed_mechanism_requires_unseen_behavior=True)
    binding = bind_candidate_surface(candidate.surface, surface_class=candidate.surface_class)
    pair = PairedIdentityValidation(False, ("missing_live_paired_t1_t2_evidence",))
    t2 = build_t2_anchor_report(
        candidate_surface=candidate.surface,
        artifacts=(),
        paired_identity=pair,
        cheap_scores=(),
        t2_scores=(),
        t0_disposition=t0.disposition,
        t1_substrate_status=binding.t1_substrate_status,
        smallest_relevant_effect=None,
        target_power=None,
        measured_noise=None,
        mde=None,
    )
    reset = compose_official_reset_workflow(
        ready_payload={"ready": False},
        operator_invoked=False,
        human_steps_remaining=("interactive foothold",),
    )
    integrity = run_integrity_audit(
        candidate_payload="redacted artifact",
        forbidden_literals=(),
        immutable_hashes=_hashes(),
        authorization_boundary=AuthorizationBoundary(),
        disposable_overlay_exists_after=False,
    )
    report = build_provisional_readiness_report(
        candidate=candidate,
        t0=t0,
        surface_binding=binding,
        paired_identity=pair,
        t2_anchor=t2,
        reset_workflow=reset,
        integrity=integrity,
        proposer_canary_passed=False,
        provider_canary_passed=False,
        authorization_boundary=AuthorizationBoundary(),
    )
    assert report.readiness_decision == "auto_harness_not_ready"
    assert T1_STRUCTURAL_UNAVAILABLE in report.failed_gates
    assert "ahi22_reset_not_unattended_ready" in report.failed_gates
    assert "proposer_or_provider_canary_not_ready" in report.failed_gates
    t0_gate = next(gate for gate in report.gate_results if gate.gate_id == "AHI-11/AHI-12")
    assert t0_gate.passed is True
    assert t0_gate.reason_code == ""


def test_phase4_provisional_report_is_typed_negative_and_authorizes_no_campaign():
    report = build_phase4_provisional_report()
    assert report["kind"] == "phase4_provisional_readiness_report"
    assert report["readiness"]["readiness_decision"] == "auto_harness_not_ready"
    assert "t1_structural_substrate_unavailable_or_underpowered" in report["readiness"]["failed_gates"]
    assert report["reset_workflow"]["ahi22_passed"] is False
