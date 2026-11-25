"""Which API an OpenAI-family chat sends, from native configuration to the provider request.

The Provider setting offers OpenAI, LiteLLM Gateway (``litellm``) and Custom (OpenAI-compatible)
(``openai_compatible``). All three build ``ChatOpenAI``. With OpenAI API blank at every layer, the API is
Responses for ``openai`` on OpenAI's own endpoint and Chat Completions everywhere else; an explicit OpenAI API
always wins. ``litellm`` and ``openai_compatible`` need an API Endpoint before any request.

Every row drives a real ``ChatRequest`` through the real resolver (``build_model_kwargs`` /
``resolve_llm_profile`` / ``resolve_watcher_llm_profile``), both model factories (``Model._get_base_chat_model``
and ``init_chat_model_from_profile``), the installed ``ChatOpenAI`` and the OpenAI SDK. Only the network transport
is replaced, on the client the adapter builds itself (``default``) and on Sage's injected retry client
(``injected``); assertions read the URL and JSON body the provider would receive. Provider replies are synthetic
and prove wiring only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models import _client_utils

from ai.langgraph.model import Model
from sage_chat.config import (
    ResolvedLLMProfile,
    build_model_kwargs,
    init_chat_model_from_profile,
    resolve_llm_profile,
    resolve_watcher_llm_profile,
)
from sage_chat.headless import build_chat_request
from tests import provider_wire_replay as replay

KEY = "offline-placeholder-key"
PROXY = "https://provider.invalid/v1"
GATEWAY = "https://gateway.invalid:7443"
OPENAI_HOST = "api.openai.com"
RESPONSES_FIELDS = ("store", "include", "access_programs")

ENV = (
    "SAGE_OPENAI_API", "SAGE_OPENAI_ACCESS_PROGRAM", "SAGE_OPENAI_SAFETY_IDENTIFIER", "SAGE_REASONING_EFFORT",
    "SAGE_PROVIDER_RETRY_POLICY", "provider", "model", "API_ENDPOINT", "API_KEY",
    "SAGE_WATCHER_PROVIDER", "SAGE_WATCHER_MODEL", "SAGE_WATCHER_API_ENDPOINT", "SAGE_WATCHER_API_KEY",
    "OPENAI_API_BASE", "OPENAI_BASE_URL", "LANGSMITH_GATEWAY", "OPENAI_API_KEY",
    "SAGE_EVAL_MAX_OUTPUT_TOKENS", "SAGE_EVAL_TEMPERATURE",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    # The adapter caches its own client per endpoint; a swapped transport must not outlive its test.
    _client_utils._cached_sync_httpx_client.cache_clear()
    _client_utils._cached_async_httpx_client.cache_clear()
    yield
    _client_utils._cached_sync_httpx_client.cache_clear()
    _client_utils._cached_async_httpx_client.cache_clear()


class Capture:
    """Records each request's URL and body; answers like the provider would (synthetic)."""

    def __init__(self) -> None:
        self.requests: list[tuple[httpx.URL, dict]] = []

    def handler(self, library):
        def answer(request):
            body = json.loads(request.content)
            self.requests.append((httpx.URL(str(request.url)), body))
            if request.url.path.endswith("/responses"):
                reply = {
                    "id": "resp_offline", "object": "response", "created_at": 0, "model": body["model"],
                    "status": "completed", "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
                    "output": [{"id": "msg_offline", "type": "message", "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": "READY", "annotations": []}]}],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}
            else:
                reply = {
                    "id": "chatcmpl-offline", "object": "chat.completion", "created": 0, "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "READY"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            return library.Response(200, content=json.dumps(reply).encode(),
                                    headers={"content-type": "application/json"})
        return answer

    def attach(self, llm, path: str) -> None:
        client = llm.root_client._client
        if path == "injected":
            client._transport.transport = httpx.MockTransport(self.handler(httpx))
        else:
            library = replay._library_for(client)
            client._transport = library.MockTransport(self.handler(library))


def _request(config: dict, secrets: dict | None = None):
    return build_chat_request("Offline request", config=config, secrets=secrets)


