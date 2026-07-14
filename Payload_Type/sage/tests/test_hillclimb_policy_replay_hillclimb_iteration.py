"""Focused tests for the first bounded replay hill-climb iteration."""
from __future__ import annotations

from pathlib import Path

from ai.hillclimb import policy_replay_hillclimb_iteration as iteration


def test_hillclimb_iteration_keeps_single_variable_objective_effect_candidate():
    report = iteration.run_hillclimb_iteration()

    assert report["passes_gate"] is True
    assert report["checks"]["candidate_is_single_variable_diff"] is True
    assert report["aggregate"]["baseline_total_score"] == 8.0
    assert report["aggregate"]["candidate_total_score"] == 7.0
    assert report["aggregate"]["score_delta"] == 1.0
    assert report["aggregate"]["changed_case_ids"] == ["gpo-dc-scope-late-blocker"]
    assert report["aggregate"]["improved_case_ids"] == ["gpo-dc-scope-late-blocker"]
    assert report["decision"]["keep_candidate"] is True
    assert report["decision"]["runtime_promotion_authorized"] is False
    assert report["iteration"]["verifier_hash"].startswith("sha256:")


def test_hillclimb_iteration_reverts_when_threshold_is_not_cleared():
    report = iteration.run_hillclimb_iteration(acceptance_threshold=2.0)

    assert report["passes_gate"] is True
    assert report["aggregate"]["score_delta"] == 1.0
    assert report["decision"]["keep_candidate"] is False
    assert report["decision"]["action"] == "revert"


def test_hillclimb_candidate_source_has_no_current_corpus_case_literals():
    source = Path(iteration.__file__).read_text(encoding="utf-8")

    assert "replication-visible-cost" not in source
    assert "ca-export-replanning" not in source
    assert "gpo-dc-scope-late-blocker" not in source
    assert "range.local" not in source
