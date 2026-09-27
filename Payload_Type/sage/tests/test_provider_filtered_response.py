"""An explicit provider filter terminates the assembled Sage graph once."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import (  # noqa: E402
    Model,
    MessageCaptureCallback,
    STOP_REASON_PROVIDER_FILTER,
    _ProviderFilteredResponse,
    stop_notice_for,
)


class _ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _handoff() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "transfer_to_Mythic_Operator",
            "args": {"handoff_title": "read stored task", "handoff_instruction": "Read task 3."},
            "id": "handoff-1",
            "type": "tool_call",
        }],
    )


def _drive(worker_response: AIMessage):
    async def run():
        calls = []

        def messages():
            calls.append("supervisor")
            yield _handoff()
            calls.append("worker")
            yield worker_response
            calls.append("unexpected_followup")
            raise AssertionError("provider filter caused another model call")

        model = Model(
            provider="openai",
            model="gpt-4o-mini",
            system_prompt="offline filter check",
            config={"configurable": {"api_key": "offline", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
            task_id=0,
            agent_task_id="filtered-response-test",
            response_emitter=AsyncMock(return_value=True),
            channel_id=1,
        )
        mythic = MagicMock()
        mythic.get_payload_names = AsyncMock(return_value=[])
        model.mythic_client = mythic
        model._payload_names = []
        model.memory = InMemorySaver()
        fake = _ScriptedModel(messages=messages())
        model._get_base_chat_model = lambda *a, **k: fake
        model._rebuild_graph()
        model.state["messages"] = [HumanMessage(content="Read the stored task output.")]
        config = model._graph_run_config("filtered-response-test")
        config["recursion_limit"] = 8
        try:
            result = await model.graph.ainvoke(model.state, config)
            return calls, mythic, result, None
        except Exception as exc:
            return calls, mythic, None, exc

    return asyncio.run(run())


@pytest.mark.parametrize("key", ["finish_reason", "stop_reason"])
def test_filter_stops_assembled_graph_before_worker_tools_and_supervisor_retry(key):
    filtered = AIMessage(
        content="The stored task has two privileges.",
        response_metadata={key: "content_filter"},
        tool_calls=[{
            "name": "get_all_task_output_by_task_id",
            "args": {"task_id": 3},
            "id": "must-not-run",
            "type": "tool_call",
        }],
    )
    calls, mythic, result, exc = _drive(filtered)

    assert result is None
    assert isinstance(exc, _ProviderFilteredResponse)
    assert exc.stop_reason == STOP_REASON_PROVIDER_FILTER
    assert calls == ["supervisor", "worker"]
    assert mythic.get_all_task_output_by_task_id.call_count == 0
    assert "content_filter" in stop_notice_for(exc.stop_reason)


def test_successful_worker_response_with_filter_word_in_content_is_unchanged():
    calls, _mythic, result, exc = _drive(
        AIMessage(content="The literal phrase content_filter appears in the stored note.",
                  response_metadata={"finish_reason": "stop"})
    )
    # A normal worker return gives Supervisor another model turn. The sentinel
    # records that call; the filter guard must not replace it with a halt.
    assert not isinstance(exc, _ProviderFilteredResponse)
    assert "unexpected_followup" in calls
    assert result is None


def test_plain_empty_successful_response_is_not_mislabeled_as_filter():
    calls, _mythic, result, exc = _drive(
        AIMessage(content="", response_metadata={"finish_reason": "stop"})
    )
    assert not isinstance(exc, _ProviderFilteredResponse)
    assert "unexpected_followup" in calls
    assert result is None


def test_filtered_content_and_tool_call_are_not_streamed_before_middleware_halts():
    streamed = AsyncMock(return_value=True)
    started_card = AsyncMock(return_value=None)
    callback = MessageCaptureCallback(
        agent_name="Mythic_Operator",
        stream_func=streamed,
        format_func=lambda msg, agent_name: str(msg.content),
        tool_use_func=started_card,
    )
    message = AIMessage(
        content="Do not display this filtered draft.",
        response_metadata={"finish_reason": "content_filter"},
        tool_calls=[{
            "name": "get_all_task_output_by_task_id", "args": {"task_id": 3},
            "id": "filtered-call", "type": "tool_call",
        }],
    )
    asyncio.run(callback.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=uuid4(),
    ))
    assert callback.captured_messages == []
    streamed.assert_not_awaited()
    started_card.assert_not_awaited()
