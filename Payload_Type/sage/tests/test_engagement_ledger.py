"""Engagement State Ledger formatting and historical JSON helpers.

Live persistence is in ``engagement_store`` (see ``test_engagement_store.py``). This module only keeps the
legacy file location, the neutral historical reader/writer, per-hop proof admission and the shared table.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_ledger as el  # noqa: E402
import proof_boundary as pb  # noqa: E402


def _proof(engagement_id="e"):
    return pb.make_runtime_task_envelope(
        engagement_id=engagement_id,
        callback_id="13",
        task_id="450",
        terminal_status="completed",
        command="dcsync",
        verifier_id="test:ledger-roundtrip",
        transaction_id="transaction-ledger",
        verifier_input={"probe": {"krbtgt_hash_present": True}},
        verifier_result={"verdict": "achieved"},
        captured_at="2026-07-14T00:00:00+00:00",
    ).to_dict()


def _achieved(proof=None):
    return {
        "id": "dcsync:lab.local", "technique": "dcsync", "target": "lab.local",
        "effect": "krbtgt-hash:lab.local", "status": "achieved",
        "evidence": {"proof_envelope": proof} if proof else {}, "proof_envelope": proof or {},
    }


def test_legacy_path_uses_state_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    assert el.ledger_path("goad-tw-0607") == str(tmp_path / "state_goad-tw-0607.json")


def test_legacy_path_sanitizes_unsafe_chars(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    assert el.ledger_path("a/b c").endswith("state_a_b_c.json")


def test_legacy_path_needs_an_explicit_key():
    import pytest

    with pytest.raises(ValueError):
        el.ledger_path("")


def test_historical_file_roundtrip_is_neutral(tmp_path):
    data = {"engagement_id": "e", "hops": [_achieved()]}
    target = tmp_path / "archive" / "state_e.json"
    el.save_file(data, target)
    loaded = el.load_file(target)
    assert loaded["hops"][0]["status"] == "achieved"  # the neutral reader never quarantines


def test_load_missing_returns_skeleton(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    assert el.load("nope") == {"engagement_id": "nope", "hops": []}


def test_quarantine_keeps_admitted_proof_and_demotes_unproven_rows():
    proven = el.quarantine_hop(_achieved(_proof("e")), "e")
    assert proven["status"] == "achieved"
    unproven = el.quarantine_hop(_achieved(), "e")
    assert (unproven["status"], unproven["pending_reason"]) == ("pending", "proof_missing")
    assert unproven["evidence"]["proof_admission_reason"] == "missing proof envelope"
    other_operation = el.quarantine_hop(_achieved(_proof("other")), "e")
    assert (other_operation["status"], other_operation["pending_reason"]) == ("pending", "proof_missing")


def test_hop_label():
    assert el.hop_label({"id": "x"}) == "x"
    assert el.hop_label({"technique": "dcsync", "target": "d"}) == "dcsync:d"


def test_table_presents_corrections_beside_historical_status():
    retracted = dict(_achieved(), number=24, corrections={"retracted": {"reason": "duplicate", "effective": True}})
    stale = dict(_achieved(), number=25, id="golden-ticket:north",
                 corrections={"no_longer_holds": {"reason": "callback died", "effective": True, "scope": "automatic"}})
    lapsed = dict(_achieved(), number=26, id="dcsync:old",
                  corrections={"no_longer_holds": {"reason": "superseded", "effective": False, "scope": "automatic"}})
    lines = el.render_hop_table([retracted, stale, lapsed], include_evidence=False)
    assert "`achieved — operator retracted`" in lines[2]
    assert "`achieved — no longer holds`" in lines[3]
    assert "`achieved`" in lines[4]
    notes = el.correction_notes([retracted, stale, lapsed])
    assert len(notes) == 2 and "hop 24" in notes[0] and "duplicate" in notes[0]


def test_state_dir_is_shared_with_mythic_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    import mythic_tools
    assert mythic_tools._engagement_state_dir() == el.state_dir()