def _factory_a(config: dict, secrets: dict | None, path: str):
    if path == "injected":
        config = {**config, "SAGE_PROVIDER_RETRY_POLICY": "transient-v1"}
    kwargs = build_model_kwargs(_request(config, secrets))
    sage = Model.__new__(Model)
    sage.provider, sage.model, sage.config = kwargs["provider"], kwargs["model"], kwargs["config"]
    sage._eval_arm_deadline_epoch = None
    sage._provider_retry_state = None
    sage._controller_runtime_telemetry = {}
    return sage._get_base_chat_model()


def _factory_b(config: dict, secrets: dict | None, _path: str):
    return init_chat_model_from_profile(resolve_llm_profile(_request(config, secrets)))


def _watcher(config: dict, secrets: dict | None, _path: str):
    watcher = {f"SAGE_WATCHER_{key.upper()}" if key in ("provider", "model") else f"SAGE_WATCHER_{key}": value
               for key, value in config.items() if key in ("provider", "model", "API_ENDPOINT", "API_KEY")}
    return init_chat_model_from_profile(resolve_watcher_llm_profile(_request(watcher, secrets)))


BUILDS = [
    pytest.param(_factory_a, "default", id="model-default"),
    pytest.param(_factory_a, "injected", id="model-injected"),
    pytest.param(_factory_b, "default", id="profile-default"),
]


@dataclass(frozen=True)
class Row:
    id: str
    provider: str
    endpoint: str | None = None          # API_ENDPOINT in the chat configuration
    api: str = ""                        # OpenAI API in the chat configuration
    env: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    extra: dict[str, str] = field(default_factory=dict)
    expect: str = "chat_completions"     # or "responses"
    host: str = "provider.invalid"
    url_path: str = "/v1"

    def config(self) -> dict[str, Any]:
        config = {"provider": self.provider, "model": "test-model", "API_KEY": KEY, **self.extra}
        if self.endpoint is not None:
            config["API_ENDPOINT"] = self.endpoint
        if self.api:
            config["SAGE_OPENAI_API"] = self.api
        return config


ROWS = [
    # OpenAI's own endpoint, OpenAI API blank: Responses.
    Row("openai-default-endpoint", "openai", expect="responses", host=OPENAI_HOST),
    Row("openai-explicit-endpoint", "openai", "https://api.openai.com/v1", expect="responses", host=OPENAI_HOST),
    Row("openai-trailing-slash", "openai", "https://api.openai.com/v1/", expect="responses", host=OPENAI_HOST),
    Row("openai-data-residency", "openai", "https://eu.api.openai.com/v1", expect="responses",
        host="eu.api.openai.com"),
    Row("openai-private-subdomain", "openai", "https://example-link.us.api.openai.com/v1", expect="responses",
        host="example-link.us.api.openai.com"),
    Row("openai-provider-case", "OpenAI", expect="responses", host=OPENAI_HOST),
    Row("openai-host-case", "openai", "https://API.OpenAI.com/v1", expect="responses", host=OPENAI_HOST),
    Row("openai-sdk-env-residency", "openai", env={"OPENAI_BASE_URL": "https://eu.api.openai.com/v1"},
        expect="responses", host="eu.api.openai.com"),
    Row("openai-endpoint-user-secret", "openai", secrets={"API_ENDPOINT": "https://api.openai.com/v1"},
        expect="responses", host=OPENAI_HOST),
    # Migration rule and near-matches: anything else stays Chat Completions.
    Row("openai-proxy-migration", "openai", PROXY),
    Row("openai-proxy-from-env", "openai", env={"API_ENDPOINT": PROXY}),
    Row("openai-proxy-user-secret", "openai", secrets={"API_ENDPOINT": PROXY}),
    Row("openai-http-scheme", "openai", "http://api.openai.com/v1", host=OPENAI_HOST),
    Row("openai-suffix-host", "openai", "https://api.openai.com.example.net/v1", host="api.openai.com.example.net"),
    Row("openai-prefix-host", "openai", "https://xapi.openai.com/v1", host="xapi.openai.com"),
    Row("openai-adapter-env", "openai", env={"OPENAI_API_BASE": PROXY}),
    Row("openai-sdk-env", "openai", env={"OPENAI_BASE_URL": PROXY}),
    Row("openai-langsmith-gateway", "openai", env={"LANGSMITH_GATEWAY": "true"},
        host="gateway.smith.langchain.com", url_path="/openai/v1"),
    Row("openai-api-endpoint-beats-env", "openai", "https://api.openai.com/v1", env={"OPENAI_BASE_URL": PROXY},
        expect="responses", host=OPENAI_HOST),
    # Gateways and custom servers: Chat Completions by default, Responses when selected.
    Row("litellm-default", "litellm", GATEWAY, host="gateway.invalid", url_path=""),
    Row("litellm-provider-case", "LiteLLM", GATEWAY, host="gateway.invalid", url_path=""),
    Row("compatible-default", "openai_compatible", PROXY),
    Row("litellm-on-openai-host", "litellm", "https://api.openai.com/v1", host=OPENAI_HOST),
    Row("litellm-responses", "litellm", GATEWAY, api="responses", expect="responses", host="gateway.invalid",
        url_path=""),
    Row("compatible-responses", "openai_compatible", PROXY, api="responses", expect="responses"),
    # An explicit API always wins.
    Row("openai-direct-chat-completions", "openai", api="chat_completions", host=OPENAI_HOST),
    Row("openai-proxy-responses", "openai", PROXY, api="responses", expect="responses"),
    Row("openai-api-from-env", "openai", env={"SAGE_OPENAI_API": "chat_completions"}, host=OPENAI_HOST),
    Row("litellm-chat-completions", "litellm", GATEWAY, api="chat_completions", host="gateway.invalid",
        url_path=""),
    # Daybreak on a default Responses route is now valid; it is still named on every Responses request.
    Row("openai-default-daybreak", "openai", extra={"SAGE_OPENAI_ACCESS_PROGRAM": "daybreak_red"},
        expect="responses", host=OPENAI_HOST),
]


