"""Focused tests for the offline same-domain GPO DC-scope late-blocker contract."""
from __future__ import annotations

from ai.hillclimb import gpo_dc_scope_late_blocker_contract as contract


def test_gpo_dc_scope_late_blocker_contract_proves_two_target_recovery_split():
    report = contract.validate_gpo_dc_scope_late_blocker_contract()

    assert report["passes_gate"] is True
    assert report["checks"]["post_blocker_frontier_is_exact_two_gpo_targets"] is True
    assert report["checks"]["equal_visible_operational_cost"] is True
    assert report["checks"]["asymmetric_modeled_downstream_cost"] is True
    assert report["checks"]["unique_best_target"] is True
    assert report["checks"]["unique_best_target_is_dc_scoped"] is True
    assert report["modeled_transaction_costs"] == [2, 1]
    assert report["best_indices"] == [1]
    assert [item["name"] for item in report["post_blocker_frontier"]] == [
        contract.TARGET_CAPABILITY,
        contract.TARGET_CAPABILITY,
    ]
    assert all(item["selected_is_best"] is False for item in report["selector_scores"])
    assert report["authorization"]["live_benchmark_authorized"] is False


def test_gpo_dc_scope_late_blocker_contract_blocks_only_certificate_auth_effect():
    report = contract.validate_gpo_dc_scope_late_blocker_contract()

    assert report["checks"]["shared_prefix_extends_existing_replanning_lane"] is True
    assert report["checks"]["blocker_is_verifier_backed_and_terminal"] is True
    assert [item["name"] for item in report["shared_path"]] == [
        "read-managed-local-admin-secret",
        "use-managed-local-admin-secret",
        "execute-as-local-admin",
        "adcs-ca-private-key-export",
    ]
    assert report["blocker"]["action"]["name"] == contract.BLOCKED_ACTION
    assert report["blocker"]["verification"]["verdict"] == "blocked"
    assert report["blocker"]["blocked_effect"].startswith(contract.BLOCKED_EFFECT_PREFIX)
    assert report["blocker"]["blocked_effect"] != "da:range.local"
