"""A failed provider request logs its status, type, code and parameter as fields in the container log.

Real boundaries: Sage's two model factories (``Model._get_base_chat_model`` and ``init_chat_model_from_profile``),
the installed OpenAI and Anthropic SDKs and their LangChain adapters, and the mythic logger that feeds
``./mythic-cli logs sage``. Stand-ins, labelled: the network transport answers with a body. OpenAI bodies are the
recorded r03/r08 battery rows (``fixtures/model_input_boundaries/provider_wire_round_trips.json``). No Anthropic
error body was recorded, so the Anthropic bodies are synthetic, in Anthropic's documented error shape, and prove
wiring only; the line's live check reads real ones.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import anthropic
import httpx2
import openai
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from ai.langgraph.operator_error import provider_configuration_rejection
from sage_chat.config import ResolvedLLMProfile, init_chat_model_from_profile
from tests import provider_wire_replay as replay

FIXTURE = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/provider_wire_round_trips.json")
                     .read_text(encoding="utf-8"))
ROWS = {row["id"]: row for row in FIXTURE["rows"]}
RECORDED = [ROWS["r03_chat_completions_400_a_reasoning_effort"],
            ROWS["r08_responses_400_b_unsupported_value_reasoning_effort"]]
LINE = re.compile(r"Provider request failed: sdk=(openai|anthropic) status=(\d{3}) type=(\S+) code=(\S+) param=(\S+)")
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

# Synthetic, in the shape Anthropic documents for every error (https://platform.claude.com/docs/en/api/errors).
ANTHROPIC_BODIES = {
    400: {"type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens: Field required"},
          "request_id": "req_synthetic_400"},
    401: {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"},
          "request_id": "req_synthetic_401"},
    529: {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"},
          "request_id": "req_synthetic_529"},
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    # SDK retries (Anthropic 529, connection errors) run without their backoff sleep.
    monkeypatch.setattr(openai._base_client.BaseClient, "_calculate_retry_timeout", lambda *a, **k: 0.0)
    monkeypatch.setattr(anthropic._base_client.BaseClient, "_calculate_retry_timeout", lambda *a, **k: 0.0)


def _oracle(row: dict) -> str:
    """The expected line, from the recorded body itself (not from Sage's reader)."""
    error = row["body"]["error"]
    return (f"Provider request failed: sdk=openai status={row['status']} type={error['type'] or '-'} "
            f"code={error['code'] or '-'} param={error['param'] or '-'}")


def _answer(library, status: int, payload: bytes, requests: list[str] | None = None):
    def answer(request):
        if requests is not None:
            requests.append(request.url.path)
        return library.Response(status, content=payload, headers={"content-type": "application/json"})
    return answer


def _openai_profile_model(row: dict, *, body: bytes | None = None, label: str = "profile"):
    route = row["route"]
    profile = ResolvedLLMProfile(provider="openai", model=row["model"], api_key=replay.API_KEY,
                                 api_endpoint=replay.base_url(row["id"], label), openai_api=route["api"],
                                 reasoning_effort=route.get("reasoning_effort") or "")
    llm = init_chat_model_from_profile(profile)
    _swap_openai(llm, row["status"], body if body is not None else json.dumps(row["body"]).encode())
    return llm


def _swap_openai(llm, status: int, payload: bytes) -> None:
    for client in (llm.root_client._client, llm.root_async_client._client):
        library = replay._library_for(client)
        client._transport = library.MockTransport(_answer(library, status, payload))


def _swap_anthropic(llm, status: int, payload: bytes, requests: list[str]) -> None:
    for client in (llm._client._client, llm._async_client._client):
        assert isinstance(client, httpx2.Client | httpx2.AsyncClient), type(client)
        client._transport = httpx2.MockTransport(_answer(httpx2, status, payload, requests))


def _anthropic_models(status: int, requests: list[str]):
    payload = json.dumps(ANTHROPIC_BODIES[status]).encode()
    endpoint = f"https://anthropic-synthetic-{status}.invalid"
    sage = replay.model_instance({"id": f"anthropic-{status}", "model": "claude-synthetic", "route": {"api": ""}},
                                 "default", provider="anthropic")
    sage.config = {"configurable": {"api_key": replay.API_KEY, "base_url": endpoint}}
    graph_llm = sage._get_base_chat_model()
    profile_llm = init_chat_model_from_profile(ResolvedLLMProfile(
        provider="anthropic", model="claude-synthetic", api_key=replay.API_KEY, api_endpoint=endpoint))
    for llm in (graph_llm, profile_llm):
        _swap_anthropic(llm, status, payload, requests)
    return {"graph": graph_llm, "profile": profile_llm}


def _fails(call):
    with replay.captured_log() as messages:
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - the failure is the input under test
            return exc, replay.fields_lines(messages), messages
    raise AssertionError("the provider request did not fail")


# 1. Recorded OpenAI rejections, both factories ----------------------------------------------------------------------

@pytest.mark.parametrize("path", replay.PATHS)
@pytest.mark.parametrize("row", RECORDED, ids=[row["id"][:3] for row in RECORDED])
def test_the_graph_factory_logs_one_fields_line_for_a_recorded_rejection(row, path):
    llm, _client, requests = replay.build_model(row, path)

    error, lines, _messages = _fails(lambda: llm.invoke(replay.REPLAY_PROMPT))

    assert isinstance(error, openai.APIStatusError)
    assert len(requests) == 1
    assert lines == [_oracle(row)]


@pytest.mark.parametrize("row", RECORDED, ids=[row["id"][:3] for row in RECORDED])
def test_the_profile_factory_logs_one_fields_line_for_a_recorded_rejection(row):
    """``init_chat_model_from_profile`` builds the Watcher's and the operation reasoner's model."""
    llm = _openai_profile_model(row)

    _error, lines, _messages = _fails(lambda: llm.invoke(replay.REPLAY_PROMPT))

    assert lines == [_oracle(row)]


@tool
def _noop(value: str) -> str:
    """Return the value."""
    return value


async def _drain(runnable):
    async for _chunk in runnable.astream(replay.REPLAY_PROMPT):
        pass


@pytest.mark.parametrize("call", ["ainvoke", "bind_tools.invoke", "bind_tools.ainvoke", "astream"])
def test_each_call_form_logs_exactly_one_line(call):
    row = RECORDED[1]
    llm, _client, _requests = replay.build_model(row, "default")
    run = {
        "ainvoke": lambda: asyncio.run(llm.ainvoke(replay.REPLAY_PROMPT)),
        "bind_tools.invoke": lambda: llm.bind_tools([_noop]).invoke(replay.REPLAY_PROMPT),
        "bind_tools.ainvoke": lambda: asyncio.run(llm.bind_tools([_noop]).ainvoke(replay.REPLAY_PROMPT)),
        "astream": lambda: asyncio.run(_drain(llm)),
    }[call]

    _error, lines, _messages = _fails(run)

    assert lines == [_oracle(row)]


def test_the_watcher_logs_the_fields_line(monkeypatch):
    from sage_chat import watcher_graph

    row = RECORDED[0]
    real = watcher_graph.init_chat_model_from_profile

    def build(profile):
        llm = real(profile)
        _swap_openai(llm, row["status"], json.dumps(row["body"]).encode())
        return llm

    monkeypatch.setattr(watcher_graph, "init_chat_model_from_profile", build)
    profile = ResolvedLLMProfile(provider="openai", model=row["model"], api_key=replay.API_KEY,
                                 api_endpoint=replay.base_url(row["id"], "watcher"), openai_api=row["route"]["api"],
                                 reasoning_effort=row["route"]["reasoning_effort"])
    graph = watcher_graph.build_watcher_graph(profile)

    _error, lines, _messages = _fails(lambda: asyncio.run(graph.ainvoke({"request": "Explain.", "findings": []})))

    assert lines == [_oracle(row)]


def test_the_operation_reasoner_logs_the_fields_line(monkeypatch):
    from sage_chat import operation_reasoner

    row = RECORDED[1]
    real = operation_reasoner.init_chat_model_from_profile

    def build(profile):
        llm = real(profile)
        _swap_openai(llm, row["status"], json.dumps(row["body"]).encode())
        return llm

    monkeypatch.setattr(operation_reasoner, "init_chat_model_from_profile", build)
    profile = ResolvedLLMProfile(provider="openai", model=row["model"], api_key=replay.API_KEY,
                                 api_endpoint=replay.base_url(row["id"], "reasoner"), openai_api=row["route"]["api"],
                                 reasoning_effort=row["route"]["reasoning_effort"])
    reasoner = operation_reasoner.OperationFindingReasoner(model_profile=profile)

    _error, lines, _messages = _fails(lambda: asyncio.run(reasoner._invoke((HumanMessage("Reason."),))))

    assert lines == [_oracle(row)]


def test_the_isolated_credential_reviewer_logs_the_fields_line(monkeypatch):
    from ai.langgraph.credential_review_llm import CredentialReviewer

    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "SAGE_OBSERVATION_DIRECTORY",
                 "OPENAI_LOG"):
        monkeypatch.delenv(name, raising=False)
    row = RECORDED[1]
    llm, _client, _requests = replay.build_model(row, "default")
    reviewer = CredentialReviewer(llm, model_id=f"openai:{row['model']}")
    excerpt = "excerpt-sentinel-do-not-log"

    _error, lines, _messages = _fails(lambda: asyncio.run(reviewer.locate([("user", excerpt)])))

    assert lines == [_oracle(row)]
    assert excerpt not in lines[0]


