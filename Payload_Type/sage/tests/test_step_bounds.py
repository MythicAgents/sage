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
    ({"allowed_capabilities": ["collect-graph", "dcsync-krbtgt"]}, "execute_capability", "dcsync-krbtgt", False),
    ({"allowed_capabilities": ["collect-graph"]}, "execute_capability", "collect-graph", False),
    ({"allowed_capabilities": ["collect-graph"]}, "execute_capability", "dcsync-krbtgt", True),
    ({"allowed_capabilities": ["collect-graph"]}, ISSUE, "", True),
])
def test_scope_admits_only_what_the_brief_names(brief, tool_name, capability, violates):
    assert bool(sb.scope_violation(brief, tool_name, capability)) is violates



def test_a_capability_scope_never_admits_a_raw_command_even_one_that_performs_it():
    """Collection runs as Sage's own procedure through execute_capability, so a capability scope never
    reopens raw tasking for it."""
    brief = {"allowed_capabilities": ["collect-graph"]}
    assert "execute_capability" in sb.scope_violation(brief, ISSUE, "")
    assert not sb.scope_violation(brief, "execute_capability", "collect-graph")


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
    async def issue_task_and_waitfor_task_output(command: str, parameters: str | dict, callback_display_id: int) -> str:
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



def test_a_collection_scope_removes_the_raw_collector_command_before_any_card():
    handoff = {"handoff_title": "collect graph", "handoff_instruction": ORIGINAL, **BRIEF,
               "allowed_capabilities": ["collect-graph"]}
    script = [_call("transfer_to_Mythic_Operator", handoff, 1),
              _call(ISSUE, {"command": "execute_assembly", "callback_display_id": 5,
                            "parameters": '{"assembly_name": "SharpHound.exe", "assembly_arguments": "-c All"}'}, 2),
              AIMessage(content="Final.")]
    effects, cards, outcomes = asyncio.run(_native(script, ""))

    assert effects == [] and cards == []
    [outcome] = outcomes
    assert (outcome["status"], outcome["bound"]) == ("rejected_by_scope", "scope")
    assert "execute_capability" in outcome["detail"]

def test_a_collection_scope_still_removes_an_unrelated_command_before_any_card():
    handoff = {"handoff_title": "collect graph", "handoff_instruction": ORIGINAL, **BRIEF,
               "allowed_capabilities": ["collect-graph"]}
    script = [_call("transfer_to_Mythic_Operator", handoff, 1), _call(OLD_ACTION["name"], OLD_ACTION["args"], 2),
              AIMessage(content="Final.")]
    effects, cards, outcomes = asyncio.run(_native(script, ""))

    assert effects == [] and cards == []
    [outcome] = outcomes
    assert (outcome["status"], outcome["bound"]) == ("rejected_by_scope", "scope")


def test_the_worker_is_told_its_scope_before_it_acts():
    """The step bound removes out-of-scope calls; the worker must see that scope in its own input first."""
    seen: list = []

    def worker(messages):
        seen.extend(messages)
        return AIMessage(content="Nothing to run.")

    handoff = {"handoff_title": "collect graph", "handoff_instruction": ORIGINAL, **BRIEF,
               "allowed_capabilities": ["collect-graph"]}
    script = [_call("transfer_to_Mythic_Operator", handoff, 1), worker, AIMessage(content="Final answer."),
              AIMessage(content="Final answer.")]
    asyncio.run(_native(script, ""))

    scopes = [message for message in seen if "<step_scope>" in str(getattr(message, "content", ""))]
    assert len(scopes) == 1
    text = scopes[0].content
    assert "only `execute_capability`, for `collect-graph`" in text and '"callback_id": "5"' in text
    # Sage's block sits immediately before the delegated operator input it scopes.
    assert seen.index(scopes[0]) == len(seen) - 2 and seen[-1].content == ORIGINAL


# --- what a scoped step is offered ----------------------------------------------------------------------------

GUIDANCE = sb.TECHNIQUE_GUIDANCE_TOOLS
WORKER_TOOLS = model_module.get_prompt_tools("mythic_operator")


@pytest.mark.parametrize("brief, withheld", [
    (None, set()),
    ({}, set()),
    # A procedure step: only execute_capability among guarded tools, and no hand-run recipes.
    ({"allowed_capabilities": ["collect-graph"]}, (GUARDED - {"execute_capability"}) | GUIDANCE),
    # A control-plane step takes no guarded action; its reads, guidance included, stay.
    ({"allowed_capabilities": ["control_plane"]}, GUARDED),
    # A single-command step keeps the guidance that explains the command.
    ({"allowed_tools": [ISSUE]}, GUARDED - {ISSUE}),
])
def test_a_step_is_offered_only_what_its_brief_admits(brief, withheld):
    names = [*GUARDED, *GUIDANCE, "list_callbacks", "handback_to_supervisor"]
    assert sb.withheld_tools(brief, names, GUARDED) == frozenset(withheld)


