from __future__ import annotations

import json

from ai.hillclimb.phase9_auto_harness_readiness import (
    build_phase9_readiness_report,
    render_report,
)
from ai.trajectory.schema import (
    EVIDENCE_ROLE_EMPIRICAL_OUTCOME,
    OUTCOME_INDEPENDENTLY_OBSERVED,
    TransitionRecord,
    TransitionVerifier,
)


def _transition() -> TransitionRecord:
    return TransitionRecord(
        run_id="run-1",
        source_files=("fixture",),
        objective="test",
        capability="collect-graph",
        observations=(),
        verifier=TransitionVerifier(status="achieved"),
        failure_label="",
        evidence_role=EVIDENCE_ROLE_EMPIRICAL_OUTCOME,
        outcome_source=OUTCOME_INDEPENDENTLY_OBSERVED,
    )


def _iteration_report() -> dict:
    return {
        "kind": "policy_replay_hillclimb_iteration",
        "iteration": {
            "id": "modeled-reachability-ranking-v1",
            "verifier_hash": "sha256:iteration",
        },
        "aggregate": {"t0_disposition": "unscorable_new_behavior"},
        "decision": {
            "action": "halt_at_live_boundary",
            "runtime_promotion_authorized": False,
        },
        "passes_gate": True,
    }


def _promotion_report() -> dict:
    return {
        "kind": "policy_replay_promotion_gate",
        "live_promotion_gate": {"runtime_promotion_authorized": False},
        "typed_verdict": {"reason_codes": ["transfer_not_independently_observed"]},
        "passes_gate": True,
    }


def _report(tmp_path) -> dict:
    return build_phase9_readiness_report(
        repo_root=tmp_path,
        source_head="a" * 40,
        records=(_transition(),),
        iteration_report=_iteration_report(),
        promotion_report=_promotion_report(),
    )


def test_phase9_report_emits_one_typed_negative_verdict_and_halts_before_live(tmp_path):
    report = _report(tmp_path)
    assert report["kind"] == "phase9_auto_harness_readiness_verdict"
    assert report["candidate"]["surface"] == "retrieval-ranking"
    assert report["frozen_gate_record"]["source_head"] == "a" * 40
    assert report["readiness"]["readiness_decision"] == "auto_harness_not_ready"
    assert report["frozen_t1_request"]["state"] == "blocked_before_operator_invocation"
    assert report["frozen_t1_request"]["operator_invocation_authorized"] is False
    assert report["frozen_t1_request"]["live_work_started"] is False
    assert report["checks"]["exactly_one_typed_surface_verdict"] is True
    assert report["checks"]["negative_verdict_stops_campaign"] is True
    assert report["passes_gate"] is True


def test_phase9_report_keeps_diagnostic_artifacts_and_fixture_canary_out_of_live_evidence(tmp_path):
    report = _report(tmp_path)
    anchor = report["t2_anchor"]
    fixture_canary = report["canaries"]["fixture_proposer_provider_cleanup"]
    effective_canary = report["canaries"]["effective_provider_proposer"]
    assert len(report["known_different_artifacts"]) == 4
    assert anchor["artifact_validation"]["valid_for_instrument_plumbing"] is True
    assert anchor["artifact_validation"]["valid_for_live_ranking"] is False
    assert report["t0"]["known_violation"]["disposition"] == "rejected_offline"
    assert report["t0"]["unseen_mechanism"]["disposition"] == "unscorable_new_behavior"
    assert fixture_canary["report"]["passed"] is True
    assert fixture_canary["qualifies_as_effective_provider_evidence"] is False
    assert effective_canary["passed"] is False
    assert effective_canary["provider_work_started"] is False


def test_phase9_report_enumerates_decisive_missing_prerequisites_and_renders_json(tmp_path):
    report = _report(tmp_path)
    failures = set(report["readiness"]["failed_prerequisites"])
    assert "t1_structural_substrate_unavailable_or_underpowered" in failures
    assert "missing_operator_returned_paired_t1_evidence" in failures
    assert "t2_anchor_missing" in failures
    assert "statistical_power_not_estimable" in failures
    assert "effective_provider_proposer_canary_not_completed" in failures
    assert "matching_unattended_reset_attestation_not_returned" in failures
    assert "ahi22_reset_not_unattended_ready" in failures
    assert json.loads(render_report(report))["readiness"]["readiness_decision"] == "auto_harness_not_ready"