def _assert_request(row: Row, llm, capture: Capture) -> None:
    assert isinstance(llm, ChatOpenAI)
    ((url, body),) = capture.requests
    assert url.scheme == ("http" if row.endpoint and row.endpoint.startswith("http:") else "https")
    assert url.host == row.host
    if row.expect == "responses":
        assert url.path == f"{row.url_path}/responses"
        assert body["store"] is False
        assert body["include"] == ["reasoning.encrypted_content"]
        program = row.extra.get("SAGE_OPENAI_ACCESS_PROGRAM", "standard")
        assert body["access_programs"] == {"cyber": program}
    else:
        assert url.path == f"{row.url_path}/chat/completions"
        assert not [name for name in RESPONSES_FIELDS if name in body]


@pytest.mark.parametrize(("build", "path"), BUILDS)
@pytest.mark.parametrize("row", ROWS, ids=lambda row: row.id)
def test_route_table(monkeypatch, row, build, path):
    for name, value in row.env.items():
        monkeypatch.setenv(name, value)
    capture = Capture()
    llm = build(row.config(), row.secrets or None, path)
    capture.attach(llm, path)

    llm.invoke("Offline request")

    _assert_request(row, llm, capture)


WATCHER_ROWS = [row for row in ROWS if row.id in {
    "openai-default-endpoint", "openai-proxy-migration", "litellm-default", "compatible-default",
    "openai-data-residency", "openai-sdk-env"}]


@pytest.mark.parametrize("row", WATCHER_ROWS, ids=lambda row: row.id)
def test_watcher_route_table(monkeypatch, row):
    for name, value in row.env.items():
        monkeypatch.setenv(name, value)
    capture = Capture()
    llm = _watcher(row.config(), None, "default")
    capture.attach(llm, "default")

    llm.invoke("Offline request")

    _assert_request(row, llm, capture)


# --------------------------------------------------------------------------------------------------
# Refused before any request: a gateway or custom server without its endpoint
# --------------------------------------------------------------------------------------------------

REFUSALS = [
    pytest.param({"provider": "litellm"}, {}, id="litellm-no-endpoint"),
    pytest.param({"provider": "openai_compatible"}, {}, id="compatible-no-endpoint"),
    pytest.param({"provider": "litellm"}, {"OPENAI_BASE_URL": PROXY}, id="litellm-sdk-env-only"),
    pytest.param({"provider": "openai_compatible"}, {"OPENAI_API_BASE": PROXY}, id="compatible-adapter-env-only"),
    pytest.param({"provider": "litellm", "API_ENDPOINT": "   "}, {}, id="litellm-blank-endpoint"),
]


