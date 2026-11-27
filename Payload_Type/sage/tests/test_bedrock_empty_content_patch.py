"""Sage's empty-content patch reaches the real langchain-openai Chat Completions payload builder.

``_apply_bedrock_patch`` (``ai/langgraph/model.py``) replaces the module function
``langchain_openai.chat_models.base._convert_message_to_dict`` so a Bedrock model behind an OpenAI-compatible
gateway never receives a blank assistant turn. It only works while the installed adapter still builds every Chat
Completions message through that module global. This test drives the installed ``ChatOpenAI._get_request_payload``
itself, with the patch and without it.
"""

from __future__ import annotations

import inspect

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
import langchain_openai.chat_models.base as openai_base

from ai.langgraph import model as model_module

TOOL_CALL = {"id": "call_fixture", "name": "query_callbacks", "args": {}, "type": "tool_call"}


def _conversation() -> list:
    return [
        HumanMessage(content="list the callbacks"),
        AIMessage(content="", tool_calls=[TOOL_CALL]),
        ToolMessage(content="callback 2", tool_call_id="call_fixture"),
        AIMessage(content=[{"type": "text", "text": "  "}]),
        AIMessage(content=""),
        HumanMessage(content="thanks"),
    ]


def _assistant_contents(llm: ChatOpenAI) -> list:
    payload = llm._get_request_payload(_conversation())
    assert "messages" in payload, "the payload must be Chat Completions for this check"
    return [message.get("content") for message in payload["messages"] if message["role"] == "assistant"]


@pytest.fixture
def chat_completions_model(monkeypatch):
    # The patch is process-wide and marks the function it wraps; start each test from the adapter's own function.
    monkeypatch.setattr(openai_base, "_convert_message_to_dict", inspect.unwrap(openai_base._convert_message_to_dict))
    monkeypatch.setattr(model_module, "_bedrock_patch_applied", False)
    return ChatOpenAI(
        model="fixture-model", api_key="fixture-key", base_url="http://127.0.0.1:9/v1", use_responses_api=False
    )


def test_the_patch_fills_every_blank_assistant_turn_in_the_real_payload(chat_completions_model):
    patched = model_module._patch_model_for_bedrock(chat_completions_model)

    assert patched is chat_completions_model
    tool_turn, blank_block_turn, empty_turn = _assistant_contents(patched)
    assert tool_turn == "."
    assert blank_block_turn == [{"type": "text", "text": "."}]
    assert empty_turn == "."


def test_without_the_patch_the_installed_builder_sends_blank_assistant_turns(chat_completions_model):
    tool_turn, blank_block_turn, empty_turn = _assistant_contents(chat_completions_model)

    assert tool_turn in (None, "")
    assert blank_block_turn == [{"type": "text", "text": "  "}]
    assert empty_turn == ""
