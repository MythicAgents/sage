"""Turn-end taxonomy: a turn end classifies as completed, blocked, or checkpoint.

A single "📊 Final Report" banner on every turn end hides whether the objective was complete,
blocked, or merely checkpointed after one step. The banner is derived from a `_turn_outcome`
classification stamped where the cause is structurally known (wrapper paths) or declared through
`respond_to_user(outcome=...)`. `_is_final_report` means "this is the turn's single closing
user-facing message" and does not imply the Final Report banner.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ai.langgraph import model as model_mod
from tests.test_turn_authority import _load_turn_authority, _never_objective


@pytest.mark.parametrize(
    ("outcome", "banner"),
    (
        ("completed", "📊 **Final Report**"),
        ("blocked", "⛔ **Blocked**"),
        ("checkpoint", "⏸️ **Checkpoint — awaiting continuation**"),
    ),
)
def test_banner_selection_declared_outcomes_select_their_banner(outcome, banner):
    assert model_mod._turn_end_banner(outcome) == banner


@pytest.mark.parametrize(
    "outcome",
    (None, "", "unknown", "Completed", "BLOCKED", " checkpoint", "partially complete"),
)
def test_banner_selection_undeclared_and_near_miss_outcomes_select_no_banner(outcome):
    """Only the exact enum selects a banner; near-misses must not silently classify."""
    assert model_mod._turn_end_banner(outcome) == ""


@pytest.mark.parametrize(
    ("verdict", "outcome"),
    (
        ("achieved", "completed"),
        ("failed", "blocked"),
        ("blocked", "blocked"),
        ("partial", "checkpoint"),
        ("ACHIEVED", "completed"),
        (" achieved ", "completed"),
    ),
)
def test_capability_verdict_maps_to_turn_outcome(verdict, outcome):
    assert model_mod._turn_outcome_from_capability_verdict(verdict) == outcome


@pytest.mark.parametrize("verdict", (None, "", "unknown", "ok", "completed"))
def test_unclassifiable_capability_verdict_maps_to_undeclared(verdict):
    assert model_mod._turn_outcome_from_capability_verdict(verdict) == ""


def _respond_runtime():
    return SimpleNamespace(
        state={"messages": [], "supervisor_messages": []},
        tool_call_id="turn-end-taxonomy",
    )


def test_respond_to_user_declared_outcome_is_stamped():
    tool = model_mod._create_respond_to_user_tool()
    command = tool.func(
        _respond_runtime(),
        final_response=(
            "Task 12 wrote the GPO; Domain Admin membership is not yet verified."
        ),
        outcome="checkpoint",
    )
    assert command.goto == "__end__"
    final = command.update["supervisor_messages"][0]
    assert final.additional_kwargs["_is_final_report"] is True
    assert final.additional_kwargs["_turn_outcome"] == "checkpoint"


def test_respond_to_user_undeclared_outcome_stamps_nothing():
    """Undeclared stays undeclared: the render site streams it without a banner."""
    tool = model_mod._create_respond_to_user_tool()
    command = tool.func(
        _respond_runtime(),
        final_response="The domain has one DC: DC01 (10.0.0.10).",
    )
    final = command.update["supervisor_messages"][0]
    assert final.additional_kwargs["_is_final_report"] is True
    assert "_turn_outcome" not in final.additional_kwargs


def test_respond_to_user_rejects_an_unrecognized_outcome():
    """The outcome is still refused: no report ships; the refusal lands as a
    remedy-bearing corrective tool message instead of raising out of the tool."""
    tool = model_mod._create_respond_to_user_tool()
    runtime = _respond_runtime()
    result = tool.func(
        runtime,
        final_response="Task 12 wrote the GPO.",
        outcome="whenever",
    )
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "[Argument review]" in result.content
    assert "outcome must be one of" in result.content
    assert runtime.state["supervisor_messages"] == []
    assert runtime.state["messages"] == []


class _CapabilityAgent:
    """One worker turn that ran a capability to a typed terminal verdict."""

    def __init__(self, payload):
        self.payload = payload

    async def ainvoke(self, args, config=None):
        return {
            "messages": list(args["messages"]) + [
                ToolMessage(
                    content=json.dumps(self.payload),
                    name="execute_capability",
                    tool_call_id="capability-turn",
                ),
                AIMessage(
                    content="The GPO write is verified on the target.",
                    name="Mythic_Operator",
                ),
            ]
        }


def _bounded_turn_model(prompt):
    turn_mod = _load_turn_authority()
    model = model_mod.Model.__new__(model_mod.Model)
    model._turn_authority = turn_mod.compile_turn_authority(
        prompt,
        objective_classifier=_never_objective,
    )
    model._message_seq = 1
    model.state = {"_message_seq": 1}
    model.llm = None
    model.mythic_client = None
    return model


def _bounded_state(prompt):
    return {
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [],
        "mythic_operator_messages": [HumanMessage(content=prompt)],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
    }


@pytest.mark.parametrize(
    ("verdict", "outcome"),
    (
        ("achieved", "completed"),
        ("failed", "blocked"),
        ("partial", "checkpoint"),
    ),
)
def test_bounded_turn_terminal_maps_the_capability_verdict(verdict, outcome):
    """A typed capability verdict classifies the bounded turn's ending mechanically."""
    prompt = "download the files from SYSVOL on callback 7, then stop"
    model = _bounded_turn_model(prompt)
    wrapped = model._wrap_create_agent(
        _CapabilityAgent({"capability": "gpo-controlled-system-exec", "verdict": verdict,
                          "ok": verdict == "achieved"}),
        "mythic_operator_messages",
        "Mythic_Operator",
    )
    result = asyncio.run(wrapped(_bounded_state(prompt), {}))
    update = result.update or {}
    finals = [
        msg for msg in update.get("supervisor_messages", [])
        if msg.additional_kwargs.get("_is_final_report")
    ]
    assert len(finals) == 1
    assert finals[0].additional_kwargs.get("_turn_outcome") == outcome


