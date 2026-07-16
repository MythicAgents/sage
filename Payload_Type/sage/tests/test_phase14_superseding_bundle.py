from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

from ai.hillclimb.phase14_bundle_validator import validate_phase14_bundle
from ai.hillclimb.phase14_superseding_bundle import build_phase14_superseding_bundle
from ai.hillclimb.phase12_proof_binding_audit import historical_authorization_provenance


def _run(root: Path, *args: str) -> str:
    return subprocess.check_output(list(args), cwd=root, text=True).strip()


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _runtime_row(
    *,
    tx_id: str,
    decision_id: str,
    capability: str = "execute-as-local-admin",
    controller_status: str = "complete",
    terminal_reason: str = "objective satisfied",
    objective_proven: bool = True,
    clean_stop: bool = True,
    password: str | None = None,
) -> dict:
    row = {
        "ts_iso": f"2026-07-16T00:00:{tx_id[-2:]}",
        "scenario": "fixture-surface",
        "side": "harness",
        "policy_mode": "hybrid",
        "configured_policy_mode": "hybrid",
        "chat_channel_id": 2,
        "chat_request_id": 1,
        "controller_status": controller_status,
        "controller_terminal_reason": terminal_reason,
        "objective_proven": objective_proven,
        "clean_stop": clean_stop,
        "backend_provenance_complete": True,
        "policy_identity_valid": True,
        "decisions": [
            {
                "decision_id": decision_id,
                "selected_capability": capability,
                "admissible_frontier": [{"name": capability}],
            }
        ],
        "transactions": [
            {
                "transaction_id": tx_id,
                "decision_id": decision_id,
                "child_tasks": [{"task_id": "task-1", "command": "whoami", "terminal_status": "completed"}],
                "proof_lineage": [
                    {
                        "task_id": "task-1",
                        "proof_envelope_id": "sha256:" + "1" * 64,
                        "verifier_id": "fixture:completed",
                    }
                ],
                "proof_envelope_ids": ["sha256:" + "1" * 64],
                "verifier_ids": ["fixture:completed"],
            }
        ],
    }
    if password is not None:
        row["password"] = password
    return row


def _fixture_repo(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    root = tmp_path
    _run(root, "git", "init", "-q")
    _run(root, "git", "config", "user.email", "fixture@example.com")
    _run(root, "git", "config", "user.name", "Fixture")
    _run(root, "git", "config", "commit.gpgsign", "false")

    policy = root / "Payload_Type" / "sage" / "ai" / "langgraph" / "policy.py"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("POLICY_HYBRID = 'hybrid'\nPOLICY_DEFAULT = POLICY_HYBRID\n", encoding="utf-8")
    _run(root, "git", "add", str(policy.relative_to(root)))
    _run(root, "git", "commit", "-q", "-m", "commit hybrid default")
    original_head = _run(root, "git", "rev-parse", "HEAD")

    phase10_generator = root / "Payload_Type" / "sage" / "ai" / "hillclimb" / "phase10_evidence_bundle.py"
    phase9_generator = root / "Payload_Type" / "sage" / "ai" / "hillclimb" / "phase9_auto_harness_readiness.py"
    phase10_generator.parent.mkdir(parents=True, exist_ok=True)
    phase10_generator.write_text("# phase10\n", encoding="utf-8")
    phase9_generator.write_text("# phase9\n", encoding="utf-8")
    _run(root, "git", "add", str(phase10_generator.relative_to(root)), str(phase9_generator.relative_to(root)))
    _run(root, "git", "commit", "-q", "-m", "commit phase10 tooling")

    results = root / "Payload_Type" / "sage" / ".hillclimb" / "results"
    original_bundle = _write_json(
        results / "phase10_portable_evidence_bundle_20260716.json",
        {
            "kind": "phase10_portable_evidence_bundle",
            "schema_version": 1,
            "bundle_hash": "sha256:" + "2" * 64,
            "bundle_metadata": {"patch_stack": {"base_head": original_head}},
        },
    )
    accepted = _runtime_row(tx_id="transaction-01", decision_id="decision-01", password="winterfell")
    duplicate = deepcopy(accepted)
    burned = _runtime_row(tx_id="transaction-02", decision_id="decision-02")
    diagnostic = _runtime_row(tx_id="transaction-03", decision_id="decision-03", capability="collect-graph")
    failed = _runtime_row(
        tx_id="transaction-04",
        decision_id="decision-04",
        controller_status="halted_blocked",
        terminal_reason="provider timeout",
        objective_proven=False,
    )
    accepted_path = _write_jsonl(results / "accepted_rows.jsonl", [accepted])
    duplicate_path = _write_jsonl(results / "accepted_rows_copy.jsonl", [duplicate])
    burned_path = _write_jsonl(results / "laps_family_transfer_policy_matrix_r4_20260715.jsonl", [burned])
    diagnostic_path = _write_jsonl(results / "laps_family_transfer_diagnostic_r1_after_native_prefix_fix_20260714.jsonl", [diagnostic])
    failed_path = _write_jsonl(results / "provider_failure_rows.jsonl", [failed])

    plan = root / "Plans" / "SAGE_ARCHITECTURE_POLICY_EVAL_COMPLETION_PLAN_2026-07-14.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# plan\n", encoding="utf-8")
    phase12 = _write_json(
        root / "Plans" / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE12_PROOF_BINDING_AUDIT_2026-07-16.json",
        {"historical_authorization_provenance": historical_authorization_provenance()},
    )
    phase13 = _write_json(
        root / "Plans" / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE13_CANONICAL_PROMOTION_STATUS_2026-07-16.json",
        {
            "superseding_status": {
                "typed_verdict": {
                    "product_policy_status": "hybrid_default_recommendation_invalidated_pending_fresh_evidence",
                    "policy_evidence_scope": "authorized_lab_harness",
                    "policy_application_scope": "explicit_authorized_harness_sessions_only",
                    "scope_governance_status": "not_evaluated_governance_program_not_authorized",
                    "approval_status": "not_approved",
                }
            }
        },
    )
    return root, {
        "original": original_bundle,
        "plan": plan,
        "phase12": phase12,
        "phase13": phase13,
        "accepted": accepted_path,
        "duplicate": duplicate_path,
        "burned": burned_path,
        "diagnostic": diagnostic_path,
        "failed": failed_path,
    }