# 2. Wording independence --------------------------------------------------------------------------------------------

def test_the_fields_do_not_depend_on_how_the_sdk_words_its_error(monkeypatch):
    reworded = "The SDK reworded this error entirely"
    for cls in (openai.OpenAI, openai.AsyncOpenAI):
        original = cls._make_status_error
        monkeypatch.setattr(cls, "_make_status_error",
                            lambda self, err_msg, *, body, response, _o=original: _o(self, reworded, body=body,
                                                                                     response=response))
    for row in RECORDED:
        llm, _client, _requests = replay.build_model(row, "default")
        error, lines, _messages = _fails(lambda: llm.invoke(replay.REPLAY_PROMPT))
        assert str(error) == reworded
        assert lines == [_oracle(row)]


def test_anthropic_fields_do_not_depend_on_how_the_sdk_words_its_error(monkeypatch):
    reworded = "Anthropic SDK reworded this error"
    for cls in (anthropic.Anthropic, anthropic.AsyncAnthropic):
        original = cls._make_status_error
        monkeypatch.setattr(cls, "_make_status_error",
                            lambda self, err_msg, *, body, response, _o=original: _o(self, reworded, body=body,
                                                                                     response=response))
    requests: list[str] = []
    llm = _anthropic_models(400, requests)["graph"]

    error, lines, _messages = _fails(lambda: llm.invoke(replay.REPLAY_PROMPT))

    assert "invalid_request_error" not in str(error)
    assert lines == ["Provider request failed: sdk=anthropic status=400 type=invalid_request_error code=- param=-"]