@pytest.mark.parametrize(("build", "path"), BUILDS)
@pytest.mark.parametrize(("config", "env"), REFUSALS)
def test_gateway_without_endpoint_is_refused_before_any_request(monkeypatch, config, env, build, path):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="API Endpoint") as raised:
        build({"model": "test-model", "API_KEY": KEY, **config}, None, path)
    assert KEY not in str(raised.value)


@pytest.mark.parametrize(("config", "env"), REFUSALS)
def test_watcher_gateway_without_endpoint_is_refused(monkeypatch, config, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="API Endpoint"):
        _watcher({"model": "test-model", "API_KEY": KEY, **config}, None, "default")


@pytest.mark.parametrize("provider", ["litellm", "openai_compatible"])
def test_each_factory_refuses_a_projection_without_an_endpoint(provider):
    sage = Model.__new__(Model)
    sage.provider, sage.model = provider, "test-model"
    sage.config = {"configurable": {"api_key": KEY}}
    with pytest.raises(ValueError, match="API Endpoint"):
        sage._get_base_chat_model()
    with pytest.raises(ValueError, match="API Endpoint"):
        init_chat_model_from_profile(ResolvedLLMProfile(provider=provider, model="test-model", api_key=KEY))


@pytest.mark.parametrize("config", [
    pytest.param({"provider": "openai", "API_ENDPOINT": PROXY, "SAGE_OPENAI_ACCESS_PROGRAM": "daybreak_red"},
                 id="openai-proxy"),
    pytest.param({"provider": "litellm", "API_ENDPOINT": GATEWAY, "SAGE_OPENAI_ACCESS_PROGRAM": "standard"},
                 id="litellm"),
    pytest.param({"provider": "openai", "SAGE_OPENAI_API": "chat_completions",
                  "SAGE_OPENAI_ACCESS_PROGRAM": "daybreak_blue"}, id="openai-direct-chat-completions"),
])
def test_daybreak_still_requires_a_responses_route(config):
    with pytest.raises(ValueError, match="requires OpenAI API = Responses"):
        _factory_a({"model": "test-model", "API_KEY": KEY, **config}, None, "default")


# --------------------------------------------------------------------------------------------------
# Family parity: LiteLLM Gateway and Custom get everything the OpenAI provider gets on the same endpoint
# --------------------------------------------------------------------------------------------------

FAMILY = ("openai", "litellm", "openai_compatible")


def _transport_chain(client) -> list[str]:
    chain, transport = [], getattr(client, "_transport", None)
    while transport is not None and len(chain) < 4:
        chain.append(type(transport).__name__)
        transport = getattr(transport, "transport", None)
    return chain


def _built(llm) -> dict[str, Any]:
    """What the factory decided, read from the built adapter and its clients."""
    return {
        "type": type(llm).__name__,
        "model": llm.model_name,
        "base_url": llm.openai_api_base,
        "reasoning_effort": llm.reasoning_effort,
        "use_responses_api": llm.use_responses_api,
        "output_version": llm.output_version,
        "store": llm.store,
        "include": llm.include,
        "extra_body": llm.extra_body,
        "max_retries": llm.max_retries,
        "sync_transport": _transport_chain(llm.root_client._client),
        "async_transport": _transport_chain(llm.root_async_client._client),
    }


PARITY = [
    pytest.param({}, id="plain"),
    pytest.param({"SAGE_REASONING_EFFORT": "low"}, id="effort"),
    pytest.param({"SAGE_PROVIDER_RETRY_POLICY": "transient-v1"}, id="retry"),
    pytest.param({"SAGE_REASONING_EFFORT": "none", "SAGE_PROVIDER_RETRY_POLICY": "transient-v1"},
                 id="effort-and-retry"),
    pytest.param({"SAGE_OPENAI_API": "responses", "SAGE_OPENAI_ACCESS_PROGRAM": "daybreak_red",
                  "SAGE_OPENAI_SAFETY_IDENTIFIER": "user_a17f", "SAGE_REASONING_EFFORT": "high"},
                 id="responses-daybreak-identifier"),
    pytest.param({"SAGE_OPENAI_API": "chat_completions", "SAGE_OPENAI_SAFETY_IDENTIFIER": "user_a17f"},
                 id="chat-completions-identifier"),
]


