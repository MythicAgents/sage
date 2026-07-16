from __future__ import annotations

import json
from pathlib import Path

from ai.hillclimb.phase12_proof_binding_audit import (
    build_candidate_effect_path_inventory,
    build_phase12_proof_binding_audit,
    validate_candidate_effect_path_inventory,
    validate_phase12_proof_binding_audit,
    write_json_with_sha256,
)


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _lineage(task_id: str, transaction_id: str, *, with_v2_commitments: bool = False) -> dict:
    row = {
        "scope": "runtime",
        "origin": "mythic_task",
        "task_id": task_id,
        "transaction_id": transaction_id,
        "proof_envelope_id": "sha256:" + task_id.zfill(64),
        "verifier_id": "capability:fixture",
    }
    if with_v2_commitments:
        row["verifier_input_sha256"] = "a" * 64
        row["verifier_result_sha256"] = "b" * 64
    return row


def _fixture_source(tmp_path: Path) -> Path:
    return _write_jsonl(
        tmp_path / "results" / "phase6_fixture.jsonl",
        [
            {
                "phase6_planned_row_id": "row-1",
                "transactions": [
                    {
                        "transaction_id": "tx-unverifiable",
                        "callback_id": "2",
                        "capability": "collect-graph",
                        "child_tasks": [
                            {"task_id": "1", "command": "execute_assembly", "terminal_status": "completed"},
                            {"task_id": "2", "command": "download", "terminal_status": "completed"},
                        ],
                        "proof_lineage": [_lineage("2", "tx-unverifiable")],
                    },
                    {
                        "transaction_id": "tx-invalid",
                        "callback_id": "2",
                        "capability": "execute-as-local-admin",
                        "child_tasks": [
                            {"task_id": "3", "command": "wmiexecute", "terminal_status": "completed"},
                            {"task_id": "4", "command": "cat", "terminal_status": "completed"},
                            {"task_id": "5", "command": "rev2self", "terminal_status": "completed"},
                        ],
                        "proof_lineage": [_lineage("5", "tx-invalid")],
                    },
                    {
                        "transaction_id": "tx-valid",
                        "callback_id": "2",
                        "capability": "read-managed-local-admin-secret",
                        "child_tasks": [
                            {
                                "task_id": "6",
                                "command": "powerpick",
                                "terminal_status": "completed",
                                "raw_output_sha256": "c" * 64,
                            },
                        ],
                        "proof_lineage": [_lineage("6", "tx-valid", with_v2_commitments=True)],
                    },
                ],
            }
        ],
    )


def test_phase12_audit_labels_rows_without_rewriting_source(tmp_path):
    source = _fixture_source(tmp_path)
    before = source.read_bytes()
    audit = build_phase12_proof_binding_audit(
        repo_root=tmp_path,
        source_artifacts=[source],
        generated_at="2026-07-16T00:00:00+00:00",
    )

    assert source.read_bytes() == before
    assert audit["totals"]["terminal_proof_count"] == 3
    assert audit["totals"]["status_counts"] == {
        "invalid": 1,
        "unverifiable": 1,
        "valid": 1,
    }
    reasons = {row["transaction_id"]: row["reason_code"] for row in audit["rows"]}
    assert reasons["tx-invalid"] == "proof_bound_to_cleanup_last_child"
    assert reasons["tx-unverifiable"] == "retained_raw_task_result_or_v2_commitment_missing"
    assert reasons["tx-valid"] == "retained_exact_task_result_and_v2_commitments_present"
    assert validate_phase12_proof_binding_audit(audit)["passes_gate"] is True


def test_phase12_audit_keeps_historical_authorization_out_of_band(tmp_path):
    audit = build_phase12_proof_binding_audit(
        repo_root=tmp_path,
        source_artifacts=[_fixture_source(tmp_path)],
        generated_at="2026-07-16T00:00:00+00:00",
    )

    auth = audit["historical_authorization_provenance"]
    assert [row["phase"] for row in auth] == list(range(0, 11))
    assert all(row["provenance_status"] == "out_of_band" for row in auth)
    assert all(row["prospective_manifest_binding_status"] == "unavailable" for row in auth)
    assert all(row["prospective_manifest_synthesized"] is False for row in auth)
    assert all("manifest_id" not in row for row in auth)


def test_phase12_candidate_inventory_is_blocked_until_later_seal(tmp_path):
    audit = build_phase12_proof_binding_audit(
        repo_root=tmp_path,
        source_artifacts=[_fixture_source(tmp_path)],
        generated_at="2026-07-16T00:00:00+00:00",
    )
    inventory = build_candidate_effect_path_inventory(audit, generated_at="2026-07-16T00:00:00+00:00")

    assert inventory["activation_status"] == "blocked_pending_phase16_seal_and_phase17_final_boundary_coverage"
    assert {row["capability"] for row in inventory["paths"]} == {
        "collect-graph",
        "execute-as-local-admin",
        "read-managed-local-admin-secret",
    }
    assert all(row["activation_authorized"] is False for row in inventory["paths"])
    assert validate_candidate_effect_path_inventory(inventory)["passes_gate"] is True


def test_phase12_json_sidecar_records_written_hash(tmp_path):
    artifact = tmp_path / "artifact.json"
    output = write_json_with_sha256(artifact, {"kind": "fixture", "rows": []})

    assert artifact.exists()
    assert Path(output["sidecar"]).read_text(encoding="utf-8").strip().endswith("artifact.json")
    assert output["sha256"] in Path(output["sidecar"]).read_text(encoding="utf-8")
