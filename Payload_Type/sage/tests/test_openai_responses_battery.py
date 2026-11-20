"""Battery rows: real gpt-6-cyber Responses output replayed through Sage's consumers.

Rows in ``fixtures/model_input_boundaries/responses_round_trips.json`` are the AIMessages the installed adapter
produced from the live provider's Responses output (see the fixture's provenance). Each is loaded with
LangChain's own deserializer and passed through the real consumers this line touched: the tool-call envelope,
the history sanitizer, the adapter's stateless Responses replay, native rendering and the strict text reader.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.load import load
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai.chat_models.base import _construct_responses_api_input

from ai.langgraph.model import (
    Model,
    _message_content_as_text,
    _sanitize_model_messages,
    _sole_provider_text,
    _tool_call_envelope_error,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/responses_round_trips.json").read_text())
ROWS = {row["id"]: row for row in FIXTURE["rows"]}


def _message(serialized) -> AIMessage:
    message = load(serialized)
    assert isinstance(message, AIMessage)
    return message


def _render(message: AIMessage) -> str:
    renderer = Model.__new__(Model)
    renderer.channel_id = 1
    renderer.verbose = True
    renderer.is_interactive = True
    return renderer._format_message_for_streaming(message, agent_name=message.name or "AI")


def test_provenance_is_recorded_and_names_its_gap():
    provenance = FIXTURE["provenance"]
    assert provenance["kind"].startswith("recorded model output")
    assert "reasoning" in provenance["gap"]
    assert set(ROWS) == {"supervisor_tool_round_trip", "handoff_then_worker_text", "plain_text_answer"}


def test_recorded_supervisor_tool_call_round_trips_through_the_responses_replay():
    row = ROWS["supervisor_tool_round_trip"]
    call, answer = _message(row["call"]), _message(row["answer"])
    expected = row["expected"]
    assert [(c["name"], c["id"]) for c in call.tool_calls] == [(expected["tool"], expected["call_id"])]
    assert [block["type"] for block in call.content] == ["function_call"]
    assert _tool_call_envelope_error(call) == "" and _tool_call_envelope_error(answer) == ""

    history = [HumanMessage(row["prompt"]), call, ToolMessage(content=row["tool_result"], tool_call_id=expected["call_id"])]
    cleaned, changed = _sanitize_model_messages(history)
    assert not changed and cleaned[1] is call
    items = _construct_responses_api_input(cleaned, store=False)
    assert [item.get("type") for item in items] == ["message", "function_call", "function_call_output"]
    assert items[1]["call_id"] == items[2]["call_id"] == expected["call_id"]
    assert "WORKSTATION-A" in items[2]["output"]

    (respond,) = answer.tool_calls
    assert respond["name"] == expected["answer_tool"]
    assert expected["answer_contains"] in respond["args"]["final_response"]
    assert _render(call) == "" and _render(answer) == ""  # call payloads are never rendered as chat text


def test_recorded_handoff_and_worker_text_render_text_only():
    row = ROWS["handoff_then_worker_text"]
    handoff, worker = _message(row["call"]), _message(row["worker"])
    assert [c["name"] for c in handoff.tool_calls] == [row["expected"]["tool"]]
    assert _tool_call_envelope_error(handoff) == ""
    rendered = _render(worker)
    assert row["expected"]["worker_text_contains"] in rendered
    assert "function_call" not in rendered and "msg_" not in rendered
    assert _message_content_as_text(worker.content).strip() in rendered


@pytest.mark.parametrize("reader", ["render", "text", "sole"])
def test_recorded_plain_text_answer_reads_as_its_text(reader):
    row = ROWS["plain_text_answer"]
    answer = _message(row["answer"])
    assert isinstance(answer.content, list) and set(answer.content[0]) >= {"type", "text", "annotations", "id"}
    value = {"render": lambda: _render(answer), "text": lambda: _message_content_as_text(answer.content),
             "sole": lambda: _sole_provider_text(answer.content)}[reader]()
    assert value.strip() == row["expected"]["text"]
