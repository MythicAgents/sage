"""Focused tests for the held-out transfer and promotion gate."""
from __future__ import annotations

from ai.hillclimb import policy_replay_promotion_gate as promotion_gate


def test_promotion_gate_uses_structurally_different_holdout_and_blocks_runtime_promotion():
    report = promotion_gate.run_promotion_gate()

    assert report["passes_gate"] is True
    assert report["training"]["training_families"] == ["gpo-directory"]
    assert report["held_out"]["family"] == "replication-kerberos"
    assert report["held_out"]["transfer_passes"] is True
    assert report["held_out"]["candidate"]["selected_index"] in report["held_out"]["best_indices"]
    assert report["held_out_budget"]["consumed_cases"] == 1
    assert report["held_out_budget"]["sealed_before_iteration"] is False
    assert report["live_promotion_gate"]["runtime_promotion_authorized"] is False
    assert set(report["live_promotion_gate"]["required_ground_truth_checks"]) == set(
        promotion_gate.LIVE_PROMOTION_REQUIRED_CHECKS
    )


def test_promotion_gate_rejects_same_family_holdout_as_transfer():
    report = promotion_gate.run_promotion_gate(held_out_surface_id="same-domain-gpo-dc-scope")

    assert report["passes_gate"] is False
    assert report["checks"]["held_out_surface_is_structurally_different"] is False
    assert report["checks"]["renamed_copy_is_not_counted_as_transfer"] is False

