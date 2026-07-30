"""Focused tests for the bounded packet-backed selector experiment."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.hillclimb import policy_replay_corpus as corpus
from ai.hillclimb import policy_replay_selector_experiment as experiment
from ai.langgraph import policy


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frontier(*, equal_wait: bool = False) -> list[dict]:
    return [
        {
            "name": "route-a",
            "target": "target=a",
            "preconditions": ["ready"],
            "effects": ["effect:a"],
            "operational_cost": {
                "interaction_class": "propagation-bound",
                "execution_scope": "policy",
                "requires_propagation_wait": True,
                "expected_wait_seconds": 0 if equal_wait else 120,
                "wait_reasons": ["refresh"],
            },
            "reason": "delayed branch",
        },
        {
            "name": "route-b",
            "target": "target=b",
            "preconditions": ["ready"],
            "effects": ["effect:b"],
            "operational_cost": {
                "interaction_class": "direct",
                "execution_scope": "direct",
                "requires_propagation_wait": False,
                "expected_wait_seconds": 0,
                "wait_reasons": [],
            },
            "reason": "immediate branch",
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
            "achieved_effects": ["state:ready"],
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


def _write_inputs(tmp_path: Path, *, equal_wait: bool = False) -> tuple[Path, Path]:
    frontier = _frontier(equal_wait=equal_wait)
    frontier_hash = _frontier_hash(frontier)
    packet, packet_hash = _packet(frontier)
    matrix_rows = [
        _row(
            policy_mode="symbolic",
            frontier_hash=frontier_hash,
            selected_capability="route-a",
            selected_target="target=a",
            semantic_transaction_count=4,
        ),
        _row(
            policy_mode="llm",
            frontier_hash=frontier_hash,
            selected_capability="route-b",
            selected_target="target=b",
            semantic_transaction_count=3,
        ),
        _row(
            policy_mode="hybrid",
            frontier_hash=frontier_hash,
            selected_capability="route-b",
            selected_target="target=b",
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
                selected_capability="route-b",
                selected_target="target=b",
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


def test_selector_uses_blocked_effect_overlap_before_visible_wait():
    packet, _packet_hash = _packet([
        {
            "name": "slow",
            "target": "target=slow",
            "preconditions": [],
            "effects": ["effect:slow"],
            "operational_cost": {"expected_wait_seconds": 30},
            "reason": "",
        },
        {
            "name": "retry",
            "target": "target=retry",
            "preconditions": [],
            "effects": ["effect:retry"],
            "operational_cost": {"expected_wait_seconds": 0},
            "reason": "",
        },
        {
            "name": "repair",
            "target": "target=repair",
            "preconditions": [],
            "effects": ["effect:repair"],
            "operational_cost": {"expected_wait_seconds": 0},
            "reason": "",
        },
    ])
    packet["normalized_state"]["recent_outcomes"] = [{
        "capability": "prior",
        "target": "target=retry",
        "effect": "effect:retry",
        "status": "blocked",
    }]

    index, ranking = experiment.select_blocked_effect_aware_visible_cost(packet)

    assert index == 2
    assert ranking[1]["blocked_effect_overlap_count"] == 1
    assert ranking[2]["blocked_effect_overlap_count"] == 0
    assert ranking[2]["expected_wait_seconds"] == 0


def test_selector_experiment_preserves_live_order_and_reports_bounded_claim(tmp_path):
    source_path, _packet_source = _write_inputs(tmp_path)
    exported = corpus.export_corpus(source_manifest_path=source_path, results_root=tmp_path)
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(exported), encoding="utf-8")

    report = experiment.run_selector_experiment(
        corpus_path=corpus_path,
        source_manifest_path=source_path,
        results_root=tmp_path,
    )

    assert report["passes_gate"] is True
    assert report["selector"]["name"] == experiment.SELECTOR_NAME
    assert report["aggregate"]["general_selector_claim_supported"] is False
    assert report["aggregate"]["adds_discrimination_over_lowest_visible_wait"] is False
    case = report["cases"][0]
    assert case["experimental_policy_order"] == [["hybrid", "llm"], ["symbolic"]]
    assert case["checks"]["experimental_selector_matches_live_learned_choices"] is True
    assert case["checks"]["experimental_selector_preserves_learned_policy_tie"] is True


def test_selector_experiment_reports_failure_without_failing_the_evaluation_gate(tmp_path):
    source_path, _packet_source = _write_inputs(tmp_path, equal_wait=True)
    exported = corpus.export_corpus(source_manifest_path=source_path, results_root=tmp_path)
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(exported), encoding="utf-8")

    report = experiment.run_selector_experiment(
        corpus_path=corpus_path,
        source_manifest_path=source_path,
        results_root=tmp_path,
    )

    assert report["passes_gate"] is True
    assert report["selector_findings"]["experimental_selector_matches_live_learned_choices"] is False
    assert report["selector_findings"]["experimental_selector_preserves_expected_order"] is False
    assert report["selector_findings"]["experimental_selector_preserves_learned_policy_tie"] is True
    assert report["aggregate"]["any_selector_improves_over_lowest_visible_wait"] is False
    assert report["aggregate"]["case_ids_where_experimental_selector_diverges_from_live_learned_choices"] == ["case"]


def test_selector_source_does_not_embed_current_corpus_branch_literals():
    source = Path(experiment.__file__).read_text(encoding="utf-8")
    stored_corpus = json.loads(experiment.DEFAULT_CORPUS_PATH.read_text(encoding="utf-8"))
    forbidden = set()
    for case in stored_corpus["cases"]:
        forbidden.add(case["id"])
        forbidden.add(case["scenario"])
        for candidate in case["decision_packet"]["admissible_frontier"]:
            forbidden.add(candidate["name"])
            forbidden.add(candidate["target"])

    assert all(value not in source for value in forbidden)


def test_selector_experiment_rejects_invalid_packet_corpus_before_scoring(tmp_path):
    source_path, _packet_source = _write_inputs(tmp_path)
    exported = corpus.export_corpus(source_manifest_path=source_path, results_root=tmp_path)
    exported["cases"][0]["decision_packet"]["admissible_frontier"][0]["name"] = "drifted-route"
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(exported), encoding="utf-8")

    with pytest.raises(experiment.SelectorExperimentError, match="validation gate"):
        experiment.run_selector_experiment(
            corpus_path=corpus_path,
            source_manifest_path=source_path,
            results_root=tmp_path,
        )