@pytest.mark.parametrize("capability", [c for c in model_module._handoff_brief.capability_choices()
                                        if c != sb.CONTROL_PLANE])
def test_every_capability_step_still_offers_execute_capability_to_the_worker(capability):
    """Every capability a brief may name resolves to execute_capability among the worker's offered tools."""
    guarded = frozenset(model_module._worker_guarded_tool_names("Mythic_Operator"))
    offered = set(WORKER_TOOLS) - sb.withheld_tools({"allowed_capabilities": [capability]}, WORKER_TOOLS, guarded)
    assert "execute_capability" in offered
    assert not (offered & (guarded - {"execute_capability"})) and not (offered & GUIDANCE)
    # Offering never admits more than the step bound enforces.
    assert all(not sb.scope_violation({"allowed_capabilities": [capability]}, name, capability)
               for name in offered & guarded)


def _offered_per_call(monkeypatch, brief: dict) -> list[set[str]]:
    """Run one supervised step and record the tool names bound for each model call."""
    bound: list[set[str]] = []

    def record(self, tools, **kwargs):
        bound.append({model_module._offered_tool_name(t) or getattr(t, "name", "") for t in tools})
        return self

    monkeypatch.setattr(_NativeScriptedModel, "bind_tools", record)

    @tool
    async def execute_capability(action: str, inputs: str = "") -> str:
        """Run one catalog capability."""
        return "not run"

    @tool
    async def get_ttp_guidance(goal: str) -> str:
        """Technique guidance."""
        return "a recipe"

    original = model_module.Model._rebuild_graph

    def rebuild(self, *args, **kwargs):
        sink = self.mythic_client
        issue = sink.get_tools.side_effect
        extra = {"execute_capability": execute_capability, "get_ttp_guidance": get_ttp_guidance}
        sink.get_tools.side_effect = lambda names: [*issue(names), *(extra[n] for n in names if n in extra)]
        return original(self, *args, **kwargs)

    monkeypatch.setattr(model_module.Model, "_rebuild_graph", rebuild)
    handoff = {"handoff_title": "scoped step", "handoff_instruction": ORIGINAL, **BRIEF, **brief}
    worker_calls: list[int] = []

    def worker(messages):
        worker_calls.append(len(bound))
        return AIMessage(content="Nothing to run.")

    script = [_call("transfer_to_Mythic_Operator", handoff, 1), worker, AIMessage(content="Final answer."),
              AIMessage(content="Final answer.")]
    asyncio.run(_native(script, ""))
    assert worker_calls, "the worker never ran"
    return [bound[index - 1] for index in worker_calls]


def test_a_collection_step_offers_the_worker_execute_capability_and_not_the_raw_path(monkeypatch):
    # Two procedures, so the step is not fully bound and the worker's model chooses between them.
    [offered] = _offered_per_call(monkeypatch, {"allowed_capabilities": ["collect-graph", "dcsync-krbtgt"]})

    assert "execute_capability" in offered
    assert ISSUE not in offered and "get_ttp_guidance" not in offered


def test_a_single_command_step_offers_the_command_and_its_guidance(monkeypatch):
    [offered] = _offered_per_call(monkeypatch, {"allowed_tools": [ISSUE]})

    assert ISSUE in offered and "get_ttp_guidance" in offered
    assert "execute_capability" not in offered


# --- a step whose brief fully binds one procedure ---------------------------------------------------------------

CG = {"capability": "collect-graph", "callback_id": "3"}


@pytest.mark.parametrize("brief, call", [
    # A fresh range: Sage knows only the callback. The operator's domain is passed as given.
    ({"allowed_capabilities": ["collect-graph"], "callback_display_id": "3", "targets": ["North.Test"]},
     {"action": {**CG, "domain": "north.test"}}),
    # No target, or a target that names a seen host: the collector's default domain (the callback user's) applies.
    ({"allowed_capabilities": ["collect-graph"], "callback_display_id": "3"}, {"action": CG}),
    ({"allowed_capabilities": ["collect-graph"], "callback_display_id": "3", "targets": ["CASTELBLACK"]},
     {"action": CG}),
    ({"allowed_capabilities": ["collect-graph"], "callback_display_id": "3",
      "targets": ["castelblack.north.test"]}, {"action": CG}),
    # Anything not fully bound leaves the choice to the worker.
    ({"allowed_capabilities": ["collect-graph"], "callback_display_id": "3",
      "targets": ["north.test", "south.test"]}, None),
    ({"allowed_capabilities": ["collect-graph"], "targets": ["north.test"]}, None),
    ({"allowed_capabilities": ["collect-graph", "dcsync-krbtgt"], "callback_display_id": "3",
      "targets": ["north.test"]}, None),
    ({"allowed_capabilities": ["dcsync-krbtgt"], "callback_display_id": "3", "targets": ["north.test"]}, None),
    ({"allowed_tools": [ISSUE], "callback_display_id": "3", "targets": ["north.test"]}, None),
    ({"allowed_capabilities": ["control_plane"]}, None),
    (None, None),
])
def test_only_a_fully_bound_procedure_is_proposed_by_sage(brief, call):
    assert sb.bound_procedure_call(brief, {"castelblack"}) == call


