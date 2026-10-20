"""Native supervised requests must yield when an issued task remains unobserved."""

import asyncio
import itertools
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ai.langgraph.model import Model, _TurnAuthorityToolMiddleware
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract
from ai.langgraph.turn_authority import authority_from_request_contract
from sage_chat.headless import build_chat_request
from sage_chat.session import drop_channel_session
from sage_chat.service import bind_channel_thread_id
from tests.test_hitl_steer_current_intent import (
    _NativeDriverChat, _NativeScriptedModel, _call, _cards, _input_response,
)


ACTION = {
    "name": "issue_task_and_waitfor_task_output",
    "args": {"callback_display_id": 5, "command": "whoami", "parameters": "", "timeout": 1},
}


def test_native_issued_pending_task_yields_without_poll_loop():
    async def run():
        model = Model(
            provider="openai", model="gpt-4o-mini", system_prompt="offline pending case",
            config={"configurable": {
                "api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1",
            }},
            task_id=0, agent_task_id="native-pending", mode="supervised",
            operation_id=1, channel_id=1, apitoken_id=0,
        )
        tools = MythicTools(agent_task_id="native-pending")
        tools.client = object()
        tools._authenticate_live_command = AsyncMock(return_value={
            "status": "available", "command": "whoami",
        })
        tools._fetch_command_schema = AsyncMock(return_value=[])
        tools._callback_tasking_liveness_blocker = AsyncMock(return_value="")
        tools._issue_mythic_task = AsyncMock(return_value={"display_id": 4242})
        model.mythic_client = tools
        model._payload_names = []
        model.memory = InMemorySaver()
        poll = _call("get_all_task_output_by_task_id", {"task_id": 4242}, 3)
        fake = _NativeScriptedModel(messages=itertools.chain(
            (
                _call("transfer_to_Mythic_Operator", {
                    "handoff_title": "inspect task",
                    "handoff_instruction": "Run the approved check and report its state.",
                }, 1),
                _call(ACTION["name"], ACTION["args"], 2),
            ),
            itertools.repeat(poll),
        ))
        model._get_base_chat_model = lambda *a, **k: fake
        model._native_chat_explicit_hitl = True
        model.command_name = "chat"
        model._rebuild_graph()
        chat = _NativeDriverChat(model)
        first = build_chat_request(
            "Run whoami on callback 5 and report the result.", channel_id=1,
            operation_id=1, request_id=1,
            config={"mode": "supervised", "max_steps": 8},
        )
        bind_channel_thread_id(first, model)
        try:
            await chat.chat(first)
            cards = _cards(chat)
            assert len(cards) == 1
            second = build_chat_request(
                "", channel_id=1, operation_id=1, request_id=2,
                config={"mode": "supervised", "max_steps": 8},
            )
            second.InputResponse = _input_response("accept", cards[0])
            async def pending_wait(**_kwargs):
                await asyncio.Event().wait()
            with patch("ai.langgraph.mythic_tools.mythic.waitfor_for_task_output", pending_wait):
                await chat.chat(second)
            pending_steps = len(fake.inputs)
            pending_terminal = list(chat.terminal_emissions)
            fake.messages = iter((
                _call("transfer_to_Mythic_Operator", {
                    "handoff_title": "inspect prior task",
                    "handoff_instruction": "Read task 4242 and report its output.",
                }, 4),
                _call("get_all_task_output_by_task_id", {"task_id": 4242}, 5),
                AIMessage(content="Task 4242 returned a late identity result."),
                AIMessage(content="The prior task returned a late identity result."),
            ))
            third = build_chat_request(
                "Read the result for prior task 4242.", channel_id=1,
                operation_id=1, request_id=3,
                config={"mode": "supervised", "max_steps": 8},
            )
            late_output = [{
                "id": 99, "response_text": "late result", "task": {
                    "display_id": 4242, "completed": True, "status": "completed",
                    "callback": {"display_id": 5}, "command_name": "whoami",
                },
            }]
            with patch("ai.langgraph.mythic_tools.mythic.get_all_task_output_by_id",
                       AsyncMock(return_value=late_output)) as read:
                await chat.chat(third)
            return model, tools, fake, chat, pending_steps, pending_terminal, read
        finally:
            await drop_channel_session(first)

    model, tools, fake, chat, pending_steps, pending_terminal, read = asyncio.run(run())
    assert tools._issue_mythic_task.await_count == 1
    assert tools._last_issued_task_display_id == 4242
    assert pending_steps == 2
    assert len(fake.inputs) == 6
    assert len(_cards(chat)) == 1
    assert len(pending_terminal) == 1
    assert pending_terminal[0]["kind"] == "complete"
    assert "4242" in pending_terminal[0]["content"]
    assert "could not confirm its state" in pending_terminal[0]["content"]
    assert "operator decision" in pending_terminal[0]["content"]
    assert len(chat.terminal_emissions) == 2
    read.assert_awaited_once()
    assert read.call_args.kwargs["task_display_id"] == 4242
    assert tools._deferred_task_receipt is None
    historical_reads = [
        json.loads(message.content)
        for batch in fake.inputs for message in batch
        if isinstance(message, ToolMessage)
        and message.name == "get_all_task_output_by_task_id"
    ]
    assert any(
        item["provenance"]["issued_by_current_request"] is False
        and item["task_output"][0]["task"]["display_id"] == 4242
        for item in historical_reads
    )


