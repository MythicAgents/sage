"""OpenAI API selection, Daybreak access program and safety identifier, from native config to the wire.

Every case drives the real resolver (``build_model_kwargs`` / ``resolve_llm_profile``), the real base-model
factory (``Model._get_base_chat_model`` or ``init_chat_model_from_profile``), the installed ``ChatOpenAI`` and the
OpenAI SDK. Only the HTTP transport is a stand-in (``httpx.MockTransport``); assertions read the request path and
JSON body the provider would receive.

Provenance: the 404 body is the operator-supplied, sanitized direct-OpenAI error from Sage v0.3.0 (2026-11-20).
Every successful provider reply here is synthetic and proves wiring only, not model behavior.
"""

from __future__ import annotations

import json

import httpx
import langchain.chat_models
import openai
import pytest

from ai.langgraph import model as model_module
from ai.openai_api import validate_route
from ai.langgraph.operator_error import (
    provider_configuration_notice,
    provider_configuration_rejection,
)
from sage_chat import models as chat_models
from sage_chat.config import (
    build_model_kwargs,
    init_chat_model_from_profile,
    resolve_llm_profile,
)
from sage_chat.headless import build_chat_request

ENDPOINT = "https://provider.invalid/v1"
KEY = "offline-placeholder-key"
PSEUDONYM = "user_a17f"
# Operator-supplied evidence (Sage v0.3.0, direct OpenAI, 2026-11-20).
REPORTED_404 = {"error": {
    "message": "This model is not supported in the v1/chat/completions endpoint. Use the v1/responses endpoint instead.",
    "type": "invalid_request_error", "param": "model", "code": None,
}}
NEW_ENV = ("SAGE_OPENAI_API", "SAGE_OPENAI_ACCESS_PROGRAM", "SAGE_OPENAI_SAFETY_IDENTIFIER")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (*NEW_ENV, "SAGE_REASONING_EFFORT", "SAGE_PROVIDER_RETRY_POLICY", "provider", "model",
                 "API_ENDPOINT", "API_KEY"):
        monkeypatch.delenv(name, raising=False)


