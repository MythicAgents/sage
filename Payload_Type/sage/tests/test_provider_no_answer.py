"""A provider Sage cannot reach, or that never answers, is reported as such, not as a rejection.

Drives the real HeadlessSageChat, Model, graph, checkpointer and channel record and the installed ChatOpenAI/OpenAI
SDK through ``tests.openai_responses_harness``; Mythic is a recorder. The unreachable cases raise the HTTP libraries'
own transport exceptions from a stand-in transport (synthetic: wiring). The silent-provider cases use a real local TCP
listener, the real SDK and its default client, with no transport stand-in. Every provider answer is synthetic.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import re
import socket
import threading
import time

import httpx
import httpx2
import langchain.chat_models
import pytest
from langchain_core.tools import tool
from openai._models import FinalRequestOptions

import sage_chat.service as service
from ai.langgraph import model as model_module
from ai.langgraph.model import Model, request_stop_for_sessions
from sage_chat import session as chat_session
from sage_chat.config import ResolvedLLMProfile, init_chat_model_from_profile
from sage_chat.session import load_channel_session_record
from tests import provider_wire_replay as replay
from tests.openai_responses_harness import CHANNEL, KEY, OPERATION, Process, request, texts
from tests.test_provider_wire_round_trips import ROWS

MODEL = "no-answer-check"
BLACKHOLE = "https://198.51.100.1/v1"
PROMPT = "Say hello in one word."
TOOL_PROMPT = "For a read-only status check, use list_callbacks once and summarize it. Do not run tasks."
CONTROL = CHANNEL + 1
REPLACED = "this channel's session was replaced"
ADVICE = ("rejected", "**Model**", "/model", "different model", KEY)
_PROCESSES: list[Process] = []


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)

    async def remove():
        for channel in (CHANNEL, CONTROL):
            await chat_session.remove_session(str(channel))

    asyncio.run(remove())
    yield

    async def close():
        await remove()
        for process in _PROCESSES:
            await process.close()

    asyncio.run(close())
    _PROCESSES.clear()
    service._CHANNEL_TURN_LOCKS.clear()


class _Process(Process):
    """A Sage process whose list_callbacks may stall (synthetic tool). ``before_stall`` may set Sage state (labelled)."""

    def __init__(self, stall: float = 0.0, before_stall=None):
        super().__init__()
        self.stall, self.before_stall = stall, before_stall
        _PROCESSES.append(self)

    def sink(self):
        sink, process = super().sink(), self

        @tool
        async def list_callbacks() -> str:
            """List the operation's callbacks (offline rows)."""
            if process.before_stall:
                process.before_stall(process.models[-1])
            await asyncio.sleep(process.stall)
            return "[]"

        sink.get_tools.side_effect = lambda names: [list_callbacks] if "list_callbacks" in names else []
        return sink


class _Listener:
    """A real local HTTP endpoint. Silent: accepts, reads the request and never replies. Otherwise it answers each
    request from ``answers`` as ``(status, body)``."""

    def __init__(self):
        self.silent, self.answers, self.requests, self._held = True, [], [], []
        self._server = socket.create_server(("127.0.0.1", 0))
        self.endpoint = f"http://127.0.0.1:{self._server.getsockname()[1]}/v1"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = connection.recv(65536)
            if not chunk:
                return
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        length = int((re.search(rb"content-length:\s*(\d+)", head, re.IGNORECASE) or [0, b"0"])[1])
        while len(body) < length:
            body += connection.recv(65536)
        self.requests.append(head.split(b"\r\n")[0].decode())
        if self.silent:
            self._held.append(connection)
            return
        status, answer = self.answers.pop(0)
        payload = json.dumps(answer).encode()
        connection.sendall(b"HTTP/1.1 %d X\r\ncontent-type: application/json\r\ncontent-length: %d\r\n"
                           b"connection: close\r\n\r\n%s" % (status, len(payload), payload))
        connection.close()

    def close(self):
        self._server.close()
        for connection in self._held:
            connection.close()


@pytest.fixture
def listener():
    endpoint = _Listener()
    yield endpoint
    endpoint.close()


