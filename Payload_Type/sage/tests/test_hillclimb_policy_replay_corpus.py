"""Focused tests for packet-backed policy replay corpus export and validation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.hillclimb import policy_replay_corpus as corpus
from ai.langgraph import policy


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frontier() -> list[dict]:
    return [
        {
            "name": "detour",
            "target": "target=detour",
            "preconditions": ["ready"],
            "effects": ["effect:detour"],
            "operational_cost": {
                "interaction_class": "propagation-bound",
                "execution_scope": "domain-policy",
                "requires_propagation_wait": True,
                "expected_wait_seconds": 120,
                "wait_reasons": ["group-policy-refresh"],
            },
            "reason": "slower branch",
        },
        {
            "name": "repair",
            "target": "target=repair",
            "preconditions": ["ready"],
            "effects": ["effect:repair"],
            "operational_cost": {
                "interaction_class": "direct",
                "execution_scope": "direct",
                "requires_propagation_wait": False,
                "expected_wait_seconds": 0,
                "wait_reasons": [],
            },
            "reason": "faster branch",
        },
    ]


def _frontier_hash(frontier: list[dict]) -> str:
    candidates = [
        SimpleNamespace(
            name=item["name"],
            target=item["target"],
            preconditions=item["preconditions"],
            effects=item["effects"],
            operational_cost=item["operational_cost"],
            reason=item["reason"],
        )
        for item in frontier
    ]
    return policy.candidate_hash(candidates)


def _packet(frontier: list[dict]) -> tuple[dict, str]:
    packet = {
        "schema_version": 1,
        "objective": "test objective",
        "normalized_state": {
            "achieved_effects": ["graph-built:test.local|baseline"],
            "footholds": [],
            "graph_facts": ["ready"],
            "recent_outcomes": [],
        },
        "admissible_frontier": frontier,
        "prior_decisions": [],
        "budgets": {"max_cycles": 60, "wall_clock_budget_s": 2700, "token_budget": 3000000},
        "selection_contract": "admissible_frontier",
        "candidate_hash": _frontier_hash(frontier),
    }
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return packet, f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _row(
    *,
    policy_mode: str,
    frontier_hash: str,
    selected_capability: str,
    selected_target: str,
    semantic_transaction_count: int,
    packet: dict | None = None,
    packet_hash: str = "",
) -> dict:
    decision = {
        "decision_id": f"{policy_mode}-decisive",
        "candidate_hash": frontier_hash,
        "candidate_count": 2,
        "selected_capability": selected_capability,
        "selected_target": selected_target,
    }
    if packet:
        decision["decision_packet"] = packet
        decision["decision_packet_hash"] = packet_hash
    return {
        "scenario": "test-scenario",
        "configured_policy_mode": policy_mode,
        "policy_mode": policy_mode,
        "objective_proven": True,
        "clean_stop": True,
        "policy_identity_valid": True,
        "backend_provenance_complete": True,
        "semantic_policy_coverage": 1.0,
        "policy_switches": [],
        "semantic_transaction_count": semantic_transaction_count,
        "decisions": [decision],
        "transactions": [{"decision_id": decision["decision_id"]}],
    }


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    frontier = _frontier()
    frontier_hash = _frontier_hash(frontier)
    packet, packet_hash = _packet(frontier)
    matrix_rows = [
        _row(
            policy_mode="symbolic",
            frontier_hash=frontier_hash,
            selected_capability="detour",
            selected_target="target=detour",
            semantic_transaction_count=4,
        ),
        _row(
            policy_mode="llm",
            frontier_hash=frontier_hash,
            selected_capability="repair",
            selected_target="target=repair",
            semantic_transaction_count=3,
        ),
        _row(
            policy_mode="hybrid",
            frontier_hash=frontier_hash,
            selected_capability="repair",
            selected_target="target=repair",
            semantic_transaction_count=3,
        ),
    ]
    matrix = tmp_path / "matrix.jsonl"
    matrix.write_text("\n".join(json.dumps(row) for row in matrix_rows) + "\n", encoding="utf-8")
    packet_source = tmp_path / "packet.jsonl"
    packet_source.write_text(
        json.dumps(
            _row(
                policy_mode="hybrid",
                frontier_hash=frontier_hash,
                selected_capability="repair",
                selected_target="target=repair",
                semantic_transaction_count=3,
                packet=packet,
                packet_hash=packet_hash,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    calibration_manifest = {
        "schema_version": 1,
        "name": "test-calibration",
        "artifacts": [{
            "id": "matrix",
            "relative_path": matrix.name,
            "sha256": _sha256(matrix),
        }],
        "cases": [{
            "id": "case",
            "artifact_id": "matrix",
            "scenario": "test-scenario",
            "decisive_frontier_hash": frontier_hash,
            "expected_candidate_count": 2,
            "expected_rows_per_policy": 1,
            "choice_fields": ["selected_capability"],
            "metric": {
                "kind": "semantic_transaction_count",
                "direction": "lower_is_better",
            },
            "expected_policy_order": [["llm", "hybrid"], ["symbolic"]],
            "tie_tolerance": 0.0,
        }],
    }
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps(calibration_manifest), encoding="utf-8")
    source_manifest = {
        "schema_version": 1,
        "name": "test-corpus",
        "calibration_manifest": calibration_path.name,
        "packet_sources": [{
            "case_id": "case",
            "relative_path": packet_source.name,
            "sha256": _sha256(packet_source),
            "policy_mode": "hybrid",
            "decision_packet_hash": packet_hash,
        }],
    }
    source_path = tmp_path / "sources.json"
    source_path.write_text(json.dumps(source_manifest), encoding="utf-8")
    return source_path, packet_source


def test_policy_replay_corpus_export_and_validate_preserves_frontier_order(tmp_path):
    source_path, _packet_source = _write_inputs(tmp_path)
    exported = corpus.export_corpus(source_manifest_path=source_path, results_root=tmp_path)
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(exported), encoding="utf-8")

    report = corpus.validate_corpus(
        corpus_path=corpus_path,
        source_manifest_path=source_path,
        results_root=tmp_path,
    )

    assert report["passes_gate"] is True
    case = report["cases"][0]
    assert case["observed_policy_order"] == [["hybrid", "llm"], ["symbolic"]]
    assert case["checks"]["reconstructed_frontier_hash_matches_case"] is True
    assert case["checks"]["pairwise_agreement"] is True
    assert case["checks"]["branch_outcome_scope_matches_live_observation"] is True
    assert case["branch_outcome_scope"]["can_score_unseen_candidates"] is False
    assert case["branch_outcome_scope"]["frontier_indices_with_live_observed_outcomes"] == [0, 1]
    assert case["branch_outcome_scope"]["frontier_indices_without_live_observed_outcomes"] == []
    scores = {item["selector"]: item for item in case["selector_scores"]}
    assert scores["first_admissible"]["selected_capability"] == "detour"
    assert scores["lowest_visible_wait"]["selected_capability"] == "repair"
    assert all(item["scored_from_live_observation"] for item in scores.values())


def test_policy_replay_corpus_branch_scope_marks_unobserved_frontier_indices():
    frontier = _frontier() + [{
        "name": "unseen",
        "target": "target=unseen",
        "preconditions": ["ready"],
        "effects": ["effect:unseen"],
        "operational_cost": {"expected_wait_seconds": 0},
        "reason": "not chosen live",
    }]
    scope = corpus._branch_outcome_scope(
        frontier,
        {
            "symbolic": {
                "selected_capability": "detour",
                "selected_target": "target=detour",
            },
            "llm": {
                "selected_capability": "repair",
                "selected_target": "target=repair",
            },
            "hybrid": {
                "selected_capability": "repair",
                "selected_target": "target=repair",
            },
        },
    )

    assert scope["kind"] == "live_observed_frontier_choices_only"
    assert scope["can_score_unseen_candidates"] is False
    assert scope["frontier_indices_with_live_observed_outcomes"] == [0, 1]
    assert scope["frontier_indices_without_live_observed_outcomes"] == [2]
    assert scope["frontier_coverage"][2]["live_observed"] is False


def test_policy_replay_corpus_validation_fails_when_packet_frontier_drifts(tmp_path):
    source_path, _packet_source = _write_inputs(tmp_path)
    exported = corpus.export_corpus(source_manifest_path=source_path, results_root=tmp_path)
    exported["cases"][0]["decision_packet"]["admissible_frontier"][0]["name"] = "drifted-detour"
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(exported), encoding="utf-8")

    report = corpus.validate_corpus(
        corpus_path=corpus_path,
        source_manifest_path=source_path,
        results_root=tmp_path,
    )

    assert report["passes_gate"] is False
    checks = report["cases"][0]["checks"]
    assert checks["packet_hash_recomputes"] is False
    assert checks["reconstructed_frontier_hash_matches_case"] is False


def test_policy_replay_corpus_validation_rejects_packet_source_artifact_drift(tmp_path):
    source_path, packet_source = _write_inputs(tmp_path)
    exported = corpus.export_corpus(source_manifest_path=source_path, results_root=tmp_path)
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(exported), encoding="utf-8")
    packet_source.write_text(packet_source.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(corpus.CorpusError, match="packet source hash mismatch"):
        corpus.validate_corpus(
            corpus_path=corpus_path,
            source_manifest_path=source_path,
            results_root=tmp_path,
        )
