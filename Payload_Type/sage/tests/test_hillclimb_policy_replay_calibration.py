"""Focused tests for offline policy replay calibration."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.hillclimb import policy_replay_calibration as calibration


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row(
    *,
    policy_mode: str,
    scenario: str,
    frontier_hash: str,
    selected_capability: str,
    candidate_count: int,
    semantic_transaction_count: int,
    transactions_from_frontier: int,
) -> dict:
    decisions = [
        {
            "decision_id": f"{policy_mode}-collect",
            "candidate_hash": "sha256:collect",
            "candidate_count": 1,
            "selected_capability": "collect-graph",
            "selected_target": "test|baseline",
        },
        {
            "decision_id": f"{policy_mode}-decisive",
            "candidate_hash": frontier_hash,
            "candidate_count": candidate_count,
            "selected_capability": selected_capability,
            "selected_target": "target=test",
        },
    ]
    for index in range(transactions_from_frontier - 1):
        decisions.append({
            "decision_id": f"{policy_mode}-tail-{index}",
            "candidate_hash": f"sha256:tail-{index}",
            "candidate_count": 1,
            "selected_capability": f"tail-{index}",
            "selected_target": "target=test",
        })
    transactions = [
        {"decision_id": decision["decision_id"]}
        for decision in decisions
    ]
    return {
        "scenario": scenario,
        "configured_policy_mode": policy_mode,
        "policy_mode": policy_mode,
        "objective_proven": True,
        "clean_stop": True,
        "policy_identity_valid": True,
        "backend_provenance_complete": True,
        "semantic_policy_coverage": 1.0,
        "policy_switches": [],
        "semantic_transaction_count": semantic_transaction_count,
        "decisions": decisions,
        "transactions": transactions,
    }


def _write_manifest(tmp_path: Path, rows: list[dict], *, expected_order: list[list[str]]) -> Path:
    artifact = tmp_path / "rows.jsonl"
    artifact.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "name": "test-calibration",
        "artifacts": [{
            "id": "rows",
            "relative_path": artifact.name,
            "sha256": _sha256(artifact),
        }],
        "cases": [{
            "id": "case",
            "artifact_id": "rows",
            "scenario": "test-scenario",
            "decisive_frontier_hash": "sha256:frontier",
            "expected_candidate_count": 2,
            "expected_rows_per_policy": 1,
            "choice_fields": ["selected_capability"],
            "metric": {
                "kind": "transactions_from_frontier",
                "direction": "lower_is_better",
            },
            "expected_policy_order": expected_order,
            "tie_tolerance": 0.0,
        }],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_policy_replay_calibration_preserves_live_order_and_tie(tmp_path):
    rows = [
        _row(
            policy_mode="symbolic",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="detour",
            candidate_count=2,
            semantic_transaction_count=4,
            transactions_from_frontier=4,
        ),
        _row(
            policy_mode="llm",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="repair",
            candidate_count=2,
            semantic_transaction_count=3,
            transactions_from_frontier=3,
        ),
        _row(
            policy_mode="hybrid",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="repair",
            candidate_count=2,
            semantic_transaction_count=3,
            transactions_from_frontier=3,
        ),
    ]
    manifest = _write_manifest(tmp_path, rows, expected_order=[["llm", "hybrid"], ["symbolic"]])

    report = calibration.run_calibration(
        manifest_path=manifest,
        results_root=tmp_path,
        repeats=2,
    )

    assert report["passes_gate"] is True
    assert report["determinism"]["deterministic"] is True
    case = report["cases"][0]
    assert case["observed_policy_order"] == [["hybrid", "llm"], ["symbolic"]]
    assert case["checks"]["pairwise_agreement"] is True


def test_policy_replay_calibration_fails_when_a_declared_tie_becomes_a_fake_winner(tmp_path):
    rows = [
        _row(
            policy_mode="symbolic",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="detour",
            candidate_count=2,
            semantic_transaction_count=4,
            transactions_from_frontier=4,
        ),
        _row(
            policy_mode="llm",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="repair",
            candidate_count=2,
            semantic_transaction_count=2,
            transactions_from_frontier=2,
        ),
        _row(
            policy_mode="hybrid",
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="repair",
            candidate_count=2,
            semantic_transaction_count=3,
            transactions_from_frontier=3,
        ),
    ]
    manifest = _write_manifest(tmp_path, rows, expected_order=[["llm", "hybrid"], ["symbolic"]])

    report = calibration.run_calibration(
        manifest_path=manifest,
        results_root=tmp_path,
        repeats=1,
    )

    assert report["passes_gate"] is False
    assert report["cases"][0]["checks"]["expected_policy_order_preserved"] is False
    assert report["cases"][0]["checks"]["pairwise_agreement"] is False


def test_policy_replay_calibration_rejects_frozen_artifact_hash_drift(tmp_path):
    rows = [
        _row(
            policy_mode=policy_mode,
            scenario="test-scenario",
            frontier_hash="sha256:frontier",
            selected_capability="repair",
            candidate_count=2,
            semantic_transaction_count=3,
            transactions_from_frontier=3,
        )
        for policy_mode in ("symbolic", "llm", "hybrid")
    ]
    manifest = _write_manifest(tmp_path, rows, expected_order=[["symbolic", "llm", "hybrid"]])
    artifact = tmp_path / "rows.jsonl"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")

    with pytest.raises(calibration.CalibrationError, match="hash mismatch"):
        calibration.run_calibration(manifest_path=manifest, results_root=tmp_path)
