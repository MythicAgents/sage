import asyncio
import json

import httpx
import pytest

from ai import resource_budget, resource_http
from ai.langgraph import model


def _instance(provider="openai"):
    instance = model.Model.__new__(model.Model)
    instance.provider = provider
    instance.model = "configured-model"
    instance.config = {"configurable": {
        "api_key": "configured-key",
        "base_url": "https://configured.invalid/v1",
    }}
    return instance


def _ledger(tmp_path):
    config, ledger = tmp_path / "resource-config.json", tmp_path / "resource-ledger.jsonl"
    config.write_text(json.dumps({
        "schema": resource_budget.CONFIG_SCHEMA,
        "resources": {
            "provider_requests": {"limit": 4, "cleanup_reserve": 0},
            "provider_tokens": {"limit": 65536, "cleanup_reserve": 0},
        },
        "calendar_seconds": 100,
        "cleanup_seconds": 10,
    }))
    return resource_budget.ResourceLedger(config, ledger)


def test_absent_shared_budget_preserves_native_init_arguments(monkeypatch):
    calls = []
    monkeypatch.delenv(resource_budget._CONFIG_ENV, raising=False)
    monkeypatch.delenv(resource_budget._LEDGER_ENV, raising=False)
    monkeypatch.delenv("SAGE_EVAL_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("SAGE_EVAL_TEMPERATURE", raising=False)
    monkeypatch.setattr(model, "init_chat_model", lambda **kwargs: calls.append(kwargs) or object())
    monkeypatch.setattr(model, "_patch_model_for_bedrock", lambda value: value)

    _instance()._get_base_chat_model()
    assert calls == [{
        "model_provider": "openai", "model": "configured-model",
        "api_key": "configured-key", "base_url": "https://configured.invalid/v1",
    }]


def test_native_temperature_is_opt_in_for_openai_and_bedrock(monkeypatch):
    calls = []
    monkeypatch.setenv("SAGE_EVAL_TEMPERATURE", "0")
    monkeypatch.setenv("SAGE_EVAL_MAX_OUTPUT_TOKENS", "4096")
    monkeypatch.setattr(model, "init_chat_model", lambda **kwargs: calls.append(kwargs) or object())
    monkeypatch.setattr(model, "_patch_model_for_bedrock", lambda value: value)
    monkeypatch.setattr(model.resource_budget, "configured_ledger", lambda: None)

    _instance()._get_base_chat_model()
    bedrock = _instance("bedrock")
    bedrock.config["configurable"].update({
        "region": "local-region", "aws_access_key_id": "synthetic-key",
        "aws_secret_access_key": "synthetic-secret", "aws_session_token": "synthetic-session",
    })
    bedrock._get_base_chat_model()
    assert [value["temperature"] for value in calls] == [0.0, 0.0]
    assert [value["max_tokens"] for value in calls] == [4096, 4096]


@pytest.mark.parametrize("value", ["invalid", "nan", "inf", "-0.1", "2.1"])
def test_invalid_native_temperature_refuses_before_ledger_or_model(value, monkeypatch):
    monkeypatch.setenv("SAGE_EVAL_TEMPERATURE", value)
    monkeypatch.setattr(
        model.resource_budget, "configured_ledger", lambda: pytest.fail("resource clients constructed")
    )
    monkeypatch.setattr(model, "init_chat_model", lambda **kwargs: pytest.fail(f"model initialized: {kwargs}"))
    with pytest.raises(ValueError, match="SAGE_EVAL_TEMPERATURE"):
        _instance()._get_base_chat_model()


def test_native_model_injects_per_client_metered_transports_and_reserves_before_mock_dispatch(tmp_path, monkeypatch):
    ledger, calls = _ledger(tmp_path), []
    monkeypatch.setenv("SAGE_EVAL_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setattr(model.resource_budget, "configured_ledger", lambda: ledger)
    monkeypatch.setattr(model, "init_chat_model", lambda **kwargs: calls.append(kwargs) or object())
    monkeypatch.setattr(model, "_patch_model_for_bedrock", lambda value: value)

    _instance()._get_base_chat_model()
    kwargs = calls[0]
    assert {key: kwargs[key] for key in ("model_provider", "model", "api_key", "base_url", "max_tokens")} == {
        "model_provider": "openai", "model": "configured-model", "api_key": "configured-key",
        "base_url": "https://configured.invalid/v1", "max_tokens": 8192,
    }
    assert kwargs["max_retries"] == 0
    assert isinstance(kwargs["http_client"]._transport, resource_http.MeteredHTTPTransport)
    assert isinstance(kwargs["http_async_client"]._transport, resource_http.MeteredAsyncHTTPTransport)
    assert kwargs["http_client"]._transport is not kwargs["http_async_client"]._transport
    assert kwargs["http_client"]._transport.ledger is ledger
    kwargs["http_client"]._transport.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"usage": {"total_tokens": 10}}, request=request)
    )
    kwargs["http_client"].post("https://provider.invalid/v1/chat/completions")
    assert ledger.status()["effective_resource_charges"] == {"provider_requests": 1, "provider_tokens": 10}
    kwargs["http_client"].close()
    asyncio.run(kwargs["http_async_client"].aclose())


@pytest.mark.parametrize("provider", ["bedrock", "anthropic", "ollama"])
def test_native_metering_refuses_unsupported_provider_before_model_init(provider, monkeypatch):
    monkeypatch.setattr(model.resource_budget, "configured_ledger", lambda: object())
    monkeypatch.setattr(model, "init_chat_model", lambda **kwargs: pytest.fail(f"model initialized: {kwargs}"))
    with pytest.raises(resource_http.ResourceHTTPError, match="OpenAI-compatible"):
        _instance(provider)._get_base_chat_model()
