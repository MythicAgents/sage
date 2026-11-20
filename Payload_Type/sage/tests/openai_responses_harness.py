"""Offline native-chat harness at the OpenAI provider boundary (helper module, not a test file).

A ``_Process`` serves requests through the real ``HeadlessSageChat`` handler, the real ``Model`` built from the
request's resolved kwargs, the real LangGraph graph and checkpointer, the real base-model factory and the
installed ``ChatOpenAI``/OpenAI SDK. Only the provider HTTP transport (``ProviderWire``), Mythic task issuance and
BloodHound are stand-ins. Provider replies are synthetic unless a case says otherwise: they prove Sage's handling
of a given provider output, not model choice.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from unittest.mock import MagicMock

import httpx
import langchain.chat_models
from langchain_core.tools import tool

from ai.langgraph import model as model_module
from ai.langgraph.model import Model
from sage_chat.headless import HeadlessSageChat, build_chat_request

OPERATION = 1
CHANNEL = 47
ENDPOINT = "https://provider.invalid/v1"
KEY = "offline-placeholder-key"
PSEUDONYM = "user_a17f"
REPORTED_404 = {"error": {
    "message": "This model is not supported in the v1/chat/completions endpoint. Use the v1/responses endpoint instead.",
    "type": "invalid_request_error", "param": "model", "code": None,
}}
CALLBACK_ROWS = [{"display_id": 5, "host": "WORKSTATION-5", "user": "offline", "active": True}]


def text_item(text: str, index: int = 0) -> dict:
    return {"id": f"msg_offline_{index}", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def reasoning_item(index: int = 0) -> dict:
    return {"id": f"rs_offline_{index}", "type": "reasoning", "summary": [],
            "encrypted_content": f"opaque-offline-reasoning-{index}"}


def call_item(name: str, args: dict, call_id: str) -> dict:
    return {"id": f"fc_{call_id}", "type": "function_call", "status": "completed", "name": name,
            "arguments": json.dumps(args), "call_id": call_id}


def responses_body(model: str, output: list[dict], index: int) -> dict:
    return {"id": f"resp_offline_{index}", "object": "response", "created_at": 0, "model": model,
            "status": "completed", "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0, "cache_write_tokens_details": {"ttl_12h_tokens": 0, "ttl_30m_tokens": 0}},
                      "output_tokens_details": {"reasoning_tokens": 3}}}


class ProviderWire:
    """Records every provider request; answers Responses requests from a script (synthetic)."""

    def __init__(self, script: list[Callable[[dict], list[dict]] | list[dict]] | None = None):
        self.requests: list[tuple[str, dict]] = []
        self.script = list(script or [])

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if request.url.path.endswith("/chat/completions"):
            if body.get("model") == "gpt-6-cyber":
                return httpx.Response(404, json=REPORTED_404)
            return httpx.Response(200, json={
                "id": "chatcmpl-offline", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Chat Completions answer."},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        step = self.script.pop(0) if self.script else [text_item("Inert final answer.")]
        output = step(body) if callable(step) else step
        return httpx.Response(200, json=responses_body(body["model"], output, len(self.requests)))

    def responses(self) -> list[dict]:
        return [body for path, body in self.requests if path.endswith("/responses")]


def install_wire(monkeypatch, wire: ProviderWire) -> None:
    original = langchain.chat_models.init_chat_model

    def initialize(**kwargs):
        return original(**kwargs,
                        http_client=httpx.Client(transport=httpx.MockTransport(wire.handle)),
                        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)))

    monkeypatch.setattr(model_module, "init_chat_model", initialize)


def route_config(**extra: Any) -> dict:
    return {"provider": "openai", "model": "gpt-6-cyber", "API_ENDPOINT": ENDPOINT, "API_KEY": KEY,
            "mode": "conversation", **extra}


CYBER_RESPONSES = {"SAGE_OPENAI_API": "responses", "SAGE_OPENAI_ACCESS_PROGRAM": "daybreak_red",
                   "SAGE_OPENAI_SAFETY_IDENTIFIER": PSEUDONYM}


def request(prompt: str, request_id: int, config: dict, *, channel: int = CHANNEL):
    return build_chat_request(prompt, channel_id=channel, operation_id=OPERATION, request_id=request_id,
                              config=config)


class Process:
    """One Sage process whose Models use the real factory over the recorded provider wire."""

    def __init__(self):
        self.models: list[Model] = []
        self.list_callbacks_calls = 0
        process = self

        class _Chat(HeadlessSageChat):
            async def _construct_model(self, kwargs):
                model = Model(**kwargs)
                process.models.append(model)
                return model

            async def _initialize_model(self, model):
                model.mythic_client = process.sink()
                model._payload_names = []
                model._rebuild_graph()

            async def _ensure_bloodhound_connected(self, **_kwargs):
                return False

            async def _mythic_read_client(self, request, model=None):
                raise AssertionError("no Mythic read is expected on this path")

        self.chat = _Chat()

    def sink(self):
        process = self
        sink = MagicMock()

        @tool
        async def list_callbacks() -> str:
            """List the operation's callbacks (offline rows)."""
            process.list_callbacks_calls += 1
            return json.dumps(CALLBACK_ROWS)

        sink.get_tools.side_effect = lambda names: [list_callbacks] if "list_callbacks" in names else []
        sink._recon_snapshot_cache = {}
        sink._current_request_task_provenance_notice = lambda *a, **k: ""
        sink.client = None
        return sink

    async def close(self) -> None:
        """Release this process's checkpoint connections (aiosqlite threads keep pytest alive)."""
        for model in self.models:
            conn = getattr(getattr(model, "memory", None), "conn", None)
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass


def texts(chat: HeadlessSageChat) -> list[str]:
    return [str(e.get("content") or e.get("error") or "") for e in chat.emissions]


def terminals(chat: HeadlessSageChat) -> list[dict]:
    return chat.terminal_emissions


def input_texts(body: dict) -> list[str]:
    """Every user/assistant text in a Responses request's input, in order."""
    out: list[str] = []
    for item in body.get("input", []):
        content = item.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return out
