"""Sage's model-output consumers accept Responses-shaped messages and reject near-matches.

Messages are produced by the installed adapter's own Responses normalizer from Responses JSON (synthetic
bodies in the shape the 2026-11-20 real-provider probe recorded: reasoning with encrypted content, function
calls with ``call_`` ids, text with ``annotations``/``id``). History is re-serialized by the adapter's own
``_construct_responses_api_input`` with ``store=False``, exactly as a Sage Responses request does.
"""

from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import (
    _construct_lc_result_from_responses_api,
    _construct_responses_api_input,
)
from openai.types.responses import Response

from ai.langgraph import model as model_module
from ai.langgraph.model import Model, _sanitize_model_messages, _tool_call_envelope_error


def _response(output: list[dict]) -> AIMessage:
    body = {"id": "resp_offline", "object": "response", "created_at": 0, "model": "gpt-6-cyber",
            "status": "completed", "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "output": output,
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                      "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0, "cache_write_tokens_details": {"ttl_12h_tokens": 0, "ttl_30m_tokens": 0}},
                      "output_tokens_details": {"reasoning_tokens": 1}}}
    result = _construct_lc_result_from_responses_api(Response.model_validate(body), output_version="responses/v1")
    return result.generations[0].message


REASONING = {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "opaque-1"}


def _call(call_id: str, args: dict, name: str = "list_callbacks") -> dict:
    return {"id": f"fc_{call_id}", "type": "function_call", "status": "completed", "name": name,
            "arguments": json.dumps(args), "call_id": call_id}


def _text(text: str, **extra) -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": [], **extra}]}


def _wire_items(messages) -> list[dict]:
    return _construct_responses_api_input(messages, store=False)


# --------------------------------------------------------------------------------------------------
# History sanitizer: a stripped call leaves no function_call (or its reasoning) for the provider
# --------------------------------------------------------------------------------------------------

def test_orphan_responses_call_is_removed_from_the_replayed_input():
    orphan = _response([REASONING, _call("call_x", {})])
    history = [HumanMessage("first"), orphan, HumanMessage("second")]
    cleaned, changed = _sanitize_model_messages(history)
    assert changed
    items = _wire_items(cleaned)
    assert not [item for item in items if item.get("type") in {"function_call", "reasoning"}], items
    assert [item.get("role") for item in items if item.get("type") == "message"][:1] == ["user"]


def test_valid_responses_call_and_result_are_kept_unchanged():
    call = _response([REASONING, _call("call_x", {})])
    history = [HumanMessage("first"), call, ToolMessage(content="[]", tool_call_id="call_x")]
    cleaned, changed = _sanitize_model_messages(history)
    assert not changed and cleaned[1] is call
    types = [item.get("type") for item in _wire_items(cleaned)]
    assert types == ["message", "reasoning", "function_call", "function_call_output"]


def test_reasoning_followed_by_text_survives_a_stripped_call_in_another_message():
    answered = _response([REASONING, _text("done")])
    orphan = _response([_call("call_y", {})])
    cleaned, _ = _sanitize_model_messages([HumanMessage("a"), answered, HumanMessage("b"), orphan,
                                           HumanMessage("c")])
    types = [item.get("type") for item in _wire_items(cleaned)]
    assert types.count("reasoning") == 1 and "function_call" not in types


# --------------------------------------------------------------------------------------------------
# Tool-call envelope: Responses function_call blocks must agree with the normalized calls
# --------------------------------------------------------------------------------------------------

def test_matching_function_call_block_is_a_valid_envelope():
    message = _response([REASONING, _call("call_a", {}), _call("call_b", {"limit": 1}, "list_callbacks")])
    assert [call["id"] for call in message.tool_calls] == ["call_a", "call_b"]
    assert _tool_call_envelope_error(message) == ""


@pytest.mark.parametrize("mutate", ["args", "name", "call_id", "extra_block", "bad_json"])
def test_divergent_function_call_block_is_an_invalid_envelope(mutate):
    message = _response([_call("call_a", {"limit": 1})])
    content = [dict(block) for block in message.content]
    if mutate == "args":
        content[0]["arguments"] = json.dumps({"limit": 2})
    elif mutate == "name":
        content[0]["name"] = "issue_task_and_waitfor_task_output"
    elif mutate == "call_id":
        content[0]["call_id"] = "call_other"
    elif mutate == "extra_block":
        content.append(dict(content[0], call_id="call_unlisted", id="fc_unlisted"))
    else:
        content[0]["arguments"] = "{\"limit\": "
    forged = message.model_copy(update={"content": content})
    assert _tool_call_envelope_error(forged)


