"""Focused tests for the offline GPO DC-scope late-blocker authorization audit."""
from __future__ import annotations

from copy import deepcopy

from ai.hillclimb import gpo_dc_scope_late_blocker_authorization as authorization
from ai.hillclimb import gpo_dc_scope_late_blocker_contract as contract


def test_gpo_dc_scope_late_blocker_authorization_allows_live_surface_build():
    report = authorization.run_gpo_dc_scope_late_blocker_authorization_audit()

    assert report["passes_gate"] is True
    assert report["authorization"]["live_benchmark_authorized"] is True
    assert report["authorization"]["failed_evidence"] == []
    assert report["decision_evidence"]["dedicated_contract_passes"] is True
    assert report["decision_evidence"]["current_selectors_miss_unique_best_target"] is True
    assert report["decision_evidence"]["generic_fact_projection_supports_best_target"] is True
    assert report["decision_evidence"]["current_gpo_proof_path_supports_best_target"] is True
    assert report["decision_evidence"]["blocked_lane_uses_current_verifier_and_execution_path"] is True
    assert report["decision_evidence"]["existing_purpose_range_substrate_validates"] is True
    assert report["fact_support"]["supported"] is True
    assert report["gpo_proof_support"]["supported"] is True
    assert report["blocker_support"]["supported"] is True
    assert report["substrate_support"]["supported"] is True


def test_gpo_dc_scope_late_blocker_authorization_fails_closed_when_contract_collapses():
    contract_report = deepcopy(contract.validate_gpo_dc_scope_late_blocker_contract())
    contract_report["checks"]["equal_visible_operational_cost"] = False
    contract_report["passes_gate"] = False

    report = authorization.run_gpo_dc_scope_late_blocker_authorization_audit(contract_report)

    assert report["passes_gate"] is True
    assert report["authorization"]["live_benchmark_authorized"] is False
    assert "dedicated_contract_passes" in report["authorization"]["failed_evidence"]
    assert "dedicated_contract_decisive_checks_pass" in report["authorization"]["failed_evidence"]
    assert report["checks"]["anti_authorization_fails_closed"] is True
