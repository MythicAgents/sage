"""Focused tests for the hermetic unseen-candidate evaluator."""
from __future__ import annotations

from ai.hillclimb import policy_replay_unseen_candidate_evaluator as evaluator


def test_unseen_candidate_evaluator_scores_only_unobserved_branches_synthetically():
    report = evaluator.run_unseen_candidate_evaluator()

    assert report["passes_gate"] is True
    assert report["checks"]["scores_at_least_one_unseen_candidate"] is True
    assert report["aggregate"]["synthetically_scored_unseen_branch_count"] >= 1
    assert report["aggregate"]["promotion_gate_required_before_live_claim"] is True
    cases = {case["id"]: case for case in report["cases"]}
    ca_export = cases["ca-export-replanning"]
    assert ca_export["synthetically_scored_unseen_frontier_indices"] == [2]
    branch = ca_export["branch_reports"][2]
    assert branch["live_observed"] is False
    assert branch["score_source"] == evaluator.SYNTHETIC_SCORE_SOURCE
    assert branch["effective_score"] is not None
    assert branch["synthetic_score_is_ground_truth"] is False


def test_unseen_candidate_evaluator_preserves_live_observed_scores():
    report = evaluator.run_unseen_candidate_evaluator()

    for case in report["cases"]:
        for branch in case["branch_reports"]:
            if branch["live_observed"]:
                assert branch["score_source"] == evaluator.LIVE_SCORE_SOURCE
                assert branch["effective_score"] == branch["live_observed_metric"]

