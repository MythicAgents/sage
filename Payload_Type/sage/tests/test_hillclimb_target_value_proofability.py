"""Focused tests for the offline target-value proofability screen."""
from __future__ import annotations

from dataclasses import replace

from ai.hillclimb import target_value_proofability as proofability


def test_target_value_proofability_screen_prefers_same_domain_gpo_dc_scope():
    report = proofability.run_target_value_proofability_screen()
    candidates = {candidate["id"]: candidate for candidate in report["candidates"]}
    gpo = candidates[proofability.GPO_CANDIDATE_ID]
    dcsync = candidates[proofability.DCSYNC_CANDIDATE_ID]

    assert report["passes_gate"] is True
    assert gpo["ready_for_contract_build"] is True
    assert gpo["checks"]["current_selectors_choose_worse_target"] is True
    assert gpo["checks"]["generic_fact_support_exists"] is True
    assert gpo["checks"]["current_proof_path_exists"] is True
    assert gpo["checks"]["existing_substrate_can_be_reused"] is True
    assert dcsync["ready_for_contract_build"] is False
    assert dcsync["checks"]["current_selectors_choose_worse_target"] is True
    assert dcsync["checks"]["generic_fact_support_exists"] is True
    assert dcsync["checks"]["current_proof_path_exists"] is True
    assert dcsync["checks"]["existing_substrate_can_be_reused"] is False
    assert report["recommendation"]["recommended_candidate_id"] == proofability.GPO_CANDIDATE_ID
    assert report["recommendation"]["recommended_next_move"] == (
        "build_same_domain_gpo_dc_scope_late_blocker_contract"
    )
    assert report["recommendation"]["live_benchmark_authorized"] is False


def test_target_value_proofability_screen_handles_no_ready_candidate_conservatively():
    no_ready_specs = tuple(
        replace(
            spec,
            shape_compatible_with_existing_substrate=False,
            can_reuse_existing_late_blocker_substrate=False,
            new_topology_family_required=True,
        )
        for spec in proofability.CANDIDATE_SPECS
    )

    report = proofability.run_target_value_proofability_screen(no_ready_specs)

    assert report["passes_gate"] is True
    assert report["recommendation"]["ready_candidate_ids"] == []
    assert report["recommendation"]["recommended_candidate_id"] == ""
    assert report["recommendation"]["recommended_next_move"] == "resolve_candidate_proofability_gap"
    assert report["recommendation"]["live_benchmark_authorized"] is False
    assert all(candidate["ready_for_contract_build"] is False for candidate in report["candidates"])
