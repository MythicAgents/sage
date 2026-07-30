"""Focused tests for the repeated live GPO DC-scope matrix validator."""
from __future__ import annotations

import copy
import json

from ai.hillclimb import gpo_dc_scope_canary as canary
from ai.hillclimb import gpo_dc_scope_late_blocker_contract as contract
from ai.hillclimb import gpo_dc_scope_live_surface as live_surface
from ai.hillclimb import gpo_dc_scope_matrix as matrix
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


def _row(policy_mode: str, *, ts: float, selected_index: int, recovery_transactions: int) -> dict:
    report = contract.validate_gpo_dc_scope_late_blocker_contract()
    packet = copy.deepcopy(report["decision_packet"])
    packet["selection_contract"] = matrix.EXPECTED_SELECTION_CONTRACTS[policy_mode]
    frontier = report["post_blocker_frontier"]
    selected = frontier[selected_index]
    decisions = [{
        "decision_id": f"{policy_mode}-decisive-{ts}",
        "candidate_hash": packet["candidate_hash"],
        "candidate_count": len(packet["admissible_frontier"]),
        "selected_capability": selected["name"],
        "selected_target": selected["target"],
        "selected_index": selected_index,
        "decision_packet": packet,
        "decision_packet_hash": corpus._packet_hash(packet),
    }]
    for tail_index in range(recovery_transactions - 1):
        decisions.append({
            "decision_id": f"{policy_mode}-tail-{ts}-{tail_index}",
            "candidate_hash": f"sha256:tail-{tail_index}",
            "candidate_count": 1,
            "selected_capability": contract.TARGET_CAPABILITY,
            "selected_target": "gpo=tier0-policy;domain=range.local",
            "selected_index": 0,
        })
    return {
        "scenario": live_surface.LIVE_SURFACE.scenario,
        "configured_policy_mode": policy_mode,
        "policy_mode": policy_mode,
        "objective_proven": True,
        "clean_stop": True,
        "request_completed": True,
        "policy_identity_valid": True,
        "backend_provenance_complete": True,
        "semantic_policy_coverage": 1.0,
        "policy_switches": [],
        "semantic_transaction_count": 6 + recovery_transactions,
        "controller_status": "complete",
        "controller_terminal_reason": "objective satisfied",
        "effective_backends": ["openai:gpt-5.5-cyber-preview"],
        "controller_cycles": [{
            "cycle": 6,
            "action": contract.GPO_DC_SCOPE_LATE_BLOCKER.blocked_action,
            "ok": False,
            "note": contract.GPO_DC_SCOPE_LATE_BLOCKER.blocker_reason,
        }],
        "decisions": decisions,
        "transactions": [{"decision_id": decision["decision_id"]} for decision in decisions],
        "ts": ts,
        "ts_iso": f"2026-07-13T00:00:{int(ts):02d}",
        "chat_channel_id": int(ts),
        "chat_request_id": int(ts),
    }


def _canary_report() -> dict:
    accepted_row = _row("hybrid", ts=1.0, selected_index=1, recovery_transactions=1)
    return canary.validate_canary_results([accepted_row], _surface_report())


def _matrix_rows() -> list[dict]:
    rows: list[dict] = []
    ts = 2.0
    for policy_mode, selected_index, recovery_transactions in (
        ("symbolic", 0, 2),
        ("llm", 1, 1),
        ("hybrid", 1, 1),
    ):
        for _ in range(3):
            rows.append(_row(
                policy_mode,
                ts=ts,
                selected_index=selected_index,
                recovery_transactions=recovery_transactions,
            ))
            ts += 1.0
    return rows


def test_gpo_dc_scope_matrix_validate_reports_stable_live_order_and_tie():
    report = matrix.validate_matrix_results(_matrix_rows(), _canary_report())

    assert report["passes_gate"] is True
    assert report["authorization"]["packet_corpus_expansion_authorized"] is True
    assert report["matching_row_count"] == 9
    assert report["observed_policy_order"] == [["hybrid", "llm"], ["symbolic"]]
    assert report["learned_policy_relation"] == "tie"
    assert report["learned_policy_tie_preserved"] is True
    assert report["policy_summaries"]["symbolic"]["recovery_transaction_values"] == [2.0, 2.0, 2.0]
    assert report["policy_summaries"]["llm"]["recovery_transaction_values"] == [1.0, 1.0, 1.0]
    assert report["checks"]["all_rows_preserve_packet_frontier_and_live_invariants"] is True


def test_gpo_dc_scope_matrix_validate_fails_closed_when_one_packet_drifts():
    rows = _matrix_rows()
    rows[4]["decisions"][0]["decision_packet"]["admissible_frontier"][1]["target"] = "gpo=drifted;domain=range.local"

    report = matrix.validate_matrix_results(rows, _canary_report())

    assert report["passes_gate"] is False
    assert report["authorization"]["packet_corpus_expansion_authorized"] is False
    assert report["checks"]["all_rows_preserve_packet_frontier_and_live_invariants"] is False
    assert report["row_reports"][4]["checks"]["packet_hash_recomputes"] is False
    assert report["row_reports"][4]["checks"]["frontier_targets_match_surface"] is False


def test_gpo_dc_scope_matrix_pin_writes_hashed_rows(tmp_path):
    path = tmp_path / "matrix.jsonl"

    metadata = matrix.pin_matrix_rows(_matrix_rows(), path)

    assert metadata["rows"] == 9
    assert metadata["sha256"].startswith("sha256:")
    assert len(path.read_text(encoding="utf-8").splitlines()) == 9
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["scenario"] == live_surface.LIVE_SURFACE.scenario