class Wire:
    """Records provider requests and answers like OpenAI would (successes are synthetic)."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if request.url.path.endswith("/chat/completions"):
            if body.get("model") == "gpt-6-cyber":
                return httpx.Response(404, json=REPORTED_404)
            return httpx.Response(200, json={
                "id": "chatcmpl-offline", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "READY"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        return httpx.Response(200, json={
            "id": "resp_offline", "object": "response", "created_at": 0, "model": body["model"],
            "status": "completed", "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "output": [{"id": "msg_offline", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "READY", "annotations": []}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                      "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0, "cache_write_tokens_details": {"ttl_12h_tokens": 0, "ttl_30m_tokens": 0}},
                      "output_tokens_details": {"reasoning_tokens": 0}}})


@pytest.fixture
def wire(monkeypatch):
    recorder = Wire()
    original = langchain.chat_models.init_chat_model
    client = httpx.Client(transport=httpx.MockTransport(recorder.handle))

    def initialize(**kwargs):
        return original(**kwargs, http_client=client, max_retries=0)

    monkeypatch.setattr(model_module, "init_chat_model", initialize)
    monkeypatch.setattr(langchain.chat_models, "init_chat_model", initialize)
    yield recorder
    client.close()


def _config(**extra):
    return {"provider": "openai", "model": "test-model", "API_ENDPOINT": ENDPOINT, "API_KEY": KEY, **extra}


def _sage_llm(config, secrets=None):
    kwargs = build_model_kwargs(build_chat_request("Offline request", config=config, secrets=secrets))
    sage = model_module.Model.__new__(model_module.Model)
    sage.provider, sage.model, sage.config = kwargs["provider"], kwargs["model"], kwargs["config"]
    return sage._get_base_chat_model()


def _send(config, secrets=None):
    return _sage_llm(config, secrets).invoke("Offline request")


# --------------------------------------------------------------------------------------------------
# Native controls
# --------------------------------------------------------------------------------------------------

def test_native_controls_follow_provider_and_model_with_specified_choices():
    names = [option.Name for option in chat_models._CONFIG_OPTIONS]
    position = names.index("model")
    assert names[position + 1: position + 4] == list(NEW_ENV)
    options = {option.Name: option for option in chat_models._CONFIG_OPTIONS}

    api = options["SAGE_OPENAI_API"]
    assert api.DisplayName == "OpenAI API"
    assert [(c.Label, c.Value) for c in api.Choices] == [
        ("Use configured default", ""), ("Chat Completions", "chat_completions"), ("Responses", "responses")]
    assert api.DefaultValue == ""

    daybreak = options["SAGE_OPENAI_ACCESS_PROGRAM"]
    assert daybreak.DisplayName == "OpenAI Daybreak"
    assert [(c.Label, c.Value) for c in daybreak.Choices] == [
        ("Use configured default", ""), ("Off", "standard"), ("Blue", "daybreak_blue"), ("Red", "daybreak_red")]
    assert daybreak.DefaultValue == ""

    identifier = options["SAGE_OPENAI_SAFETY_IDENTIFIER"]
    assert identifier.DisplayName == "OpenAI Safety Identifier"
    assert identifier.Required is False

    for option in (api, daybreak, identifier):
        help_text = f"{option.Description} {option.HelpText}"
        assert "user secret" in help_text and "environment" in help_text
        # The route settings apply to every OpenAI-family provider (the provider split).
        assert "OpenAI, LiteLLM Gateway and Custom" in option.Description
    daybreak_help = f"{daybreak.Description} {daybreak.HelpText}"
    assert "Off" in daybreak_help and "standard safeguards" in daybreak_help
    assert "Responses" in daybreak_help
    # Mythic shows only the Description. It must state the validator's rule: every explicit Daybreak choice,
    # Off included, is refused unless the route is Responses, which a blank OpenAI API means only on OpenAI's own
    # API (the provider split's default); blank Daybreak stays valid on any API.
    assert "Off included" in daybreak.Description and "requires the Responses API" in daybreak.Description
    for choice in daybreak.Choices:
        for api, endpoint in (("", ENDPOINT), ("chat_completions", ENDPOINT), ("chat_completions", "")):
            if choice.Value:
                with pytest.raises(ValueError, match="requires OpenAI API = Responses"):
                    validate_route(provider="openai", model="m", api=api, access_program=choice.Value,
                                   endpoint=endpoint)
            else:
                validate_route(provider="openai", model="m", api=api, access_program=choice.Value,
                               endpoint=endpoint)
        route = validate_route(provider="openai", model="m", api="", access_program=choice.Value, endpoint="",
                               environ={})
        assert route.effective_access_program == (choice.Value or "standard")
    assert "pseudonym" in f"{identifier.Description} {identifier.HelpText}"

    sage = next(model for model in chat_models.SAGE_MODELS if model.Name == "Sage")
    assert set(NEW_ENV) <= set(sage.Metadata.OptionalUserSecrets)
    watcher = next(model for model in chat_models.SAGE_MODELS if model.Name == "Sage Watcher")
    assert not set(NEW_ENV) & {option.Name for option in watcher.Metadata.ConfigurationOptions}


@pytest.mark.parametrize("layer", ["config", "secret", "environment"])
def test_each_layer_supplies_the_settings_and_config_wins(monkeypatch, layer):
    values = {"SAGE_OPENAI_API": "responses", "SAGE_OPENAI_ACCESS_PROGRAM": "daybreak_blue",
              "SAGE_OPENAI_SAFETY_IDENTIFIER": PSEUDONYM}
    config, secrets = _config(), {}
    if layer == "config":
        config.update(values)
        secrets = {name: "chat_completions" if name == "SAGE_OPENAI_API" else "ignored" for name in values}
        expected_source = "ui-config"
    elif layer == "secret":
        secrets = dict(values)
        expected_source = "user-secret"
    else:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        expected_source = "environment"
    profile = resolve_llm_profile(build_chat_request("x", config=config, secrets=secrets))
    assert (profile.openai_api, profile.openai_access_program, profile.openai_safety_identifier) == (
        "responses", "daybreak_blue", PSEUDONYM)
    for field in ("openai_api", "openai_access_program", "openai_safety_identifier"):
        assert profile.source_for(field) == expected_source


# --------------------------------------------------------------------------------------------------
# Wire bodies
# --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("model", ["test-model", "bedrock-claude-4-6-sonnet"])
def test_unset_settings_keep_the_existing_chat_completions_body(wire, model):
    assert _send(_config(model=model)).content == "READY"
    ((path, body),) = wire.requests
    assert path == "/v1/chat/completions"
    assert set(body) == {"messages", "model", "stream"}


def test_explicit_chat_completions_pins_the_route_and_sends_no_access_program(wire):
    # gpt-5.5-pro is a model name the adapter would otherwise route to Responses on its own.
    _send(_config(model="gpt-5.5-pro", SAGE_OPENAI_API="chat_completions"))
    ((path, body),) = wire.requests
    assert path == "/v1/chat/completions"
    assert "access_programs" not in body and "store" not in body and "include" not in body


def test_documented_gpt6_cyber_selection_sends_the_documented_responses_request(wire):
    message = _send(_config(model="gpt-6-cyber", SAGE_OPENAI_API="responses",
                            SAGE_OPENAI_ACCESS_PROGRAM="daybreak_red",
                            SAGE_OPENAI_SAFETY_IDENTIFIER=PSEUDONYM))
    assert message.text == "READY"
    ((path, body),) = wire.requests
    assert path == "/v1/responses"
    assert body["model"] == "gpt-6-cyber"
    assert body["access_programs"] == {"cyber": "daybreak_red"}
    assert body["safety_identifier"] == PSEUDONYM
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert "previous_response_id" not in body and "messages" not in body
    assert body["input"][-1]["role"] == "user"
    assert not body.get("stream")



def test_safety_identifier_is_optional_even_for_gpt6_cyber(wire):
    # Operator decision (2026-11-20): OpenAI documents the safety identifier as recommended, not required, so
    # Sage sends gpt-6-cyber on Responses without one rather than refusing the chat.
    message = _send(_config(model="gpt-6-cyber", SAGE_OPENAI_API="responses",
                            SAGE_OPENAI_ACCESS_PROGRAM="daybreak_red"))
    assert message.text == "READY"
    ((path, body),) = wire.requests
    assert path == "/v1/responses"
    assert body["model"] == "gpt-6-cyber" and body["access_programs"] == {"cyber": "daybreak_red"}
    assert "safety_identifier" not in body

@pytest.mark.parametrize(("selected", "sent"), [
    (None, "standard"),          # operator decision: Sage's default is Off, never a project's Blue default
    ("standard", "standard"),
    ("daybreak_blue", "daybreak_blue"),
    ("daybreak_red", "daybreak_red"),
])
def test_responses_requests_always_name_the_access_program(wire, selected, sent):
    config = _config(SAGE_OPENAI_API="responses")
    if selected:
        config["SAGE_OPENAI_ACCESS_PROGRAM"] = selected
    _send(config)
    ((path, body),) = wire.requests
    assert path == "/v1/responses"
    assert body["access_programs"] == {"cyber": sent}
    assert "safety_identifier" not in body


def test_responses_selected_from_the_environment_and_reasoning_effort_converted(wire, monkeypatch):
    monkeypatch.setenv("SAGE_OPENAI_API", "responses")
    _send(_config(SAGE_REASONING_EFFORT="high"))
    ((path, body),) = wire.requests
    assert path == "/v1/responses"
    assert body["reasoning"] == {"effort": "high"} and "reasoning_effort" not in body


def test_profile_factory_uses_the_same_request_projection(wire):
    config = _config(model="gpt-6-cyber", SAGE_OPENAI_API="responses", SAGE_OPENAI_ACCESS_PROGRAM="daybreak_red",
                     SAGE_OPENAI_SAFETY_IDENTIFIER=PSEUDONYM)
    _send(config)
    profile = resolve_llm_profile(build_chat_request("Offline request", config=config))
    init_chat_model_from_profile(profile).invoke("Offline request")
    (first_path, first), (second_path, second) = wire.requests
    assert first_path == second_path == "/v1/responses"
    assert first == second


# --------------------------------------------------------------------------------------------------
# Local validation: fails before any provider request, names the setting and the action
# --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("extra", "expected"), [
    ({"SAGE_OPENAI_API": "Responses"}, "SAGE_OPENAI_API must be"),
    ({"SAGE_OPENAI_API": "responses_api"}, "SAGE_OPENAI_API must be"),
    ({"SAGE_OPENAI_API": "chat"}, "SAGE_OPENAI_API must be"),
    ({"SAGE_OPENAI_API": "responses", "SAGE_OPENAI_ACCESS_PROGRAM": "red"}, "SAGE_OPENAI_ACCESS_PROGRAM must be"),
    ({"SAGE_OPENAI_API": "responses", "SAGE_OPENAI_ACCESS_PROGRAM": "daybreak-red"},
     "SAGE_OPENAI_ACCESS_PROGRAM must be"),
    ({"SAGE_OPENAI_API": "responses", "SAGE_OPENAI_ACCESS_PROGRAM": "off"}, "SAGE_OPENAI_ACCESS_PROGRAM must be"),
    ({"SAGE_OPENAI_ACCESS_PROGRAM": "daybreak_red"}, "requires OpenAI API = Responses"),
    ({"SAGE_OPENAI_API": "chat_completions", "SAGE_OPENAI_ACCESS_PROGRAM": "standard"},
     "requires OpenAI API = Responses"),
    ({"provider": "anthropic", "SAGE_OPENAI_API": "responses"}, "require the OpenAI, LiteLLM Gateway or Custom"),
    ({"provider": "bedrock", "SAGE_OPENAI_SAFETY_IDENTIFIER": PSEUDONYM},
     "require the OpenAI, LiteLLM Gateway or Custom"),
])
def test_invalid_combinations_fail_locally_with_the_configuration_action(wire, extra, expected):
    with pytest.raises(ValueError, match=expected) as raised:
        _send(_config(**extra))
    assert not wire.requests
    assert KEY not in str(raised.value)


def test_model_factory_rejects_an_invalid_projection_it_is_handed(wire):
    sage = model_module.Model.__new__(model_module.Model)
    sage.provider, sage.model = "openai", "test-model"
    sage.config = {"configurable": {"api_key": KEY, "base_url": ENDPOINT,
                                    "openai_access_program": "daybreak_red"}}
    with pytest.raises(ValueError, match="requires OpenAI API = Responses"):
        sage._get_base_chat_model()
    assert not wire.requests


# --------------------------------------------------------------------------------------------------
# Provider rejections: real reported 404 replayed through the SDK; Daybreak codes from OpenAI's guide
# --------------------------------------------------------------------------------------------------

def _rejection(status, error):
    request = httpx.Request("POST", f"{ENDPOINT}/responses")
    response = httpx.Response(status, json={"error": error}, request=request)
    client = openai.OpenAI(api_key=KEY, base_url=ENDPOINT, max_retries=0,
                           http_client=httpx.Client(transport=httpx.MockTransport(lambda _r: response)))
    try:
        client.responses.create(model="gpt-6-cyber", input="x")
    except openai.APIStatusError as exc:
        return exc
    raise AssertionError("no rejection raised")


def test_reported_404_is_replayed_and_explained_with_the_recovery_action(wire):
    config = _config(model="gpt-6-cyber", SAGE_OPENAI_API="chat_completions")
    with pytest.raises(openai.NotFoundError) as raised:
        _send(config)
    ((path, _),) = wire.requests
    assert path == "/v1/chat/completions"
    wrapped = RuntimeError("graph failed")
    wrapped.__cause__ = raised.value
    assert provider_configuration_rejection(wrapped) is raised.value

    profile = resolve_llm_profile(build_chat_request("x", config=config))
    notice = provider_configuration_notice(wrapped, profile)
    assert "HTTP 404" in notice and "`model`" in notice
    assert "not supported in the v1/chat/completions endpoint" in notice
    assert "gpt-6-cyber" in notice and "Chat Completions" in notice and "ui-config" in notice
    assert "set **OpenAI API** to **Responses**" in notice
    assert "SAGE_OPENAI_API" in notice
    assert "keeps its conversation" in notice
    assert "did not change the model, provider or endpoint" in notice
    assert "https://provider.invalid/v1" in notice
    assert KEY not in notice


@pytest.mark.parametrize(("status", "error", "action"), [
    (400, {"type": "invalid_request_error", "code": "invalid_access_program", "param": None,
           "message": "Use daybreak_red."}, "value named in the error"),
    (400, {"type": "invalid_request_error", "code": "unsupported_access_program", "param": None,
           "message": "Unsupported."}, "Off"),
    (403, {"type": "invalid_request_error", "code": "access_program_not_enabled", "param": None,
           "message": "Not enabled."}, "project"),
    (401, {"type": "invalid_request_error", "code": "expired_secret_key", "param": None,
           "message": "Your API key has expired."}, "API key"),
])
def test_daybreak_and_access_rejections_are_configuration_errors(status, error, action):
    exc = _rejection(status, error)
    assert provider_configuration_rejection(exc) is exc
    profile = resolve_llm_profile(build_chat_request("x", config=_config(
        model="gpt-6-cyber", SAGE_OPENAI_API="responses", SAGE_OPENAI_ACCESS_PROGRAM="daybreak_red",
        SAGE_OPENAI_SAFETY_IDENTIFIER=PSEUDONYM)))
    notice = provider_configuration_notice(exc, profile)
    assert f"HTTP {status}" in notice and action in notice
    assert "OpenAI Daybreak: Red" in notice and "OpenAI Safety Identifier: configured" in notice
    assert PSEUDONYM not in notice and KEY not in notice


@pytest.mark.parametrize(("status", "error"), [
    (429, {"type": "rate_limit_error", "code": "rate_limit_exceeded", "param": None, "message": "Slow down."}),
    (500, {"type": "server_error", "code": None, "param": None, "message": "Server error."}),
    (400, {"type": "invalid_request_error", "code": None, "param": "input",
           "message": "No tool output found for function call call_1."}),
])
def test_transient_and_history_errors_are_not_configuration_errors(status, error):
    exc = _rejection(status, error)
    assert provider_configuration_rejection(exc) is None
    assert provider_configuration_notice(exc, None) == ""


def test_dotenv_files_supply_the_settings_through_the_environment_layer(wire, tmp_path, monkeypatch):
    """`.env.local` then `.env` load into the process environment at startup (dotenv_bootstrap); the resolver's
    environment layer then reads them, below the chat field and the user secret."""
    import os

    import dotenv_bootstrap

    (tmp_path / ".env.local").write_text("SAGE_OPENAI_API=responses\nSAGE_OPENAI_ACCESS_PROGRAM=daybreak_blue\n")
    (tmp_path / ".env").write_text("SAGE_OPENAI_API=chat_completions\nSAGE_OPENAI_SAFETY_IDENTIFIER=user_env\n")
    environ = dict(os.environ)
    monkeypatch.setattr(os, "environ", environ)
    applied = dotenv_bootstrap.load_sage_dotenv(str(tmp_path))
    assert set(NEW_ENV) <= set(applied)

    profile = resolve_llm_profile(build_chat_request("x", config=_config()))
    assert (profile.openai_api, profile.openai_access_program, profile.openai_safety_identifier) == (
        "responses", "daybreak_blue", "user_env")
    assert {profile.source_for(f) for f in ("openai_api", "openai_access_program")} == {"environment"}
    _send(_config())
    ((path, body),) = wire.requests
    assert path == "/v1/responses"
    assert body["access_programs"] == {"cyber": "daybreak_blue"} and body["safety_identifier"] == "user_env"

    # A user secret still outranks the files, and the chat field outranks both.
    secret = resolve_llm_profile(build_chat_request("x", config=_config(),
                                                    secrets={"SAGE_OPENAI_ACCESS_PROGRAM": "standard"}))
    assert secret.openai_access_program == "standard" and secret.source_for("openai_access_program") == "user-secret"
    field = resolve_llm_profile(build_chat_request("x", config=_config(SAGE_OPENAI_ACCESS_PROGRAM="daybreak_red"),
                                                   secrets={"SAGE_OPENAI_ACCESS_PROGRAM": "standard"}))
    assert field.openai_access_program == "daybreak_red" and field.source_for("openai_access_program") == "ui-config"


def test_shipped_env_template_names_the_settings_but_sets_none():
    from pathlib import Path

    template = (Path(__file__).resolve().parents[1] / ".env").read_text().splitlines()
    for name in NEW_ENV:
        assert any(line.startswith(f"#{name}=") for line in template), name
        assert not any(line.startswith(f"{name}=") for line in template), name


# --------------------------------------------------------------------------------------------------
# Cold review (2026-11-20): the notice promises the conversation survives only for fixes that keep it.
# Changing OpenAI API, OpenAI Daybreak or OpenAI Safety Identifier continues the chat; a new API key, model, provider or
# endpoint starts a new session (general switching is #220), so the notice must say so.
# --------------------------------------------------------------------------------------------------

KEEPS = "keeps its conversation"
NEW_CHAT = "starts a new chat"


def _notice(status, error, **route):
    exc = _rejection(status, error)
    profile = resolve_llm_profile(build_chat_request("x", config=_config(model="gpt-6-cyber", **route)))
    return provider_configuration_notice(exc, profile)


CYBER = {"SAGE_OPENAI_API": "responses", "SAGE_OPENAI_ACCESS_PROGRAM": "daybreak_red",
         "SAGE_OPENAI_SAFETY_IDENTIFIER": PSEUDONYM}


@pytest.mark.parametrize(("status", "error"), [
    (400, {"type": "invalid_request_error", "code": "invalid_access_program", "param": None, "message": "x"}),
    (400, {"type": "invalid_request_error", "code": None, "param": "safety_identifier", "message": "x"}),
])
def test_route_setting_fixes_promise_the_conversation_continues(status, error):
    notice = _notice(status, error, **CYBER)
    assert KEEPS in notice
    # Changed expectation (reasoning-tool recovery plan, claim): Reasoning Effort now continues the chat too.
    assert "OpenAI API, OpenAI Daybreak, OpenAI Safety Identifier or Reasoning Effort" in notice


def test_responses_only_404_on_chat_completions_promises_continuation():
    exc = RuntimeError("graph failed")
    exc.__cause__ = _rejection(404, REPORTED_404["error"])
    profile = resolve_llm_profile(build_chat_request(
        "x", config=_config(model="gpt-6-cyber", SAGE_OPENAI_API="chat_completions")))
    assert KEEPS in provider_configuration_notice(exc, profile)


@pytest.mark.parametrize(("status", "error", "names"), [
    (401, {"type": "invalid_request_error", "code": "invalid_api_key", "param": None,
           "message": "Incorrect API key provided."}, "API key"),
    (403, {"type": "invalid_request_error", "code": None, "param": None, "message": "Forbidden."}, "API key"),
    (403, {"type": "invalid_request_error", "code": "access_program_not_enabled", "param": None,
           "message": "Not enabled."}, "API key"),
])
def test_key_or_project_fixes_say_a_change_starts_a_new_chat(status, error, names):
    notice = _notice(status, error, **CYBER)
    assert KEEPS not in notice
    assert NEW_CHAT in notice and names in notice
    assert "send your request again here" in notice  # a fix made at OpenAI without changing Sage's settings


def test_an_unknown_model_fix_promises_the_conversation_continues():
    """Changed expectation (model-switch plan, required outcome 2): a model change no longer starts a new chat."""
    notice = _notice(404, {"type": "invalid_request_error", "code": "model_not_found", "param": "model",
                           "message": "The model `gpt-6-cyber` does not exist or you do not have access to it."},
                     **CYBER)
    assert KEEPS in notice and NEW_CHAT not in notice
    assert "**Model**" in notice and "/model <name>" in notice
    assert "send your request again here" in notice  # access granted at OpenAI is retried as it is
