"""Supervised step bounds and the Sage-authored step outcome.

Pure cases cover the accounting; assembled cases drive the real graph. The scripted handoff harness covers the
tool-call and active-time bounds with a neutral probe tool; the native chat driver covers the one-guarded-action,
rejection and scope cases through real approval cards, with the guarded tool replaced by an inert recorder.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
import pytest

_HARNESS_PATH = Path(__file__).resolve().with_name("test_scripted_handoff.py")
_SPEC = importlib.util.spec_from_file_location("scripted_handoff_harness_bounds", _HARNESS_PATH)
_harness = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_harness)

from ai.langgraph import model as model_module  # noqa: E402
from ai.langgraph import step_bounds as sb  # noqa: E402
from ai.langgraph.mythic_tools import GUARDED_TOOLS, MythicTools  # noqa: E402
from sage_chat.headless import build_chat_request  # noqa: E402
from sage_chat.service import bind_channel_thread_id  # noqa: E402
from sage_chat.session import drop_channel_session  # noqa: E402
from tests.test_hitl_steer_current_intent import (  # noqa: E402
    NEW_ACTION, OLD_ACTION, ORIGINAL, _NativeDriverChat, _NativeScriptedModel, _call, _cards, _input_response,
)

GUARDED = frozenset(GUARDED_TOOLS)
ISSUE = "issue_task_and_waitfor_task_output"


# --- pure accounting -------------------------------------------------------------------------------------------

def _tool(name: str, content: str = "ok", status: str = "success") -> ToolMessage:
    return ToolMessage(content=content, name=name, tool_call_id=f"id-{name}-{content[:5]}", status=status)


def _record(**fields: Any) -> sb.StepRecord:
    return sb.StepRecord(request_id="r", delegation_seq=1, worker="Mythic_Operator", started=0.0, **fields)


def test_bounds_default_to_the_baseline_and_read_positive_overrides():
    assert sb.StepBounds.from_env({}) == sb.StepBounds(tool_calls=20, active_seconds=600.0, consecutive_errors=3)
    assert sb.StepBounds.from_env({"SAGE_STEP_MAX_TOOL_CALLS": "12", "SAGE_STEP_MAX_ACTIVE_SECONDS": "90"}) \
        == sb.StepBounds(tool_calls=12, active_seconds=90.0)
    assert sb.StepBounds.from_env({"SAGE_STEP_MAX_TOOL_CALLS": "0"}).tool_calls == 20
    assert sb.StepBounds.from_env({"SAGE_STEP_MAX_TOOL_CALLS": "many"}).tool_calls == 20


def test_activity_counts_executions_not_queued_proposals_and_marks_denials():
    activity = sb.step_activity([
        _tool("get_task_output"),
        _tool(ISSUE, "[Queued] issue_task_and_waitfor_task_output will be proposed later"),
        _tool(ISSUE, "[DENIED by operator] issue_task_and_waitfor_task_output was not executed."),
        _tool("list_callbacks", "boom", status="error"),
    ], GUARDED)
    assert activity.tool_calls == 3
    assert activity.guarded == ((ISSUE, True),)
    assert (activity.executed, activity.rejected, activity.error_streak) == (0, 1, 1)


@pytest.mark.parametrize("calls, errors, active, reached", [
    (19, 0, 0.0, None),
    (20, 0, 0.0, "tool_calls"),
    (5, 3, 0.0, "consecutive_errors"),
    (5, 2, 599.0, None),
    (5, 0, 600.0, "active_time"),
])
def test_bound_reached_at_each_limit_and_not_before(calls, errors, active, reached):
    activity = sb.StepActivity(tool_calls=calls, error_streak=errors)
    result = sb.bound_reached(activity, _record(active_seconds=active), sb.StepBounds())
    assert (result[0] if result else None) == reached


@pytest.mark.parametrize("brief, tool_name, capability, violates", [
    (None, ISSUE, "", False),
    ({}, ISSUE, "", False),
    ({"allowed_tools": [ISSUE]}, ISSUE, "", False),
    ({"allowed_tools": [ISSUE]}, "upload_file_by_file_uuid", "", True),
    ({"allowed_capabilities": ["control_plane"]}, ISSUE, "", True),
    ({"allowed_capabilities": ["off_catalog"]}, ISSUE, "", False),
    ({"allowed_capabilities": ["collect-graph"]}, "execute_capability", "collect-graph", False),
    ({"allowed_capabilities": ["collect-graph"]}, "execute_capability", "dcsync-krbtgt", True),
    ({"allowed_capabilities": ["collect-graph"]}, ISSUE, "", True),
])
def test_scope_admits_only_what_the_brief_names(brief, tool_name, capability, violates):
    assert bool(sb.scope_violation(brief, tool_name, capability)) is violates


def test_outcome_separates_active_action_and_waiting_time():
    record = _record(active_seconds=40.0, action_seconds=500.0)
    record.end(sb.StepStatus.ACTION_EXECUTED, "guarded_actions", "one action ran")
    outcome = sb.step_outcome(record, sb.StepActivity(tool_calls=2, guarded=((ISSUE, False),)), now=4140.0,
                              tasks_issued={"12", "9"})
    assert outcome["status"] == "action_executed" and outcome["tasks_issued"] == ["9", "12"]
    assert (outcome["active_seconds"], outcome["action_seconds"], outcome["waiting_seconds"]) == (40.0, 500.0, 3600.0)
    text = sb.render_outcome(outcome)
    assert "choose the next step" in text and "did not cancel any target-side task" in text
    pending = sb.step_outcome(record, sb.StepActivity(), now=1.0, status=sb.StepStatus.TASK_PENDING)
    assert "choose the next step" not in sb.render_outcome(pending)


# --- assembled: scripted harness with a neutral probe tool ------------------------------------------------------

PROMPT = "Record probe steps for this request."
READ_BRIEF = {"handoff_title": "Probe", "allowed_capabilities": ["control_plane"], "expected_result": "Probe steps."}


@pytest.fixture
def probe(monkeypatch):
    state = {"calls": [], "clock": 0.0, "advance": 0.0}

    @tool
    def baseline_probe_step(step: int) -> str:
        """Test-only probe. Records one worker step and returns immediately."""
        state["calls"].append(step)
        state["clock"] += state["advance"]
        return f"probe step {step} recorded"

    original = model_module.filter_tools_by_frontmatter

    def with_probe(agent: str, tools):
        selected = original(agent, tools)
        return [*selected, baseline_probe_step] if agent == "mythic_operator" else selected

    monkeypatch.setattr(model_module, "filter_tools_by_frontmatter", with_probe)
    return state


def _handoff(args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "transfer_to_Mythic_Operator", "args": args, "id": call_id, "type": "tool_call"}])


def _probe_step(step: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "baseline_probe_step", "args": {"step": step}, "id": f"probe_{step}", "type": "tool_call"}])


def _run_probe(script: list[AIMessage], probe: dict, bounds: sb.StepBounds) -> dict:
    harness = _harness._Harness(script)
    original_build = harness._build

    def build(script):
        model = original_build(script)
        model._step_bounds_config = bounds
        model._step_clock = lambda: probe["clock"]
        return model

    harness._build = build
    limit = _harness._RECURSION_LIMIT
    _harness._RECURSION_LIMIT = 200
    try:
        return asyncio.run(harness.run(PROMPT, assisted=True))
    finally:
        _harness._RECURSION_LIMIT = limit


def _outcomes(messages: list) -> list[dict]:
    return [m.additional_kwargs["_step_outcome"] for m in messages if "_step_outcome" in m.additional_kwargs]


def test_active_time_ends_a_step_at_the_next_model_boundary(probe):
    probe["advance"] = 700.0  # each neutral read takes longer than the whole active-time bound
    script = [_handoff(READ_BRIEF, "h1"), _probe_step(0), AIMessage(content="final")]
    result = _run_probe(script, probe, sb.StepBounds())

    assert probe["calls"] == [0]
    [outcome] = _outcomes(result["supervisor_messages"])
    assert (outcome["status"], outcome["bound"]) == ("budget_exhausted", "active_time")
    assert outcome["active_seconds"] >= 600


def test_a_bounded_step_hands_control_back_and_the_next_step_starts_fresh(probe):
    bounds = sb.StepBounds(tool_calls=3)
    script = [_handoff(READ_BRIEF, "h1"), *(_probe_step(i) for i in range(3)),
              _handoff(READ_BRIEF, "h2"), *(_probe_step(i) for i in range(3, 6)),
              AIMessage(content="final")]
    result = _run_probe(script, probe, bounds)

    assert probe["calls"] == list(range(6))
    outcomes = _outcomes(result["supervisor_messages"])
    assert [(o["status"], o["tool_calls"]) for o in outcomes] == [("budget_exhausted", 3)] * 2
    assert not any(m.additional_kwargs.get("_is_final_report") and "Blocked" in str(m.content)
                   for m in result["supervisor_messages"])


def test_conversation_lane_steps_are_not_bounded(probe):
    real_build = _harness.build_request_contract
    _harness.build_request_contract = lambda **kw: real_build(**{**kw, "mode": "conversation"})
    try:
        script = [_handoff({"handoff_title": "Probe"}, "h1"), *(_probe_step(i) for i in range(4)),
                  AIMessage(content="worker done"), AIMessage(content="final")]
        result = _run_probe(script, probe, sb.StepBounds(tool_calls=2))
    finally:
        _harness.build_request_contract = real_build
    assert probe["calls"] == list(range(4))
    assert _outcomes(result["supervisor_messages"]) == []


@pytest.mark.parametrize("mode, bounded", [("auto", False), ("supervised", True), ("conversation", True)])
def test_auto_workers_are_built_without_the_bound(mode, bounded):
    model = SimpleNamespace(mode=mode, _get_base_chat_model=lambda: None)
    middleware = model_module.Model._context_middleware(model, agent_name="Mythic_Operator")
    assert any(isinstance(m, model_module._SupervisedStepBoundMiddleware) for m in middleware) is bounded


def test_allowed_tools_offer_the_workers_declared_guarded_tools(monkeypatch):
    def schema(agent):
        tool_ = model_module._create_handoff_tool(agent_name=agent, brief_check=lambda *args: None)
        prop = tool_.tool_call_schema.model_json_schema()["properties"]["allowed_tools"]
        return next(branch["items"] for branch in prop["anyOf"] if branch.get("type") == "array").get("enum")

    assert schema("Mythic_Operator") == list(model_module._worker_guarded_tool_names("Mythic_Operator"))
    assert ISSUE in schema("Mythic_Operator") and "create_payload" not in schema("Mythic_Operator")
    original = model_module.get_prompt_tools
    monkeypatch.setattr(model_module, "get_prompt_tools",
                        lambda name: [*original(name), "create_payload"] if name == "mythic_operator" else original(name))
    assert "create_payload" in schema("Mythic_Operator")


# --- assembled: native approval cards -------------------------------------------------------------------------

BRIEF = {"callback_display_id": 5, "targets": ["WORKSTATION-5"], "expected_result": "The callback's privileges."}


async def _native(script: list, decision: str, *, clock: dict | None = None):
    effects: list[str] = []

    @tool
    async def issue_task_and_waitfor_task_output(command: str, parameters: str, callback_display_id: int) -> str:
        """Issue one callback task and wait for its output."""
        effects.append(command)
        if clock is not None:
            clock["now"] += 500.0  # the Mythic wait inside a guarded action
        return "offline effect"

    model = model_module.Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline step bounds",
        config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
        task_id=0, agent_task_id="step-bounds", mode="supervised", operation_id=1, channel_id=1, apitoken_id=0,
    )
    sink = MagicMock()
    preparer = MythicTools(agent_task_id="offline-step-bounds")
    preparer.client = object()

    async def live_command(command, callback):
        return {"status": "available", "command": command}

    async def known_schema(command, callback):
        return []

    preparer._authenticate_live_command = live_command
    preparer._fetch_command_schema = known_schema
    sink._prepare_supervised_issue_action = preparer._prepare_supervised_issue_action
    sink.get_tools.side_effect = lambda names: [issue_task_and_waitfor_task_output] if ISSUE in names else []
    sink._recon_snapshot_cache = {("list_callbacks", "all"): [{"id": 5, "host": "WORKSTATION-5"}]}
    model.mythic_client = sink
    model._payload_names = []
    model.memory = InMemorySaver()
    fake = _NativeScriptedModel(messages=iter(script))  # one script shared by every agent, in call order
    model._get_base_chat_model = lambda *a, **k: fake
    if clock is not None:
        model._step_clock = lambda: clock["now"]
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = _NativeDriverChat(model)
    try:
        first = build_chat_request(ORIGINAL, channel_id=1, operation_id=1, request_id=1,
                                   config={"mode": "supervised"})
        bind_channel_thread_id(first, model)
        await chat.chat(first)
        cards = _cards(chat)
        if decision and cards:
            if clock is not None:
                clock["now"] += 3600.0  # the operator takes an hour to decide
            second = build_chat_request("", channel_id=1, operation_id=1, request_id=2, config={"mode": "supervised"})
            second.InputResponse = _input_response(decision, cards[0])
            await chat.chat(second)
        snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
        # Accepting a card re-emits that same card with its resolution; count distinct proposals.
        proposed = list(dict.fromkeys(str(card.get("data", {}).get("arguments", {}).get("command"))
                                      for card in _cards(chat)))
        return effects, proposed, _outcomes(snapshot.values.get("supervisor_messages", []))
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


def _script(brief: dict) -> list:
    handoff = {"handoff_title": "inspect token privileges", "handoff_instruction": ORIGINAL, **brief}
    return [_call("transfer_to_Mythic_Operator", handoff, 1), _call(OLD_ACTION["name"], OLD_ACTION["args"], 2),
            _call(NEW_ACTION["name"], NEW_ACTION["args"], 3), AIMessage(content="Final answer.")]


def test_one_executed_action_ends_the_step_before_a_second_card():
    clock = {"now": 0.0}
    effects, cards, outcomes = asyncio.run(_native(_script({**BRIEF, "allowed_tools": [ISSUE]}), "accept",
                                                   clock=clock))

    assert effects == ["getprivs"]          # the second proposal never ran
    assert cards == ["getprivs"]            # and never reached a card
    [outcome] = outcomes
    assert (outcome["status"], outcome["bound"], outcome["guarded_executed"]) == ("action_executed",
                                                                               "guarded_actions", 1)
    # The hour awaiting approval and the guarded action's own wait do not count as active time.
    assert outcome["active_seconds"] == 0.0 and outcome["action_seconds"] == 500.0
    assert outcome["waiting_seconds"] == 3600.0


def test_an_operator_rejection_ends_the_step_without_another_proposal_card():
    effects, cards, outcomes = asyncio.run(_native(_script({**BRIEF, "allowed_tools": [ISSUE]}), "reject"))

    assert effects == [] and cards == ["getprivs"]
    [outcome] = outcomes
    assert (outcome["status"], outcome["guarded_rejected"]) == ("rejected", 1)


def test_a_guarded_call_outside_the_brief_is_removed_before_any_card():
    script = _script({**BRIEF, "allowed_tools": ["upload_file_by_file_uuid"]})[:2] + [AIMessage(content="Final.")]
    effects, cards, outcomes = asyncio.run(_native(script, ""))

    assert effects == [] and cards == []
    [outcome] = outcomes
    assert (outcome["status"], outcome["bound"]) == ("rejected_by_scope", "scope")
    assert "allowed_tools" in outcome["detail"]