@pytest.mark.parametrize("mode,lane,request_id,expected", [
    ("supervised", "supervised_workflow", "request-one", True),
    ("supervised", "supervised_workflow", "request-two", False),
    ("conversation", "conversational", "request-one", False),
    ("auto", "autonomous_objective", "request-one", False),
])
def test_pending_handoff_is_bound_to_supervised_request(mode, lane, request_id, expected):
    model = Model.__new__(Model)
    model.mode = mode
    model.state = {"_request_lane": lane, "_request_id": request_id}
    model.mythic_client = SimpleNamespace(_deferred_task_receipt={
        "request_id": "request-one", "task_display_id": 4242,
    })
    middleware = _TurnAuthorityToolMiddleware(model, agent_name="Mythic_Operator")
    result = asyncio.run(middleware.abefore_model({}, None))
    assert bool(result) is expected


@pytest.mark.parametrize("completed,status,expected", [
    (False, "agent processing", "pending"),
    (None, "", "unknown"),
    (True, "completed", None),
    (True, "error", None),
])
def test_exact_native_task_state_controls_pending_receipt(completed, status, expected):
    tools = MythicTools(agent_task_id="offline-state")
    tools.client = object()
    tools._request_contract = SimpleNamespace(
        lane="supervised_workflow", request_id="request-one", digest="digest-one",
    )
    tools._issue_mythic_task = AsyncMock(return_value={"display_id": 4242})
    tools._callback_tasking_liveness_blocker = AsyncMock(return_value="")
    tools._supervised_task_state = AsyncMock(return_value={
        "task_completed": completed, "task_status": status,
    })
    with patch.object(tools, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(tools, "_bind_contract_task_issue_parameters", return_value=""), \
         patch("ai.langgraph.mythic_tools.mythic.waitfor_for_task_output",
               AsyncMock(return_value="partial output")):
        asyncio.run(tools.get_tools([ACTION["name"]])[0].ainvoke(ACTION["args"]))
    receipt = tools._deferred_task_receipt
    assert (receipt or {}).get("observation_status") == expected
    if expected == "pending":
        assert receipt["task_display_id"] == 4242
        assert receipt["task_status"] == status


def test_autonomous_nonterminal_observation_does_not_install_supervised_receipt():
    tools = MythicTools(agent_task_id="offline-auto-state")
    tools.client = object()
    tools._request_contract = SimpleNamespace(
        lane="autonomous_objective", request_id="request-one", digest="digest-one",
    )
    tools._issue_mythic_task = AsyncMock(return_value={"display_id": 4242})
    tools._callback_tasking_liveness_blocker = AsyncMock(return_value="")
    tools._supervised_task_state = AsyncMock(return_value={
        "task_completed": False, "task_status": "processing",
    })
    with patch.object(tools, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(tools, "_bind_contract_task_issue_parameters", return_value=""), \
         patch("ai.langgraph.mythic_tools.mythic.waitfor_for_task_output",
               AsyncMock(return_value="partial output")):
        asyncio.run(tools.get_tools([ACTION["name"]])[0].ainvoke(ACTION["args"]))
    assert tools._deferred_task_receipt is None


def test_dead_callback_blocks_before_task_issue_without_pending_receipt():
    tools = MythicTools(agent_task_id="offline-dead-callback")
    tools.client = object()
    tools._request_contract = SimpleNamespace(
        lane="supervised_workflow", request_id="request-one", digest="digest-one",
    )
    tools._issue_mythic_task = AsyncMock(return_value={"display_id": 4242})
    tools._callback_tasking_liveness_blocker = AsyncMock(return_value="STOP: callback is dead")
    with patch.object(tools, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(tools, "_bind_contract_task_issue_parameters", return_value=""):
        result = asyncio.run(tools.get_tools([ACTION["name"]])[0].ainvoke(ACTION["args"]))
    assert "callback is dead" in result
    tools._issue_mythic_task.assert_not_awaited()
    assert tools._deferred_task_receipt is None


def test_observation_exception_marks_issued_task_without_reissue():
    tools = MythicTools(agent_task_id="offline-wait-error")
    tools.client = object()
    tools._request_contract = SimpleNamespace(
        lane="supervised_workflow", request_id="request-one", digest="digest-one",
    )
    tools._issue_mythic_task = AsyncMock(return_value={"display_id": 4242})
    tools._callback_tasking_liveness_blocker = AsyncMock(return_value="")
    with patch.object(tools, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(tools, "_bind_contract_task_issue_parameters", return_value=""), \
         patch("ai.langgraph.mythic_tools.mythic.waitfor_for_task_output",
               AsyncMock(side_effect=ConnectionError("observation unavailable"))):
        result = asyncio.run(tools.get_tools([ACTION["name"]])[0].ainvoke(ACTION["args"]))
    assert "Error observing output for issued task 4242" in result
    assert tools._deferred_task_receipt["observation_status"] == "wait_failed"
    assert tools._issue_mythic_task.await_count == 1


def test_explicit_cancel_does_not_install_pending_handoff_receipt():
    tools = MythicTools(agent_task_id="offline-cancel")
    tools.client = object()
    tools._request_contract = SimpleNamespace(
        lane="supervised_workflow", request_id="request-one", digest="digest-one",
    )
    tools._issue_mythic_task = AsyncMock(return_value={"display_id": 4242})
    tools._callback_tasking_liveness_blocker = AsyncMock(return_value="")
    waiting = asyncio.Event()

    async def wait(**_kwargs):
        waiting.set()
        await asyncio.Event().wait()

    async def run():
        with patch.object(tools, "_turn_authority_issue_blocker", return_value=""), \
             patch.object(tools, "_bind_contract_task_issue_parameters", return_value=""), \
             patch("ai.langgraph.mythic_tools.mythic.waitfor_for_task_output", wait):
            task = asyncio.create_task(tools.get_tools([ACTION["name"]])[0].ainvoke(ACTION["args"]))
            await waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    tools._issue_mythic_task.assert_awaited_once()
    assert tools._deferred_task_receipt is None


# ── Reconcile the deferred receipt at the Operator handback ─────────────────────────

_LATE_COMPLETED_OUTPUT = json.dumps([{
    "id": 99, "response_text": "late identity result", "task": {
        "id": 4242, "display_id": 4242, "completed": True, "status": "success",
        "callback": {"display_id": 5}, "command_name": "whoami",
    },
}])


def _handback_model(request_id: str) -> tuple[Model, MythicTools]:
    """A supervised Model + MythicTools holding a deferred receipt, wired for wrapper tests.

    Must be called from inside a running event loop: the constructor creates the production
    SQLite checkpointer, which binds the running loop.
    """
    model = Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline handback case",
        config={"configurable": {
            "api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1",
        }},
        task_id=0, agent_task_id="pending-handback", mode="supervised",
        operation_id=1, channel_id=1, apitoken_id=0,
    )
    model.memory = InMemorySaver()
    model._thread_id_override = "pending-handback-channel"
    model._native_chat_explicit_hitl = True
    tools = MythicTools(agent_task_id="pending-handback")
    tools.client = object()
    model.mythic_client = tools
    model._payload_names = []
    contract = build_request_contract(
        request_id=request_id, channel_id="1", operation_id="1",
        mode="supervised", autonomous_solve=False,
    )
    model.install_request_contract(contract)
    model._install_turn_authority(authority_from_request_contract(contract))
    tools._deferred_task_receipt = {
        "command": "whoami", "callback_display_id": 5, "task_display_id": 4242,
        "waited_seconds": 1, "request_id": contract.request_id,
        "observation_status": "deferred",
    }
    return model, tools


def _operator_wrapped(model: Model, operator_final: str):
    class _Agent:
        async def ainvoke(self, payload, _config=None):
            return {"messages": [*payload["messages"], AIMessage(content=operator_final)]}

    return model._wrap_create_agent(_Agent(), "mythic_operator_messages", "Mythic_Operator")


def test_completed_deferred_task_is_reconciled_at_handback_and_the_turn_continues():
    """The deferred task completed by handback time — deliver the real result, keep the turn.

    Force-ending the request with 'Observation is incomplete … inspect that exact task in a fresh
    request' when the task has already finished inserts an operator round-trip ('Proceed.') between
    every deferred step. The deferral cap still applies while the task is genuinely unobserved; once
    the one bounded read resolves it, the Supervisor receives the typed outcome and the request
    continues.
    """
    async def run():
        model, tools = _handback_model("chat:pending-handback:request:1")
        reader = AsyncMock(return_value=_LATE_COMPLETED_OUTPUT)
        tools.get_all_task_output_by_task_id = reader
        wrapped = _operator_wrapped(model, "Issued task 4242; output was deferred.")
        state = dict(model.state)
        state["mythic_operator_messages"] = [
            AIMessage(content="Issued task 4242; output was deferred.")]
        result = await wrapped(state, {})
        return result, tools, reader

    result, tools, reader = asyncio.run(run())
    reader.assert_awaited_once()
    assert tools._deferred_task_receipt is None, (
        "a reconciled receipt must be cleared so the deferral cap does not leak into the continued turn"
    )
    assert isinstance(result, Command)
    assert result.goto == "Supervisor", (
        f"a resolved deferred task must not force END: {result}"
    )
    update = result.update or {}
    supervisor_channel = update.get("supervisor_messages") or []
    reconciled = [
        m for m in supervisor_channel
        if getattr(m, "additional_kwargs", {}).get("_deferred_task_reconciled")
    ]
    assert reconciled, f"the reconciled outcome never reached the Supervisor: {update.keys()}"
    content = str(reconciled[0].content)
    assert "completed" in content
    assert "late identity result" in content
    assert "4242" in content
    assert "Observation is incomplete" not in content
    assert "unconfirmed" not in content


def test_failed_deferred_task_is_reconciled_at_handback_and_the_turn_continues():
    """A reconciled FAILURE is still an observed outcome: deliver it, continue the turn."""
    async def run():
        model, tools = _handback_model("chat:pending-handback:request:2")
        reader = AsyncMock(return_value=json.dumps([{
            "id": 100, "response_text": "boom", "task": {
                "id": 4242, "display_id": 4242, "completed": True, "status": "error",
                "callback": {"display_id": 5}, "command_name": "whoami",
            },
        }]))
        tools.get_all_task_output_by_task_id = reader
        wrapped = _operator_wrapped(model, "Issued task 4242; output was deferred.")
        state = dict(model.state)
        state["mythic_operator_messages"] = [
            AIMessage(content="Issued task 4242; output was deferred.")]
        result = await wrapped(state, {})
        return result, tools

    result, tools = asyncio.run(run())
    assert tools._deferred_task_receipt is None
    assert isinstance(result, Command) and result.goto == "Supervisor"
    reconciled = [
        m for m in (result.update or {}).get("supervisor_messages", [])
        if getattr(m, "additional_kwargs", {}).get("_deferred_task_reconciled")
    ]
    assert reconciled and "FAILED" in str(reconciled[0].content)


def test_still_pending_deferred_task_keeps_the_forced_turn_end():
    """The deferral cap holds while the task is genuinely unobserved: forced END, honest wording."""
    async def run():
        model, tools = _handback_model("chat:pending-handback:request:3")
        reader = AsyncMock(return_value=json.dumps([{
            "id": 101, "response_text": "", "task": {
                "id": 4242, "display_id": 4242, "completed": False, "status": "processing",
                "callback": {"display_id": 5}, "command_name": "whoami",
            },
        }]))
        tools.get_all_task_output_by_task_id = reader
        wrapped = _operator_wrapped(model, "Issued task 4242; output was deferred.")
        state = dict(model.state)
        state["mythic_operator_messages"] = [
            AIMessage(content="Issued task 4242; output was deferred.")]
        result = await wrapped(state, {})
        return result, tools

    result, tools = asyncio.run(run())
    assert tools._deferred_task_receipt is not None, (
        "an unresolved task must keep its receipt: the deferral cap is still load-bearing"
    )
    assert isinstance(result, Command) and result.goto == END
    update = result.update or {}
    final_msgs = [
        m for m in (update.get("supervisor_messages") or [])
        if getattr(m, "additional_kwargs", {}).get("_pending_task_handoff")
    ]
    assert final_msgs, f"the forced-end handback message is missing: {update.keys()}"
    content = str(final_msgs[0].content)
    assert "4242" in content
    assert "no output rows" in content
    assert "awaiting interactive input" in content, (
        f"the triage wording must name the input-wait possibility for the no-output case: {content}"
    )
    assert "operator decision" in content
    assert "No further target task may be issued in this turn" in content
    assert final_msgs[0].additional_kwargs.get("_turn_outcome") == "checkpoint", (
        "a pending-task forced end is a mid-objective pause and must classify as a "
        "checkpoint, not a final report"
    )


def test_unresolvable_reconciliation_read_keeps_the_forced_turn_end():
    """A failed/absent reconciliation read resolves nothing: today's forced end survives."""
    async def run():
        model, tools = _handback_model("chat:pending-handback:request:4")
        reader = AsyncMock(side_effect=RuntimeError("transport down"))
        tools.get_all_task_output_by_task_id = reader
        wrapped = _operator_wrapped(model, "Issued task 4242; output was deferred.")
        state = dict(model.state)
        state["mythic_operator_messages"] = [
            AIMessage(content="Issued task 4242; output was deferred.")]
        result = await wrapped(state, {})
        return result, tools

    result, tools = asyncio.run(run())
    assert tools._deferred_task_receipt is not None
    assert isinstance(result, Command) and result.goto == END