def _bound_step(monkeypatch, brief: dict, decision: str, worker_replies: int, *, footholds: bool = True):
    """Run a supervised collect-graph step on callback 5; return the worker's model calls and the capability runs."""
    ran: list[dict] = []
    worker_calls: list[int] = []
    cards: list[str] = []

    @tool
    async def execute_capability(action: dict | str, inputs: dict | str | None = None) -> str:
        """Run one catalog capability."""
        ran.append({"action": action, "inputs": inputs})
        return '{"ok": true, "status": "ingested"}'

    original = model_module.Model._rebuild_graph

    def rebuild(self, *args, **kwargs):
        sink = self.mythic_client
        issue = sink.get_tools.side_effect
        sink.get_tools.side_effect = lambda names: [*issue(names),
                                                    *([execute_capability] if "execute_capability" in names else [])]
        if footholds:
            sink._engagement_footholds = [SimpleNamespace(callback_id="5", host="WORKSTATION-5", forest="north.test",
                                                          identity="NORTH\\operator")]
        return original(self, *args, **kwargs)

    monkeypatch.setattr(model_module.Model, "_rebuild_graph", rebuild)
    original_card = model_module.Model._emit_tool_use_card

    async def card(self, *args, **kwargs):
        if kwargs.get("tool_name") == "execute_capability":
            cards.append(kwargs.get("status") or "completed")
        return await original_card(self, *args, **kwargs)

    monkeypatch.setattr(model_module.Model, "_emit_tool_use_card", card)

    def worker(messages):
        worker_calls.append(1)
        return AIMessage(content="Collection result read.")

    handoff = {"handoff_title": "collect graph", "handoff_instruction": ORIGINAL, **BRIEF, **brief}
    script = [_call("transfer_to_Mythic_Operator", handoff, 1), *([worker] * worker_replies),
              AIMessage(content="Final answer."), AIMessage(content="Final answer.")]
    asyncio.run(_native(script, decision))
    _bound_step.cards = cards
    return worker_calls, ran


def test_sage_proposes_a_fully_bound_collection_without_asking_the_worker_model(monkeypatch):
    brief = {"allowed_capabilities": ["collect-graph"], "targets": ["north.test"]}
    worker_calls, ran = _bound_step(monkeypatch, brief, "accept", worker_replies=1)

    # The approved call is exactly the one the brief binds; the worker's model only read the result.
    assert ran == [{"action": {"capability": "collect-graph", "callback_id": "5", "domain": "north.test"},
                    "inputs": None}]
    assert len(worker_calls) == 1
    # Its tool card opens once and closes once, as a model's call does; a live run failed reconciliation without it.
    assert _bound_step.cards[0] == "started" and _bound_step.cards.count("started") == 1
    assert len(_bound_step.cards) == 2


def test_a_rejected_bound_collection_runs_nothing(monkeypatch):
    brief = {"allowed_capabilities": ["collect-graph"], "targets": ["north.test"]}
    worker_calls, ran = _bound_step(monkeypatch, brief, "reject", worker_replies=1)

    assert ran == []


def test_a_fresh_range_binds_the_collection_on_the_callback_alone(monkeypatch):
    """No footholds or domains loaded, only the observed callback and its host: the callback binds the procedure,
    and the collector's own default domain applies."""
    brief = {"allowed_capabilities": ["collect-graph"], "targets": ["WORKSTATION-5"]}
    worker_calls, ran = _bound_step(monkeypatch, brief, "accept", worker_replies=1, footholds=False)

    assert ran == [{"action": {"capability": "collect-graph", "callback_id": "5"}, "inputs": None}]


def test_a_collection_step_with_several_targets_is_left_to_the_worker_model(monkeypatch):
    brief = {"allowed_capabilities": ["collect-graph"], "targets": ["north.test", "WORKSTATION-5"]}
    worker_calls, ran = _bound_step(monkeypatch, brief, "", worker_replies=1)

    assert ran == [] and len(worker_calls) == 1


def test_a_sage_proposed_call_id_is_one_every_provider_accepts():
    """A live Bedrock call rejected the bound call's id, built from a request id containing colons."""
    import re
    call_id = model_module._bound_call_id("chat:11:request:10", 3)
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", call_id) and call_id != model_module._bound_call_id("chat:11:request:10", 4)


