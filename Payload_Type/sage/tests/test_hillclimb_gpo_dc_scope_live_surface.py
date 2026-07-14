"""Focused tests for the repeated live GPO DC-scope surface validator."""
from __future__ import annotations

import asyncio

import pytest

from ai.hillclimb import gpo_dc_scope_late_blocker_contract as contract
from ai.hillclimb import gpo_dc_scope_live_surface as live_surface


def _observation(label: str, facts: list[object] | None = None) -> dict[str, object]:
    return live_surface.make_observation(
        label=label,
        graph_facts=facts or list(contract.synthetic_collected_state().graph_facts),
        collected_domains=["range.local"],
        captured_at=f"2026-07-13T00:00:0{label[-1]}+00:00",
    )


def test_gpo_dc_scope_live_surface_reconstructs_repeated_frontier():
    evidence = live_surface.new_evidence_manifest([
        _observation("clean-reset-1"),
        _observation("clean-reset-2"),
    ])

    report = live_surface.validate_live_surface_evidence(evidence)

    assert report["passes_gate"] is True
    assert report["authorization"]["live_canary_authorized"] is True
    assert report["checks"]["two_clean_reset_observations_present"] is True
    assert report["checks"]["all_observations_reconstruct_contract"] is True
    assert report["checks"]["repeated_frontier_hash_is_stable"] is True
    assert len(set(report["candidate_hashes"])) == 1
    assert report["spec"]["snapshot"] == live_surface.SURFACE_SNAPSHOT
    assert report["spec"]["retained_callback_config"] == live_surface.SURFACE_RETAINED_CALLBACK_CONFIG
    assert tuple(report["spec"]["reset_path"]) == live_surface.SURFACE_RESET_PATH
    for observation in report["observations"]:
        assert observation["passes_gate"] is True
        assert observation["modeled_transaction_costs"] == [2, 1]
        assert observation["best_indices"] == [1]
        assert [item["target"] for item in observation["post_blocker_frontier"]] == list(
            live_surface.LIVE_SURFACE.expected_targets
        )


def test_gpo_dc_scope_live_surface_fails_closed_when_dc_scope_fact_disappears():
    facts = [
        fact
        for fact in contract.synthetic_collected_state().graph_facts
        if fact.predicate != "gpo-affects-dc:tier0-policy:dc01:range.local"
    ]
    evidence = live_surface.new_evidence_manifest([
        _observation("clean-reset-1"),
        _observation("clean-reset-2", facts),
    ])

    report = live_surface.validate_live_surface_evidence(evidence)

    assert report["passes_gate"] is False
    assert report["authorization"]["live_canary_authorized"] is False
    assert report["checks"]["all_observations_reconstruct_contract"] is False
    assert report["observations"][1]["checks"]["post_blocker_frontier_matches_contract"] is False
    assert report["observations"][1]["checks"]["one_dc_scoped_and_one_non_dc_scoped_target"] is False


def test_gpo_dc_scope_live_surface_capture_loads_sync_tools_before_async_refresh(monkeypatch):
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
        return ["RANGE.LOCAL"]

    monkeypatch.setattr(live_surface.bare_bloodhound, "load_bloodhound_mcp_tools", fake_load_tools)
    monkeypatch.setattr(live_surface.frontier_census, "_live_refresh_fn", fake_refresh_fn)
    monkeypatch.setattr(live_surface.frontier_census, "_live_collected_domains", fake_collected_domains)

    observation = live_surface.capture_live_observation("clean-reset-1")

    assert observation["label"] == "clean-reset-1"
    assert observation["collected_domains"] == ["range.local"]
    assert observation["observation_hash"] == live_surface.observation_hash(observation)
