"""Supervisor argument guards on respond_to_user recover with a remedy, not a crash.

The live release-blocker defect: the Supervisor
called respond_to_user with a blank final_response; the guard raised ValueError
straight out of the tool, the tool node re-raised it, and the whole graph errored
instead of delivering the report. Blank final reports must still be refused — but
the refusal lands as a remedy-bearing correction the model can retry, in the same
shape the tool already uses for "worker answer reuse unavailable".
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import pytest


def _load_model_module():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        return importlib.import_module("ai.langgraph.model")
    except Exception as e:  # pragma: no cover - environment guard
        pytest.skip(f"model.py runtime unavailable: {e}")


def _respond_call(call_id: str, **args):
    args.setdefault("final_response", "")
    args.setdefault("reuse_latest_worker_answer", False)
    args.setdefault("outcome", "")
    return {
        "name": "respond_to_user",
        "args": args,
        "id": call_id,
        "type": "tool_call",
    }


class BoundFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def _supervisor_agent(mod, responses):
    from ai.langgraph.turn_authority import TurnAuthority

    model = mod.Model.__new__(mod.Model)
    model._turn_authority = TurnAuthority(mode="observe")
    model._request_contract = None
    model.mythic_client = None
    agent = create_agent(
        model=BoundFake(responses=responses),
        tools=[mod._create_respond_to_user_tool()],
        middleware=[mod._TurnAuthorityToolMiddleware(model)],
    )

    async def collect():
        snapshots = []
        exc = None
        try:
            async for snapshot in agent.astream(
                {"messages": [HumanMessage(content="finish")], "supervisor_messages": []},
                stream_mode="values",
            ):
                snapshots.append(snapshot)
        except BaseException as error:
            exc = error
        return snapshots, exc

    return asyncio.run(collect())


def _final_message(exc):
    command = getattr(exc, "command", None)
    if command is None and getattr(exc, "args", None):
        command = exc.args[0]
    message = command.update["messages"][0]
    assert isinstance(message, AIMessage)
    return message


def test_respond_to_user_argument_guards_recover_with_remedy():
    """Blank final_response plus guard misfires produce corrective tool messages;
    the retry with a complete final_response ends the request with a final report.
    Before the fix the first blank call raised ValueError('final_response must be
    complete') and errored the whole run."""
    mod = _load_model_module()
    worker_report = "## Probe final report\n\nBoth identities confirmed."
    responses = [
        AIMessage(content="", tool_calls=[_respond_call("blank")]),
        AIMessage(
            content="",
            tool_calls=[_respond_call(
                "combined", final_response="probe", reuse_latest_worker_answer=True)],
        ),
        AIMessage(
            content="",
            tool_calls=[_respond_call("outcome", final_response="probe", outcome="chaos")],
        ),
        AIMessage(
            content="",
            tool_calls=[_respond_call(
                "final", final_response=worker_report, outcome="completed")],
        ),
    ]

    snapshots, exc = _supervisor_agent(mod, responses)

    assert isinstance(exc, BaseException) and type(exc).__name__ == "ParentCommand", exc
    final = _final_message(exc)
    assert final.content == worker_report
    assert final.additional_kwargs["_is_final_report"] is True
    assert final.additional_kwargs["_turn_outcome"] == "completed"

    latest_messages = snapshots[-1]["messages"]
    correctives = [
        message for message in latest_messages
        if isinstance(message, ToolMessage) and message.name == "respond_to_user"
    ]
    assert [correction.status for correction in correctives] == ["error"] * 3

    blank = str(correctives[0].content)
    assert "final_response is required" in blank
    assert "reuse_latest_worker_answer" in blank

    combined = str(correctives[1].content)
    assert "cannot be combined" in combined

    outcome = str(correctives[2].content)
    assert "outcome must be one of" in outcome


def test_respond_to_user_working_paths_unchanged():
    """The synthesis path keeps its end-graph behavior, the reuse-ineligible path
    keeps its pre-existing corrective message, and a blank report is still refused
    (the guard's intent survives; only the crash delivery changed)."""
    mod = _load_model_module()
    responses = [
        AIMessage(content="", tool_calls=[_respond_call(
            "reuse", final_response="", reuse_latest_worker_answer=True)]),
        AIMessage(content="", tool_calls=[_respond_call(
            "synthesis", final_response="## Synthesis path", outcome="")]),
    ]

    snapshots, exc = _supervisor_agent(mod, responses)

    assert isinstance(exc, BaseException) and type(exc).__name__ == "ParentCommand", exc
    final = _final_message(exc)
    assert final.content == "## Synthesis path"
    assert final.additional_kwargs["_is_final_report"] is True

    reuse_unavailable = [
        message for message in snapshots[-1]["messages"]
        if isinstance(message, ToolMessage) and message.name == "respond_to_user"
    ]
    assert len(reuse_unavailable) == 1
    assert reuse_unavailable[0].status == "error"
    assert "Worker answer reuse unavailable for this request. " in str(reuse_unavailable[0].content)
