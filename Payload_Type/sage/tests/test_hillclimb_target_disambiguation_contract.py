"""Focused tests for the offline target-disambiguation contract audit."""
from __future__ import annotations

from ai.hillclimb import target_disambiguation_contract as contract


def test_adcs_proposal_is_not_a_real_target_discriminator_under_current_model():
    report = contract.validate_target_disambiguation_contract()
    proposal = report["proposed_surface"]

    assert report["passes_gate"] is True
    assert report["authorization"]["live_benchmark_authorized"] is False
    assert proposal["id"] == contract.ADCS_PROPOSAL_ID
    assert proposal["checks"]["same_capability_multi_target_frontier"] is True
    assert proposal["checks"]["equal_visible_cost"] is True
    assert proposal["checks"]["asymmetric_downstream_value"] is False
    assert proposal["checks"]["unique_best_target"] is False
    assert proposal["modeled_transaction_costs"] == [2, 2]


def test_control_surface_proves_audit_detects_real_target_value_difference():
    report = contract.validate_target_disambiguation_contract()
    control = report["control_surface"]

    assert control["id"] == contract.CONTROL_ID
    assert control["checks"]["same_capability_multi_target_frontier"] is True
    assert control["checks"]["equal_visible_cost"] is True
    assert control["checks"]["asymmetric_downstream_value"] is True
    assert control["checks"]["unique_best_target"] is True
    assert control["modeled_transaction_costs"] == [3, 2]
    assert control["best_indices"] == [1]
    assert all(item["selected_is_best"] is False for item in control["selector_scores"])
    assert report["checks"]["current_selectors_collapse_on_control_shape"] is True
