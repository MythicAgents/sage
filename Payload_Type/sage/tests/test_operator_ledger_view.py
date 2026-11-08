"""Permanent hop numbers shared by every ledger view, and the operator-facing `/state` and turn-end tables.

Hops carry permanent numbers shared by every view. The model no longer receives a copy of a table the operator was
shown: every Supervisor and Mythic_Operator call carries its own fresh view of the same rows (see
test_engagement_ledger_phase5_fresh_awareness.py), so these operator surfaces queue nothing for the model.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.langgraph import engagement_ledger as el
from ai.langgraph import model as graph_model
from ai.langgraph import mythic_tools
from ai.langgraph import engagement_state
from sage_chat import slash
from sage_chat.headless import build_chat_request
from tests.esl_fixtures import seed_ledger


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))


# ── permanent hop numbers (allocated by the shared store) ───────────────────────────────


def _session(namespace="numbers"):
    mythic_tools.SAGE_ENGAGEMENT_ID = namespace
    return mythic_tools.MythicTools(agent_task_id="solve-1")


def _pending(*targets):
    state = engagement_state.EngagementState(objective="test")
    for target in targets:
        state = engagement_state.record_hop_result(
            state, "gpo-abuse", target, "pending", {"source": "test"}, "2026-10-29T00:00:00Z",
        )
    return state.hops


def _numbers(mt):
    return [(hop["target"], hop["number"]) for hop in mt._ledger_snapshot().display_hops()]


def _state(mt, arg, channel_id=5):
    model = SimpleNamespace(mythic_client=mt, mode="supervised", provider="test", model="test", state={})
    request = build_chat_request("x", channel_id=channel_id, operation_id=0)
    return asyncio.run(slash._handle_state(model, request, arg))


@pytest.fixture
def _restore_namespace(monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", mythic_tools.SAGE_ENGAGEMENT_ID)


def test_numbers_are_assigned_once_in_creation_order(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local", "two.local", "three.local"))
    assert _numbers(mt) == [("one.local", 1), ("two.local", 2), ("three.local", 3)]
    seed_ledger(mt, hops=_pending("two.local"))  # an identical re-observation allocates nothing
    assert _numbers(mt) == [("one.local", 1), ("two.local", 2), ("three.local", 3)]
    assert mt._ledger_snapshot().next_hop_number == 4


def test_retraction_keeps_the_row_its_number_and_later_numbers(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local", "two.local", "three.local"))
    shown = _state(mt, "remove 1 --reason duplicate")
    assert "Retracted 1 hop(s)" in shown and "operator retracted" in shown and "duplicate" in shown
    assert _numbers(mt) == [("one.local", 1), ("two.local", 2), ("three.local", 3)]
    assert [hop.target for hop in mt._engagement_hops] == ["two.local", "three.local"]


def test_a_number_is_never_reused_after_wipe(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local", "two.local"))
    _state(mt, "wipe")
    assert mt._engagement_hops == []
    seed_ledger(mt, hops=_pending("new.local"))
    assert _numbers(mt)[-1] == ("new.local", 3)


def test_a_session_round_trip_keeps_stored_numbers_and_revisions(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local", "two.local", "three.local"))
    _state(mt, "remove 1")
    other = _session()  # a second session of the same namespace, writing after the operator's retraction
    hops = list(other._engagement_hops)
    assert [(hop.number, hop.revision) for hop in hops] == [(2, 1), (3, 1)]
    updated = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test", hops=hops),
        "gpo-abuse", "four.local", "pending", {"source": "test"}, "2026-10-29T00:00:00Z",
    )
    assert [hop.number for hop in updated.hops] == [2, 3, 0]  # dataclasses keep stored numbers; new rows have none
    assert other._ledger_commit_hops(hops, updated.hops)
    assert _numbers(mt) == [("one.local", 1), ("two.local", 2), ("three.local", 3), ("four.local", 4)]
    assert "operator retracted" in _state(mt, "")


@pytest.mark.parametrize("selector", ["0", "5", "-1", "1.0"])
def test_selector_that_is_not_an_existing_hop_number_matches_nothing(_restore_namespace, selector):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local", "two.local"))
    assert "Retracted 0 hop(s)" in _state(mt, f"remove {selector}")
    assert len(mt._engagement_hops) == 2


def test_state_set_cannot_set_a_status_and_changes_nothing(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local"))
    assert "Only `achieved` can be set" in _state(mt, "set 1 failed")
    assert mt._engagement_hops[0].status == "pending"


# ── one shared table ─────────────────────────────────────────────────────────────────────


def _hops():
    return [
        {"id": "kerberoast:svc", "effect": "creds:svc@corp.local", "status": "achieved", "number": 1,
         "evidence": {"mythic_task_id": 11, "callback_id": 3, "result_preview": "TARGET-OUTPUT-PREVIEW"}},
        {"id": "dcsync:corp.local", "effect": "krbtgt-hash:corp.local", "status": "pending", "number": 4,
         "evidence": {"mythic_task_id": 12}},
    ]


def test_one_table_for_every_view_links_tasks_and_never_shows_output():
    table = "\n".join(el.render_hop_table(_hops()))
    assert "| 4 | `dcsync:corp.local` | krbtgt-hash:corp.local | `pending (awaiting effect)` | [12](/new/task/12) | - |" \
        in table
    assert "TARGET-OUTPUT-PREVIEW" not in table and "Evidence" not in table


def test_table_limit_keeps_newest_numbers():
    lines = el.render_hop_table(_hops(), limit=1)
    assert len(lines) == 3 and lines[2].startswith("| 4 |")


def test_operator_view_copy_plumbing_is_retired():
    for name in ("record_operator_view", "take_operator_view", "operator_view_text", "_OPERATOR_VIEWS"):
        assert not hasattr(el, name), name


# ── /state and the turn-end table: unchanged for the operator, no model copy ───────────


def _seeded_view_session():
    mt = _session("view")
    seed_ledger(mt, hops=[
        {"id": "kerberoast:svc", "technique": "kerberoast", "target": "svc", "effect": "creds:svc@corp.local",
         "status": "achieved", "evidence": {"mythic_task_id": 11, "callback_id": 3,
                                             "result_preview": "TARGET-OUTPUT-PREVIEW"}},
        {"id": "dcsync:corp.local", "technique": "dcsync", "target": "corp.local",
         "effect": "krbtgt-hash:corp.local", "status": "pending", "evidence": {"mythic_task_id": 12}},
    ])
    mt._ledger_store().update_objective(mt._require_ledger_binding(), "compromise CORP", "operator")
    return mt


def test_state_command_shows_the_rows_unchanged(_restore_namespace):
    mt = _seeded_view_session()
    shown = _state(mt, "")
    assert "| 2 | `dcsync:corp.local` | krbtgt-hash:corp.local | `pending (awaiting effect)` | [12](/new/task/12) |" \
        in shown
    assert "TARGET-OUTPUT-PREVIEW" not in shown
    assert "compromise CORP" in shown


def test_turn_end_table_shows_the_rows_and_unchanged_on_repeat(_restore_namespace):
    mt = _seeded_view_session()
    model = object.__new__(graph_model.Model)
    model.mythic_client = mt
    model.channel_id = 7
    shown = asyncio.run(model._engagement_state_turn_summary_text())
    assert "| 1 | `kerberoast:svc` |" in shown and "| 2 | `dcsync:corp.local` |" in shown
    assert "unchanged" in asyncio.run(model._engagement_state_turn_summary_text())

    # A retraction changes neither technique, target nor historical status, but it is a change.
    _state(mt, "remove 1")
    shown = asyncio.run(model._engagement_state_turn_summary_text())
    assert "unchanged" not in shown and "operator retracted" in shown


# ── the assembled request path ───────────────────────────────────────────────────────────


def _inventory_model(channel_id):
    """A minimal Model driven through invoke() on the deterministic callback-inventory path."""
    m = object.__new__(graph_model.Model)
    m.mode = "supervised"
    m.command_name = "chat"
    m.channel_id = channel_id
    m._autonomous_solve = False
    m._controller_hitl_pending = None
    m._thread_id_override = f"channel-ledger-view-{channel_id}"
    m._running_tasks = set()
    m._message_seq = 1
    m.graph = object()
    m.state = {"messages": [], "_message_seq": 1}
    m.provider = "test"
    m.model = "test"
    m.is_interactive = True
    streamed = []

    class FakeMythic:
        async def list_callbacks(self):
            return [{"id": 1, "agent": "apollo", "host": "TEST-HOST", "user": "operator",
                     "integrity": 2, "status": "alive", "secs_since_checkin": 1}]

    async def no_interrupt(_thread_id):
        return False

    async def stream(text):
        streamed.append(text)
        return True

    m.mythic_client = FakeMythic()
    m._hitl_interrupt_pending = no_interrupt
    m._seed_autonomous_objective = lambda _prompt: None
    m._format_message_for_streaming = graph_model.Model._format_message_for_streaming.__get__(m)
    m._stream_message_to_mythic = stream
    return m, streamed


def _drive(m):
    assert asyncio.run(m.invoke("list_callbacks", is_interactive=True)) == ""
    return [
        msg for msg in m.state["supervisor_messages"]
        if isinstance(msg, graph_model.HumanMessage)
    ]


def test_a_request_adds_no_ledger_copy_to_the_supervisor_history():
    m, streamed = _inventory_model(9)
    humans = _drive(m)
    assert [msg.content for msg in humans] == ["list_callbacks"]
    assert not any("ledger view" in str(msg.content) for msg in m.state["supervisor_messages"])
    assert not any("ledger view" in text for text in streamed)
