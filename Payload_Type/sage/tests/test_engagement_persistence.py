"""Durable cross-run engagement-ledger tests (Bug 2 fix).

Verify the incremental achieved-hops ledger (maintained in code, zero LLM inference) survives across
separate MythicTools instances (== separate solves/runs) via a per-engagement JSON file:

- engagement_state.hops_to_dicts / hops_from_dicts round-trip a Hop list losslessly.
- A hop recorded + persisted by one MythicTools is auto-loaded by a fresh instance (cross-run resume).
- With the gate OFF, __init__ never reads disk (byte-for-byte no-op on the load side).

No live Mythic is required — MythicTools.__init__ does no network (login is separate). Persistence is
pure filesystem I/O against a tmp dir. Mirrors the repo's no-pytest-asyncio convention.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state  # noqa: E402
import mythic_tools  # noqa: E402


def _gpo_hop():
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        "gpo-abuse",
        "winterfell.north.sevenkingdoms.local",
        "achieved",
        {"source": "test", "scheduled_task_present": True},
        "2026-06-07T00:00:00Z",
    )
    return state.hops


# ---------------------------------------------------------------------------
# 1. Serialize / deserialize round-trip
# ---------------------------------------------------------------------------


def test_hops_dict_roundtrip_is_lossless():
    hops = _gpo_hop()
    dicts = engagement_state.hops_to_dicts(hops)
    assert isinstance(dicts, list) and dicts and isinstance(dicts[0], dict)
    # Must be JSON-serializable (this is what the durable ledger writes).
    back = engagement_state.hops_from_dicts(json.loads(json.dumps(dicts)))
    assert len(back) == len(hops)
    a, b = hops[0], back[0]
    for fld in ("id", "technique", "target", "effect", "status",
                "preconditions", "satisfied_effects", "source", "timestamp"):
        assert getattr(a, fld) == getattr(b, fld)
    assert a.evidence == b.evidence


def test_from_dicts_skips_malformed_entries():
    out = engagement_state.hops_from_dicts(["nope", 5, None, {"technique": "x"}])
    assert len(out) == 1  # only the dict yields a Hop (with safe defaults)
    assert out[0].technique == "x"


def test_to_dicts_skips_non_dataclass():
    assert engagement_state.hops_to_dicts(["nope", 5, None]) == []


# ---------------------------------------------------------------------------
# 2. Cross-run resume: instance A records -> instance B auto-loads
# ---------------------------------------------------------------------------


def test_cross_run_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "test-eng")
    monkeypatch.setattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    # First run: fresh ledger, record an achieved hop -> write-through to disk.
    mt1 = mythic_tools.MythicTools(agent_task_id="solve-1")
    assert mt1._engagement_hops == []  # nothing on disk yet
    mt1._pending_engagement_hop = (
        "gpo-abuse", "winterfell.north.sevenkingdoms.local", "2026-06-07T00:00:00Z",
    )
    mt1._record_engagement_success("success: STARKWALLPAPER GPO modified; scheduled task present")
    ledger = mt1._engagement_ledger_path()
    assert Path(ledger).exists()
    # File holds the hop under the per-engagement key.
    payload = json.loads(Path(ledger).read_text())
    assert payload["engagement_id"] == "test-eng"
    assert any(h.get("technique") == "gpo-abuse" for h in payload["hops"])

    # Second run (a new solve == a new MythicTools): __init__ auto-loads the ledger.
    mt2 = mythic_tools.MythicTools(agent_task_id="solve-2")
    assert any(getattr(h, "technique", "") == "gpo-abuse" for h in mt2._engagement_hops)
    assert any(getattr(h, "status", "") == "achieved" for h in mt2._engagement_hops)


# ---------------------------------------------------------------------------
# 3. Gate-OFF no-op on the load side (ISC-21)
# ---------------------------------------------------------------------------


def test_gate_off_does_not_read_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "test-eng2")

    # Seed a ledger with the gate ON.
    monkeypatch.setattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)
    seed = mythic_tools.MythicTools(agent_task_id="seed")
    seed._pending_engagement_hop = (
        "gpo-abuse", "winterfell.north.sevenkingdoms.local", "2026-06-07T00:00:00Z",
    )
    seed._record_engagement_success("success: gpo modified")
    assert Path(seed._engagement_ledger_path()).exists()

    # Gate OFF: a fresh instance must NOT load from disk.
    monkeypatch.setattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED", False)
    off = mythic_tools.MythicTools(agent_task_id="off")
    assert off._engagement_hops == []

    # Gate back ON: it loads again.
    monkeypatch.setattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)
    on = mythic_tools.MythicTools(agent_task_id="on")
    assert any(getattr(h, "technique", "") == "gpo-abuse" for h in on._engagement_hops)
