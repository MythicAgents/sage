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


def _history(row, call):
    return [HumanMessage(row["prompt"]), call,
            ToolMessage(content=row["tool_result"], tool_call_id=row["expected"]["call_id"])]


def test_recorded_rows_replay_to_another_model_with_calls_results_and_text_intact():
    """Cross-model replay of the recorded gpt-6-cyber rows (they hold no reasoning item; see provenance gap)."""
    row = ROWS["supervisor_tool_round_trip"]
    call = _message(row["call"])
    history = _history(row, call)
    same, same_changed = _sanitize_model_messages(history, configured_model="gpt-6-cyber")
    other, other_changed = _sanitize_model_messages(history, configured_model="gpt-5.6-sol")
    assert not same_changed and same[1] is call
    assert not other_changed and other[1] is call
    items = _construct_responses_api_input(other, store=False)
    assert [item.get("type") for item in items] == ["message", "function_call", "function_call_output"]
    assert items[1]["call_id"] == items[2]["call_id"] == row["expected"]["call_id"]
    plain = _message(ROWS["plain_text_answer"]["answer"])
    cleaned, changed = _sanitize_model_messages([HumanMessage("hi"), plain], configured_model="gpt-5.6-sol")
    assert not changed and cleaned[1] is plain


def _with_reasoning(call: AIMessage, *, stamp: str | None, provider_name: str) -> AIMessage:
    """Synthetic: an encrypted reasoning item added before the recorded call, as the adapter lays it out."""
    metadata = {**call.response_metadata, "model_name": provider_name}
    if stamp is not None:
        metadata["sage_model"] = stamp
    reasoning = {"type": "reasoning", "id": "rs_synthetic", "summary": [], "encrypted_content": "opaque-synthetic"}
    return call.model_copy(update={"content": [reasoning, *call.content], "response_metadata": metadata})


@pytest.mark.parametrize("stamp,provider_name,configured,kept", [
    ("gpt-6-cyber", "gpt-6-cyber-2026-11-01", "gpt-6-cyber", True),   # a dated provider name is not compared
    ("gpt-6-cyber", "gpt-6-cyber", "gpt-5.6-sol", False),             # another model: reasoning dropped
    (None, "gpt-6-cyber", "gpt-6-cyber", True),                       # unstamped, exact provider name
    (None, "gpt-6-cyber-2026-11-01", "gpt-6-cyber", False),           # unstamped and unsure: safe direction
    (None, "gpt-6-cyber", "gpt-6", False),                            # a prefix is not the same model
])
def test_reasoning_replays_only_to_the_model_sage_had_configured(stamp, provider_name, configured, kept):
    row = ROWS["supervisor_tool_round_trip"]
    call = _with_reasoning(_message(row["call"]), stamp=stamp, provider_name=provider_name)
    cleaned, changed = _sanitize_model_messages(_history(row, call), configured_model=configured)
    replayed = cleaned[1]
    types = [block["type"] for block in replayed.content]
    if kept:
        assert not changed and replayed is call
    else:
        assert changed and types == ["function_call"] and replayed.tool_calls == call.tool_calls
    items = _construct_responses_api_input(cleaned, store=False)
    assert ("reasoning" in [item.get("type") for item in items]) is kept
    assert [item.get("type") for item in items][-2:] == ["function_call", "function_call_output"]


@pytest.mark.parametrize("block", [
    {"type": "thinking", "thinking": "synthetic", "signature": "sig-synthetic"},
    {"type": "redacted_thinking", "data": "synthetic"},
    {"type": "reasoning_content", "reasoning_content": {"text": "synthetic", "signature": "sig"}},
])
def test_other_providers_signed_reasoning_is_not_sent_to_another_model(block):
    """Synthetic Anthropic and Bedrock reasoning blocks: Sage does not enable them, so none is recorded."""
    message = AIMessage(content=[block, {"type": "text", "text": "Answer text."}],
                        response_metadata={"sage_model": "claude-sonnet-5"})
    cleaned, changed = _sanitize_model_messages([HumanMessage("hi"), message], configured_model="claude-opus-5")
    assert changed and cleaned[1].content == [{"type": "text", "text": "Answer text."}]
    same, same_changed = _sanitize_model_messages([HumanMessage("hi"), message], configured_model="claude-sonnet-5")
    assert not same_changed and same[1] is message


SWITCH = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/model_switch_round_trips.json").read_text())
SWITCH_ROWS = {row["id"]: row for row in SWITCH["rows"]}


def test_model_switch_rows_record_their_provenance_and_gap():
    assert SWITCH["provenance"]["kind"].startswith("recorded model output")
    assert "text only" in SWITCH["provenance"]["gap"]
    assert set(SWITCH_ROWS) == {"cross_model_tool_history", "answer_after_switch_to_b", "answer_after_switch_back_to_a"}


def test_recorded_cyber_tool_exchange_replays_unchanged_to_the_second_model():
    row = SWITCH_ROWS["cross_model_tool_history"]
    call, respond = _message(row["call"]), _message(row["respond"])
    assert call.response_metadata["model_name"] == row["producer_model"]
    assert [(c["name"], c["id"]) for c in call.tool_calls] == [(row["expected"]["tool"], row["expected"]["call_id"])]
    history = [HumanMessage(row["prompt"]), call,
               ToolMessage(content=row["tool_result"], tool_call_id=row["expected"]["call_id"])]
    cleaned, changed = _sanitize_model_messages(history, configured_model=row["replayed_to"])
    assert not changed and cleaned[1] is call
    items = _construct_responses_api_input(cleaned, store=False)
    assert [item.get("type") for item in items] == ["message", "function_call", "function_call_output"]
    assert items[1]["call_id"] == items[2]["call_id"] == row["expected"]["call_id"]
    assert [c["name"] for c in respond.tool_calls] == [row["expected"]["answer_tool"]]
    assert _tool_call_envelope_error(call) == "" and _tool_call_envelope_error(respond) == ""


def test_recorded_answer_after_switching_to_the_second_model_carries_the_label():
    row = SWITCH_ROWS["answer_after_switch_to_b"]
    answer = _message(row["answer"])
    assert answer.response_metadata["model_name"] == row["configured_model"]
    (respond,) = answer.tool_calls
    assert respond["name"] == row["expected"]["answer_tool"]
    assert row["expected"]["answer_contains"] in respond["args"]["final_response"]
    assert _tool_call_envelope_error(answer) == "" and _render(answer) == ""
    # Nothing provider-opaque to drop if this answer is later replayed to another model.
    assert [block["type"] for block in answer.content] == ["function_call"]


@pytest.mark.parametrize("reader", ["render", "text", "sole"])
def test_recorded_answer_after_switching_back_reads_as_its_text(reader):
    row = SWITCH_ROWS["answer_after_switch_back_to_a"]
    answer = _message(row["answer"])
    assert answer.response_metadata["model_name"] == row["configured_model"]
    value = {"render": lambda: _render(answer), "text": lambda: _message_content_as_text(answer.content),
             "sole": lambda: _sole_provider_text(answer.content)}[reader]()
    assert value.strip() == row["expected"]["text"]