@pytest.mark.parametrize("settings", PARITY)
def test_family_providers_build_the_same_adapter_as_openai_on_the_same_endpoint(settings):
    built = {
        provider: _built(_factory_a({"provider": provider, "model": "test-model", "API_ENDPOINT": PROXY,
                                     "API_KEY": KEY, **settings}, None, "default"))
        for provider in FAMILY
    }
    assert built["litellm"] == built["openai"]
    assert built["openai_compatible"] == built["openai"]
    if "SAGE_REASONING_EFFORT" in settings:
        assert built["openai"]["reasoning_effort"] == settings["SAGE_REASONING_EFFORT"]
    if "SAGE_PROVIDER_RETRY_POLICY" in settings:
        assert built["openai"]["sync_transport"][0] == "RetryingHTTPTransport"
        assert built["openai"]["max_retries"] == 0


@pytest.mark.parametrize("settings", PARITY[:2] + PARITY[4:])
def test_family_providers_match_openai_in_the_profile_factory(settings):
    built = {
        provider: _built(_factory_b({"provider": provider, "model": "test-model", "API_ENDPOINT": PROXY,
                                     "API_KEY": KEY, **settings}, None, "default"))
        for provider in FAMILY
    }
    assert built["litellm"] == built["openai"] == built["openai_compatible"]


def test_family_providers_are_metered_like_openai(monkeypatch):
    """Resource metering wraps every family provider's transport. The ledger object is a stand-in: the gate under
    test is which providers may be metered, not the ledger's accounting."""
    from ai import resource_budget

    ledger = object()
    monkeypatch.setattr(resource_budget, "configured_ledger", lambda: ledger)
    built = {
        provider: _built(_factory_a({"provider": provider, "model": "test-model", "API_ENDPOINT": PROXY,
                                     "API_KEY": KEY}, None, "default"))
        for provider in FAMILY
    }
    assert built["openai"]["sync_transport"][0] == "MeteredHTTPTransport"
    assert built["litellm"] == built["openai"] == built["openai_compatible"]
    from ai import resource_http

    with pytest.raises(resource_http.ResourceHTTPError, match="OpenAI-compatible provider"):
        _factory_a({"provider": "ollama", "model": "test-model", "API_KEY": KEY}, None, "default")


@pytest.mark.parametrize("provider", ["anthropic", "ollama", "bedrock"])
@pytest.mark.parametrize("setting", ["SAGE_REASONING_EFFORT", "SAGE_PROVIDER_RETRY_POLICY", "SAGE_OPENAI_API"])
def test_family_only_settings_still_refuse_other_providers(provider, setting):
    value = {"SAGE_REASONING_EFFORT": "low", "SAGE_PROVIDER_RETRY_POLICY": "transient-v1",
             "SAGE_OPENAI_API": "responses"}[setting]
    with pytest.raises(ValueError, match="OpenAI, LiteLLM Gateway or Custom"):
        build_model_kwargs(_request({"provider": provider, "model": "test-model", setting: value}))


def test_family_providers_get_the_strict_issue_schema():
    from tests.test_enrolled_executable_schema import _Request, _fixture

    bound = {}
    for provider in FAMILY:
        middleware, _client, issue, other = _fixture(provider)
        bound[provider] = middleware._with_enrolled_schema(_Request([issue, other])).tools[0]
    assert bound["openai"]["function"]["strict"] is True
    assert bound["litellm"] == bound["openai"] == bound["openai_compatible"]


@pytest.mark.parametrize("provider", ["litellm", "openai_compatible"])
def test_family_providers_get_the_empty_content_patch(monkeypatch, provider):
    import langchain_openai.chat_models.base as adapter

    from ai.langgraph import model as model_module

    monkeypatch.setattr(model_module, "_bedrock_patch_applied", False)
    monkeypatch.setattr(adapter, "_convert_message_to_dict", adapter._convert_message_to_dict)
    original = adapter._convert_message_to_dict

    _factory_a({"provider": provider, "model": "test-model", "API_ENDPOINT": PROXY, "API_KEY": KEY}, None,
               "default")

    assert adapter._convert_message_to_dict is not original
    assert adapter._convert_message_to_dict.__name__ == "_patched_convert_message_to_dict"


