"""Verify Sage and raw configuration reaches actual SDK request bodies offline."""

import json

import httpx
import langchain.chat_models
import pytest

from ai.hillclimb.live_seams import make_model_fn
from ai.langgraph import model as model_module
from sage_chat.config import build_model_kwargs
from sage_chat.headless import build_chat_request


@pytest.mark.parametrize("effort", [None, "none", "high"])
def test_sage_and_raw_send_equal_explicit_effort_without_changing_api(monkeypatch, effort):
    monkeypatch.delenv("SAGE_REASONING_EFFORT", raising=False)
    requests = []

    def respond(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0,
            "model": "test-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "READY"},
                "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    original = langchain.chat_models.init_chat_model
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def initialize(**kwargs):
            return original(**kwargs, http_client=client)

        monkeypatch.setattr(model_module, "init_chat_model", initialize)
        monkeypatch.setattr(langchain.chat_models, "init_chat_model", initialize)
        config = {"provider": "openai", "model": "test-model",
                  "API_ENDPOINT": "https://example.invalid/v1", "API_KEY": "offline-placeholder"}
        if effort is not None:
            config["SAGE_REASONING_EFFORT"] = effort
            monkeypatch.setenv("SAGE_REASONING_EFFORT", "low")
        kwargs = build_model_kwargs(build_chat_request("Offline request", config=config))
        sage = model_module.Model.__new__(model_module.Model)
        sage.provider, sage.model, sage.config = kwargs["provider"], kwargs["model"], kwargs["config"]
        assert sage._get_base_chat_model().invoke("Offline request").content == "READY"
        raw = make_model_fn("openai", "test-model", api_key="offline-placeholder",
                            base_url="https://example.invalid/v1", reasoning_effort=effort)
        tool = {"type": "function", "function": {"name": "offline_echo",
                "description": "Offline fixture", "parameters": {"type": "object", "properties": {}}}}
        assert raw("Offline request", [tool], [])["final"] == "READY"

    assert len(requests) == 2
    for path, body in requests:
        assert path == "/v1/chat/completions"
        assert ("reasoning_effort" in body) == (effort is not None)
        assert body.get("reasoning_effort") == effort
    assert "tools" not in requests[0][1]
    assert len(requests[1][1]["tools"]) == 1