def test_fragmented_streamed_arguments_only_become_a_call_when_complete():
    """Adapter stream path: argument deltas aggregate into one call; a partial chunk is never executable."""
    events = [
        {"type": "response.created", "sequence_number": 0, "response": {
            "id": "resp_s", "object": "response", "created_at": 0, "model": "gpt-6-cyber", "status": "in_progress",
            "output": [], "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}},
        {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0, "item": {
            "id": "fc_s", "type": "function_call", "status": "in_progress", "name": "list_callbacks",
            "arguments": "", "call_id": "call_s"}},
        *[{"type": "response.function_call_arguments.delta", "sequence_number": 2 + i, "output_index": 0,
           "item_id": "fc_s", "delta": piece} for i, piece in enumerate(['{"lim', 'it": ', '1}'])],
        {"type": "response.function_call_arguments.done", "sequence_number": 5, "output_index": 0,
         "item_id": "fc_s", "arguments": '{"limit": 1}', "name": "list_callbacks"},
        {"type": "response.output_item.done", "sequence_number": 6, "output_index": 0, "item": {
            "id": "fc_s", "type": "function_call", "status": "completed", "name": "list_callbacks",
            "arguments": '{"limit": 1}', "call_id": "call_s"}},
        {"type": "response.completed", "sequence_number": 7, "response": {
            "id": "resp_s", "object": "response", "created_at": 0, "model": "gpt-6-cyber", "status": "completed",
            "output": [{"id": "fc_s", "type": "function_call", "status": "completed", "name": "list_callbacks",
                        "arguments": '{"limit": 1}', "call_id": "call_s"}],
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                      "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0, "cache_write_tokens_details": {"ttl_12h_tokens": 0, "ttl_30m_tokens": 0}},
                      "output_tokens_details": {"reasoning_tokens": 0}}}},
    ]
    sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    transport = httpx.MockTransport(lambda _r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=sse.encode()))
    llm = ChatOpenAI(model="gpt-6-cyber", api_key="offline", base_url="https://provider.invalid/v1",
                     use_responses_api=True, output_version="responses/v1", store=False, max_retries=0,
                     http_client=httpx.Client(transport=transport))
    chunks = list(llm.stream("Use list_callbacks."))
    partial = None
    for chunk in chunks:
        partial = chunk if partial is None else partial + chunk
        if partial.tool_call_chunks and partial.tool_call_chunks[0].get("args") == '{"lim':
            assert _tool_call_envelope_error(AIMessage(content=partial.content, tool_calls=partial.tool_calls,
                                                       invalid_tool_calls=partial.invalid_tool_calls))
    assert isinstance(partial, AIMessageChunk)
    whole = AIMessage(content=partial.content, tool_calls=partial.tool_calls,
                      invalid_tool_calls=partial.invalid_tool_calls)
    assert [(c["name"], c["args"], c["id"]) for c in whole.tool_calls] == [("list_callbacks", {"limit": 1}, "call_s")]
    assert _tool_call_envelope_error(whole) == ""


# --------------------------------------------------------------------------------------------------
# Strict one-label parsers (supervised semantic routing, continuation intent)
# --------------------------------------------------------------------------------------------------

PARSERS = [
    (Model._parse_supervised_semantic_intent, "action", "action"),
    (Model._parse_continuation_intent_response, "CONTINUE", "CONTINUE"),
]


@pytest.mark.parametrize(("parse", "label", "expected"), PARSERS)
def test_responses_label_with_reasoning_is_accepted(parse, label, expected):
    assert parse(_response([REASONING, _text(label)])) == expected
    assert parse(_response([_text(label)])) == expected
    assert parse(AIMessage(content=label)) == expected


@pytest.mark.parametrize(("parse", "label", "expected"), PARSERS)
def test_responses_near_matches_are_still_rejected(parse, label, expected):
    two_texts = _response([_text(label), {**_text(label), "id": "msg_2"}])
    with_annotation = _response([_text(label)])
    with_annotation = with_annotation.model_copy(update={"content": [
        {**with_annotation.content[0], "annotations": [{"type": "url_citation", "url": "https://x.invalid"}]}]})
    unknown_key = _response([_text(label)])
    unknown_key = unknown_key.model_copy(update={"content": [{**unknown_key.content[0], "extra": True}]})
    with_call = _response([_text(label), _call("call_z", {})])
    for value in (two_texts, with_annotation, unknown_key, with_call, _response([REASONING])):
        assert parse(value) == "", value.content


# --------------------------------------------------------------------------------------------------
# Plain-text consumers of direct base-model calls never publish block lists
# --------------------------------------------------------------------------------------------------

class _OneReply:
    def __init__(self, message):
        self.message = message
        self.calls = 0

    async def ainvoke(self, *_args, **_kwargs):
        self.calls += 1
        return self.message


def test_per_agent_summary_publishes_text_never_the_block_list():
    import asyncio

    sage = Model.__new__(Model)
    sage.state = {"supervisor_messages": [HumanMessage("status?"), AIMessage(content="One callback.")]}
    sage.llm = _OneReply(_response([REASONING, _text("Supervisor listed one callback.")]))
    summary = asyncio.run(sage._generate_per_agent_summaries())
    assert sage.llm.calls == 1
    assert summary == "**Supervisor:**\nSupervisor listed one callback."
    assert "opaque-1" not in summary and "encrypted_content" not in summary


def test_text_extraction_skips_a_reasoning_item_that_carries_string_content():
    content = [{"type": "reasoning", "id": "rs_2", "content": "hidden chain", "encrypted_content": "opaque-2"},
               {"type": "text", "text": "Visible answer.", "annotations": [], "id": "msg_2"}]
    assert model_module._message_content_as_text(content) == "Visible answer."