# --------------------------------------------------------------------------------------------------
# Notices: the recorded r03 Chat Completions 400 replayed on each family route
# --------------------------------------------------------------------------------------------------

def _r03() -> dict:
    from pathlib import Path

    rows = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/provider_wire_round_trips.json")
                      .read_text(encoding="utf-8"))["rows"]
    return next(row for row in rows if row["id"] == "r03_chat_completions_400_a_reasoning_effort")


ADVISES_RESPONSES = "set **OpenAI API** to **Responses**"
NOTICE_ROWS = [
    pytest.param({"provider": "openai"}, {}, "Responses (default) for OpenAI's API", id="openai-default"),
    pytest.param({"provider": "openai", "API_ENDPOINT": PROXY}, {},
                 "Chat Completions (default) for an endpoint other than OpenAI's", id="openai-proxy"),
    pytest.param({"provider": "litellm", "API_ENDPOINT": GATEWAY}, {},
                 "Chat Completions (default) for LiteLLM Gateway", id="litellm"),
    pytest.param({"provider": "openai_compatible", "API_ENDPOINT": PROXY}, {},
                 "Chat Completions (default) for Custom (OpenAI-compatible)", id="compatible"),
    pytest.param({"provider": "litellm", "API_ENDPOINT": GATEWAY, "SAGE_OPENAI_API": "responses"}, {},
                 "Responses (ui-config)", id="litellm-responses"),
    pytest.param({"provider": "openai", "SAGE_OPENAI_API": "chat_completions"}, {},
                 "Chat Completions (ui-config)", id="openai-direct-chat-completions"),
    pytest.param({"provider": "openai"}, {"SAGE_OPENAI_API": "chat_completions"},
                 "Chat Completions (environment)", id="openai-direct-chat-completions-env"),
    pytest.param({"provider": "openai"}, {"OPENAI_BASE_URL": PROXY},
                 "Chat Completions (default) for an endpoint other than OpenAI's", id="openai-sdk-env-proxy"),
]


@pytest.mark.parametrize(("config", "env", "label"), NOTICE_ROWS)
def test_recorded_rejection_notice_names_the_effective_api_and_its_source(monkeypatch, config, env, label):
    from sage_chat.service import _provider_configuration_text

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    row = _r03()
    config = {"model": "recorded-model-a", "API_KEY": KEY, "SAGE_REASONING_EFFORT": "low", **config}

    class Rejecting(Capture):
        def handler(self, library):
            def answer(request):
                self.requests.append((httpx.URL(str(request.url)), json.loads(request.content)))
                return library.Response(row["status"], content=json.dumps(row["body"]).encode(),
                                        headers={"content-type": "application/json"})
            return answer

    capture = Rejecting()
    llm = _factory_a(config, None, "default")
    capture.attach(llm, "default")
    error = replay.outcome(llm)
    assert isinstance(error, Exception)

    notice = _provider_configuration_text(error, _request(config))

    assert f"OpenAI API: {label} ·" in notice
    assert KEY not in notice
    responses = label.startswith("Responses")
    ((url, _body),) = capture.requests
    assert url.path.endswith("/responses" if responses else "/chat/completions")
    if responses:
        assert ADVISES_RESPONSES not in notice
        assert "OpenAI Daybreak: Off (Sage default)" in notice
        assert "set **Reasoning Effort** to a supported value" in notice
    else:
        assert ADVISES_RESPONSES in notice
        assert "OpenAI Daybreak: not sent" in notice


# --------------------------------------------------------------------------------------------------
# History continuity: a recorded Chat Completions conversation continues on the new default Responses route
# --------------------------------------------------------------------------------------------------

def _recorded_history() -> tuple[list, str]:
    from pathlib import Path

    frozen = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/chat_completions_history.json")
                        .read_text(encoding="utf-8"))
    return replay.restore_checkpoint(frozen["checkpoint"]), frozen["call_id"]


