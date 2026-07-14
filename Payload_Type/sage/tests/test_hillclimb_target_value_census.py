"""Focused tests for the offline target-value census."""
from __future__ import annotations

from ai.hillclimb import target_value_census as census


def test_target_value_census_classifies_current_same_capability_surfaces():
    report = census.run_target_value_census()
    surfaces = {surface["id"]: surface for surface in report["surfaces"]}

    assert report["passes_gate"] is True
    assert report["checks"]["covers_required_capability_families"] is True
    assert surfaces["multi-domain-dcsync-krbtgt"]["diagnosis"]["classification"] == census.NATURALLY_ASYMMETRIC
    assert surfaces["multi-domain-dcsync-krbtgt"]["modeled_transaction_costs"] == [3, 2]
    assert surfaces["same-domain-gpo-dc-scope"]["diagnosis"]["classification"] == census.NATURALLY_ASYMMETRIC
    assert surfaces["same-domain-gpo-dc-scope"]["modeled_transaction_costs"] == [2, 1]
    assert surfaces["adcs-enrollment-account-targets"]["diagnosis"]["classification"] == census.COLLAPSED_EQUAL_COST
    assert surfaces["adcs-enrollment-account-targets"]["modeled_transaction_costs"] == [2, 2]
    assert surfaces["same-domain-gpo-unscoped-targets"]["diagnosis"]["classification"] == census.INDISTINGUISHABLE_FROM_CURRENT_FACTS
    assert surfaces["same-domain-gpo-unscoped-targets"]["modeled_transaction_costs"] == [4, 4]
    assert surfaces["managed-local-admin-host-targets"]["diagnosis"]["classification"] == census.INDISTINGUISHABLE_FROM_CURRENT_FACTS
    assert surfaces["managed-local-admin-host-targets"]["modeled_transaction_costs"] == [5, 5]


def test_target_value_census_recommends_existing_asymmetric_surface_before_new_abstraction():
    report = census.run_target_value_census()
    recommendation = report["recommendation"]

    assert report["checks"]["has_naturally_asymmetric_surface"] is True
    assert report["checks"]["has_collapsed_equal_cost_surface"] is True
    assert report["checks"]["has_indistinguishable_surface"] is True
    assert recommendation["generic_target_value_abstraction_supported"] is False
    assert recommendation["prefer_naturally_asymmetric_surface"] is True
    assert recommendation["recommended_next_move"] == "choose_naturally_asymmetric_live_surface_first"
    assert recommendation["live_benchmark_authorized"] is False
    assert recommendation["candidate_surface_ids"] == [
        "multi-domain-dcsync-krbtgt",
        "same-domain-gpo-dc-scope",
    ]
