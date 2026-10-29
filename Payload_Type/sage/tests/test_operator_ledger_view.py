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


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    el._OPERATOR_VIEWS.clear()
    yield
    el._OPERATOR_VIEWS.clear()


# ── permanent hop numbers ────────────────────────────────────────────────────────────────


def test_numbers_assigned_in_order_and_idempotent():
    data = {"hops": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    el.assign_hop_numbers(data)
    assert [h["number"] for h in data["hops"]] == [1, 2, 3]
    assert data["next_hop_number"] == 4
    el.assign_hop_numbers(data)
    assert [h["number"] for h in data["hops"]] == [1, 2, 3]
    assert data["next_hop_number"] == 4


def test_remove_leaves_gap_and_later_numbers_do_not_shift():
    data = {"hops": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    data, removed = el.remove_hops(data, ["1"])
    assert removed == 1
    assert [(h["id"], h["number"]) for h in data["hops"]] == [("b", 2), ("c", 3)]
    # "3" still means hop c; a positional row number would have pointed past the end.
    data, changed = el.set_hop_status(data, "3", "failed")
    assert changed == 1 and data["hops"][1]["status"] == "failed"


def test_removed_highest_number_is_never_reused():
    data = {"hops": [{"id": "a"}, {"id": "b"}]}
    data, _ = el.remove_hops(data, ["2"])
    data["hops"].append({"id": "new"})
    el.assign_hop_numbers(data)
    assert [h["number"] for h in data["hops"]] == [1, 3]


def test_unnumbered_rewrite_keeps_numbers_from_previous_by_label():
    previous = {"hops": [{"id": "a", "number": 4}, {"id": "b", "number": 7}], "next_hop_number": 9}
    rebuilt = {"hops": [{"id": "b"}, {"id": "a"}, {"id": "c"}]}  # dataclass round-trip drops numbers
    el.assign_hop_numbers(rebuilt, previous=previous)
    assert [(h["id"], h["number"]) for h in rebuilt["hops"]] == [("b", 7), ("a", 4), ("c", 9)]
    assert rebuilt["next_hop_number"] == 10


def test_duplicate_number_is_renumbered_not_shared():
    data = {"hops": [{"id": "a", "number": 2}, {"id": "b", "number": 2}]}
    el.assign_hop_numbers(data)
    assert [h["number"] for h in data["hops"]] == [2, 3]


@pytest.mark.parametrize("selector", ["0", "5", "-1", "1.0"])
def test_selector_that_is_not_an_existing_hop_number_matches_nothing(selector):
    data = {"hops": [{"id": "a"}, {"id": "b"}]}
    data, removed = el.remove_hops(data, [selector])
    assert removed == 0 and len(data["hops"]) == 2


def test_runtime_load_numbers_an_old_ledger_and_save_persists_it():
    el.save({"hops": [{"id": "a", "status": "pending"}, {"id": "b", "status": "failed"}]}, "eng")
    loaded = el.load_runtime("eng")
    assert [h["number"] for h in loaded["hops"]] == [1, 2]
    el.save_runtime(loaded, "eng")
    raw = el.load("eng")
    assert [h["number"] for h in raw["hops"]] == [1, 2] and raw["next_hop_number"] == 3


def test_in_memory_persist_keeps_disk_numbers(monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "persist-numbers")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    state = engagement_state.EngagementState(objective="test")
    for target in ("one.local", "two.local", "three.local"):
        state = engagement_state.record_hop_result(
            state, "gpo-abuse", target, "pending", {"source": "test"}, "2026-10-29T00:00:00Z",
        )
    mt._engagement_hops = list(state.hops)
    mt._persist_engagement_ledger()
    path = Path(mt._engagement_ledger_path())
    first = json.loads(path.read_text())
    assert [h["number"] for h in first["hops"]] == [1, 2, 3]

    # The operator removes hop 1 on disk; the in-memory writer later persists hops 2 and 3 plus a new one.
    data = el.load_runtime(mt._eng_key())
    data, _ = el.remove_hops(data, ["1"])
    el.save_runtime(data, mt._eng_key())
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test", hops=list(mt._engagement_hops[1:])),
        "gpo-abuse", "four.local", "pending", {"source": "test"}, "2026-10-29T00:00:00Z",
    )
    mt._engagement_hops = list(state.hops)
    mt._persist_engagement_ledger()
    second = json.loads(path.read_text())
    assert [(h["target"], h["number"]) for h in second["hops"]] == [
        ("two.local", 2), ("three.local", 3), ("four.local", 4),
    ]


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


def test_state_command_queues_the_shown_rows_for_the_model(monkeypatch):
    el.save({"objective": "compromise CORP", "hops": _hops()}, "Operation_Chimera_1")

    async def _resolve(_model, _request):
        return "Operation_Chimera_1"

    monkeypatch.setattr(slash, "_resolve_chat_engagement_id", _resolve)
    shown = asyncio.run(slash._handle_state(None, build_chat_request("x", channel_id=5), ""))
    assert "| 4 | `dcsync:corp.local` |" in shown
    view = el.take_operator_view(5)
    assert view.startswith("[Sage ledger view]") and "via `/state`" in view
    assert "| 4 | `dcsync:corp.local` | krbtgt-hash:corp.local | `pending` | 12 |" in view
    assert "TARGET-OUTPUT-PREVIEW" not in view
    assert "compromise CORP" in view


def test_state_usage_error_does_not_queue_a_view(monkeypatch):
    async def _resolve(_model, _request):
        return "Operation_Chimera_1"

    monkeypatch.setattr(slash, "_resolve_chat_engagement_id", _resolve)
    asyncio.run(slash._handle_state(None, build_chat_request("x", channel_id=5), "set 1"))
    assert el.take_operator_view(5) == ""


class _SummaryClient:
    def __init__(self, key):
        self._key = key
        self._last_issued_task_display_id = ""
        self.client = None

    def _eng_key(self):
        return self._key


def test_turn_end_table_queues_the_same_rows_and_unchanged_queues_nothing():
    el.save({"hops": _hops()}, "eng-turn")
    model = object.__new__(graph_model.Model)
    model.mythic_client = _SummaryClient("eng-turn")
    model.channel_id = 7
    shown = asyncio.run(model._engagement_state_turn_summary_text())
    assert "| 1 | `kerberoast:svc` |" in shown and "| 4 | `dcsync:corp.local` |" in shown
    view = el.take_operator_view(7)
    assert "end-of-turn engagement-state table" in view and "| 4 | `dcsync:corp.local` |" in view

    assert "unchanged" in asyncio.run(model._engagement_state_turn_summary_text())
    assert el.take_operator_view(7) == ""


# ── the assembled request path ───────────────────────────────────────────────────────────


def _inventory_model(channel_id):
    """A minimal Model driven through invoke() on the deterministic callback-inventory path."""
    m = object.__new__(graph_model.Model)
    m.mode = "supervised"
    m.command_name = "chat"
    m.channel_id = channel_id
    m._autonomous_solve = False
    m._supervised_objective_active = False
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