@pytest.mark.parametrize("path", ["default", "injected"])
def test_recorded_chat_completions_history_continues_on_the_default_responses_route(path):
    """An existing direct-OpenAI chat after the default flip: its next request carries the earlier call, result and
    answers, call id intact, on Responses. The history is recorded (checkpoint as stored); the reply is synthetic."""
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import tool

    from ai.langgraph.model import _sanitize_model_messages

    @tool
    def list_callbacks() -> str:
        """List the operation's callbacks."""
        return "[]"

    history, call_id = _recorded_history()
    # Through the first answer, then the recorded second prompt: the second request as Sage would send it.
    messages, _changed = _sanitize_model_messages(history[:6], configured_model="gpt-5.6-sol")
    capture = Capture()
    llm = _factory_a({"provider": "openai", "model": "gpt-5.6-sol", "API_KEY": KEY,
                      "SAGE_REASONING_EFFORT": "low"}, None, path)
    capture.attach(llm, path)

    llm.bind_tools([list_callbacks]).invoke(messages)

    ((url, body),) = capture.requests
    assert (url.host, url.path) == (OPENAI_HOST, "/v1/responses")
    assert body["store"] is False and body["access_programs"] == {"cyber": "standard"}
    items = body["input"]
    calls = [item for item in items if item.get("type") == "function_call"]
    outputs = [item for item in items if item.get("type") == "function_call_output"]
    tool_result = next(m for m in history if isinstance(m, ToolMessage))
    assert [(c["call_id"], c["name"], json.loads(c["arguments"])) for c in calls] == [(call_id, "list_callbacks", {})]
    assert [(o["call_id"], o["output"]) for o in outputs] == [(call_id, tool_result.content)]
    assert items.index(calls[0]) < items.index(outputs[0])
    # No Chat Completions or LangChain id is offered to Responses as an item id.
    assert all(not str(item.get("id", "")).startswith(("chatcmpl", "lc_run", "tooluse")) for item in items)
    texts = [
        (item.get("role"), part.get("text") if isinstance(part, dict) else part)
        for item in items if item.get("type", "message") == "message"
        for part in (item["content"] if isinstance(item["content"], list) else [item["content"]])
    ]
    # Sage's sanitizer backfills the empty text of the recorded tool-call turn with "." on every route.
    assert texts == [
        ("system", history[0].content),
        ("user", history[1].content),
        ("assistant", "."),
        ("assistant", history[4].content),
        ("user", history[5].content),
    ]
    assert "_seq" not in json.dumps(body) and "_operator_input" not in json.dumps(body)


def test_recorded_chat_completions_history_still_replays_on_its_gateway_route():
    """The same recorded history on its own route (LiteLLM Gateway, Chat Completions): unchanged behavior."""
    from ai.langgraph.model import _sanitize_model_messages

    history, call_id = _recorded_history()
    messages, _changed = _sanitize_model_messages(history[:6], configured_model="recorded-gateway-model")
    capture = Capture()
    llm = _factory_a({"provider": "litellm", "model": "recorded-gateway-model", "API_ENDPOINT": GATEWAY,
                      "API_KEY": KEY}, None, "default")
    capture.attach(llm, "default")

    llm.invoke(messages)

    ((url, body),) = capture.requests
    assert url.path == "/chat/completions"
    roles = [(m["role"], m.get("tool_call_id") or [c["id"] for c in m.get("tool_calls") or []] or None)
             for m in body["messages"]]
    assert roles == [("system", None), ("user", None), ("assistant", [call_id]), ("tool", call_id),
                     ("assistant", None), ("user", None)]
    assert body["messages"][2]["content"] == "."
    assert body["messages"][3]["content"] == next(m.content for m in history if m.type == "tool")


@pytest.mark.parametrize(("config", "label"), [
    pytest.param({"provider": "openai"}, "openai_api=Responses (default) for OpenAI's API", id="openai-default"),
    pytest.param({"provider": "litellm", "API_ENDPOINT": GATEWAY},
                 "openai_api=Chat Completions (default) for LiteLLM Gateway", id="litellm"),
    pytest.param({"provider": "openai", "API_ENDPOINT": PROXY, "SAGE_OPENAI_API": "responses"},
                 "openai_api=Responses (configured)", id="explicit"),
])
def test_debug_line_names_the_effective_api_and_where_it_came_from(caplog, config, label):
    import logging

    with caplog.at_level(logging.DEBUG):
        _factory_a({"model": "test-model", "API_KEY": KEY, **config}, None, "default")
    lines = [record.getMessage() for record in caplog.records if "Initializing model" in record.getMessage()]
    assert len(lines) == 1 and label in lines[0]
    assert KEY not in lines[0]
