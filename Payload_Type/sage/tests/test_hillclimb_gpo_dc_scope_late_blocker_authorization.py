"""Focused tests for the offline GPO DC-scope late-blocker authorization audit."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

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
    blocker_checks = report["blocker_support"]["checks"]
    assert blocker_checks["blocked_action_uses_current_certificate_auth_path"] is True
    assert blocker_checks["explicit_pkinit_remains_pkinit_first"] is True
    assert blocker_checks["ticket_required_action_remains_pkinit_first"] is True
    assert report["blocker_support"]["plan_operations"][:4] == [
        "kerberos-ticket-list",
        "kerberos-context-service-proof",
        "adcs-certificate-forge",
        "certificate-schannel-ldap-proof",
    ]
    assert "certificate-pkinit-tgt" in report["blocker_support"]["explicit_pkinit_plan_operations"]
    assert "certificate-pkinit-tgt" in report["blocker_support"]["ticket_required_plan_operations"]
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


@pytest.mark.parametrize(
    ("collapsed_route", "failed_check"),
    [
        ("explicit-pkinit", "explicit_pkinit_remains_pkinit_first"),
        ("ticket-required", "ticket_required_action_remains_pkinit_first"),
    ],
)
def test_gpo_dc_scope_late_blocker_authorization_fails_closed_when_pkinit_route_collapses(
    monkeypatch,
    collapsed_route,
    failed_check,
):
    original = authorization.capabilities.build_capability_execution_plan

    def collapse_route(action, inputs):
        inputs = dict(inputs)
        if collapsed_route == "explicit-pkinit" and inputs.get("certificate_auth_method") == "pkinit":
            inputs.pop("certificate_auth_method")
        elif collapsed_route == "ticket-required" and any(
            str(effect).startswith("kerberos-context:")
            for effect in list(getattr(action, "effects", []) or [])
        ):
            action = replace(
                action,
                effects=[
                    effect
                    for effect in list(getattr(action, "effects", []) or [])
                    if not str(effect).startswith("kerberos-context:")
                ],
            )
        return original(action, inputs)

    monkeypatch.setattr(
        authorization.capabilities,
        "build_capability_execution_plan",
        collapse_route,
    )

    report = authorization.run_gpo_dc_scope_late_blocker_authorization_audit()

    assert report["passes_gate"] is True
    assert report["authorization"]["live_benchmark_authorized"] is False
    assert report["blocker_support"]["checks"][failed_check] is False
    assert "blocked_lane_uses_current_verifier_and_execution_path" in report["authorization"]["failed_evidence"]