# --- capability grounding + recoverable refusal (cairn #145 / #127) ------------------------------------------

from ai.langgraph import capabilities as _caps  # noqa: E402


class _GroundRecord:
    """Minimal step record: captures the end() outcome the middleware assigns."""

    def __init__(self):
        self.status = None
        self.ended = None

    def end(self, status, bound, reason):
        self.status = status
        self.ended = (status, bound, reason)


class _GroundClient:
    """A capability client whose augmentation injects a fixed grounding dict, so a test controls whether the
    plan becomes buildable. Action parsing and planning use the real capabilities module."""

    def __init__(self, grounding: dict):
        self._grounding = dict(grounding)

    def _capability_text(self, value) -> str:
        return str(value or "").strip()

    def _capability_tool_inputs(self, inputs) -> dict:
        return dict(inputs) if isinstance(inputs, dict) else {}

    def _capability_tool_action(self, action, inputs, capabilities_mod):
        name = action.get("capability") if isinstance(action, dict) else str(action)
        return capabilities_mod.CapabilityAction(name=str(name), target="", intent={"domain": inputs.get("domain", "")})

    def _ensure_capability_transaction_context(self, action, inputs):
        return action, ""

    async def _augment_capability_runtime_inputs(self, action, inputs):
        inputs.update(self._grounding)


def _ground_middleware(client):
    model = SimpleNamespace(
        mythic_client=client,
        _supervised_step_record=lambda agent, msgs: _ground_middleware.record,
        _step_guarded_tools={},
        _step_clock=lambda: 0.0,
    )
    return model_module._SupervisedStepBoundMiddleware(model, "Mythic_Operator", frozenset({"execute_capability"}))


def _gpo_call_message():
    return AIMessage(content="", name="Mythic_Operator", tool_calls=[{
        "name": "execute_capability", "id": "c1", "type": "tool_call",
        "args": {"action": {"capability": "gpo-controlled-system-exec", "callback_id": "3"},
                 "inputs": {"domain": "north.sevenkingdoms.local"}}}])


def test_an_ungroundable_capability_is_returned_as_a_recoverable_step_not_a_terminal():
    # No grounding: the GPO SYSTEM action has no command/principal/DC, so the plan cannot be built. The call is
    # removed with a plain-words note (never a fabricated command), and the step ends as a recoverable outcome.
    _ground_middleware.record = _GroundRecord()
    mw = _ground_middleware(_GroundClient({}))
    message = _gpo_call_message()
    asyncio.run(mw._ground_and_limit_capability(SimpleNamespace(state={"messages": []}), message))

    assert message.tool_calls == []  # not surfaced to the approval gate
    assert "could not be planned yet" in str(message.content)
    assert _ground_middleware.record.status == sb.StepStatus.PRECONDITION_UNSATISFIABLE


def test_a_groundable_capability_survives_with_its_inputs_bound_from_state():
    # Grounding supplies the durable effect (controlled principal + affected DC), so the plan builds and the call
    # reaches the approval card, carrying the inputs bound from engagement state.
    _ground_middleware.record = _GroundRecord()
    grounding = {"gpo": "starkwallpaper", "controlled_principal": "NORTH\\samwell.tarly",
                 "affected_dc_hosts": ["WINTERFELL.NORTH.SEVENKINGDOMS.LOCAL"]}
    mw = _ground_middleware(_GroundClient(grounding))
    message = _gpo_call_message()
    asyncio.run(mw._ground_and_limit_capability(SimpleNamespace(state={"messages": []}), message))

    assert len(message.tool_calls) == 1  # survives to the card
    assert message.tool_calls[0]["args"]["inputs"]["controlled_principal"] == "NORTH\\samwell.tarly"
    assert _ground_middleware.record.status is None


def test_the_planner_refuses_a_gpo_system_action_without_an_effect_and_never_fabricates_one():
    # Pure planner contract (step 4): an unspecified SYSTEM action is refused, not invented.
    action = _caps.CapabilityAction(name="gpo-controlled-system-exec", target="",
                                    intent={"domain": "north.sevenkingdoms.local"})
    base = {"domain": "north.sevenkingdoms.local", "gpo": "starkwallpaper"}
    plan = _caps.build_capability_execution_plan(action, dict(base))
    assert plan.ok is False  # no command, principal or proof -> refused, nothing invented
    assert plan.missing  # it names what it needs rather than fabricating an action
    # With an explicit proof-only probe it becomes buildable — the model supplies intent, Sage does not invent it.
    ok_plan = _caps.build_capability_execution_plan(action, {**base, "allow_proof_only": True})
    assert ok_plan.ok is True
