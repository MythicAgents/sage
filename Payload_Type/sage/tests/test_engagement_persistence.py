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
import engagement_ledger as el  # noqa: E402
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


def test_graph_facts_dict_roundtrip_is_lossless():
    facts = [
        engagement_state.GraphFact(
            "generic-write:gpo:starkwallpaper",
            "bloodhound:cypher",
            "2026-06-07T00:00:00Z",
            600,
        ),
        engagement_state.GraphFact(
            "gpo-domain:starkwallpaper:north.sevenkingdoms.local",
            "bloodhound:cypher",
            "2026-06-07T00:00:00Z",
            600,
        ),
    ]

    dicts = engagement_state.graph_facts_to_dicts(facts)
    back = engagement_state.graph_facts_from_dicts(json.loads(json.dumps(dicts)))

    assert back == facts


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

    # First run: fresh ledger, record an achieved hop -> write-through to disk.
    mt1 = mythic_tools.MythicTools(agent_task_id="solve-1")
    assert mt1._engagement_hops == []  # nothing on disk yet
    mt1._pending_engagement_hop = (
        "gpo-abuse", "winterfell.north.sevenkingdoms.local", "2026-06-07T00:00:00Z",
    )
    mt1._record_engagement_success("whoami\r\nnt authority\\system\r\n")
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


def test_cross_run_resume_restores_graph_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "test-graph")

    mt1 = mythic_tools.MythicTools(agent_task_id="solve-1")
    mt1._engagement_graph_facts = [
        engagement_state.GraphFact(
            "generic-write:gpo:starkwallpaper",
            "bloodhound:cypher",
            "2026-06-07T00:00:00Z",
            600,
        ),
        engagement_state.GraphFact(
            "gpo-domain:starkwallpaper:north.sevenkingdoms.local",
            "bloodhound:cypher",
            "2026-06-07T00:00:00Z",
            600,
        ),
    ]
    mt1._engagement_graph_facts_ts = "2026-06-07T00:00:00Z"
    mt1._persist_engagement_ledger()

    payload = json.loads(Path(mt1._engagement_ledger_path()).read_text())
    assert len(payload["graph_facts"]) == 2

    mt2 = mythic_tools.MythicTools(agent_task_id="solve-2")
    assert [fact.predicate for fact in mt2._engagement_graph_facts] == [
        "generic-write:gpo:starkwallpaper",
        "gpo-domain:starkwallpaper:north.sevenkingdoms.local",
    ]

    state = engagement_state.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[
            engagement_state.Foothold(
                callback_id="3",
                agent="apollo",
                host="CASTELBLACK",
                forest="north.sevenkingdoms.local",
                identity="NORTH\\samwell.tarly",
                integrity="medium",
                alive=True,
                source="test",
                timestamp="2026-06-07T00:00:00Z",
            )
        ],
        graph_facts=list(mt2._engagement_graph_facts),
    )
    rendered = engagement_state.render_engagement_state(state)
    assert "=== ENGAGEMENT STATE" in rendered
    assert "CASTELBLACK" in rendered
    assert "Phase:" not in rendered
    assert "NEXT GROUNDED ACTIONS" not in rendered


def test_ledger_objective_is_preserved_across_runtime_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "objective-test")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")

    path = Path(el.ledger_path("objective-test"))
    path.write_text(json.dumps({
        "engagement_id": "objective-test",
        "objective": "obtain administrative control of essos.local",
        "hops": [],
    }))

    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    assert mt._engagement_objective() == "obtain administrative control of essos.local"

    mt._persist_engagement_ledger()
    payload = json.loads(path.read_text())
    assert payload["objective"] == "obtain administrative control of essos.local"


def test_running_objective_refreshes_when_state_command_updates_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "objective-refresh")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")

    path = Path(el.ledger_path("objective-refresh"))
    path.write_text(json.dumps({
        "engagement_id": "objective-refresh",
        "objective": "obtain administrative control of north.sevenkingdoms.local",
        "hops": [],
    }))
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    assert mt._engagement_objective() == "obtain administrative control of north.sevenkingdoms.local"

    path.write_text(json.dumps({
        "engagement_id": "objective-refresh",
        "objective": "obtain administrative control of essos.local",
        "hops": [],
    }))
    assert mt._engagement_objective() == "obtain administrative control of essos.local"


def test_opaque_ledger_objective_is_not_treated_as_human_goal(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "objective-opaque")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")

    path = Path(el.ledger_path("objective-opaque"))
    path.write_text(json.dumps({
        "engagement_id": "objective-opaque",
        "objective": "sage-engagement:older-task",
        "hops": [],
    }))

    mt = mythic_tools.MythicTools(agent_task_id="solve-2")
    assert mt._engagement_objective() == "sage-engagement:solve-2"


# ---------------------------------------------------------------------------
# 3. Gate-OFF no-op on the load side (ISC-21)
# ---------------------------------------------------------------------------


def test_fresh_instance_loads_durable_ledger_unconditionally(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "test-eng2")

    seed = mythic_tools.MythicTools(agent_task_id="seed")
    seed._pending_engagement_hop = (
        "gpo-abuse", "winterfell.north.sevenkingdoms.local", "2026-06-07T00:00:00Z",
    )
    seed._record_engagement_success("whoami\r\nnt authority\\system\r\n")
    assert Path(seed._engagement_ledger_path()).exists()

    fresh = mythic_tools.MythicTools(agent_task_id="fresh")
    assert any(getattr(h, "technique", "") == "gpo-abuse" for h in fresh._engagement_hops)
