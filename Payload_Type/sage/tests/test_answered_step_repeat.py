"""A supervised step that already answered is not sent again unchanged.

Live supervised runs re-sent a step that had answered: a read the worker had fully answered, a worker's report that
it needed the operator to supply a tool, and an "already achieved" capability verdict. Each repeat returned the same
answer until the no-progress stop. Sage now refuses an unchanged repeat of the step that just answered: it hands the
answer back once, and a second repeat ends the request with that answer.

The scripted checks prove the handoff boundary, not that a real model then calls respond_to_user.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
import pytest

_HARNESS_PATH = Path(__file__).resolve().with_name("test_scripted_handoff.py")
_SPEC = importlib.util.spec_from_file_location("scripted_handoff_harness_answered", _HARNESS_PATH)
_harness = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_harness)

from ai.langgraph import model as model_module  # noqa: E402
from ai.langgraph import step_bounds as sb  # noqa: E402
from tests import test_step_bounds as native  # noqa: E402

READ = {"handoff_title": "Fetch task 88 output", "handoff_instruction": "Fetch task 88 output.",
        "allowed_capabilities": ["control_plane"], "expected_result": "Task 88 output."}
ANSWER = "Task 88 failed: Rubeus.exe is not staged. Upload it to the tools drop zone to continue."


def _handoff(args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "transfer_to_Mythic_Operator", "args": args, "id": call_id, "type": "tool_call"}])


def _run(script: list[AIMessage]) -> dict:
    limit = _harness._RECURSION_LIMIT
    _harness._RECURSION_LIMIT = 60
    try:
        return asyncio.run(_harness._Harness(script).run("Check the results of task 88.", assisted=True))
    finally:
        _harness._RECURSION_LIMIT = limit


def _steps(messages: list) -> int:
    return sum("_step_outcome" in m.additional_kwargs for m in messages)


def _final(messages: list) -> AIMessage:
    """The Supervisor's last answer: its own final text, or a final report Sage wrote for it."""
    return next(m for m in reversed(messages) if isinstance(m, AIMessage) and m.name == "Supervisor"
                and str(m.content).strip() and not m.tool_calls)


def _stops(messages: list) -> list:
    return [m for m in messages if "Stopped" in str(m.content) or "Halted" in str(m.content)]


# --- assembled: the scripted graph ------------------------------------------------------------------------------

def test_an_answered_step_sent_again_hands_its_answer_back_without_dispatch():
    script = [_handoff(READ, "h1"), AIMessage(content=ANSWER), _handoff(READ, "h2"),
              AIMessage(content="Rubeus.exe is missing; upload it to continue.")]
    messages = _run(script)["supervisor_messages"]

    assert _steps(messages) == 1  # the repeat never reached the worker
    [handback] = [m for m in messages if getattr(m, "name", "") == "transfer_to_Mythic_Operator"
                  and "not dispatched" in str(m.content)]
    assert ANSWER in str(handback.content)
    assert str(_final(messages).content) == "Rubeus.exe is missing; upload it to continue."
    assert _stops(messages) == []


def test_a_second_unchanged_repeat_ends_the_request_with_the_answer():
    script = [_handoff(READ, "h1"), AIMessage(content=ANSWER), _handoff(READ, "h2"), _handoff(READ, "h3"),
              AIMessage(content="never reached")]
    messages = _run(script)["supervisor_messages"]

    assert _steps(messages) == 1
    final = _final(messages)
    assert str(final.content) == ANSWER
    assert final.additional_kwargs["_turn_outcome"] == "checkpoint"
    assert _stops(messages) == []


@pytest.mark.parametrize("change", [
    {"expected_result": "Task 89 output."},
    {"handoff_title": "Fetch task 89 output"},
])
def test_a_changed_step_is_dispatched(change):
    script = [_handoff(READ, "h1"), AIMessage(content=ANSWER), _handoff({**READ, **change}, "h2"),
              AIMessage(content="Task 89 succeeded."), AIMessage(content="final")]
    messages = _run(script)["supervisor_messages"]

    assert _steps(messages) == 2
    assert str(_final(messages).content) == "final"


def test_the_record_is_only_the_step_that_just_answered():
    """A -> B -> A is planning, not a repeat: only a back-to-back resend is refused."""
    other = {**READ, "handoff_title": "List callbacks", "expected_result": "Live callbacks."}
    script = [_handoff(READ, "h1"), AIMessage(content=ANSWER), _handoff(other, "h2"), AIMessage(content="cb 5"),
              _handoff(READ, "h3"), AIMessage(content=ANSWER), AIMessage(content="final")]
    messages = _run(script)["supervisor_messages"]

    assert _steps(messages) == 3


