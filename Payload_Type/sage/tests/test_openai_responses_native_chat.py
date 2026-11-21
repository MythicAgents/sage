"""A native chat uses the OpenAI Responses API and recovers from a rejected route in the same channel.

Drives the real HeadlessSageChat handler, real Model and graph, real checkpointer and channel record, real
base-model factory and the installed ChatOpenAI/OpenAI SDK through ``tests.openai_responses_harness``. The
provider's 404 body is the operator-supplied v0.3.0 evidence; every successful provider reply is synthetic and
proves Sage's handling of that output, not model choice.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import sage_chat.service as service
from sage_chat import session as chat_session
from sage_chat.session import load_channel_session_record
from tests.openai_responses_harness import (
    CHANNEL,
    CYBER_RESPONSES,
    KEY,
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


def test_settings_change_on_a_healthy_idle_channel_continues_and_so_does_a_model_change(monkeypatch):
    wire = ProviderWire([
        [text_item(FIRST_ANSWER, 1)],
        [text_item("Your label is amber-seven.", 2)],
        [text_item("Your label is amber-seven.", 3)],
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
    # Changed expectation (model-switch plan, required outcome 1): a model change on an idle channel now
    # continues the same conversation with the new model instead of replacing the session.
    assert not any(REPLACED in text for text in model_change)
    assert [body["model"] for body in wire.responses()] == ["gpt-6-cyber", "gpt-6-cyber", "gpt-6-sol"]


def test_pending_approval_holds_a_settings_change_and_runs_nothing(monkeypatch):
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
    # Changed expectation (model-switch plan step 4): a change that meets an outstanding approval is held:
    # nothing runs and the session stays, instead of replacing it.
    assert not any(REPLACED in text for text in after), after
    assert any("has not applied" in text for text in after), after
    assert len(wire.responses()) == 1 and len(process.models) == 1


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
    # Changed expectation (model-switch plan step 4): an outstanding claim holds the change; nothing runs.
    assert any("has not applied" in text for text in blocked), blocked
    assert not any(REPLACED in text for text in blocked), blocked


def test_a_rejected_api_key_notice_matches_what_a_key_change_does(monkeypatch):
    """Cold-review reproduction: a key change starts a new session (#220 owns general switching), so the 401
    notice must say so rather than promise the conversation survives."""
    import httpx

    class KeyWire(ProviderWire):
        def handle(self, request):
            if request.headers.get("authorization") == "Bearer bad-key":
                self.requests.append((request.url.path, {}))
                return httpx.Response(401, json={"error": {"message": "Incorrect API key provided.",
                                                           "type": "invalid_request_error", "param": None,
                                                           "code": "invalid_api_key"}})
            return super().handle(request)

    wire = KeyWire([[text_item("Noted amber-seven.", 1)]])
    install_wire(monkeypatch, wire)
    good = route_config(**CYBER_RESPONSES)

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, {**good, "API_KEY": "bad-key"}))
        mark = len(process.chat.emissions)
        await process.chat.chat(request(LABEL_PROMPT, 2, good))
        return process, mark

    process, mark = asyncio.run(scenario())
    notice = "\n".join(texts(process.chat)[:mark])
    after = "\n".join(texts(process.chat)[mark:])
    assert "HTTP 401" in notice and "starts a new chat" in notice and "keeps its conversation" not in notice
    assert REPLACED in after  # what the notice told the operator to expect


# ------------------------------------------------------------------------------------------------
# In-place model switching. Provider replies are synthetic except REPORTED_404 (operator evidence);
# model_not_found follows OpenAI's documented error shape and is labelled synthetic in the harness.
# ------------------------------------------------------------------------------------------------

MODEL_B = "gpt-5.6-sol"
B_RESPONSES = {"model": MODEL_B, "SAGE_OPENAI_API": "responses", "SAGE_OPENAI_ACCESS_PROGRAM": "standard",
               "SAGE_OPENAI_SAFETY_IDENTIFIER": PSEUDONYM}
SECOND_ANSWER = "Your label is amber-seven."


async def _restarted(first: Process) -> Process:
    await first.close()
    await chat_session.remove_session(str(CHANNEL))
    service._CHANNEL_TURN_LOCKS.clear()
    return _process()


def _invoked(wire: ProviderWire) -> list[str]:
    return [body.get("model") for _path, body in wire.requests]


def test_a_model_change_on_an_idle_channel_continues_its_conversation_with_the_new_model(monkeypatch):
    wire = ProviderWire([[text_item(FIRST_ANSWER, 1)], [text_item(SECOND_ANSWER, 2)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, route_config(**CYBER_RESPONSES)))
        thread = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        mark = len(process.chat.emissions)
        await process.chat.chat(request(REPEAT_PROMPT, 2, route_config(**B_RESPONSES)))
        return process, thread, mark, await load_channel_session_record(OPERATION, CHANNEL)

    process, thread, mark, record = asyncio.run(scenario())
    _no_strand(process.chat)
    assert _terminal(process.chat, mark)["kind"] == "complete"
    assert record["thread_id"] == thread and record["model"] == MODEL_B
    assert _invoked(wire) == ["gpt-6-cyber", MODEL_B]
    history = input_texts(wire.responses()[1])
    assert any("amber-seven" in text for text in history if text != REPEAT_PROMPT)
    assert any(FIRST_ANSWER in text for text in history)
    notices = texts(process.chat)[mark:]
    assert any("gpt-6-cyber" in text and MODEL_B in text for text in notices), notices


def test_a_model_changed_while_sage_was_down_continues_after_the_restart(monkeypatch):
    wire = ProviderWire([[text_item(FIRST_ANSWER, 1)], [text_item(SECOND_ANSWER, 2)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        first = _process()
        await first.chat.chat(request(LABEL_PROMPT, 1, route_config(**CYBER_RESPONSES)))
        thread = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        second = await _restarted(first)
        await second.chat.chat(request(REPEAT_PROMPT, 2, route_config(**B_RESPONSES)))
        return second, thread

    second, thread = asyncio.run(scenario())
    _no_strand(second.chat)
    assert _terminal(second.chat)["kind"] == "complete"
    assert second.models[0]._thread_id_override == thread
    assert _invoked(wire) == ["gpt-6-cyber", MODEL_B]
    assert any(FIRST_ANSWER in text for text in input_texts(wire.responses()[1]))


def test_a_restart_after_a_switch_restores_the_new_model(monkeypatch):
    wire = ProviderWire([[text_item(FIRST_ANSWER, 1)], [text_item(SECOND_ANSWER, 2)],
                         [text_item(SECOND_ANSWER, 3)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        first = _process()
        await first.chat.chat(request(LABEL_PROMPT, 1, route_config(**CYBER_RESPONSES)))
        await first.chat.chat(request(REPEAT_PROMPT, 2, route_config(**B_RESPONSES)))
        thread = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        second = await _restarted(first)
        await second.chat.chat(request(REPEAT_PROMPT, 3, route_config(**B_RESPONSES)))
        return second, thread

    second, thread = asyncio.run(scenario())
    _no_strand(second.chat)
    assert _terminal(second.chat)["kind"] == "complete"
    assert second.models[0]._thread_id_override == thread
    assert _invoked(wire) == ["gpt-6-cyber", MODEL_B, MODEL_B]
    assert not any("gpt-6-cyber" in text for text in texts(second.chat))  # nothing changed across the restart


def test_a_first_model_the_endpoint_rejects_is_corrected_by_a_model_change(monkeypatch):
    """Operator evidence: the real v0.3.0 404 body for a Responses-only model on Chat Completions."""
    wire = ProviderWire()
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, route_config(SAGE_OPENAI_API="chat_completions")))
        failed = _terminal(process.chat)
        thread = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        mark = len(process.chat.emissions)
        await process.chat.chat(request(
            REPEAT_PROMPT, 2, route_config(SAGE_OPENAI_API="chat_completions", model=MODEL_B)))
        return process, failed, thread, mark, await load_channel_session_record(OPERATION, CHANNEL)

    process, failed, thread, mark, record = asyncio.run(scenario())
    assert failed["kind"] == "error" and "HTTP 404" in failed["error"]
    _no_strand(process.chat)
    assert _terminal(process.chat, mark)["kind"] == "complete"
    assert record["thread_id"] == thread
    assert _invoked(wire) == ["gpt-6-cyber", MODEL_B]
    assert [path for path, _ in wire.requests] == ["/v1/chat/completions", "/v1/chat/completions"]


def test_an_invalid_first_model_is_reported_precisely_and_corrected_in_place(monkeypatch):
    wire = ProviderWire([[text_item(FIRST_ANSWER, 1)]], unknown_models=frozenset({"nonexistent-model-a"}))
    install_wire(monkeypatch, wire)
    wrong = route_config(**{**B_RESPONSES, "model": "nonexistent-model-a"})

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, wrong))
        failed = _terminal(process.chat)
        after_failure = await load_channel_session_record(OPERATION, CHANNEL)
        mark = len(process.chat.emissions)
        await process.chat.chat(request(LABEL_PROMPT, 2, route_config(**CYBER_RESPONSES)))
        return process, failed, after_failure, mark, await load_channel_session_record(OPERATION, CHANNEL)

    process, failed, after_failure, mark, record = asyncio.run(scenario())
    assert failed["kind"] == "error"
    error = failed["error"]
    assert "nonexistent-model-a" in error and "does not exist or you do not have access" in error
    assert "/model" in error and "keeps its conversation" in error and "starts a new chat" not in error
    assert after_failure["status"] == chat_session.CHANNEL_SESSION_ACTIVE
    _no_strand(process.chat)
    assert _terminal(process.chat, mark)["kind"] == "complete"
    assert record["thread_id"] == after_failure["thread_id"]
    assert _invoked(wire) == ["nonexistent-model-a", "gpt-6-cyber"]


@pytest.mark.parametrize("restart", [False, True])
def test_a_first_build_that_fails_is_corrected_in_place(monkeypatch, restart):
    wire = ProviderWire([[text_item(FIRST_ANSWER, 1)]])
    install_wire(monkeypatch, wire)
    unbuildable = route_config(model="nonexistent-model-a", SAGE_OPENAI_API="chat_completions",
                               SAGE_OPENAI_ACCESS_PROGRAM="daybreak_red")

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, unbuildable))
        failed = _terminal(process.chat)
        record_after_failure = await load_channel_session_record(OPERATION, CHANNEL)
        if restart:
            process = await _restarted(process)
        mark = len(process.chat.emissions)
        await process.chat.chat(request(LABEL_PROMPT, 2, route_config(**CYBER_RESPONSES)))
        return process, failed, record_after_failure, mark

    process, failed, record_after_failure, mark = asyncio.run(scenario())
    assert failed["kind"] == "error"
    assert "OpenAI Daybreak" in failed["error"] and "requires OpenAI API = Responses" in failed["error"]
    assert record_after_failure is None
    _no_strand(process.chat)
    assert _terminal(process.chat, mark)["kind"] == "complete"
    assert _invoked(wire) == ["gpt-6-cyber"]  # the failed build made no provider call


def test_an_invalid_replacement_keeps_the_conversation_and_switches_back(monkeypatch):
    wire = ProviderWire([[text_item(FIRST_ANSWER, 1)], [text_item(SECOND_ANSWER, 2)]],
                        unknown_models=frozenset({"nonexistent-model-b"}))
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, route_config(**CYBER_RESPONSES)))
        thread = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        mark = len(process.chat.emissions)
        await process.chat.chat(request(
            REPEAT_PROMPT, 2, route_config(**{**B_RESPONSES, "model": "nonexistent-model-b"})))
        failed = _terminal(process.chat, mark)
        mark3 = len(process.chat.emissions)
        await process.chat.chat(request(REPEAT_PROMPT, 3, route_config(**CYBER_RESPONSES)))
        return process, thread, failed, mark3, await load_channel_session_record(OPERATION, CHANNEL)

    process, thread, failed, mark3, record = asyncio.run(scenario())
    assert failed["kind"] == "error" and "nonexistent-model-b" in failed["error"]
    _no_strand(process.chat)
    assert _terminal(process.chat, mark3)["kind"] == "complete"
    assert record["thread_id"] == thread and record["model"] == "gpt-6-cyber"
    assert _invoked(wire) == ["gpt-6-cyber", "nonexistent-model-b", "gpt-6-cyber"]
    assert any(FIRST_ANSWER in text for text in input_texts(wire.responses()[2]))


def test_a_replacement_that_fails_to_build_keeps_the_old_session(monkeypatch):
    """Synthetic: the replacement Model's initialize() raises (a build failure after its settings resolved)."""
    wire = ProviderWire([[text_item(FIRST_ANSWER, 1)], [text_item(SECOND_ANSWER, 2)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        initialize = process.chat._initialize_model

        async def failing_initialize(model):
            if model.model == "unbuildable-model":
                raise RuntimeError("Model initialization failed: unbuildable-model (synthetic)")
            await initialize(model)

        process.chat._initialize_model = failing_initialize
        await process.chat.chat(request(LABEL_PROMPT, 1, route_config(**CYBER_RESPONSES)))
        before = await load_channel_session_record(OPERATION, CHANNEL)
        mark = len(process.chat.emissions)
        await process.chat.chat(request(
            REPEAT_PROMPT, 2, route_config(**{**B_RESPONSES, "model": "unbuildable-model"})))
        failed = _terminal(process.chat, mark)
        registered = await chat_session.get_session(str(CHANNEL))
        after = await load_channel_session_record(OPERATION, CHANNEL)
        mark3 = len(process.chat.emissions)
        await process.chat.chat(request(REPEAT_PROMPT, 3, route_config(**CYBER_RESPONSES)))
        return process, before, failed, registered, after, mark3

    process, before, failed, registered, after, mark3 = asyncio.run(scenario())
    assert failed["kind"] == "error" and "unbuildable-model" in failed["error"]
    assert registered is process.models[0] and after == before
    _no_strand(process.chat)
    assert _terminal(process.chat, mark3)["kind"] == "complete"
    assert await_registered_is_first(process)
    assert _invoked(wire) == ["gpt-6-cyber", "gpt-6-cyber"]
    assert any(FIRST_ANSWER in text for text in input_texts(wire.responses()[1]))


def await_registered_is_first(process: Process) -> bool:
    return asyncio.run(chat_session.get_session(str(CHANNEL))) is process.models[0]


def test_reasoning_from_another_model_is_not_replayed_but_its_calls_and_answers_are(monkeypatch):
    wire = ProviderWire([
        [reasoning_item(1), call_item("list_callbacks", {}, "call_alpha")],
        [reasoning_item(2), text_item("Label amber-seven. One callback: WORKSTATION-5.", 2)],
        [text_item(SECOND_ANSWER, 3)],
        [text_item(SECOND_ANSWER, 4)],
    ])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        await process.chat.chat(request(TOOL_PROMPT, 1, route_config(**CYBER_RESPONSES)))
        await process.chat.chat(request(REPEAT_PROMPT, 2, route_config(**B_RESPONSES)))
        await process.chat.chat(request(REPEAT_PROMPT, 3, route_config(**CYBER_RESPONSES)))
        return process

    process = asyncio.run(scenario())
    _no_strand(process.chat)
    first, second, to_b, back_to_a = wire.responses()
    # Same model, same request: its own reasoning is replayed with the call that followed it.
    assert [i["encrypted_content"] for i in second["input"] if i.get("type") == "reasoning"] == [
        "opaque-offline-reasoning-1"]
    # Another model: no reasoning item, but the call, its output and the answer all carry over.
    assert to_b["model"] == MODEL_B
    assert not [item for item in to_b["input"] if item.get("type") == "reasoning"]
    types = [item.get("type") for item in to_b["input"]]
    assert "function_call" in types and "function_call_output" in types
    assert any("One callback: WORKSTATION-5." in text for text in input_texts(to_b))
    # Back on the model that produced it, its reasoning may be replayed again.
    assert back_to_a["model"] == "gpt-6-cyber"
    assert {i["encrypted_content"] for i in back_to_a["input"] if i.get("type") == "reasoning"} == {
        "opaque-offline-reasoning-1", "opaque-offline-reasoning-2"}


class _AnthropicWire:
    """Synthetic Anthropic Messages endpoint: unknown models get Anthropic's documented not-found error."""

    def __init__(self, unknown: frozenset[str]):
        self.unknown = unknown
        self.requests: list[dict] = []

    def handle(self, request):
        import httpx
        import json as _json

        body = _json.loads(request.content)
        self.requests.append(body)
        if body.get("model") in self.unknown:
            return httpx.Response(404, json={"type": "error", "error": {
                "type": "not_found_error", "message": f"model: {body['model']}"}})
        return httpx.Response(200, json={
            "id": f"msg_offline_{len(self.requests)}", "type": "message", "role": "assistant",
            "model": body["model"], "content": [{"type": "text", "text": SECOND_ANSWER}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}})


def test_a_rejection_before_any_answer_keeps_the_channel_for_any_provider(monkeypatch):
    """Synthetic provider: the real ChatAnthropic and Anthropic SDK over a mock transport."""
    import httpx
    from langchain_anthropic import chat_models as anthropic_models

    wire = _AnthropicWire(frozenset({"claude-nonexistent"}))
    monkeypatch.setattr(anthropic_models, "_get_default_async_httpx_client",
                        lambda **kw: httpx.AsyncClient(base_url=kw["base_url"],
                                                       transport=httpx.MockTransport(wire.handle)))
    monkeypatch.setattr(anthropic_models, "_get_default_httpx_client",
                        lambda **kw: httpx.Client(base_url=kw["base_url"],
                                                  transport=httpx.MockTransport(wire.handle)))

    def config(model: str) -> dict:
        return {"provider": "anthropic", "model": model, "API_KEY": KEY,
                "API_ENDPOINT": "https://anthropic.invalid", "mode": "conversation"}

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, config("claude-nonexistent")))
        failed = _terminal(process.chat)
        after_failure = await load_channel_session_record(OPERATION, CHANNEL)
        mark = len(process.chat.emissions)
        await process.chat.chat(request(LABEL_PROMPT, 2, config("claude-sonnet-5")))
        return process, failed, after_failure, mark, await load_channel_session_record(OPERATION, CHANNEL)

    process, failed, after_failure, mark, record = asyncio.run(scenario())
    error = failed["error"]
    assert "anthropic" in error and "claude-nonexistent" in error and "model: claude-nonexistent" in error
    assert "/model" in error and "keeps its conversation" in error
    assert after_failure["status"] == chat_session.CHANNEL_SESSION_ACTIVE
    _no_strand(process.chat)
    assert _terminal(process.chat, mark)["kind"] == "complete"
    assert record["thread_id"] == after_failure["thread_id"]
    assert [body["model"] for body in wire.requests] == ["claude-nonexistent", "claude-sonnet-5"]


# ------------------------------------------------------------------------------------------------
# /model and the Model chip. Mythic is a recorder (synthetic rows in Mythic's chat_channel columns;
# the write follows chatUpdateChannel, which replaces ai_metadata whole).
# ------------------------------------------------------------------------------------------------

class _MythicChannel:
    def __init__(self, config: dict, *, refuse: str = ""):
        self.ai_metadata = {"config": dict(config), "channel_metadata": {"items": [{"key": "cfg_mode"}]},
                            "display": "Mode:info"}
        self.refuse = refuse
        self.queries: list[str] = []
        self.writes: list[dict] = []

    async def execute(self, query, variables):
        self.queries.append(query)
        if "chatUpdateChannel" in query:
            assert variables["channel_id"] == CHANNEL, variables
            self.writes.append(variables["ai_metadata"])
            if self.refuse:
                return {"chatUpdateChannel": {"status": "error", "error": self.refuse}}
            self.ai_metadata = variables["ai_metadata"]
            return {"chatUpdateChannel": {"status": "success", "error": ""}}
        assert variables == {"channel_id": CHANNEL, "operation_id": OPERATION}, variables
        return {"chat_channel": [{"id": CHANNEL, "ai_metadata": self.ai_metadata}]}


def _slash(name: str, argument: str, request_id: int, config: dict):
    from mythic_container.ChatBase import ChatSlashCommandInvocation

    item = request(f"/{name} {argument}".strip(), request_id, config)
    item.SlashCommand = ChatSlashCommandInvocation(name=name, argument=argument)
    return item


def _with_mythic(monkeypatch, process: Process, channel: _MythicChannel) -> None:
    from mythic import mythic as mythic_api

    async def execute_custom_query(client, query, variables=None):
        return await client.execute(query, dict(variables or {}))

    async def read_client(_request, _model=None):
        return channel

    monkeypatch.setattr(mythic_api, "execute_custom_query", execute_custom_query)
    process.chat._mythic_read_client = read_client


def test_model_command_shows_the_configured_model_and_its_source(monkeypatch):
    wire = ProviderWire()
    install_wire(monkeypatch, wire)
    config = route_config(**CYBER_RESPONSES)

    async def scenario():
        process = _process()
        channel = _MythicChannel(config)
        _with_mythic(monkeypatch, process, channel)
        await process.chat.chat(_slash("model", "", 1, config))
        return process, channel

    process, channel = asyncio.run(scenario())
    reply = _terminal(process.chat)
    assert reply["kind"] == "complete"
    assert "gpt-6-cyber" in reply["content"] and "ui-config" in reply["content"]
    assert "/model <name>" in reply["content"]
    assert not channel.writes and not wire.requests


def test_model_command_writes_only_the_model_and_the_next_message_uses_it(monkeypatch):
    wire = ProviderWire([[text_item(FIRST_ANSWER, 1)], [text_item(SECOND_ANSWER, 2)]])
    install_wire(monkeypatch, wire)
    config = route_config(**CYBER_RESPONSES)

    async def scenario():
        process = _process()
        channel = _MythicChannel(config)
        _with_mythic(monkeypatch, process, channel)
        await process.chat.chat(request(LABEL_PROMPT, 1, config))
        thread = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        before = json.loads(json.dumps(channel.ai_metadata))
        mark = len(process.chat.emissions)
        await process.chat.chat(_slash("model", MODEL_B, 2, config))
        reply = _terminal(process.chat, mark)
        mark3 = len(process.chat.emissions)
        await process.chat.chat(request(REPEAT_PROMPT, 3, channel.ai_metadata["config"]))
        return process, channel, before, thread, reply, mark3

    process, channel, before, thread, reply, mark3 = asyncio.run(scenario())
    assert reply["kind"] == "complete" and MODEL_B in reply["content"] and "next message" in reply["content"]
    (written,) = channel.writes
    expected = json.loads(json.dumps(before))
    expected["config"]["model"] = MODEL_B
    assert written == expected  # every other ai_metadata key survives the whole-value write
    _no_strand(process.chat)
    assert _terminal(process.chat, mark3)["kind"] == "complete"
    assert process.models[-1]._thread_id_override == thread
    assert _invoked(wire) == ["gpt-6-cyber", MODEL_B]


@pytest.mark.parametrize("argument", ["two words", "tab\tseparated", "bell\x07"])
def test_model_command_rejects_a_value_that_is_not_one_token(monkeypatch, argument):
    install_wire(monkeypatch, ProviderWire())
    config = route_config(**CYBER_RESPONSES)

    async def scenario():
        process = _process()
        channel = _MythicChannel(config)
        _with_mythic(monkeypatch, process, channel)
        await process.chat.chat(_slash("model", argument, 1, config))
        return process, channel

    process, channel = asyncio.run(scenario())
    reply = _terminal(process.chat)
    assert "Usage" in reply["content"] and not channel.queries


@pytest.mark.parametrize("value", ["arn:aws:bedrock:us-east-1:111122223333:inference-profile/us.example.model-v1:0",
                                   "llama3.1:8b", "org/model@2026-01-01"])
def test_model_command_accepts_provider_identifiers(monkeypatch, value):
    install_wire(monkeypatch, ProviderWire())
    config = route_config(**CYBER_RESPONSES)

    async def scenario():
        process = _process()
        channel = _MythicChannel(config)
        _with_mythic(monkeypatch, process, channel)
        await process.chat.chat(_slash("model", value, 1, config))
        return channel

    channel = asyncio.run(scenario())
    assert [write["config"]["model"] for write in channel.writes] == [value]


def test_model_command_reports_mythics_refusal_and_changes_nothing(monkeypatch):
    install_wire(monkeypatch, ProviderWire())
    config = route_config(**CYBER_RESPONSES)
    refusal = "only the channel creator or an operation admin can update this channel"

    async def scenario():
        process = _process()
        channel = _MythicChannel(config, refuse=refusal)
        _with_mythic(monkeypatch, process, channel)
        await process.chat.chat(_slash("model", MODEL_B, 1, config))
        return process, channel

    process, channel = asyncio.run(scenario())
    reply = _terminal(process.chat)
    assert refusal in reply["content"] and "gpt-6-cyber" in reply["content"]
    assert channel.ai_metadata["config"]["model"] == "gpt-6-cyber"


def test_model_chip_runs_model_and_is_the_only_model_chip():
    from types import SimpleNamespace

    from sage_chat import models as chat_models
    from sage_chat.metadata import build_channel_metadata
    from sage_chat.slash import SAGE_SLASH_COMMANDS, WATCHER_SLASH_COMMANDS

    items = {item["key"]: item for item in build_channel_metadata(SimpleNamespace(model=MODEL_B))["items"]}
    chip = items["cfg_model"]
    assert chip["label"] == "Model" and chip["value"] == MODEL_B and chip["click"] == "/model"
    assert chip["click_confirmation_text"]
    sage_model_option = next(o for o in chat_models._CONFIG_OPTIONS if o.Name == "model")
    assert not sage_model_option.DisplayAsChip
    assert "model" in {c.Name for c in SAGE_SLASH_COMMANDS}
    assert "model" not in {c.Name for c in WATCHER_SLASH_COMMANDS}


def test_a_dated_provider_model_name_still_replays_the_models_own_reasoning(monkeypatch):
    """Synthetic: the provider reports a dated snapshot name for the configured model, as OpenAI may."""
    import httpx

    class DatedWire(ProviderWire):
        def handle(self, request):
            response = super().handle(request)
            body = response.json()
            if response.status_code == 200 and "model" in body:
                body["model"] = f"{body['model']}-2026-11-01"
            return httpx.Response(response.status_code, json=body)

    wire = DatedWire([[reasoning_item(1), text_item(FIRST_ANSWER, 1)], [text_item(SECOND_ANSWER, 2)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        await process.chat.chat(request(LABEL_PROMPT, 1, route_config(**CYBER_RESPONSES)))
        await process.chat.chat(request(REPEAT_PROMPT, 2, route_config(**CYBER_RESPONSES)))
        return process

    process = asyncio.run(scenario())
    _no_strand(process.chat)
    replayed = [i for i in wire.responses()[1]["input"] if i.get("type") == "reasoning"]
    assert [item["encrypted_content"] for item in replayed] == ["opaque-offline-reasoning-1"]
