"""A native chat uses the OpenAI Responses API and recovers from a rejected route in the same channel.

Drives the real HeadlessSageChat handler, real Model and graph, real checkpointer and channel record, real
base-model factory and the installed ChatOpenAI/OpenAI SDK through ``tests.openai_responses_harness``. The
provider's 404 body is the operator-supplied v0.3.0 evidence; every successful provider reply is synthetic and
proves Sage's handling of that output, not model choice.
"""

from __future__ import annotations

import asyncio

import pytest

import sage_chat.service as service
from sage_chat import session as chat_session
from sage_chat.session import load_channel_session_record
from tests.openai_responses_harness import (
    CHANNEL,
    CYBER_RESPONSES,
    OPERATION,
    PSEUDONYM,
    Process,
    ProviderWire,
    call_item,
    input_texts,
    install_wire,
    reasoning_item,
    request,
    route_config,
    text_item,
    texts,
)

LABEL_PROMPT = "Remember the label amber-seven. Explain what an API endpoint is in one sentence."
TOOL_PROMPT = ("For a read-only status check, use list_callbacks once, then tell me the label from the previous "
               "turn and summarize the returned callback list. Do not run tasks.")
REPEAT_PROMPT = "Repeat my label without using tools."
FIRST_ANSWER = "Noted: amber-seven. An API endpoint is the URL a client calls to reach one service operation."
REPLACED = "Sage replaced this channel's session"
REFUSAL = "Sage restarted or this channel's session was replaced"
_PROCESSES: list[Process] = []


@pytest.fixture(autouse=True)
def _isolation():
    asyncio.run(chat_session.remove_session(str(CHANNEL)))
    yield

    async def close():
        await chat_session.remove_session(str(CHANNEL))
        for process in _PROCESSES:
            await process.close()

    asyncio.run(close())
    _PROCESSES.clear()
    service._CHANNEL_TURN_LOCKS.clear()
    service._NEW_CHANNEL_WITHOUT_SESSION.clear()


def _process() -> Process:
    process = Process()
    _PROCESSES.append(process)
    return process


def _terminal(chat, since: int = 0, until: int | None = None) -> dict:
    found = [e for e in chat.emissions[since:until] if e in chat.terminal_emissions]
    assert len(found) == 1, chat.emissions[since:]
    return found[0]


def _no_strand(chat):
    joined = "\n".join(texts(chat))
    assert REPLACED not in joined and REFUSAL not in joined, joined


def test_rejected_chat_completions_request_is_corrected_and_continued_in_the_same_channel(monkeypatch):
    wire = ProviderWire([
        [reasoning_item(1), text_item(FIRST_ANSWER, 1)],
        [text_item("Your label is amber-seven.", 2)],
    ])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        chat = process.chat
        await chat.chat(request(LABEL_PROMPT, 1, route_config(SAGE_OPENAI_API="chat_completions")))
        first = _terminal(chat)
        failed_turn = "\n".join(texts(chat))
        record_after_failure = await load_channel_session_record(OPERATION, CHANNEL)
        mark = len(chat.emissions)
        await chat.chat(request(LABEL_PROMPT, 2, route_config(**CYBER_RESPONSES)))
        second = _terminal(chat, mark)
        corrected_texts = texts(chat)[mark:]
        mark3 = len(chat.emissions)
        await chat.chat(request(REPEAT_PROMPT, 3, route_config(**CYBER_RESPONSES)))
        third = _terminal(chat, mark3)
        record = await load_channel_session_record(OPERATION, CHANNEL)
        return process, first, failed_turn, record_after_failure, second, corrected_texts, third, record

    process, first, failed_turn, after_failure, second, corrected_texts, third, record = asyncio.run(scenario())
    # Reported once, with the configuration action; no generic "try a different model" advice.
    assert "Partial Work Preserved" not in failed_turn and "different model" not in failed_turn
    assert failed_turn.count("set **OpenAI API** to **Responses**") == 1

    # The first request reached Chat Completions and failed with the real reported body.
    assert wire.requests[0][0] == "/v1/chat/completions"
    assert first["kind"] == "error"
    assert "HTTP 404" in first["error"] and "set **OpenAI API** to **Responses**" in first["error"]
    assert "keeps its conversation" in first["error"]
    # The channel was not retired by a configuration rejection.
    assert after_failure["status"] == chat_session.CHANNEL_SESSION_ACTIVE

    # The corrected request continued the same channel generation on the Responses API.
    _no_strand(process.chat)
    assert second["kind"] == "complete" and "amber-seven" in str(second.get("content"))
    assert any("OpenAI settings" in text for text in corrected_texts), corrected_texts
    responses = wire.responses()
    assert len(responses) == 2
    for body in responses:
        assert body["model"] == "gpt-6-cyber"
        assert body["access_programs"] == {"cyber": "daybreak_red"}
        assert body["safety_identifier"] == PSEUDONYM
        assert body["store"] is False
    assert record["thread_id"] == after_failure["thread_id"]

    # The third request replays the corrected turn: the label, the answer and the encrypted reasoning.
    assert third["kind"] == "complete" and "amber-seven" in str(third.get("content"))
    history = input_texts(responses[1])
    assert any("amber-seven" in text for text in history if text != REPEAT_PROMPT)
    assert any(FIRST_ANSWER in text for text in history)
    replayed = [item for item in responses[1]["input"] if item.get("type") == "reasoning"]
    assert replayed and replayed[0]["encrypted_content"] == "opaque-offline-reasoning-1"
    assert not process.list_callbacks_calls


