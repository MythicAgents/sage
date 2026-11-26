"""Static provider-construction coverage for Sage's supported LLM routes."""

import httpx
import pytest

from ai.langgraph import model as model_module


class _FakeLLM:
    pass


# What a blank OpenAI API means on OpenAI's own endpoint (the provider split): Responses with Sage's projection.
DIRECT_OPENAI_RESPONSES = {
    "use_responses_api": True,
    "output_version": "responses/v1",
    "store": False,
    "include": ["reasoning.encrypted_content"],
    "extra_body": {"access_programs": {"cyber": "standard"}},
}

# Changed expectation (provider no-answer): every OpenAI-family client bounds only its connect phase, at 10 s.
CONNECT_BOUND = {"timeout": httpx.Timeout(None, connect=10.0)}


@pytest.fixture(autouse=True)
def _no_adapter_endpoint(monkeypatch):
    # The adapter's own endpoint variables decide what a blank API Endpoint means; keep these rows hermetic.
    for name in ("OPENAI_API_BASE", "OPENAI_BASE_URL", "LANGSMITH_GATEWAY"):
        monkeypatch.delenv(name, raising=False)


def _model(provider, configurable):
    instance = model_module.Model.__new__(model_module.Model)
    instance.provider = provider
    instance.model = "test-model"
    instance.config = {"configurable": configurable}
    return instance


@pytest.mark.parametrize(
    ("provider", "configurable", "expected_kwargs"),
    [
        ("ollama", {}, {}),
        ("ollama", {"base_url": "http://127.0.0.1:11434"}, {"base_url": "http://127.0.0.1:11434"}),
        ("anthropic", {}, {}),
        (
            "anthropic",
            {"api_key": "anthropic-key", "base_url": "http://anthropic.test"},
            {"api_key": "anthropic-key", "base_url": "http://anthropic.test"},
        ),
        (
            "openai",
            {"api_key": "openai-key", "base_url": "http://openai.test/v1"},
            # A blank OpenAI API pins Chat Completions (operator decision 2026-11-24): the adapter never chooses Responses.
            {"api_key": "openai-key", "base_url": "http://openai.test/v1", "use_responses_api": False,
             **CONNECT_BOUND},
        ),
    ],
)
def test_non_bedrock_model_kwargs_are_independent(monkeypatch, provider, configurable, expected_kwargs):
    calls = []

    def _init_chat_model(**kwargs):
        calls.append(kwargs)
        return _FakeLLM()

    monkeypatch.setattr(model_module, "init_chat_model", _init_chat_model)
    monkeypatch.setattr(model_module, "_patch_model_for_bedrock", lambda llm: llm)

    assert isinstance(_model(provider, configurable)._get_base_chat_model(), _FakeLLM)
    assert calls == [{
        "model_provider": provider,
        "model": "test-model",
        **expected_kwargs,
    }]


def test_bedrock_constructor_contract_is_unchanged(monkeypatch):
    calls = []

    def _init_chat_model(**kwargs):
        calls.append(kwargs)
        return _FakeLLM()

    monkeypatch.setattr(model_module, "init_chat_model", _init_chat_model)
    monkeypatch.setattr(model_module, "_patch_model_for_bedrock", lambda llm: llm)
    configurable = {
        "region": "us-west-2",
        "aws_access_key_id": "access",
        "aws_secret_access_key": "secret",
        "aws_session_token": "token",
    }

    assert isinstance(_model("bedrock", configurable)._get_base_chat_model(), _FakeLLM)
    assert calls == [{
        "model_provider": "bedrock",
        "model": "test-model",
        **configurable,
    }]


def test_eval_max_output_tokens_is_applied_to_shipping_model(monkeypatch):
    calls = []
    monkeypatch.setenv("SAGE_EVAL_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setattr(
        model_module,
        "init_chat_model",
        lambda **kwargs: calls.append(kwargs) or _FakeLLM(),
    )
    monkeypatch.setattr(model_module, "_patch_model_for_bedrock", lambda llm: llm)

    assert isinstance(_model("openai", {})._get_base_chat_model(), _FakeLLM)
    # Changed expectation (provider split): no endpoint is OpenAI's own, where a blank OpenAI API means Responses.
    assert calls == [{
        "model_provider": "openai",
        "model": "test-model",
        "max_tokens": 8192,
        **DIRECT_OPENAI_RESPONSES,
        **CONNECT_BOUND,
    }]


def test_missing_configurable_section_still_fails_closed(monkeypatch):
    instance = _model("ollama", {})
    instance.config = {}
    monkeypatch.setattr(
        model_module,
        "init_chat_model",
        lambda **kwargs: pytest.fail(f"unexpected model construction: {kwargs}"),
    )

    assert instance._get_base_chat_model() is None


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_explicit_openai_reasoning_effort_reaches_model_constructor(monkeypatch, effort):
    calls = []
    monkeypatch.setattr(model_module, "init_chat_model", lambda **kwargs: calls.append(kwargs) or _FakeLLM())
    monkeypatch.setattr(model_module, "_patch_model_for_bedrock", lambda llm: llm)

    assert isinstance(_model("openai", {"reasoning_effort": effort})._get_base_chat_model(), _FakeLLM)
    # Changed expectation (provider split): no endpoint is OpenAI's own, where a blank OpenAI API means Responses.
    # The adapter still never chooses the API itself.
    assert calls == [{"model_provider": "openai", "model": "test-model", "reasoning_effort": effort,
                      **DIRECT_OPENAI_RESPONSES, **CONNECT_BOUND}]


@pytest.mark.parametrize("provider", ["anthropic", "bedrock", "ollama"])
def test_explicit_reasoning_effort_rejected_for_other_provider(monkeypatch, provider):
    monkeypatch.setattr(model_module, "init_chat_model", lambda **kwargs: pytest.fail(f"model initialized: {kwargs}"))
    with pytest.raises(ValueError, match="SAGE_REASONING_EFFORT requires"):
        _model(provider, {"reasoning_effort": "none"})._get_base_chat_model()


@pytest.mark.parametrize("effort", ["NONE", "unsupported"])
def test_invalid_direct_reasoning_effort_rejected_before_model_construction(monkeypatch, effort):
    monkeypatch.setattr(model_module, "init_chat_model", lambda **kwargs: pytest.fail(f"model initialized: {kwargs}"))
    with pytest.raises(ValueError, match="SAGE_REASONING_EFFORT must be"):
        _model("openai", {"reasoning_effort": effort})._get_base_chat_model()
