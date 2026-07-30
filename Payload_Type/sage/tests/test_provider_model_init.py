"""Static provider-construction coverage for Sage's supported LLM routes."""

import pytest

from ai.langgraph import model as model_module


class _FakeLLM:
    pass


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
            {"api_key": "openai-key", "base_url": "http://openai.test/v1"},
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


def test_missing_configurable_section_still_fails_closed(monkeypatch):
    instance = _model("ollama", {})
    instance.config = {}
    monkeypatch.setattr(
        model_module,
        "init_chat_model",
        lambda **kwargs: pytest.fail(f"unexpected model construction: {kwargs}"),
    )

    assert instance._get_base_chat_model() is None