def test_bounded_turn_terminal_without_typed_verdict_stays_undeclared():
    """Prose-only worker endings must not guess: no outcome, no banner."""
    prompt = "download the files from SYSVOL on callback 7, then stop"

    class PlainAgent:
        async def ainvoke(self, args, config=None):
            return {
                "messages": list(args["messages"]) + [
                    AIMessage(content="Collected the requested SYSVOL files.",
                              name="Mythic_Operator")
                ]
            }

    model = _bounded_turn_model(prompt)
    wrapped = model._wrap_create_agent(
        PlainAgent(), "mythic_operator_messages", "Mythic_Operator",
    )
    result = asyncio.run(wrapped(_bounded_state(prompt), {}))
    update = result.update or {}
    finals = [
        msg for msg in update.get("supervisor_messages", [])
        if msg.additional_kwargs.get("_is_final_report")
    ]
    assert len(finals) == 1
    assert "_turn_outcome" not in finals[0].additional_kwargs


def _pinned_model(prompt):
    turn_mod = _load_turn_authority()
    model = model_mod.Model.__new__(model_mod.Model)
    model._turn_authority = turn_mod.compile_turn_authority(
        prompt,
        objective_classifier=_never_objective,
    )
    model._message_seq = 1
    model.state = {
        "_message_seq": 1,
        "messages": [],
        "supervisor_messages": [],
        "mcp_manager_messages": [],
    }
    model._thread_id_override = "chat:turn-end:1"
    model._stream_message_to_mythic = (
        lambda message: asyncio.sleep(0)
    )
    model._format_message_for_streaming = (
        lambda message, agent_name=None: f"{agent_name}:{message.content}"
    )
    return model


def test_mcp_pin_failure_classifies_blocked(monkeypatch):
    """A fail-closed MCP pin refusal is a blocked turn end, not a final report."""
    model = _pinned_model("Use the Nemesis MCP server to inspect this artifact.")
    monkeypatch.setattr(model_mod.MCPManager, "get_connected_servers", lambda: [])

    asyncio.run(model._run_pinned_mcp_turn("Use the Nemesis MCP server."))

    finals = [
        msg for msg in model.state["supervisor_messages"]
        if msg.additional_kwargs.get("_is_final_report")
    ]
    assert len(finals) == 1
    assert "not connected" in str(finals[0].content)
    assert finals[0].additional_kwargs.get("_turn_outcome") == "blocked"


def test_terminal_worker_fallback_classifies_blocked(monkeypatch):
    """A structurally selected worker that returned nothing ends blocked, not completed."""
    model = _pinned_model("Use the Nemesis MCP server to inspect this artifact.")
    monkeypatch.setattr(
        model_mod.MCPManager, "get_connected_servers", lambda: ["Nemesis"],
    )
    monkeypatch.setattr(model_mod.MCPManager, "is_bloodhound_server", lambda _n: False)

    async def empty_worker(_state, _config):
        return model_mod.Command(
            goto=model_mod.END,
            update={"mcp_manager_messages": []},
        )

    model._mcp_manager_agent = lambda: empty_worker

    asyncio.run(model._run_pinned_mcp_turn("Use the Nemesis MCP server."))

    finals = [
        msg for msg in model.state["supervisor_messages"]
        if msg.additional_kwargs.get("_is_final_report")
    ]
    assert len(finals) == 1
    assert finals[0].additional_kwargs.get("_turn_outcome") == "blocked"


def _streamed_final_report_model(additional_kwargs):
    model = model_mod.Model.__new__(model_mod.Model)
    model._stop_requested = False
    model._stop_reason = ""
    model._final_turn_outcome = ""
    report = AIMessage(content="report", name="Supervisor", additional_kwargs=additional_kwargs)

    async def extract(_state_update):
        return [report]

    async def no_summary():
        return ""

    model._extract_new_messages_from_event = extract
    model._engagement_state_turn_summary_text = no_summary
    model._format_message_for_streaming = lambda message, agent_name=None: str(message.content)
    model._stream_message_to_mythic = lambda message: asyncio.sleep(0)
    return model


@pytest.mark.parametrize("outcome", ("completed", "blocked", "checkpoint"))
def test_streamed_final_report_records_its_declared_outcome_for_the_request(outcome):
    model = _streamed_final_report_model({"_is_final_report": True, "_turn_outcome": outcome})
    asyncio.run(model._process_stream_event({"Supervisor": {}}))
    assert model.request_turn_outcome() == outcome


@pytest.mark.parametrize("outcome", (None, "Completed", "unknown"))
def test_undeclared_or_unknown_final_report_outcome_records_nothing(outcome):
    kwargs = {"_is_final_report": True}
    if outcome is not None:
        kwargs["_turn_outcome"] = outcome
    model = _streamed_final_report_model(kwargs)
    asyncio.run(model._process_stream_event({"Supervisor": {}}))
    assert model.request_turn_outcome() == ""

