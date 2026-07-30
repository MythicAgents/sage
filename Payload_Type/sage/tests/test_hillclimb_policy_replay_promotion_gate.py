"""Focused tests for the held-out transfer and promotion gate."""
from __future__ import annotations

from ai.hillclimb import policy_replay_promotion_gate as promotion_gate
import pytest  # noqa: E402


@pytest.mark.pinned_policy_rows
def test_promotion_gate_relabels_known_synthetic_row_and_blocks_runtime_promotion():
    report = promotion_gate.run_promotion_gate()

    assert report["passes_gate"] is True
    assert report["training"]["training_families"] == [
        "gpo-directory",
        "managed-local-admin",
        "replication-kerberos",
    ]
    assert report["held_out"]["family"] == "replication-kerberos"
    assert report["held_out"]["evidence_classification"] == "known_synthetic_model_consistency"
    assert report["held_out"]["transfer_passes"] is False
    assert report["held_out"]["candidate"]["selected_index"] in report["held_out"]["best_indices"]
    assert report["held_out_budget"]["consumed_cases"] == 1
    assert report["held_out_budget"]["sealed_before_iteration"] is False
    assert report["observations"]["diagnostic_surface_is_structurally_different"] is False
    assert report["typed_verdict"]["promotion_evidence_passed"] is False
    assert report["live_promotion_gate"]["runtime_promotion_authorized"] is False
    assert set(report["live_promotion_gate"]["required_ground_truth_checks"]) == set(
        promotion_gate.LIVE_PROMOTION_REQUIRED_CHECKS
    )


@pytest.mark.pinned_policy_rows
def test_promotion_gate_never_counts_same_family_synthetic_row_as_transfer():
    report = promotion_gate.run_promotion_gate(held_out_surface_id="same-domain-gpo-dc-scope")

    assert report["passes_gate"] is True
    assert report["observations"]["diagnostic_surface_is_structurally_different"] is False
    assert report["observations"]["diagnostic_surface_is_not_a_renamed_training_copy"] is False
    assert report["held_out"]["transfer_passes"] is False
    assert report["checks"]["synthetic_holdout_is_not_transfer_evidence"] is True
