from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from ai.hillclimb.phase10_evidence_bundle import (
    build_phase10_evidence_bundle,
    render_bundle,
    validate_bundle,
)


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _verification() -> dict:
    return {
        "focused_tests": {"status": "passed", "command": "pytest focused", "result": "3 passed"},
        "architecture_budget": {"status": "passed", "command": "check_arch_budget", "result": "passed"},
        "full_offline_suite": {"status": "passed", "command": "pytest tests", "result": "1900 passed"},
        "coverage_note": "No unexplained reduction in collected coverage.",
    }


def _fixture_bundle(tmp_path: Path, *, extra_result_payload: dict | None = None) -> dict:
    plans = tmp_path / "Plans"
    results = tmp_path / "Payload_Type" / "sage" / ".hillclimb" / "results"
    trajectory = tmp_path / "Payload_Type" / "sage" / ".trajectory" / "transitions.jsonl"
    plan = plans / "SAGE_ARCHITECTURE_POLICY_EVAL_COMPLETION_PLAN_2026-07-14.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# plan\n", encoding="utf-8")
    phase6 = _write_json(
        results / "laps_family_transfer_matrix_validation_r5_20260715.json",
        {"passes_gate": True, "authorization": {"phase6_complete": True}, "policy_summaries": {}},
    )
    phase7 = _write_json(
        results / "trust_context_corroboration_live_validation_v2_20260715.json",
        {"passes_gate": True, "authorization": {"phase7_complete": True}, "matching_row_count": 3},
    )
    phase8 = _write_json(
        results / "phase8_goad_regression_validation_v2_20260716.json",
        {
            "passes_gate": True,
            "authorization": {"phase8_complete": True},
            "policy_summaries": {},
            "recommendation": {"reason": "all gates passed"},
        },
    )
    phase9 = _write_json(
        results / "phase9_auto_harness_readiness_verdict_20260716.json",
        {
            "passes_gate": True,
            "frozen_gate_record": {"reward_version": "dense-reward-v1"},
            "readiness": {
                "candidate_surface": "retrieval-ranking",
                "readiness_decision": "auto_harness_not_ready",
                "failed_prerequisites": ["t1_structural_substrate_unavailable_or_underpowered"],
                "cheapest_decisive_next_experiment": "verify one structural T1 family",
            },
            "t0": {"known_violation": {"disposition": "rejected_offline"}},
            "t2_anchor": {"ranking_authorized": False},
            "canaries": {"reset": {"ahi22_passed": False}},
        },
    )
    row_artifact = _write_jsonl(
        results / "fixture_attempts.jsonl",
        [
            {
                "controller_status": "complete",
                "objective_proven": True,
                "clean_stop": True,
                "backend_provenance_complete": True,
                "decisions": [{"decision_id": "decision-1", "admissible_frontier": [{"name": "collect-graph"}]}],
                "transactions": [
                    {
                        "transaction_id": "transaction-1",
                        "task_ids": ["task-1"],
                        "proof_ids": ["proof-1"],
                        "child_tasks": [{"task_id": "task-1", "terminal_status": "completed"}],
                        "proof_lineage": [{"task_id": "task-1", "proof_envelope_id": "proof-1", "verifier_id": "fixture"}],
                    }
                ],
            },
            extra_result_payload or {"controller_status": "failed", "status": "provider_error"},
        ],
    )
    _write_jsonl(
        trajectory,
        [
            {
                "schema_version": 2,
                "run_id": "run-1",
                "episode_id": "episode-1",
                "engagement_id": "engagement-1",
                "decision_id": "decision-1",
                "transaction_id": "transaction-1",
                "capability": "collect-graph",
                "failure_label": "",
                "label_source": "mythic_proof",
                "evidence_role": "empirical_outcome",
                "outcome_source": "independently_observed",
                "transition_outcome": "achieved",
                "proof_envelope_ref": "proof-1",
                "effective_backend": "fixture",
                "raw_frontier_hash": "sha256:" + "1" * 64,
                "admissible_frontier_hash": "sha256:" + "2" * 64,
                "semantic_candidate_ids": ["candidate:sha256:" + "3" * 64],
            }
        ],
    )
    return build_phase10_evidence_bundle(
        repo_root=tmp_path,
        source_head="a" * 40,
        generated_at="2026-07-16T00:00:00+00:00",
        verification=_verification(),
        plan_artifacts=[plan],
        result_artifacts=[phase6, phase7, phase8, phase9, row_artifact],
        hillclimb_manifests=[],
        transitions_path=trajectory,
        patch_state={
            "base_head": "a" * 40,
            "tracked_name_status": ["M\tPayload_Type/sage/ai/hillclimb/example.py"],
            "cached_name_status": [],
            "untracked_paths": [],
            "tracked_diff_stat": [],
            "cached_diff_stat": [],
            "changed_file_hashes": [],
            "worktree_clean": False,
        },
    )


def test_phase10_bundle_preserves_negative_readiness_and_campaign_stop(tmp_path):
    bundle = _fixture_bundle(tmp_path)
    assert bundle["kind"] == "phase10_portable_evidence_bundle"
    assert bundle["readiness"]["typed_verdict"] == "auto_harness_not_ready"
    assert bundle["supervised_campaign"]["status"] == "not_started"
    assert bundle["final_dispositions"]["program"]["status"] == "eligible_pending_review_and_commit"
    assert bundle["trajectory_provenance"]["schema_v2_record_count"] == 1
    assert bundle["attempt_coverage"]["row_count"] == 2
    assert bundle["lineage_summary"]["nested_objects_with_child_tasks"] == 1
    assert bundle["lineage_summary"]["nested_objects_with_task_id"] == 2
    assert bundle["lineage_summary"]["nested_objects_with_proof_envelope_id"] == 1
    assert bundle["validation"]["passes_gate"] is True


def test_phase10_bundle_redacts_secrets_and_home_paths_while_preserving_hashes(tmp_path):
    secret_hash = "4" * 64
    bundle = _fixture_bundle(
        tmp_path,
        extra_result_payload={
            "controller_status": "failed",
            "password": "winterfell",
            "path": "/home/alice/private/proof.txt",
            "sha256": secret_hash,
            "note": "ntlm=" + "5" * 32,
        },
    )
    rendered = render_bundle(bundle)
    assert "winterfell" not in rendered
    assert "/home/alice/" not in rendered
    assert "<password:redacted>" in rendered
    assert "<local-path:redacted>" in rendered
    assert secret_hash in rendered
    assert "5" * 32 not in rendered
    assert bundle["validation"]["passes_gate"] is True


def test_phase10_bundle_validation_rejects_campaign_upgrade_after_negative_readiness(tmp_path):
    bundle = _fixture_bundle(tmp_path)
    tampered = deepcopy(bundle)
    tampered["supervised_campaign"]["status"] = "started"
    report = validate_bundle(tampered, repo_root=tmp_path)
    assert report["passes_gate"] is False
    assert report["checks"]["campaign_not_claimed_after_negative_readiness"] is False
