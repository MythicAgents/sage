"""Focused tests for the runtime target-value architecture decision gate."""
from __future__ import annotations

from copy import deepcopy

from ai.hillclimb import policy_replay_selector_experiment as selector_experiment
from ai.hillclimb import target_value_census
from ai.hillclimb import target_value_runtime_decision as decision


def test_target_value_runtime_decision_prefers_eval_only_downstream_scoring():
    report = decision.run_target_value_runtime_decision()

    assert report["passes_gate"] is True
    assert report["checks"]["multiple_unrelated_families_already_express_target_asymmetry"] is True
    assert report["checks"]["packet_selector_gap_is_observed"] is True
    assert report["decision"]["add_runtime_target_value_abstraction"] is False
    assert report["decision"]["recommended_next_move"] == "build_cheap_unseen_candidate_evaluator"
    assert report["decision"]["runtime_change_scope"] == "none"
    assert report["evidence"]["selector_any_improves_over_lowest_visible_wait"] is False


def test_target_value_runtime_decision_fails_closed_without_multi_family_evidence():
    census_report = deepcopy(target_value_census.run_target_value_census())
    census_report["aggregate"]["naturally_asymmetric_families"] = ["gpo-directory"]
    selector_report = selector_experiment.run_selector_experiment()

    report = decision.run_target_value_runtime_decision(
        census_report=census_report,
        selector_report=selector_report,
    )

    assert report["passes_gate"] is False
    assert report["checks"]["multiple_unrelated_families_already_express_target_asymmetry"] is False

