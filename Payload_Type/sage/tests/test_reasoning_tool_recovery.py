"""Reasoning-enabled tool calls: a rejected API or effort setting is explained and corrected in the same chat.

Drives the real HeadlessSageChat handler, real Model and graph, real checkpointer and channel record, real
base-model factory and the installed ChatOpenAI/OpenAI SDK through ``tests.openai_responses_harness``; only the
provider HTTP transport is a stand-in.

Provenance: the two rejection **messages** are the operator-transcribed team errors (Sage v0.3.0, relayed
2026-11-21/22). Their **envelopes** (status/param/code fields) are synthetic: no recorded envelope exists, so each
case states the fields it assumes and the equivalents it accepts. Every successful provider reply is synthetic
and proves Sage's handling of that output, not model choice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import openai
import pytest

import sage_chat.service as service
from ai.langgraph.operator_error import (
    provider_configuration_notice,
    provider_configuration_rejection,
)
from sage_chat import session as chat_session
from sage_chat.config import resolve_llm_profile
from sage_chat.session import load_channel_session_record
from tests.openai_responses_harness import (
    CHANNEL,
    ENDPOINT,
    KEY,
    OPERATION,
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

SOL_A = "gpt-5.6-sol"
SOL_B = "gpt-6.1-sol"
# Operator-transcribed team errors (#227), verbatim as relayed.
TOOLS_ON_CHAT_COMPLETIONS = (
    "Function tools with reasoning_effort are not supported for gpt-5.6-sol in /v1/chat/completions. To use "
    "function tools, use /v1/responses or set reasoning effort to none"
)
NONE_UNSUPPORTED = (
    "Unsupported value reasoning_effort does not support none for this model. Supported values are low, medium, "
    "high, and xhigh."
)
# Synthetic envelopes around the transcribed messages.
E1 = {"message": TOOLS_ON_CHAT_COMPLETIONS, "type": "invalid_request_error", "param": None, "code": None}
E2 = {"message": NONE_UNSUPPORTED, "type": "invalid_request_error", "param": "reasoning.effort",
      "code": "unsupported_value"}

SEED_PROMPT = ("Remember the label amber-seven. For a read-only status check, use list_callbacks once, then tell "
               "me the label and summarize the returned callback list. Do not run tasks.")
TOOL_PROMPT = ("For a read-only status check, use list_callbacks once, then tell me the label from the previous "
               "turn and summarize the returned callback list. Do not run tasks.")
RECALL_PROMPT = "Repeat my label without using tools."
SEED_ANSWER = "Label amber-seven. One callback: WORKSTATION-5."
REFUSALS = ("Sage replaced this channel's session", "Sage restarted or this channel's session was replaced",
            "Start a new Sage chat")
_PROCESSES: list[Process] = []


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    for name in ("SAGE_REASONING_EFFORT", "SAGE_OPENAI_API", "SAGE_OPENAI_ACCESS_PROGRAM",
                 "SAGE_OPENAI_SAFETY_IDENTIFIER", "SAGE_PROVIDER_RETRY_POLICY"):
        monkeypatch.delenv(name, raising=False)
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


async def _restarted(first: Process) -> Process:
    await first.close()
    await chat_session.remove_session(str(CHANNEL))
    service._CHANNEL_TURN_LOCKS.clear()
    return _process()


class SolWire(ProviderWire):
    """Rejects the two reported combinations the way the team saw them (envelopes synthetic)."""

    def handle(self, item: httpx.Request) -> httpx.Response:
        body = json.loads(item.content)
        if (item.url.path.endswith("/chat/completions") and body.get("tools")
                and body.get("reasoning_effort") not in (None, "none")):
            self.requests.append((item.url.path, body))
            return httpx.Response(400, json={"error": E1})
        if (item.url.path.endswith("/responses") and body.get("model") == SOL_B
                and (body.get("reasoning") or {}).get("effort") == "none"):
            self.requests.append((item.url.path, body))
            return httpx.Response(400, json={"error": E2})
        return super().handle(item)


def sol(model: str, *, api: str = "responses", effort: str = "low") -> dict:
    extra = {"model": model, "SAGE_OPENAI_API": api, "SAGE_REASONING_EFFORT": effort}
    if api == "responses":
        extra["SAGE_OPENAI_ACCESS_PROGRAM"] = "standard"
    return route_config(**extra)


def _terminal(chat, since: int = 0, until: int | None = None) -> dict:
    found = [e for e in chat.emissions[since:until] if e in chat.terminal_emissions]
    assert len(found) == 1, chat.emissions[since:]
    return found[0]


def _no_refusal(chat) -> None:
    joined = "\n".join(texts(chat))
    assert not any(refusal in joined for refusal in REFUSALS), joined


def _pairs(body: dict) -> list[tuple[str, str]]:
    items = body["input"]
    return [(item["call_id"], items[i + 1].get("call_id")) for i, item in enumerate(items)
            if item.get("type") == "function_call" and items[i + 1].get("type") == "function_call_output"]


def _tool_round_trip(call_id: str, answer: str, index: int) -> list:
    return [[reasoning_item(index), call_item("list_callbacks", {}, call_id)], [text_item(answer, index + 1)]]


async def _steps(process: Process, steps: list[tuple[str, dict]]) -> list[tuple[dict, list[str], str]]:
    """Send each (prompt, config) in turn: (terminal, texts emitted, the record's thread) per step."""
    out = []
    for index, (prompt, config) in enumerate(steps, start=1):
        mark = len(process.chat.emissions)
        await process.chat.chat(request(prompt, index, config))
        record = await load_channel_session_record(OPERATION, CHANNEL)
        out.append((_terminal(process.chat, mark), texts(process.chat)[mark:],
                    str((record or {}).get("thread_id") or ""), dict(record or {})))
    return out


# --------------------------------------------------------------------------------------------------
# A: gpt-5.6-sol, tools + effort rejected on Chat Completions, corrected by an API-only change
# --------------------------------------------------------------------------------------------------

def test_tools_with_effort_on_chat_completions_is_explained_and_corrected_to_responses_in_the_same_chat(monkeypatch):
    wire = SolWire([
        *_tool_round_trip("call_a1", SEED_ANSWER, 1),
        *_tool_round_trip("call_a3", "Still amber-seven; WORKSTATION-5.", 3),
        [text_item("Your label is amber-seven.", 5)],
    ])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        return process, await _steps(process, [
            (SEED_PROMPT, sol(SOL_A)),
            (TOOL_PROMPT, sol(SOL_A, api="chat_completions")),
            (TOOL_PROMPT, sol(SOL_A)),
            (RECALL_PROMPT, sol(SOL_A)),
        ])

    process, ((a1, _, thread1, _r1), (a2, a2_texts, thread2, record2), (a3, a3_texts, thread3, _r3),
              (a4, _, thread4, _r4)) = asyncio.run(scenario())

    # A2 reached the provider on Chat Completions with tools and the explicit effort, and was refused once.
    chat_path, chat_body = wire.requests[2]
    assert chat_path == "/v1/chat/completions"
    assert chat_body["model"] == SOL_A and chat_body["reasoning_effort"] == "low" and chat_body["tools"]
    assert a2["kind"] == "error"
    notice = a2["error"]
    assert "HTTP 400" in notice and "Function tools with reasoning_effort are not supported" in notice
    assert notice.count("set **OpenAI API** to **Responses**") == 1
    assert "keeps reasoning and tools" in notice
    assert "Reasoning Effort: Low (ui-config)" in notice
    assert "keeps its conversation" in notice and "Reasoning Effort" in notice.split("To fix it")[1]
    joined_a2 = "\n".join(a2_texts)
    assert "Partial Work Preserved" not in joined_a2 and "different model" not in joined_a2
    assert record2["status"] == chat_session.CHANNEL_SESSION_ACTIVE

    # A3 and A4 continue the same generation on Responses with the same model and effort.
    _no_refusal(process.chat)
    assert thread1 == thread2 == thread3 == thread4
    assert a1["kind"] == a3["kind"] == a4["kind"] == "complete"
    responses = wire.responses()
    assert len(responses) == 5
    for body in responses:
        assert body["model"] == SOL_A and body["reasoning"]["effort"] == "low" and body["store"] is False
        assert body["access_programs"] == {"cyber": "standard"} and body["tools"]
    # A3's later request consumes its own call's result, after A1's history (label, call and result).
    assert _pairs(responses[3]) == [("call_a1", "call_a1"), ("call_a3", "call_a3")]
    assert any("amber-seven" in text for text in input_texts(responses[2]))
    assert any("amber-seven" in text for text in input_texts(responses[4]) if text != RECALL_PROMPT)
    assert process.list_callbacks_calls == 2
    assert "opaque-offline-reasoning" not in "\n".join(texts(process.chat))


# --------------------------------------------------------------------------------------------------
# B: gpt-6.1-sol rejects None; an effort-only change continues the same chat
# --------------------------------------------------------------------------------------------------

def test_unsupported_none_is_explained_and_an_effort_only_correction_continues_the_same_chat(monkeypatch):
    wire = SolWire([
        *_tool_round_trip("call_b1", SEED_ANSWER, 1),
        *_tool_round_trip("call_b3", "Still amber-seven; WORKSTATION-5.", 3),
        [text_item("Your label is amber-seven.", 5)],
    ])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        return process, await _steps(process, [
            (SEED_PROMPT, sol(SOL_B)),
            (TOOL_PROMPT, sol(SOL_B, effort="none")),
            (TOOL_PROMPT, sol(SOL_B)),
            (RECALL_PROMPT, sol(SOL_B)),
        ])

    process, ((b1, _, thread1, _r1), (b2, b2_texts, thread2, record2), (b3, b3_texts, thread3, _r3),
              (b4, _, thread4, _r4)) = asyncio.run(scenario())

    sent = [(body["model"], (body.get("reasoning") or {}).get("effort")) for _p, body in wire.requests]
    assert sent == [(SOL_B, "low"), (SOL_B, "low"), (SOL_B, "none"), (SOL_B, "low"), (SOL_B, "low"),
                    (SOL_B, "low")]
    assert b2["kind"] == "error"
    notice = b2["error"]
    assert "HTTP 400" in notice and "`reasoning.effort`" in notice and "does not support none" in notice
    assert "set **Reasoning Effort** to a supported value" in notice
    assert "Low, Medium, High or Extra high" in notice
    assert "Reasoning Effort: None (ui-config)" in notice
    assert "not the same as **None**" in notice
    assert "keeps its conversation" in notice
    assert "set **OpenAI API**" not in notice
    assert record2["status"] == chat_session.CHANNEL_SESSION_ACTIVE

    _no_refusal(process.chat)
    assert thread1 == thread2 == thread3 == thread4
    assert any("Reasoning Effort" in text for text in b2_texts + b3_texts if "Applied" in text)
    assert b1["kind"] == b3["kind"] == b4["kind"] == "complete"
    responses = [body for _p, body in wire.requests if (body.get("reasoning") or {}).get("effort") == "low"]
    assert _pairs(responses[3]) == [("call_b1", "call_b1"), ("call_b3", "call_b3")]
    assert any("amber-seven" in text for text in input_texts(responses[4]) if text != RECALL_PROMPT)
    assert len(process.models) == 3  # B2 and B3 each rebuilt the Model on the same generation


def test_an_effort_rejection_before_any_answer_names_reasoning_effort_not_the_model(monkeypatch):
    wire = SolWire([[text_item("Your label is amber-seven.", 2)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        return process, await _steps(process, [
            (SEED_PROMPT, sol(SOL_B, effort="none")),
            (RECALL_PROMPT, sol(SOL_B)),
        ])

    process, ((first, _, thread1, _r1), (second, _, thread2, _r2)) = asyncio.run(scenario())
    assert first["kind"] == "error" and "set **Reasoning Effort** to a supported value" in first["error"]
    assert "change **Model**" not in first["error"]
    _no_refusal(process.chat)
    assert second["kind"] == "complete" and thread1 == thread2


def test_an_effort_change_meeting_a_pending_approval_is_held_and_runs_nothing(monkeypatch):
    wire = SolWire([[text_item(SEED_ANSWER, 1)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        await process.chat.chat(request(RECALL_PROMPT, 1, sol(SOL_B)))
        # Synthetic state: an approval card is outstanding on this session.
        process.models[0]._hitl_card_pending = True
        mark = len(process.chat.emissions)
        await process.chat.chat(request(RECALL_PROMPT, 2, sol(SOL_B, effort="medium")))
        return process, texts(process.chat)[mark:]

    process, after = asyncio.run(scenario())
    assert not any(refusal in text for text in after for refusal in REFUSALS), after
    assert any("has not applied" in text for text in after), after
    assert len(wire.requests) == 1 and len(process.models) == 1


@pytest.mark.parametrize("changed", [
    {"mode": "supervised"},
    {"API_ENDPOINT": "https://other-provider.invalid/v1"},
])
def test_an_unrelated_change_beside_an_effort_change_still_starts_a_new_chat(monkeypatch, changed):
    wire = SolWire([[text_item(SEED_ANSWER, 1)], [text_item("Fresh chat.", 2)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        return await _steps(process, [
            (RECALL_PROMPT, sol(SOL_B)),
            (RECALL_PROMPT, {**sol(SOL_B, effort="high"), **changed}),
        ])

    (_, _, thread1, _), (_, _, thread2, _) = asyncio.run(scenario())
    assert thread1 != thread2


# --------------------------------------------------------------------------------------------------
# Saved channel records: effort changes across a restart, including records saved before this version
# --------------------------------------------------------------------------------------------------

def _signature(item, *, drop_model: bool, drop_effort: bool) -> str:
    """A core signature as an earlier version computed it (independent of the code under test)."""
    kwargs = service.build_model_kwargs(item)
    payload = dict(kwargs)
    if drop_model:
        payload.pop("model", None)
    dropped = {"openai_api", "openai_access_program", "openai_safety_identifier"}
    if drop_effort:
        dropped.add("reasoning_effort")
    config = dict(payload.get("config") or {})
    config["configurable"] = {k: v for k, v in dict(config.get("configurable") or {}).items() if k not in dropped}
    payload["config"] = config
    env = service._bloodhound_env_or_empty(item)
    if env:
        payload["__bloodhound__"] = dict(sorted(env.items()))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _restart_after(monkeypatch, second_config, *, record_format: str):
    """Serve one answer, rewrite the record as ``record_format`` saved it, restart and send ``second_config``."""
    wire = SolWire([[text_item(SEED_ANSWER, 1)], [text_item("Your label is amber-seven.", 2)]])
    install_wire(monkeypatch, wire)

    async def scenario():
        first = _process()
        first_request = request(SEED_PROMPT, 1, sol(SOL_B))
        await first.chat.chat(first_request)
        record = await load_channel_session_record(OPERATION, CHANNEL)
        if record_format == "model-continuable":  # model stored; effort inside the core signature
            saved = {k: v for k, v in record.items() if k != "reasoning_effort"}
            saved["core_signature"] = _signature(first_request, drop_model=True, drop_effort=False)
        elif record_format == "route-only":  # neither stored; model and effort inside the core signature
            saved = {k: v for k, v in record.items() if k not in {"model", "reasoning_effort"}}
            saved["core_signature"] = _signature(first_request, drop_model=False, drop_effort=False)
        else:
            saved = record
        await chat_session.save_channel_session_record(saved)
        second = await _restarted(first)
        await second.chat.chat(request(RECALL_PROMPT, 2, second_config))
        return second, wire, await load_channel_session_record(OPERATION, CHANNEL), record["thread_id"]

    return asyncio.run(scenario())


@pytest.mark.parametrize("record_format", ["current", "model-continuable", "route-only"])
def test_an_effort_change_across_a_restart_continues_the_same_generation(monkeypatch, record_format):
    second, wire, record, thread = _restart_after(monkeypatch, sol(SOL_B, effort="high"),
                                                  record_format=record_format)
    _no_refusal(second.chat)
    assert _terminal(second.chat)["kind"] == "complete"
    assert [(body.get("reasoning") or {}).get("effort") for _p, body in wire.requests] == ["low", "high"]
    assert record["thread_id"] == thread and record["reasoning_effort"] == "high"
    assert record["core_signature"] == second.models[0]._chat_request_core_signature
    assert any(SEED_PROMPT in text for text in input_texts(wire.requests[1][1]))


@pytest.mark.parametrize("record_format", ["model-continuable", "route-only"])
def test_an_unrelated_change_on_an_old_record_keeps_the_refusal(monkeypatch, record_format):
    second, wire, record, thread = _restart_after(
        monkeypatch, {**sol(SOL_B, effort="high"), "mode": "supervised"}, record_format=record_format)
    assert any("Start a new Sage chat" in text for text in texts(second.chat))
    assert len(wire.requests) == 1 and record["thread_id"] == thread


@pytest.mark.parametrize("record_format", ["model-continuable", "route-only"])
def test_an_unchanged_restart_upgrades_an_old_record_with_its_effort(monkeypatch, record_format):
    second, wire, record, thread = _restart_after(monkeypatch, sol(SOL_B), record_format=record_format)
    _no_refusal(second.chat)
    assert record["reasoning_effort"] == "low" and record["model"] == SOL_B
    assert record["core_signature"] == second.models[0]._chat_request_core_signature


# --------------------------------------------------------------------------------------------------
# Classification: accepted equivalents and rejected near-matches (envelopes synthetic)
# --------------------------------------------------------------------------------------------------

def _rejection(status: int, error: dict, path: str = "/responses") -> openai.APIStatusError:
    item = httpx.Request("POST", f"{ENDPOINT}{path}")
    response = httpx.Response(status, json={"error": error}, request=item)
    client = openai.OpenAI(api_key=KEY, base_url=ENDPOINT, max_retries=0,
                           http_client=httpx.Client(transport=httpx.MockTransport(lambda _r: response)))
    try:
        client.responses.create(model=SOL_A, input="x")
    except openai.APIStatusError as exc:
        return exc
    raise AssertionError("no rejection raised")


def _profile(**config):
    from sage_chat.headless import build_chat_request

    return resolve_llm_profile(build_chat_request("x", config=route_config(**config)))


_QUOTED_NONE = ("Unsupported value: 'reasoning_effort' does not support 'none' with this model. Supported values "
                "are: 'low', 'medium', 'high', 'xhigh', and 'max'.")


@pytest.mark.parametrize(("error", "action"), [
    (E1, "set **OpenAI API** to **Responses**"),
    ({**E1, "param": "tools"}, "set **OpenAI API** to **Responses**"),
    ({**E1, "param": "reasoning_effort", "code": "unsupported_parameter"}, "set **OpenAI API** to **Responses**"),
    ({**E1, "message": TOOLS_ON_CHAT_COMPLETIONS.replace(". To", ".\nTo")}, "set **OpenAI API** to **Responses**"),
    (E2, "set **Reasoning Effort** to a supported value"),
    ({**E2, "param": None, "code": None}, "set **Reasoning Effort** to a supported value"),
    ({**E2, "param": "reasoning_effort", "message": _QUOTED_NONE}, "set **Reasoning Effort** to a supported value"),
    ({"message": "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
      "type": "invalid_request_error", "param": "reasoning_effort", "code": "unsupported_parameter"},
     "set **Reasoning Effort** to a supported value"),
])
def test_equivalent_effort_rejections_are_configuration_errors_with_the_matching_action(error, action):
    exc = _rejection(400, error)
    assert provider_configuration_rejection(exc) is exc
    notice = provider_configuration_notice(exc, _profile(model=SOL_A, SAGE_OPENAI_API="chat_completions",
                                                         SAGE_REASONING_EFFORT="low"))
    assert action in notice and "keeps its conversation" in notice
    assert KEY not in notice


def test_a_quoted_supported_list_is_named_including_maximum():
    exc = _rejection(400, {**E2, "param": "reasoning_effort", "message": _QUOTED_NONE})
    notice = provider_configuration_notice(exc, _profile(model=SOL_B, SAGE_OPENAI_API="responses",
                                                         SAGE_REASONING_EFFORT="none"))
    assert "Low, Medium, High, Extra high or Maximum" in notice


def test_an_effort_rejection_without_a_supported_list_asks_for_a_value_this_model_supports():
    exc = _rejection(400, {"message": "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
                           "type": "invalid_request_error", "param": "reasoning_effort",
                           "code": "unsupported_parameter"})
    notice = provider_configuration_notice(exc, _profile(model="older-model", SAGE_REASONING_EFFORT="low"))
    assert "a value this model supports" in notice


@pytest.mark.parametrize(("status", "error"), [
    (400, {"type": "invalid_request_error", "code": None, "param": "input",
           "message": "Item 'rs_1' of type 'reasoning' was provided without its required following item."}),
    (400, {"type": "invalid_request_error", "code": None, "param": "input",
           "message": "No tool output found for function call call_1."}),
    (400, {"type": "invalid_request_error", "code": "invalid_function_parameters", "param": "tools[0].parameters",
           "message": "Invalid schema for function 'list_callbacks'."}),
    (400, {"type": "invalid_request_error", "code": "context_length_exceeded", "param": "messages",
           "message": "This model's maximum context length was exceeded; reduce reasoning_effort or input."}),
    (400, {"type": "invalid_request_error", "code": None, "param": None,
           "message": "The reasoning summary could not be generated for this request."}),
    (429, {"type": "rate_limit_error", "code": "rate_limit_exceeded", "param": None,
           "message": "Rate limit reached for reasoning_effort requests."}),
    (500, {"type": "server_error", "code": None, "param": None,
           "message": "Function tools with reasoning_effort failed upstream."}),
])
def test_history_schema_and_transient_errors_are_not_effort_configuration_errors(status, error):
    exc = _rejection(status, error)
    assert provider_configuration_rejection(exc) is None
    assert provider_configuration_notice(exc, None) == ""


@pytest.mark.parametrize(("config", "secrets", "shown"), [
    ({"SAGE_REASONING_EFFORT": "xhigh"}, None, "Reasoning Effort: Extra high (ui-config)"),
    ({}, {"SAGE_REASONING_EFFORT": "medium"}, "Reasoning Effort: Medium (user-secret)"),
    ({}, None, "Reasoning Effort: not sent (provider default)"),
])
def test_the_route_line_names_the_effective_effort_and_its_source(config, secrets, shown):
    from sage_chat.headless import build_chat_request

    profile = resolve_llm_profile(build_chat_request(
        "x", config=route_config(model=SOL_A, SAGE_OPENAI_API="chat_completions", **config), secrets=secrets))
    notice = provider_configuration_notice(_rejection(400, E1, "/chat/completions"), profile)
    assert shown in notice


def test_the_effort_environment_layer_is_named_as_its_source(monkeypatch):
    monkeypatch.setenv("SAGE_REASONING_EFFORT", "high")
    notice = provider_configuration_notice(_rejection(400, E2), _profile(model=SOL_B, SAGE_OPENAI_API="responses"))
    assert "Reasoning Effort: High (environment)" in notice


# --------------------------------------------------------------------------------------------------
# Preserved request shapes: blank omits effort, an explicit value (None included) is sent, tools are kept
# --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("api", ["responses", "chat_completions"])
@pytest.mark.parametrize("effort", ["", "none", "low"])
def test_blank_none_and_low_stay_distinct_with_tools_on_both_apis(monkeypatch, api, effort):
    wire = ProviderWire([*_tool_round_trip("call_p", SEED_ANSWER, 1)])
    install_wire(monkeypatch, wire)

    async def scenario():
        process = _process()
        config = sol(SOL_A, api=api, effort=effort)
        if not effort:
            config.pop("SAGE_REASONING_EFFORT")
        await process.chat.chat(request(TOOL_PROMPT, 1, config))
        return process

    process = asyncio.run(scenario())
    path, body = wire.requests[0]
    assert body["model"] == SOL_A and body["tools"]
    if api == "responses":
        assert path == "/v1/responses"
        assert (body.get("reasoning") or {}).get("effort") == (effort or None)
    else:
        assert path == "/v1/chat/completions"
        assert body.get("reasoning_effort") == (effort or None)
    _no_refusal(process.chat)


# --------------------------------------------------------------------------------------------------
# Native setting text: the resolution order, blank versus None, and the labels the guidance uses
# --------------------------------------------------------------------------------------------------

def _effort_option():
    from sage_chat import models as chat_models

    return {option.Name: option for option in chat_models._CONFIG_OPTIONS}["SAGE_REASONING_EFFORT"]


def test_the_native_effort_setting_states_its_resolution_and_that_none_is_not_blank():
    option = _effort_option()
    assert "user secret" in option.Description and "container environment" in option.Description
    assert "None included, is sent as chosen" in option.Description
    assert "OpenAI API = Responses" in option.Description
    assert "Select none to disable reasoning" not in option.Description
    assert {choice.Value for choice in option.Choices} == {
        "", "none", "minimal", "low", "medium", "high", "xhigh", "max"}


def test_the_guidance_uses_the_native_setting_labels():
    from ai.langgraph import operator_error

    labels = {choice.Value: choice.Label for choice in _effort_option().Choices if choice.Value}
    assert labels == operator_error._EFFORT_LABELS