def _completion(tool_call: bool = False) -> dict:
    message = ({"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_offline_1", "type": "function", "function": {"name": "list_callbacks", "arguments": "{}"}}]}
        if tool_call else {"role": "assistant", "content": "Hello."})
    return {"id": "chatcmpl-offline", "object": "chat.completion", "created": 0, "model": MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_call else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def _route(endpoint: str, **extra) -> dict:
    return {"provider": "openai_compatible", "model": MODEL, "API_ENDPOINT": endpoint, "API_KEY": KEY,
            "mode": "conversation", **extra}


def _install(monkeypatch, library, handler) -> None:
    """Replace only the network transport (synthetic) on the client Sage hands the SDK."""
    original = langchain.chat_models.init_chat_model
    monkeypatch.setattr(model_module, "init_chat_model", lambda **kwargs: original(
        **kwargs, http_client=library.Client(transport=library.MockTransport(handler)),
        http_async_client=library.AsyncClient(transport=library.MockTransport(handler))))


async def _turn(process, prompt, request_id, config, channel=CHANNEL):
    """``(terminal, the turn's texts, channel record)`` for one request."""
    mark = len(process.chat.emissions)
    await process.chat.chat(request(prompt, request_id, config, channel=channel))
    found = [e for e in process.chat.emissions[mark:] if e in process.chat.terminal_emissions]
    assert len(found) == 1, process.chat.emissions[mark:]
    return found[0], texts(process.chat)[mark:], await load_channel_session_record(OPERATION, channel)


def _no_advice(turn_texts) -> None:
    joined = "\n".join(turn_texts)
    for phrase in ADVICE:
        assert phrase not in joined, joined


def _timeouts(llm) -> list[dict]:
    options = FinalRequestOptions.construct(method="post", url="/chat/completions", json_data={})
    return [client._build_request(options).extensions["timeout"] for client in (llm.root_client, llm.root_async_client)]


# 1. Bound -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["openai", "litellm", "openai_compatible"])
@pytest.mark.parametrize("path", replay.PATHS)
def test_every_family_client_bounds_only_the_connect_phase(provider, path):
    sage = replay.model_instance({"id": f"bound-{provider}", "model": MODEL, "route": {"api": ""}}, path)
    sage.provider = provider
    clients = [sage._get_base_chat_model()]
    if path == "default":  # Watcher and the operation reasoner: the profile factory has no injected path
        clients.append(init_chat_model_from_profile(ResolvedLLMProfile(
            provider=provider, model=MODEL, api_key=KEY, api_endpoint=f"https://bound-{provider}.invalid/v1")))
    library = httpx2 if path == "default" else httpx
    for llm in clients:
        assert isinstance(llm.root_async_client._client, library.AsyncClient)
        assert _timeouts(llm) == [{"connect": 10.0, "read": None, "write": None, "pool": None}] * 2


# 2. Unreachable -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("library_name", "failure"), [("httpx2", "ConnectTimeout"), ("httpx", "ConnectError")])
def test_an_unreachable_endpoint_is_reported_as_unreachable_and_the_chat_continues(monkeypatch, library_name, failure):
    library = importlib.import_module(library_name)
    reachable: list[bool] = []

    def handler(outgoing):
        if not reachable:
            raise getattr(library, failure)("connection attempt failed", request=outgoing)
        return library.Response(200, json=_completion())

    _install(monkeypatch, library, handler)

    async def scenario():
        process = _Process()
        failed = await _turn(process, PROMPT, 1, _route(BLACKHOLE))
        reachable.append(True)
        return failed, await _turn(process, PROMPT, 2, _route(BLACKHOLE))

    (terminal, turn_texts, after), (answered, _, record) = asyncio.run(scenario())
    error = terminal["error"]
    assert terminal["kind"] == "error" and "could not be reached" in error and failure in error, error
    assert "openai_compatible" in error and f"`{MODEL}`" in error and BLACKHOLE in error
    assert "keeps its conversation; nothing was run" in error
    _no_advice(turn_texts)
    assert after["status"] == chat_session.CHANNEL_SESSION_ACTIVE
    assert answered["kind"] == "complete" and record["thread_id"] == after["thread_id"]


# 3. No answer, real socket ------------------------------------------------------------------------------------------

def _waited(error: str) -> int:
    found = re.search(r"gave no answer within (\d+) seconds", error)
    assert found, error
    return int(found[1])


def test_a_silent_provider_is_reported_as_giving_no_answer_and_the_chat_continues(monkeypatch, listener):
    monkeypatch.setattr(model_module, "SAGE_NODE_IDLE_TIMEOUT", 2)

    async def scenario():
        process = _Process()
        failed = await _turn(process, PROMPT, 1, _route(listener.endpoint))
        listener.silent, listener.answers = False, [(200, _completion())]
        return failed, await _turn(process, PROMPT, 2, _route(listener.endpoint))

    (terminal, turn_texts, after), (answered, _, record) = asyncio.run(scenario())
    error = terminal["error"]
    assert terminal["kind"] == "error" and _waited(error) >= 2
    assert listener.requests[0].startswith("POST /v1/chat/completions ")
    assert "openai_compatible" in error and f"`{MODEL}`" in error and listener.endpoint in error
    assert "keeps its conversation; nothing was run" in error
    _no_advice(turn_texts)
    assert after["status"] == chat_session.CHANNEL_SESSION_ACTIVE
    assert answered["kind"] == "complete" and record["thread_id"] == after["thread_id"]


def test_a_silent_provider_after_an_answer_gets_the_notice_and_a_proven_runtime_errors_session_handling(
        monkeypatch, listener):
    monkeypatch.setattr(model_module, "SAGE_NODE_IDLE_TIMEOUT", 2)
    refusal = {"error": {"message": "Malformed input.", "type": "invalid_request_error", "param": "messages",
                         "code": None}}

    async def scenario():
        process = _Process()
        listener.silent, listener.answers = False, [(200, _completion())] * 2 + [(400, refusal)]
        await _turn(process, PROMPT, 1, _route(listener.endpoint))
        await _turn(process, PROMPT, 1, _route(listener.endpoint), channel=CONTROL)
        control = await _turn(process, PROMPT, 2, _route(listener.endpoint), channel=CONTROL)
        listener.silent = True
        return control, await _turn(process, PROMPT, 2, _route(listener.endpoint))

    (control, _, control_record), (terminal, turn_texts, record) = asyncio.run(scenario())
    assert control["kind"] == terminal["kind"] == "error" and "400 - Malformed input" in control["error"]
    assert _waited(terminal["error"]) >= 2 and "keeps its conversation" not in terminal["error"]
    _no_advice(turn_texts)
    assert (record or {}).get("status") == (control_record or {}).get("status")


# 4. Near-matches keep today's text ----------------------------------------------------------------------------------

def _other_agent_pending(sage: Model) -> None:
    sage._provider_call_pending = ("BloodHound", time.monotonic())  # synthetic: a consultation's call inside this node


@pytest.mark.parametrize("before_stall", [None, _other_agent_pending], ids=["no-pending-call", "another-agents-call"])
def test_a_watchdog_without_this_nodes_pending_call_keeps_todays_text(monkeypatch, listener, before_stall):
    monkeypatch.setattr(model_module, "SAGE_NODE_IDLE_TIMEOUT", 2)
    listener.silent, listener.answers = False, [(200, _completion(tool_call=True))]
    terminal, _, _ = asyncio.run(_turn(_Process(stall=4, before_stall=before_stall), TOOL_PROMPT, 1,
                                       _route(listener.endpoint)))
    error = terminal["error"]
    assert "Node 'Supervisor' exceeded its idle timeout" in error and "no answer" not in error, error


def test_the_step_budget_halt_keeps_its_notice(listener):
    listener.silent, listener.answers = False, [(200, _completion(tool_call=True))]
    _, turn_texts, _ = asyncio.run(_turn(_Process(), TOOL_PROMPT, 1, _route(listener.endpoint, max_steps="1")))
    joined = "\n".join(turn_texts)
    assert "step limit" in joined and "no answer" not in joined and "could not be reached" not in joined, joined


@pytest.mark.parametrize("failure", [429, 503, "budget"])
def test_status_errors_and_a_budget_refusal_keep_todays_text(monkeypatch, failure):
    from ai.resource_budget import ResourceBudgetExceeded

    def handler(outgoing):
        if failure == "budget":
            raise ResourceBudgetExceeded("provider reservation refused")
        return httpx.Response(failure, json={"error": {"message": "Try later.", "type": "server_error"}},
                              headers={"retry-after-ms": "1"})

    _install(monkeypatch, httpx, handler)
    terminal, _, _ = asyncio.run(_turn(_Process(), PROMPT, 1, _route(BLACKHOLE)))
    error = terminal["error"]
    expected = ("resource budget stopped this request" if failure == "budget"
                else f"rejected this chat's request before the model answered (HTTP {failure})")
    assert expected in error and "no answer" not in error and "could not be reached" not in error, error


@pytest.mark.parametrize("row", [row for row in ROWS if row["id"].startswith(("r03_", "r08_"))],
                         ids=lambda row: row["id"])
@pytest.mark.parametrize("path", replay.PATHS)
def test_recorded_refusals_are_not_unanswered(row, path):
    from ai.langgraph.operator_error import provider_unanswered

    error = replay.outcome(replay.build_model(row, path)[0])
    assert isinstance(error, Exception) and provider_unanswered(error, ("Supervisor", time.monotonic())) is None


def test_a_stop_during_a_silent_call_does_not_label_the_next_turns_stall(monkeypatch, listener):
    monkeypatch.setattr(model_module, "SAGE_NODE_IDLE_TIMEOUT", 30)

    async def scenario():
        process = _Process(stall=4)
        turn = asyncio.create_task(process.chat.chat(request(PROMPT, 1, _route(listener.endpoint))))
        while not listener.requests:
            await asyncio.sleep(0.05)
        await request_stop_for_sessions(str(CHANNEL))  # what /stop calls
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        stopped_texts = texts(process.chat)
        monkeypatch.setattr(model_module, "SAGE_NODE_IDLE_TIMEOUT", 2)
        listener.silent, listener.answers = False, [(200, _completion(tool_call=True))]
        sent = len(listener.requests)
        # Today a stopped channel is retired and refuses its next request, so the next turn is in a new chat.
        replaced, _, _ = await _turn(process, PROMPT, 2, _route(listener.endpoint))
        assert len(listener.requests) == sent and REPLACED in str(replaced.get("content")), replaced
        return stopped_texts, await _turn(process, TOOL_PROMPT, 1, _route(listener.endpoint), channel=CONTROL)

    stopped_texts, (terminal, _, _) = asyncio.run(scenario())
    assert not any("no answer" in text for text in stopped_texts), stopped_texts
    assert "Node 'Supervisor' exceeded its idle timeout" in str(terminal.get("error")), (terminal, stopped_texts)
