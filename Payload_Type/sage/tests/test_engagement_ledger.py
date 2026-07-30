"""Shared engagement-ledger module: path resolution, round-trip IO, and operator edit operations.

Backs the `engagement` Mythic command (show/remove/set/wipe) and is the single source of truth the running
agent (`mythic_tools`) also uses — the no-drift test guards that they resolve the SAME file.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_ledger as el  # noqa: E402
import proof_boundary as pb  # noqa: E402


def test_path_uses_state_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    assert el.ledger_path("goad-tw-0607") == str(tmp_path / "state_goad-tw-0607.json")


def test_path_sanitizes_unsafe_chars(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    assert el.ledger_path("a/b c").endswith("state_a_b_c.json")


def test_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    data = {"engagement_id": "e", "hops": [
        {"id": "dcsync-user:cersei@d", "technique": "dcsync-user", "target": "cersei@d",
         "effect": "creds:cersei@d", "status": "achieved", "evidence": {"provenance": "run"}}]}
    el.save(data, "e")
    assert el.load("e")["hops"][0]["effect"] == "creds:cersei@d"


def test_runtime_roundtrip_preserves_admitted_proof_envelope(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    proof = pb.make_runtime_task_envelope(
        engagement_id="e",
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
    data = {"engagement_id": "e", "hops": [{
        "id": "dcsync:lab.local",
        "technique": "dcsync",
        "target": "lab.local",
        "effect": "krbtgt-hash:lab.local",
        "status": "achieved",
        "evidence": {"proof_envelope": proof},
        "proof_envelope": proof,
    }]}

    el.save_runtime(data, "e")
    loaded = el.load_runtime("e")

    assert loaded["hops"][0]["status"] == "achieved"
    assert loaded["hops"][0]["proof_envelope"] == proof


def test_load_missing_returns_skeleton(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    assert el.load("nope") == {"engagement_id": "nope", "hops": []}


def test_remove_by_effect_and_by_label():
    data = {"hops": [{"id": "a", "effect": "creds:x"}, {"id": "b", "effect": "da:y"}]}
    data, n = el.remove_hop(data, "creds:x")  # by effect
    assert n == 1 and [h["id"] for h in data["hops"]] == ["b"]
    data, n = el.remove_hop(data, "b")          # by label/id
    assert n == 1 and data["hops"] == []


def test_set_hop_status():
    data, n = el.set_hop_status({"hops": [{"id": "a", "status": "achieved"}]}, "a", "failed")
    assert n == 1 and data["hops"][0]["status"] == "failed"


def test_set_hop_status_cannot_promote_to_achieved():
    data, n = el.set_hop_status({"hops": [{"id": "a", "status": "pending"}]}, "a", "achieved")
    assert n == 0 and data["hops"][0]["status"] == "pending"


def test_remove_by_row_number():
    # Operators naturally type the row # from `state show` (the bug: `state remove 11` matched nothing).
    data = {"hops": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    data, n = el.remove_hop(data, "2")  # row 2 = 'b'
    assert n == 1 and [h["id"] for h in data["hops"]] == ["a", "c"]


def test_set_status_by_row_number():
    data = {"hops": [{"id": "a", "status": "achieved"}, {"id": "b", "status": "achieved"}]}
    data, n = el.set_hop_status(data, "1", "failed")
    assert n == 1 and data["hops"][0]["status"] == "failed" and data["hops"][1]["status"] == "achieved"


def test_row_number_out_of_range_falls_back_to_label_match():
    data, n = el.remove_hop({"hops": [{"id": "a"}]}, "99")  # not a valid row → treated as label → no match
    assert n == 0


def test_remove_hops_csv_row_numbers_index_stable():
    data = {"hops": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]}
    data, n = el.remove_hops(data, ["1", "3"])  # rows 1 and 3 = a and c, resolved against original indexing
    assert n == 2 and [h["id"] for h in data["hops"]] == ["b", "d"]


def test_remove_hops_mixed_selectors():
    data = {"hops": [{"id": "x", "effect": "creds:x"}, {"id": "y", "effect": "da:y"}, {"id": "z", "effect": "creds:z"}]}
    data, n = el.remove_hops(data, ["2", "creds:z"])  # row 2 (y) + effect creds:z
    assert n == 2 and [h["id"] for h in data["hops"]] == ["x"]


def test_remove_nonexistent_is_zero():
    data, n = el.remove_hop({"hops": [{"id": "a"}]}, "zzz")
    assert n == 0 and len(data["hops"]) == 1


def test_wipe(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    el.save({"hops": []}, "e")
    assert el.wipe("e") is True
    assert el.wipe("e") is False  # already gone


def test_list_engagements(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    el.save({"hops": []}, "e1")
    el.save({"hops": []}, "e2")
    assert el.list_engagements() == ["e1", "e2"]


def test_hop_label():
    assert el.hop_label({"id": "x"}) == "x"
    assert el.hop_label({"technique": "dcsync", "target": "d"}) == "dcsync:d"


def test_no_drift_with_mythic_tools(monkeypatch, tmp_path):
    # CRITICAL: the agent (mythic_tools) and the command (engagement_ledger) must resolve the SAME file.
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    import mythic_tools
    assert mythic_tools._engagement_ledger_file("goad-tw-0607") == el.ledger_path("goad-tw-0607")
    assert mythic_tools._engagement_state_dir() == el.state_dir()
