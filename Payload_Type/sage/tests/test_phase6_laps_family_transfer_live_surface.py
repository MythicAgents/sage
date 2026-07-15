from __future__ import annotations

import asyncio

import pytest

from ai.hillclimb import laps_family_transfer_holdout as contract
from ai.hillclimb import laps_family_transfer_live_surface as live_surface


def _observation(label: str, facts: list[object] | None = None) -> dict[str, object]:
    return live_surface.make_observation(
        label=label,
        graph_facts=facts or list(contract.synthetic_collected_state().graph_facts),
        collected_domains=["cinder.local", "ember.cinder.local", "ash.cinder.local"],
        captured_at=f"2026-07-14T00:00:0{label[-1]}+00:00",
    )


def test_phase6_live_surface_reconstructs_repeated_sealed_frontier():
    evidence = live_surface.new_evidence_manifest([
        _observation("clean-reset-1"),
        _observation("clean-reset-2"),
    ])

    report = live_surface.validate_live_surface_evidence(evidence)

    assert report["passes_gate"] is True
    assert report["authorization"]["mechanics_canaries_authorized"] is True
    assert report["checks"]["sealed_manifest_hash_matches"] is True
    assert report["checks"]["topology_hash_matches"] is True
    assert report["checks"]["two_clean_reset_observations_present"] is True
    assert report["checks"]["all_observations_reconstruct_contract"] is True
    assert report["checks"]["repeated_candidate_set_hash_is_stable"] is True
    assert report["checks"]["repeated_ordered_frontier_hash_is_stable"] is True
    assert live_surface.LIVE_SURFACE.snapshot == contract.LAPS_FAMILY_TRANSFER_HOLDOUT.live_reset_snapshot
    assert len(set(report["candidate_set_hashes"])) == 1
    assert report["candidate_set_hashes"] == [
        contract.canonical_candidate_set_hash(
            list(contract.capabilities.actions_from_state(contract.synthetic_collected_state()))
        )
    ] * 2
    for observation in report["observations"]:
        assert observation["passes_gate"] is True
        assert observation["families"] == ["managed-local-admin"]
        assert [item["target"] for item in observation["frontier"]] == list(
            live_surface.LIVE_SURFACE.expected_targets
        )


def test_phase6_live_surface_fails_closed_when_one_laps_edge_disappears():
    facts = [
        fact
        for fact in contract.synthetic_collected_state().graph_facts
        if "target=ember-ops01" not in fact.predicate
    ]
    evidence = live_surface.new_evidence_manifest([
        _observation("clean-reset-1"),
        _observation("clean-reset-2", facts),
    ])

    report = live_surface.validate_live_surface_evidence(evidence)

    assert report["passes_gate"] is False
    assert report["authorization"]["mechanics_canaries_authorized"] is False
    assert report["checks"]["all_observations_reconstruct_contract"] is False
    assert report["observations"][1]["checks"]["exact_two_laps_candidates"] is False


def test_phase6_live_surface_capture_loads_sync_tools_before_async_refresh(monkeypatch):
    cypher_tool = object()
    domain_tool = object()

    def fake_load_tools():
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return [], {"cypher_query": cypher_tool, "domain_info": domain_tool}

    def fake_refresh_fn(_manager, _now, _ttl_seconds):
        async def refresh(_state):
            return contract.synthetic_collected_state()

        return refresh

    async def fake_collected_domains(tool):
        assert tool is domain_tool
        return ["CINDER.LOCAL", "EMBER.CINDER.LOCAL", "ASH.CINDER.LOCAL"]

    monkeypatch.setattr(live_surface.bare_bloodhound, "load_bloodhound_mcp_tools", fake_load_tools)
    monkeypatch.setattr(live_surface.frontier_census, "_live_refresh_fn", fake_refresh_fn)
    monkeypatch.setattr(live_surface.frontier_census, "_live_collected_domains", fake_collected_domains)

    observation = live_surface.capture_live_observation("clean-reset-1")

    assert observation["label"] == "clean-reset-1"
    assert observation["collected_domains"] == ["ash.cinder.local", "cinder.local", "ember.cinder.local"]
    assert observation["observation_hash"] == live_surface.observation_hash(observation)
