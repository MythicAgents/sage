"""An operator's rejection is recognized from the result the installed approval middleware returns.

From langchain 1.3.17 the middleware no longer returns Sage's rejection message as the tool result; it wraps it as
``User rejected the tool call for `<tool>` with reason: <message>``. Sage's two readers of that result, the step
accounting (`step_bounds.step_activity`) and the record handed to another worker (`model._handed_result`), must
classify each decision as they did when the result was Sage's message itself.

The wrapped results here come from the real installed middleware: a minimal agent with an inert tool, interrupted
and resumed with Sage's own decision. The legacy results are the langchain 1.3.14 shape (the message itself, error
status). The near-matches are synthetic.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ai.langgraph import model as model_module
from ai.langgraph import step_bounds as sb


class _ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


CAPABILITY_ARGS = {"action": {"name": "collect-graph"}}
TASK_ARGS = {"command": "whoami", "callback_display_id": 2}

# Sage's decision messages, as `Model.handle_hitl_resume` writes them (model.py: steer, pick-one, bare deny).
STEER = "[Operator steering] use callback 3 instead"
NOT_SELECTED = ("[Not selected] The operator chose a different action first. "
                "`issue_task` was not executed this round but may be proposed again.")


def _bare_deny(tool_name: str, args: dict[str, Any]) -> str:
    return model_module._capability_rejection_message(tool_name, args)


def _middleware_result(tool_name: str, args: dict[str, Any], decision: dict[str, Any]) -> tuple[ToolMessage, list]:
    """Run one guarded call through the installed middleware and return its tool result and what the tool ran."""
    ran: list[dict[str, Any]] = []

    def inert(command: str = "", callback_display_id: int | None = None, action: Any = None) -> str:
        ran.append({"command": command, "action": action})
        return "inert tool ran"

    tool = StructuredTool.from_function(func=inert, name=tool_name, description="inert guarded tool")
    call_id = f"call-{tool_name}"
    model = _ScriptedModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": call_id, "type": "tool_call"}]),
        AIMessage(content="done"),
    ]))
    agent = create_agent(
        model=model, tools=[tool], checkpointer=InMemorySaver(),
        middleware=[HumanInTheLoopMiddleware(
            interrupt_on={tool_name: InterruptOnConfig(allowed_decisions=["approve", "reject"])})],
    )
    config = {"configurable": {"thread_id": f"denial-{tool_name}"}}

    async def run() -> list:
        paused = await agent.ainvoke({"messages": [HumanMessage(content="go")]}, config)
        assert paused.get("__interrupt__"), "the guarded call did not pause for a decision"
        final = await agent.ainvoke(Command(resume={"decisions": [decision]}), config)
        return final["messages"]

    [result] = [message for message in asyncio.run(run())
                if isinstance(message, ToolMessage) and message.tool_call_id == call_id]
    return result, ran


def _legacy(tool_name: str, reason: str) -> ToolMessage:
    """The langchain 1.3.14 result for a reject decision that carries a message."""
    return ToolMessage(content=reason, name=tool_name, tool_call_id=f"call-{tool_name}", status="error")


def _classified(message: ToolMessage) -> tuple[bool, str]:
    [(name, rejected)] = sb.step_activity([message], frozenset({str(message.name)})).guarded
    assert name == message.name
    return rejected, model_module._handed_result(message, {})["status"]


DECISIONS = [
    # (id, tool, args, Sage's reject message or None for a reasonless reject, step rejected, handed status)
    ("bare-deny-capability", "execute_capability", CAPABILITY_ARGS, _bare_deny("execute_capability", CAPABILITY_ARGS),
     True, "denied_by_operator"),
    ("bare-deny-task", "issue_task", TASK_ARGS, _bare_deny("issue_task", TASK_ARGS), True, "denied_by_operator"),
    ("steer", "issue_task", TASK_ARGS, STEER, False, "error"),
    ("not-selected", "issue_task", TASK_ARGS, NOT_SELECTED, False, "error"),
    ("reasonless", "issue_task", TASK_ARGS, None, False, "denied_by_operator"),
]


@pytest.mark.parametrize("tool_name, args, reason, rejected, handed",
                         [row[1:] for row in DECISIONS], ids=[row[0] for row in DECISIONS])
def test_each_decision_through_the_installed_middleware_keeps_sages_classification(
        tool_name, args, reason, rejected, handed):
    decision = {"type": "reject", **({"message": reason} if reason is not None else {})}
    message, ran = _middleware_result(tool_name, args, decision)

    assert ran == []                                         # a rejected call never runs
    assert message.tool_call_id == f"call-{tool_name}" and message.name == tool_name
    assert message.status == "error"
    if reason is not None:
        assert reason in str(message.content)                # the model still reads Sage's whole reason
    assert _classified(message) == (rejected, handed)


def test_a_bare_capability_denial_still_tells_the_model_not_to_re_attempt_the_effect():
    message, _ran = _middleware_result(
        "execute_capability", CAPABILITY_ARGS,
        {"type": "reject", "message": _bare_deny("execute_capability", CAPABILITY_ARGS)})
    text = str(message.content)
    assert "collect-graph capability was not executed" in text
    assert "do NOT re-attempt it in this request through issue_task or any other tool" in text


@pytest.mark.parametrize("tool_name, args, reason, rejected, handed",
                         [row[1:] for row in DECISIONS if row[3] is not None],
                         ids=[row[0] for row in DECISIONS if row[3] is not None])
def test_the_legacy_result_keeps_its_classification(tool_name, args, reason, rejected, handed):
    assert _classified(_legacy(tool_name, reason)) == (rejected, handed)


def test_an_approved_call_runs_and_is_not_a_denial():
    message, ran = _middleware_result("issue_task", TASK_ARGS, {"type": "approve"})
    assert len(ran) == 1 and message.status == "success"
    assert _classified(message) == (False, "completed")


WRAPPED_DENIAL = ("User rejected the tool call for `{tool}` with reason: [DENIED by operator] issue_task was not "
                  "executed.")

NEAR_MATCHES = [
    # (id, content, tool name, status, step rejected, handed status) -- synthetic
    ("unrelated-tool-error", "Mythic task 12 failed: access is denied", "issue_task", "error", False, "error"),
    ("quoted-marker", 'Command output: "[DENIED by operator] issue_task was not executed."', "issue_task", "error",
     False, "error"),
    # An envelope naming another tool is not unwrapped; the handed record keeps its reasonless-rejection rule.
    ("other-tools-envelope", WRAPPED_DENIAL.format(tool="execute_capability"), "issue_task", "error", False,
     "denied_by_operator"),
    ("success-carrying-envelope", WRAPPED_DENIAL.format(tool="issue_task"), "issue_task", "success", False,
     "completed"),
]


@pytest.mark.parametrize("content, tool_name, status, rejected, handed",
                         [row[1:] for row in NEAR_MATCHES], ids=[row[0] for row in NEAR_MATCHES])
def test_near_matches_are_not_operator_denials(content, tool_name, status, rejected, handed):
    message = ToolMessage(content=content, name=tool_name, tool_call_id="call-near", status=status)
    assert _classified(message) == (rejected, handed)