def test_same_config_retry_after_a_rejection_reuses_the_channel(monkeypatch):
    wire = ProviderWire()
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        config = route_config(SAGE_OPENAI_API="chat_completions")
        await process.chat.chat(request(LABEL_PROMPT, 1, config))
        mark = len(process.chat.emissions)
        await process.chat.chat(request(LABEL_PROMPT, 2, config))
        return process, mark

    process, mark = asyncio.run(scenario())
    _no_strand(process.chat)
    second = _terminal(process.chat, mark)
    assert second["kind"] == "error" and "HTTP 404" in second["error"]
    assert [path for path, _ in wire.requests] == ["/v1/chat/completions", "/v1/chat/completions"]
    assert len(process.models) == 1


def test_function_calls_and_results_round_trip_over_responses_across_turns(monkeypatch):
    wire = ProviderWire([
        [text_item(FIRST_ANSWER, 1)],
        [reasoning_item(2), call_item("list_callbacks", {}, "call_alpha")],
        [text_item("Label amber-seven. One callback: WORKSTATION-5.", 3)],
        [call_item("list_callbacks", {}, "call_bravo")],
        [text_item("Still amber-seven; WORKSTATION-5 is the only callback.", 5)],
    ])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        config = route_config(**CYBER_RESPONSES)
        marks = []
        for index, prompt in enumerate((LABEL_PROMPT, TOOL_PROMPT, TOOL_PROMPT), start=1):
            marks.append(len(process.chat.emissions))
            await process.chat.chat(request(prompt, index, config))
        return process, marks

    process, marks = asyncio.run(scenario())
    _no_strand(process.chat)
    assert process.list_callbacks_calls == 2
    for start, end in zip(marks, [*marks[1:], None]):
        assert _terminal(process.chat, start, end)["kind"] == "complete"
    responses = wire.responses()
    assert len(responses) == 5

    # The request after each call carries that call and its matching output, adjacent and in order.
    def pairs(body):
        items = body["input"]
        return [(item["call_id"], items[i + 1].get("type"), items[i + 1].get("call_id"))
                for i, item in enumerate(items) if item.get("type") == "function_call"]

    assert pairs(responses[2]) == [("call_alpha", "function_call_output", "call_alpha")]
    assert pairs(responses[4]) == [("call_alpha", "function_call_output", "call_alpha"),
                                   ("call_bravo", "function_call_output", "call_bravo")]
    outputs = [item["output"] for item in responses[4]["input"] if item.get("type") == "function_call_output"]
    assert all("WORKSTATION-5" in output for output in outputs)
    # Visible answers are delivered once each and never include reasoning or call payloads.
    # (A terminal finalizes the last block under the same response key: one Mythic row, not two.)
    rows = {e["response_key"] for e in process.chat.emissions
            if "One callback: WORKSTATION-5." in str(e.get("content") or "")}
    assert len(rows) == 1, rows
    joined = "\n".join(texts(process.chat))
    assert "opaque-offline-reasoning" not in joined and "call_alpha" not in joined


