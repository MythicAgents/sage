"""Request isolation regressions for checkpointed native-chat turns.

These tests exercise the production ``Model.invoke`` seam and the real supervised gate with
an in-memory checkpoint.  Only the leaf agent runnable is scripted; state reducers, request
contracts, node wrapping, request reuse, and termination commands are production code.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import Model, SageState  # noqa: E402
from ai.langgraph.request_contract import build_request_contract  # noqa: E402
from ai.langgraph.turn_authority import authority_from_request_contract  # noqa: E402


_OLD_PROMPT = "Analyze the previous configuration."
_OLD_SUMMARY = "The previous configuration was summarized."
_FIRST_FINAL = "The first request is complete."
_SECOND_PROMPT = "Apply the newly requested configuration."
_FRESH_FINAL = "The new request reached the Supervisor."


class _RecordingAgent:
    def __init__(self, *responses: str) -> None:
        self.responses = iter(responses)
        self.inputs: list[list[Any]] = []

    async def ainvoke(self, payload: dict[str, Any], _config: dict | None = None) -> dict[str, Any]:
        messages = list(payload["messages"])
        self.inputs.append(messages)
        return {"messages": [*messages, AIMessage(content=next(self.responses))]}


async def _discard_emit(_text: str) -> bool:
    return True


def _contract(request_id: str):
    return build_request_contract(
        request_id=request_id,
        channel_id="channel-isolation",
        operation_id="operation-isolation",
        mode="supervised",
        autonomous_solve=False,
    )


def _new_model() -> Model:
    model = Model(
        provider="openai",
        model="gpt-4o-mini",
        system_prompt="checkpoint request isolation test",
        config={
            "configurable": {
                "api_key": "sk-not-a-real-key",
                "API_ENDPOINT": "http://127.0.0.1:9/v1",
            }
        },
        task_id=0,
        agent_task_id="checkpoint-request-isolation",
        response_emitter=_discard_emit,
        channel_id=1,
    )
    model.memory = InMemorySaver()
    model._thread_id_override = "same-native-chat-channel"
    model._native_chat_explicit_hitl = True
    return model


def _install_contract_without_graph_rebuild(model: Model, request_id: str) -> None:
    contract = _contract(request_id)
    model.install_request_contract(contract)
    model._install_turn_authority(authority_from_request_contract(contract))
    model._graph_signature = model._graph_turn_signature()


def test_same_checkpoint_starts_a_fresh_supervisor_turn_with_prior_context_once():
    async def run() -> tuple[Model, _RecordingAgent]:
        model = _new_model()
        specialist = _RecordingAgent(_OLD_SUMMARY)
        supervisor = _RecordingAgent(_FIRST_FINAL, _FRESH_FINAL)

        async def dispatch(state: dict[str, Any]) -> Command:
            current = str(state.get("_request_id") or "")
            return Command(
                goto="Generalist" if current.endswith("request:1") else "Supervisor",
                update={"_request_id": current},
            )

        model.graph = (
            StateGraph(SageState)
            .add_node("Dispatch", dispatch)
            .add_node(
                "Generalist",
                model._wrap_create_agent(specialist, "generalist_messages", "Generalist"),
            )
            .add_node(
                "Supervisor",
                model._wrap_create_agent(supervisor, "supervisor_messages", "Supervisor"),
            )
            .add_edge(START, "Dispatch")
            .add_edge("Generalist", "Supervisor")
            .add_edge("Supervisor", END)
            .compile(checkpointer=model.memory, name="Sage")
        )

        _install_contract_without_graph_rebuild(model, "chat:channel-isolation:request:1")
        await model.invoke(_OLD_PROMPT, is_interactive=True)
        old_headers = [
            message
            for message in model.state["supervisor_messages"]
            if message.additional_kwargs.get("_is_completion_header")
        ]
        assert len(old_headers) == 1
        assert old_headers[0].additional_kwargs.get("_request_id") == (
            "chat:channel-isolation:request:1"
        )

        _install_contract_without_graph_rebuild(model, "chat:channel-isolation:request:2")
        await model.invoke(_SECOND_PROMPT, is_interactive=True)
        return model, supervisor

    model, supervisor = asyncio.run(run())

    assert len(supervisor.inputs) == 2, "the prior completion budget bypassed the second model turn"
    second_input = supervisor.inputs[1]
    assert sum(message.content == _OLD_PROMPT for message in second_input) == 1
    assert sum(message.content == _SECOND_PROMPT for message in second_input) == 1
    assert any(message.content == _FRESH_FINAL for message in model.state["supervisor_messages"])
    assert not any(
        message.content == _OLD_SUMMARY
        and message.additional_kwargs.get("_is_final_report")
        for message in model.state["supervisor_messages"]
    )


def test_prior_request_completion_headers_do_not_consume_current_budget():
    async def run() -> tuple[Command | dict[str, Any], _RecordingAgent]:
        model = _new_model()
        _install_contract_without_graph_rebuild(model, "chat:channel-isolation:request:2")
        supervisor = _RecordingAgent(_FRESH_FINAL)
        wrapped = model._wrap_create_agent(supervisor, "supervisor_messages", "Supervisor")
        state = {
            "_request_id": "chat:channel-isolation:request:2",
            "supervisor_messages": [
                AIMessage(
                    content="[Generalist completed task]",
                    additional_kwargs={
                        "_is_completion_header": True,
                        "_request_id": "chat:channel-isolation:request:1",
                    },
                ),
                AIMessage(
                    content="[MCP_Manager completed task]",
                    additional_kwargs={
                        "_is_completion_header": True,
                        "_request_id": "chat:channel-isolation:request:1",
                    },
                ),
                HumanMessage(content=_SECOND_PROMPT),
            ],
        }
        return await wrapped(state, {}), supervisor

    result, supervisor = asyncio.run(run())

    assert isinstance(result, dict)
    assert len(supervisor.inputs) == 1
    assert any(message.content == _FRESH_FINAL for message in result["supervisor_messages"])


def test_current_request_completion_headers_still_trigger_the_cap():
    async def run() -> tuple[Command | dict[str, Any], _RecordingAgent]:
        model = _new_model()
        request_id = "chat:channel-isolation:request:2"
        _install_contract_without_graph_rebuild(model, request_id)
        supervisor = _RecordingAgent("must not run")
        wrapped = model._wrap_create_agent(supervisor, "supervisor_messages", "Supervisor")
        state = {
            "_request_id": request_id,
            "supervisor_messages": [
                AIMessage(
                    content="[Generalist completed task]",
                    additional_kwargs={"_is_completion_header": True, "_request_id": request_id},
                ),
                AIMessage(content="first current-request summary"),
                AIMessage(
                    content="[MCP_Manager completed task]",
                    additional_kwargs={"_is_completion_header": True, "_request_id": request_id},
                ),
                AIMessage(content="second current-request summary"),
            ],
        }
        return await wrapped(state, {}), supervisor

    result, supervisor = asyncio.run(run())

    assert isinstance(result, Command)
    assert result.goto == END
    assert supervisor.inputs == []
    assert result.update["supervisor_messages"][0].content == "second current-request summary"