# 3. Anthropic (synthetic, documented shape), both factories ---------------------------------------------------------

@pytest.mark.parametrize("factory", ["graph", "profile"])
@pytest.mark.parametrize("status", sorted(ANTHROPIC_BODIES))
def test_an_anthropic_error_logs_one_fields_line(status, factory):
    requests: list[str] = []
    llm = _anthropic_models(status, requests)[factory]

    error, lines, _messages = _fails(lambda: llm.invoke(replay.REPLAY_PROMPT))

    assert isinstance(error, anthropic.APIStatusError)
    assert requests and set(requests) == {"/v1/messages"}  # a 529 is retried by the SDK: still one line
    error_type = ANTHROPIC_BODIES[status]["error"]["type"]
    assert lines == [f"Provider request failed: sdk=anthropic status={status} type={error_type} code=- param=-"]


# 4. Must survive ----------------------------------------------------------------------------------------------------

def test_an_anthropic_authentication_error_is_still_not_a_configuration_rejection():
    llm = _anthropic_models(401, [])["graph"]

    error, lines, _messages = _fails(lambda: llm.invoke(replay.REPLAY_PROMPT))

    assert isinstance(error, anthropic.AuthenticationError)
    assert provider_configuration_rejection(error) is None
    assert len(lines) == 1


@pytest.mark.parametrize("row", RECORDED, ids=[row["id"][:3] for row in RECORDED])
def test_the_line_carries_no_key_prompt_or_provider_message(row):
    llm, _client, _requests = replay.build_model(row, "default")

    _error, lines, _messages = _fails(lambda: llm.invoke(replay.REPLAY_PROMPT))

    message_words = row["body"]["error"]["message"].split(":")[0]
    assert len(lines) == 1
    for sentinel in (replay.API_KEY, replay.REPLAY_PROMPT, message_words, row["model"]):
        assert sentinel not in lines[0]


