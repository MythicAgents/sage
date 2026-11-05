"""The model sees the engagement-ledger views the operator is shown, with the same permanent hop numbers.

`/state` and the turn-end engagement-state table were operator-only, so "why is hop 1 pending?" had no
referent in the model's conversation. Hops now carry permanent numbers shared by every view, and the latest
view shown on a channel enters that channel's next model request once, as hidden ledger data.
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
    el._OPERATOR_VIEWS.clear()
    yield
    el._OPERATOR_VIEWS.clear()


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


def test_state_set_is_disabled_and_changes_nothing(_restore_namespace):
    mt = _session()
    seed_ledger(mt, hops=_pending("one.local"))
    assert "disabled" in _state(mt, "set 1 failed")
    assert mt._engagement_hops[0].status == "pending"


# ── one shared table ─────────────────────────────────────────────────────────────────────


def _hops():
    return [
        {"id": "kerberoast:svc", "effect": "creds:svc@corp.local", "status": "achieved", "number": 1,
         "evidence": {"mythic_task_id": 11, "callback_id": 3, "result_preview": "TARGET-OUTPUT-PREVIEW"}},
        {"id": "dcsync:corp.local", "effect": "krbtgt-hash:corp.local", "status": "pending", "number": 4,
         "evidence": {"mythic_task_id": 12}},
    ]


def test_table_rows_use_hop_numbers_and_model_copy_omits_evidence():
    operator = "\n".join(el.render_hop_table(_hops()))
    model_copy = "\n".join(el.render_hop_table(_hops(), include_evidence=False))
    assert "| 4 | `dcsync:corp.local` |" in operator and "| 4 | `dcsync:corp.local` |" in model_copy
    assert "TARGET-OUTPUT-PREVIEW" in operator
    assert "TARGET-OUTPUT-PREVIEW" not in model_copy and "Evidence" not in model_copy


def test_table_limit_keeps_newest_numbers():
    lines = el.render_hop_table(_hops(), limit=1)
    assert len(lines) == 3 and lines[2].startswith("| 4 |")


def test_pending_view_is_per_channel_and_consumed_once():
    el.record_operator_view(5, "view for 5")
    assert el.take_operator_view(6) == ""
    assert el.take_operator_view("5") == "view for 5"
    assert el.take_operator_view(5) == ""


# ── /state and the turn-end table both hand their view to the model ─────────────────────


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


def test_state_command_queues_the_shown_rows_for_the_model(_restore_namespace):
    mt = _seeded_view_session()
    shown = _state(mt, "")
    assert "| 2 | `dcsync:corp.local` |" in shown
    view = el.take_operator_view(5)
    assert view.startswith("[Sage ledger view]") and "via `/state`" in view
    assert "| 2 | `dcsync:corp.local` | krbtgt-hash:corp.local | `pending` | 12 |" in view
    assert "TARGET-OUTPUT-PREVIEW" not in view
    assert "compromise CORP" in view


def test_state_usage_error_does_not_queue_a_view(_restore_namespace):
    mt = _seeded_view_session()
    _state(mt, "remove")
    assert el.take_operator_view(5) == ""


def test_turn_end_table_queues_the_same_rows_and_unchanged_queues_nothing(_restore_namespace):
    mt = _seeded_view_session()
    model = object.__new__(graph_model.Model)
    model.mythic_client = mt
    model.channel_id = 7
    shown = asyncio.run(model._engagement_state_turn_summary_text())
    assert "| 1 | `kerberoast:svc` |" in shown and "| 2 | `dcsync:corp.local` |" in shown
    view = el.take_operator_view(7)
    assert "end-of-turn engagement-state table" in view and "| 2 | `dcsync:corp.local` |" in view

    assert "unchanged" in asyncio.run(model._engagement_state_turn_summary_text())
    assert el.take_operator_view(7) == ""

    # A retraction changes neither technique, target nor historical status, but it is a change.
    _state(mt, "remove 1")
    el.take_operator_view(5)
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


def test_next_request_carries_the_view_once_before_the_operator_message():
    el.record_operator_view(9, "[Sage ledger view] | 1 | `kerberoast:svc` |")
    el.record_operator_view(10, "[Sage ledger view] other channel")
    m, streamed = _inventory_model(9)

    humans = _drive(m)
    assert [msg.content for msg in humans] == ["[Sage ledger view] | 1 | `kerberoast:svc` |", "list_callbacks"]
    view_msg, user_msg = humans
    assert graph_model._is_internal_human_message(view_msg)
    assert not graph_model._is_internal_human_message(user_msg)
    assert user_msg.additional_kwargs.get("_operator_input") is True
    assert view_msg.additional_kwargs["_request_id"] == user_msg.additional_kwargs["_request_id"]
    assert not any("Sage ledger view" in text for text in streamed)

    # Consumed: a second request adds no second copy, and another channel's view is untouched.
    humans = _drive(m)
    assert sum("Sage ledger view" in str(msg.content) for msg in humans) == 1
    assert el.take_operator_view(10) == "[Sage ledger view] other channel"


def test_request_without_a_pending_view_adds_nothing():
    m, _ = _inventory_model(11)
    humans = _drive(m)
    assert [msg.content for msg in humans] == ["list_callbacks"]