def test_settings_change_on_a_healthy_idle_channel_continues_and_a_model_change_does_not(monkeypatch):
    wire = ProviderWire([
        [text_item(FIRST_ANSWER, 1)],
        [text_item("Your label is amber-seven.", 2)],
    ])
    install_wire(monkeypatch, wire)
    off = route_config(SAGE_OPENAI_API="responses", SAGE_OPENAI_ACCESS_PROGRAM="standard",
                       SAGE_OPENAI_SAFETY_IDENTIFIER=PSEUDONYM)

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, off))
        thread = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        mark = len(process.chat.emissions)
        await process.chat.chat(request(REPEAT_PROMPT, 2, route_config(**CYBER_RESPONSES)))
        continued = texts(process.chat)[mark:]
        thread_after = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        mark3 = len(process.chat.emissions)
        await process.chat.chat(request(REPEAT_PROMPT, 3, route_config(**{**CYBER_RESPONSES, "model": "gpt-6-sol"})))
        return process, thread, thread_after, continued, texts(process.chat)[mark3:]

    process, thread, thread_after, continued, model_change = asyncio.run(scenario())
    assert thread_after == thread
    assert not any(REPLACED in text for text in continued)
    assert any("OpenAI settings" in text for text in continued)
    first, second = wire.responses()[:2]
    assert first["access_programs"] == {"cyber": "standard"}
    assert second["access_programs"] == {"cyber": "daybreak_red"}
    assert any(FIRST_ANSWER in text for text in input_texts(second))
    # A model-string change is general model switching (#220): today's replacement refusal is unchanged.
    assert any(REPLACED in text for text in model_change)
    assert len(wire.responses()) == 2


def test_pending_approval_keeps_the_replacement_refusal_on_a_settings_change(monkeypatch):
    wire = ProviderWire([[text_item(FIRST_ANSWER, 1)]])
    install_wire(monkeypatch, wire)
    off = route_config(SAGE_OPENAI_API="responses", SAGE_OPENAI_ACCESS_PROGRAM="standard",
                       SAGE_OPENAI_SAFETY_IDENTIFIER=PSEUDONYM)

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, off))
        # Synthetic state: an approval card is outstanding on this session.
        process.models[0]._hitl_card_pending = True
        mark = len(process.chat.emissions)
        await process.chat.chat(request(REPEAT_PROMPT, 2, route_config(**CYBER_RESPONSES)))
        return process, texts(process.chat)[mark:]

    process, after = asyncio.run(scenario())
    assert any(REPLACED in text for text in after), after
    assert len(wire.responses()) == 1


def test_settings_correction_after_a_restart_restores_the_same_generation(monkeypatch):
    wire = ProviderWire([[text_item("Your label is amber-seven.", 1)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        first = _process()
        await first.chat.chat(request(LABEL_PROMPT, 1, route_config(SAGE_OPENAI_API="chat_completions")))
        thread = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        await first.close()
        await chat_session.remove_session(str(CHANNEL))
        service._CHANNEL_TURN_LOCKS.clear()
        second = _process()
        await second.chat.chat(request(REPEAT_PROMPT, 2, route_config(**CYBER_RESPONSES)))
        return second, thread

    second, thread = asyncio.run(scenario())
    _no_strand(second.chat)
    assert _terminal(second.chat)["kind"] == "complete"
    assert second.models[0]._thread_id_override == thread
    assert any(LABEL_PROMPT in text for text in input_texts(wire.responses()[0]))


def test_a_finished_approval_claim_does_not_block_a_settings_continuation(monkeypatch):
    wire = ProviderWire([[text_item(FIRST_ANSWER, 1)], [text_item("Your label is amber-seven.", 2)]])
    install_wire(monkeypatch, wire)
    off = route_config(SAGE_OPENAI_API="responses", SAGE_OPENAI_ACCESS_PROGRAM="standard",
                       SAGE_OPENAI_SAFETY_IDENTIFIER=PSEUDONYM)

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, off))
        # Synthetic record state: an earlier approval's claim that finished (consumed), then one still claimed.
        record = await load_channel_session_record(OPERATION, CHANNEL)
        await chat_session.save_channel_session_record({**record, "claim": {"approval_id": "a1", "status": "consumed"}})
        mark = len(process.chat.emissions)
        await process.chat.chat(request(REPEAT_PROMPT, 2, route_config(**CYBER_RESPONSES)))
        continued = texts(process.chat)[mark:]
        record = await load_channel_session_record(OPERATION, CHANNEL)
        await chat_session.save_channel_session_record({**record, "claim": {"approval_id": "a2", "status": "claimed"}})
        mark = len(process.chat.emissions)
        await process.chat.chat(request(REPEAT_PROMPT, 3, off))
        return continued, texts(process.chat)[mark:]

    continued, blocked = asyncio.run(scenario())
    assert not any(REPLACED in text for text in continued) and any("OpenAI settings" in t for t in continued)
    assert any(REPLACED in text for text in blocked), blocked