def test_an_untrusted_field_cannot_split_or_forge_the_line():
    row = RECORDED[1]
    body = json.loads(json.dumps(row["body"]))
    body["error"]["param"] = "reasoning.effort\r\nERROR forged: sdk=openai status=200 code=ok"
    body["error"]["code"] = "x" * 500
    body["error"]["type"] = "invalid request error"
    llm = _openai_profile_model(row, body=json.dumps(body).encode(), label="forge")

    _error, lines, _messages = _fails(lambda: llm.invoke(replay.REPLAY_PROMPT))

    assert len(lines) == 1
    line = lines[0]
    assert not any(ch in line for ch in "\r\n  \x85")
    found = LINE.fullmatch(line)
    assert found, line
    assert line.count("code=") == 1 and line.count("status=") == 1 and line.count("sdk=") == 1
    assert found.group(2) == "400"
    assert len(found.group(4)) <= 64


def test_a_failure_without_a_provider_status_logs_no_fields_line():
    row = RECORDED[1]
    llm, _client, _requests = replay.build_model(row, "default")

    def refuse(request):
        raise httpx2.ConnectError("connection refused", request=request)

    for client in (llm.root_client._client, llm.root_async_client._client):
        client._transport = httpx2.MockTransport(refuse)

    error, lines, _messages = _fails(lambda: llm.invoke(replay.REPLAY_PROMPT))

    assert isinstance(error, openai.APIConnectionError)
    assert lines == []


def test_control_flow_and_cancellation_log_no_fields_line():
    from langgraph.errors import ParentCommand
    from langgraph.types import Command

    llm, _client, _requests = replay.build_model(RECORDED[0], "default")
    handlers = [handler for handler in (llm.callbacks or []) if hasattr(handler, "on_llm_error")]
    assert handlers, "the factory attached no error handler"
    with replay.captured_log() as messages:
        for handler in handlers:
            for error in (asyncio.CancelledError(), ParentCommand(Command(update={"messages": []})),
                          ValueError("plain failure")):
                handler.on_llm_error(error)
    assert replay.fields_lines(messages) == []


# 5. A whole conversation turn (the live check's path, offline) -----------------------------------------------------

# Synthetic, in OpenAI's documented shape for an invalid key (https://developers.openai.com/api/docs/guides/error-codes).
INVALID_KEY_401 = {"error": {"message": "Incorrect API key provided: offline-*****-key.",
                             "type": "invalid_request_error", "param": None, "code": "invalid_api_key"}}
TURN_CHANNEL = 238


def test_a_conversation_turn_refused_for_its_key_logs_one_fields_line_beside_the_graph_error(monkeypatch):
    """Real ``HeadlessSageChat``, ``Model``, graph and factory; only the provider transport answers 401."""
    import httpx

    from sage_chat import session as chat_session
    from tests.openai_responses_harness import Process, ProviderWire, install_wire, request, route_config

    class _Refusing(ProviderWire):
        def handle(self, outgoing):
            self.requests.append((outgoing.url.path, json.loads(outgoing.content)))
            return httpx.Response(401, json=INVALID_KEY_401)

    wire = _Refusing()
    install_wire(monkeypatch, wire)
    process = Process()

    async def scenario():
        await chat_session.remove_session(str(TURN_CHANNEL))
        try:
            await process.chat.chat(request("Reply with OK.", 1, route_config(SAGE_OPENAI_API="responses"),
                                            channel=TURN_CHANNEL))
        finally:
            await chat_session.remove_session(str(TURN_CHANNEL))
            await process.close()

    with replay.captured_log() as messages:
        asyncio.run(scenario())

    terminals = process.chat.terminal_emissions
    assert len(terminals) == 1 and terminals[0]["kind"] == "error", terminals
    assert [path for path, _body in wire.requests] == ["/v1/responses"]
    assert replay.fields_lines(messages) == [
        "Provider request failed: sdk=openai status=401 type=invalid_request_error code=invalid_api_key param=-"]
    assert any(message.startswith("Error during graph execution:") for message in messages)
    assert "Reply with OK." not in replay.fields_lines(messages)[0]
