"""ISC-69 — the identical-re-proposal guard.

Replaces the round-2 backstop, which counted consecutive ZERO-MESSAGE node returns. The 2026-07-28
channel-57 loop emitted a denial ToolMessage plus a fresh AIMessage every cycle, so that counter
reset each time and nine approval cards went past it. Progress is not "messages moved"; it is "the
effect boundary was crossed". These tests drive the real `_surface_hitl_interrupt`.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import Model, _ZERO_PROGRESS_DELEGATION_CAP


class _Interrupt:
    def __init__(self, value):
        self.value = value


def _event(command="ticket_cache_list", callback=7):
    return {"__interrupt__": [_Interrupt({"action_requests": [{
        "name": "issue_task_and_waitfor_task_output",
        "args": {"command": command, "callback_display_id": callback, "parameters": ""},
    }]})]}


class _Client:
    """Stands in for MythicTools; only the issued-task marker matters here."""
    def __init__(self):
        self._last_issued_task_display_id = None


def _model():
    m = Model.__new__(Model)
    m.mythic_client = _Client()
    m._hitl_card_pending = False
    m.cards = []
    m.streamed = []

    async def emitter(action_requests):
        m.cards.append(action_requests)

    async def streamer(text):
        m.streamed.append(text)

    m._hitl_card_emitter = emitter
    m._stream_message_to_mythic = streamer
    m.bind_supervised_request_proposal = lambda ar: None
    return m


def _surface(m, event):
    return asyncio.run(m._surface_hitl_interrupt(event))


def test_third_identical_card_is_refused():
    """The channel-57 shape: same action, re-carded, no task ever issued."""
    m = _model()
    for _ in range(_ZERO_PROGRESS_DELEGATION_CAP - 1):
        assert _surface(m, _event()) is True
    assert len(m.cards) == 2, "first two identical proposals still get a card"

    assert _surface(m, _event()) is True
    assert len(m.cards) == 2, "third identical proposal must NOT emit another card"
    assert m.streamed, "operator must be told why it stopped"
    assert "stopped" in m.streamed[-1].lower()


def test_a_different_action_resets_the_streak():
    m = _model()
    _surface(m, _event(command="ticket_cache_list"))
    _surface(m, _event(command="ticket_cache_list"))
    _surface(m, _event(command="whoami"))          # different fingerprint
    _surface(m, _event(command="whoami"))
    assert len(m.cards) == 4, "distinct actions must never be suppressed"
    assert not m.streamed


def test_real_progress_resets_the_streak():
    """ISC-61: three legitimate identical runs must not be truncated.

    A moving Mythic task display id means the effect boundary was actually crossed.
    """
    m = _model()
    for i in range(5):
        m.mythic_client._last_issued_task_display_id = 100 + i  # each proposal actually ran
        assert _surface(m, _event()) is True
    assert len(m.cards) == 5, "an action that keeps executing is healthy, not a loop"
    assert not m.streamed


def test_guard_counts_only_consecutive_repeats_without_execution():
    """Two repeats, then a real execution, then two more repeats — never three in a row."""
    m = _model()
    _surface(m, _event())
    _surface(m, _event())
    m.mythic_client._last_issued_task_display_id = 42   # it finally ran
    _surface(m, _event())
    _surface(m, _event())
    assert len(m.cards) == 4
    assert not m.streamed, "the streak was broken by a real effect"


def test_guard_survives_arguments_that_drift_between_proposals():
    """ISC-69a: the channel-57 loop did NOT repeat identical arguments.

    Recorded coverage denials showed `luid: ""` on one cycle and `luid: "0"` on a later one. Keyed on
    the full canonical argument dict (the original implementation), the streak reset every cycle and
    the guard would have sat silent through all nine cards. Keyed on (tool, command, callback) it
    fires.
    """
    m = _model()

    def drifting(luid):
        return {"__interrupt__": [_Interrupt({"action_requests": [{
            "name": "issue_task_and_waitfor_task_output",
            "args": {
                "command": "ticket_cache_list",
                "callback_display_id": 7,
                "parameters": {"getSystemTickets": "false", "luid": luid},
            },
        }]})]}

    assert _surface(m, drifting("")) is True
    assert _surface(m, drifting("0")) is True
    assert len(m.cards) == 2, "first two still card the operator"

    assert _surface(m, drifting("0x5b16c")) is True
    assert len(m.cards) == 2, "third cosmetically-different re-proposal must be refused"
    assert m.streamed and "stopped" in m.streamed[-1].lower()


def test_same_command_different_callback_is_not_a_repeat():
    """Target is part of the key — the same command against another callback is new work."""
    m = _model()
    _surface(m, _event(callback=7))
    _surface(m, _event(callback=8))
    _surface(m, _event(callback=9))
    assert len(m.cards) == 3
    assert not m.streamed