def test_a_bounded_step_is_still_continued_with_the_same_step():
    """A step that reached its tool-call bound has not answered; the same step again continues it."""
    outcome = {"status": "budget_exhausted", "tasks_issued": [], "credentials_added": [], "guarded_rejected": 0}
    assert not sb.answered_without_effect(outcome)


# --- pure: what counts as answered, and what is compared --------------------------------------------------------

@pytest.mark.parametrize("fields, answered", [
    ({"status": "ended"}, True),
    ({"status": "action_executed"}, True),            # e.g. an "already achieved" capability verdict
    ({"status": "precondition_unsatisfiable"}, True),
    ({"status": "action_executed", "tasks_issued": ["41"]}, False),
    ({"status": "action_executed", "credentials_added": [3]}, False),
    ({"status": "rejected", "guarded_rejected": 1}, False),
    ({"status": "ended", "guarded_rejected": 1}, False),
    ({"status": "rejected_by_scope"}, False),
    ({"status": "budget_exhausted"}, False),
    ({"status": "task_pending", "tasks_issued": ["41"]}, False),
])
def test_answered_without_effect(fields, answered):
    outcome = {"tasks_issued": [], "credentials_added": [], "guarded_rejected": 0, **fields}
    assert sb.answered_without_effect(outcome) is answered


def test_a_delegation_without_a_brief_is_never_compared():
    # Specialist questions with no structural brief differ only in their prose.
    assert model_module._answered_step_key("BloodHound", "Find a path", None) == ""
    assert model_module._answered_step_key("BloodHound", "Find a path", {"targets": [], "expected_result": ""}) == ""
    brief = {"allowed_capabilities": ["control_plane"], "expected_result": "x"}
    assert model_module._answered_step_key("Mythic_Operator", "Read  TASK", brief) \
        == model_module._answered_step_key("Mythic_Operator", "read task", brief)


# --- assembled: native chat, an "already achieved" capability verdict -------------------------------------------

ACHIEVED = {
    "ok": True, "verdict": "achieved", "capability": "collect-graph",
    "reason": "requested capability effect is already achieved in the engagement ledger; no Mythic tasks issued",
    "issued": [], "recorded_effects": [], "achieved_effects": ["graph-built:north.test"],
    "proof_chain": [{"effect": "graph-built:north.test", "task_id": "41", "callback_id": "5"}],
    "stopped_after": "already_achieved",
}


def test_an_already_achieved_verdict_is_not_delegated_again(monkeypatch):
    ran: list[dict] = []

    @tool
    async def execute_capability(action: dict | str, inputs: dict | str | None = None) -> str:
        """Run one catalog capability."""
        ran.append({"action": action})
        return json.dumps(ACHIEVED)

    original = model_module.Model._rebuild_graph

    def rebuild(self, *args, **kwargs):
        sink = self.mythic_client
        issue = sink.get_tools.side_effect
        sink.get_tools.side_effect = lambda names: [*issue(names),
                                                    *([execute_capability] if "execute_capability" in names else [])]
        sink._engagement_footholds = [SimpleNamespace(callback_id="5", host="WORKSTATION-5", forest="north.test",
                                                      identity="NORTH\\operator")]
        return original(self, *args, **kwargs)

    monkeypatch.setattr(model_module.Model, "_rebuild_graph", rebuild)
    captured: list = []
    monkeypatch.setattr(native, "_outcomes", lambda messages: captured.extend(messages) or [])
    collect = {"handoff_title": "collect graph", "handoff_instruction": native.ORIGINAL, **native.BRIEF,
               "allowed_capabilities": ["collect-graph"], "targets": ["north.test"]}
    # A terminal capability result ends the worker's turn before another model call.
    script = [native._call("transfer_to_Mythic_Operator", collect, 1),
              native._call("transfer_to_Mythic_Operator", collect, 2),
              native._call("transfer_to_Mythic_Operator", collect, 3),
              AIMessage(content="never reached")]
    asyncio.run(native._native(script, "accept"))

    assert len(ran) == 1  # the capability ran once; neither repeat reached the worker
    assert _steps(captured) == 1
    final = _final(captured)
    assert "Executor verdict: `achieved` for `collect-graph`" in str(final.content)
    assert "task=41" in str(final.content)  # the existing proof is named
    assert final.additional_kwargs["_turn_outcome"] == "checkpoint"
    assert _stops(captured) == []
