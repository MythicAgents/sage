"""Focused tests for the packet-backed GPO DC-scope canary validator."""
from __future__ import annotations

import json

from ai.hillclimb import gpo_dc_scope_canary as canary
from ai.hillclimb import gpo_dc_scope_late_blocker_contract as contract
from ai.hillclimb import gpo_dc_scope_live_surface as live_surface
from ai.hillclimb import policy_replay_corpus as corpus


def _surface_report() -> dict:
    facts = list(contract.synthetic_collected_state().graph_facts)
    return live_surface.validate_live_surface_evidence(
        live_surface.new_evidence_manifest([
            live_surface.make_observation(
                label="clean-reset-1",
                graph_facts=facts,
                collected_domains=["range.local"],
                captured_at="2026-07-13T00:00:01+00:00",
            ),
            live_surface.make_observation(
                label="clean-reset-2",
                graph_facts=facts,
                collected_domains=["range.local"],
                captured_at="2026-07-13T00:00:02+00:00",
            ),
        ])
    )


def _row() -> dict:
    report = contract.validate_gpo_dc_scope_late_blocker_contract()
    packet = report["decision_packet"]
    packet_hash = corpus._packet_hash(packet)
    best_index = report["best_indices"][0]
    best = report["post_blocker_frontier"][best_index]
    return {
        "scenario": live_surface.LIVE_SURFACE.scenario,
        "configured_policy_mode": "hybrid",
        "policy_mode": "hybrid",
        "objective_proven": True,
        "clean_stop": True,
        "request_completed": True,
        "policy_identity_valid": True,
        "backend_provenance_complete": True,
        "semantic_policy_coverage": 1.0,
        "policy_switches": [],
        "semantic_transaction_count": 6,
        "controller_status": "complete",
        "controller_terminal_reason": "objective satisfied",
        "effective_backends": ["openai:gpt-5.5-cyber-preview"],
        "controller_cycles": [
            {
                "cycle": 5,
                "action": contract.GPO_DC_SCOPE_LATE_BLOCKER.blocked_action,
                "ok": False,
                "note": contract.GPO_DC_SCOPE_LATE_BLOCKER.blocker_reason,
            },
            {
                "cycle": 6,
                "action": contract.TARGET_CAPABILITY,
                "ok": True,
                "note": "capability effects are achieved in the ledger",
            },
        ],
        "decisions": [{
            "decision_id": "decision-gpo-dc-scope",
            "candidate_hash": packet["candidate_hash"],
            "candidate_count": len(packet["admissible_frontier"]),
            "selected_capability": best["name"],
            "selected_target": best["target"],
            "selected_index": best_index,
            "decision_packet": packet,
            "decision_packet_hash": packet_hash,
        }],
        "ts": 1.0,
        "ts_iso": "2026-07-13T00:00:03",
        "chat_channel_id": 2,
        "chat_request_id": 1,
    }


def test_gpo_dc_scope_canary_validate_releases_matrix_after_clean_packet_backed_row():
    report = canary.validate_canary_results([_row()], _surface_report())

    assert report["passes_gate"] is True
    assert report["authorization"]["live_matrix_authorized"] is True
    assert report["decisive_decision"]["selected_target"] == "gpo=tier0-policy;domain=range.local"
    assert report["checks"]["packet_hash_recomputes"] is True
    assert report["checks"]["reconstructed_frontier_hash_matches_surface"] is True
    assert report["checks"]["packet_preserves_blocked_outcome"] is True


def test_gpo_dc_scope_canary_validate_fails_closed_when_packet_frontier_drifts():
    row = _row()
    row["decisions"][0]["decision_packet"]["admissible_frontier"][1]["target"] = "gpo=drifted;domain=range.local"

    report = canary.validate_canary_results([row], _surface_report())

    assert report["passes_gate"] is False
    assert report["authorization"]["live_matrix_authorized"] is False
    assert report["checks"]["packet_hash_recomputes"] is False
    assert report["checks"]["reconstructed_frontier_hash_matches_surface"] is False
    assert report["checks"]["frontier_targets_match_surface"] is False


def test_gpo_dc_scope_canary_pin_writes_one_hashed_row(tmp_path):
    path = tmp_path / "canary.jsonl"

    metadata = canary.pin_canary_row(_row(), path)

    assert metadata["rows"] == 1
    assert metadata["sha256"].startswith("sha256:")
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["scenario"] == live_surface.LIVE_SURFACE.scenario