def _build_fixture_bundle(tmp_path: Path) -> tuple[dict, dict[str, Path]]:
    root, paths = _fixture_repo(tmp_path)
    bundle = build_phase14_superseding_bundle(
        repo_root=root,
        generated_at="2026-07-16T00:00:00+00:00",
        plan_artifacts=[paths["plan"]],
        result_artifacts=[
            paths["accepted"],
            paths["duplicate"],
            paths["burned"],
            paths["diagnostic"],
            paths["failed"],
        ],
        hillclimb_manifests=[],
        original_bundle_path=paths["original"],
        phase12_audit_path=paths["phase12"],
        phase13_status_path=paths["phase13"],
    )
    return bundle, paths


def test_phase14_bundle_deduplicates_attempts_and_reports_separate_dispositions(tmp_path):
    bundle, _ = _build_fixture_bundle(tmp_path)
    counts = bundle["sample_accounting"]["counts"]
    assert counts["source_row_count"] == 5
    assert counts["unique_logical_attempt_count"] == 4
    assert counts["duplicate_source_row_count"] == 1
    assert counts["primary_disposition_counts"] == {
        "accepted": 1,
        "burned": 1,
        "diagnostic_only": 1,
        "infrastructure_or_provider_failure": 1,
    }
    assert counts["promotion_eligible_count"] == 0
    assert all(counts["reconciliation"].values())


def test_phase14_bundle_reports_git_objects_recursive_lineage_and_redaction_commitments(tmp_path):
    bundle, paths = _build_fixture_bundle(tmp_path)
    original_sha256 = hashlib.sha256(paths["original"].read_bytes()).hexdigest()
    assert bundle["supersedes"]["source_file_sha256"] == f"sha256:{original_sha256}"
    assert bundle["git_disposition"]["derived_disposition"]["hybrid_default_at_original_bundle_baseline"] == "committed"
    assert bundle["git_disposition"]["derived_disposition"]["phase10_generator_at_original_bundle_baseline"] == "uncommitted_or_absent"
    assert bundle["git_disposition"]["derived_disposition"]["phase10_generator_at_current_head"] == "committed"
    assert bundle["lineage_summary"]["nested_child_task_entry_count"] == 4
    assert bundle["lineage_summary"]["nested_proof_lineage_entry_count"] == 4
    assert bundle["lineage_summary"]["nested_objects_with_task_id"] >= 8
    assert bundle["lineage_summary"]["nested_objects_with_proof_envelope_id"] == 4
    assert bundle["artifact_commitment_summary"]["source_commitment_only_count"] >= 1
    assert bundle["artifact_commitment_summary"]["canonical_raw_output_commitment_count"] >= 1


def test_phase14_independent_validator_recomputes_bundle_and_rejects_count_tampering(tmp_path):
    bundle, _ = _build_fixture_bundle(tmp_path)
    report = validate_phase14_bundle(bundle, repo_root=tmp_path)
    assert report["passes_gate"] is True
    tampered = deepcopy(bundle)
    tampered["sample_accounting"]["counts"]["unique_logical_attempt_count"] = 99
    tampered["bundle_hash"] = "sha256:" + "0" * 64
    tampered_report = validate_phase14_bundle(tampered, repo_root=tmp_path)
    assert tampered_report["passes_gate"] is False
    assert "sample_counts_match_independent_recompute" in tampered_report["failed_checks"]
