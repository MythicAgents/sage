"""Replay recorded provider bodies through Sage's real model factory, SDK and adapter (helper, not a test file).

``build_model`` builds the chat model with the real ``Model._get_base_chat_model`` for a row's route and replaces
only the network transport with one that answers every request with the row's recorded body:

* ``default``: the HTTP client the adapter builds when Sage injects none (the container deployment). The client
  object, its type and its configuration are the adapter's; only ``client._transport`` is swapped.
* ``injected``: a provider retry policy is set, so Sage hands the SDK its own classic-httpx client over
  ``RetryingHTTPTransport``; only that transport's inner network transport is swapped.

``message_view``, ``history_view`` and ``error_view`` project an outcome onto what Sage's consumers read. The fixture
``fixtures/model_input_boundaries/provider_wire_round_trips.json`` freezes those projections as the pre-upgrade
adapter produced them; ``skills/sage-focused-capability-tests/scripts/freeze_provider_wire.py`` writes it.
The replay prompt and tool result below are synthetic; the provider bodies are recorded.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any, Callable

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from ai.langgraph.model import (
    Model,
    _apply_bedrock_patch,
    _message_content_as_text,
    _sanitize_model_messages,
    _sole_provider_text,
    _tool_call_envelope_error,
)
from ai.langgraph.operator_error import (
    operator_error_text,
    provider_configuration_notice,
    provider_configuration_rejection,
    unproven_model_notice,
)

REPLAY_PROMPT = "Synthetic replay prompt: list the callbacks."
TOOL_RESULT = "Synthetic tool result for the replay."
API_KEY = "offline-placeholder-key"
PATHS = ("default", "injected")

# response_metadata keys Sage's consumers read (model.py model-name check, finish/stop reason checks,
# sage_chat/service.py model identity on restore).
READ_METADATA_KEYS = ("model_name", "model", "model_id", "finish_reason", "stop_reason", "status", "id", "refusal")


def base_url(row_id: str, path: str) -> str:
    """One endpoint per row and path, so the adapter's per-endpoint client cache never hands back a swapped client."""
    return f"https://replay-{row_id.replace('_', '-')}-{path}.invalid/v1"


def configurable(row: dict, path: str, *, endpoint: str | None = None) -> dict[str, Any]:
    route = row["route"]
    cfg: dict[str, Any] = {"api_key": API_KEY, "base_url": endpoint or base_url(row["id"], path),
                           "openai_api": route["api"]}
    if route.get("reasoning_effort"):
        cfg["reasoning_effort"] = route["reasoning_effort"]
    if path == "injected":
        cfg["provider_retry_policy"] = "transient-v1"
    return cfg


def model_instance(row: dict, path: str, *, endpoint: str | None = None) -> Model:
    """A ``Model`` carrying only what ``_get_base_chat_model`` reads, for ``row``'s route on ``path``."""
    instance = Model.__new__(Model)
    instance.provider = "openai"
    instance.model = row["model"]
    instance.config = {"configurable": configurable(row, path, endpoint=endpoint)}
    instance._eval_arm_deadline_epoch = None
    instance._provider_retry_state = None
    instance._controller_runtime_telemetry = {}
    return instance


def _library_for(client: Any):
    try:
        import httpx2
    except ImportError:  # pragma: no cover - httpx2 ships with the runtime set
        return httpx
    return httpx2 if isinstance(client, httpx2.Client | httpx2.AsyncClient) else httpx


def build_model(row: dict, path: str, *, body: bytes | None = None, status: int | None = None):
    """``(llm, sync_client, requests)`` for one row on one client path; ``requests`` records each request path."""
    if path not in PATHS:
        raise ValueError(path)
    payload = body if body is not None else json.dumps(row["body"]).encode()
    code = status if status is not None else row["status"]
    requests: list[str] = []

    def handler(library) -> Callable[[Any], Any]:
        def answer(request):
            requests.append(request.url.path)
            return library.Response(code, content=payload, headers={"content-type": "application/json"})
        return answer

    llm = model_instance(row, path)._get_base_chat_model()
    sync_client = llm.root_client._client
    async_client = llm.root_async_client._client
    if path == "default":
        for client in (sync_client, async_client):
            client._transport = _library_for(client).MockTransport(handler(_library_for(client)))
    else:
        sync_client._transport.transport = httpx.MockTransport(handler(httpx))
        async_client._transport.transport = httpx.MockTransport(handler(httpx))
    return llm, sync_client, requests


def history(message: AIMessage) -> list[BaseMessage]:
    messages: list[BaseMessage] = [HumanMessage(REPLAY_PROMPT), message]
    messages.extend(ToolMessage(content=TOOL_RESULT, tool_call_id=call["id"]) for call in message.tool_calls)
    return messages


def _render(message: BaseMessage) -> str:
    renderer = Model.__new__(Model)
    renderer.channel_id = 1
    renderer.verbose = True
    renderer.is_interactive = True
    return renderer._format_message_for_streaming(message, agent_name=message.name or "AI")


def message_view(message: AIMessage) -> dict[str, Any]:
    usage = message.usage_metadata or {}
    return {
        "type": type(message).__name__,
        "id": message.id,
        "name": message.name,
        "tool_calls": [{"name": c["name"], "args": c["args"], "id": c["id"]} for c in message.tool_calls],
        "invalid_tool_calls": [{"name": c.get("name"), "args": c.get("args"), "id": c.get("id")}
                               for c in message.invalid_tool_calls],
        "content_types": ([block.get("type") if isinstance(block, dict) else type(block).__name__
                           for block in message.content] if isinstance(message.content, list) else "str"),
        "text": _message_content_as_text(message.content),
        "sole_text": _sole_provider_text(message.content),
        "envelope_error": _tool_call_envelope_error(message),
        "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                  "total_tokens": usage.get("total_tokens"),
                  "reasoning_tokens": (usage.get("output_token_details") or {}).get("reasoning")},
        "metadata": {key: message.response_metadata[key] for key in READ_METADATA_KEYS
                     if key in message.response_metadata},
        "rendered": _render(message),
    }


def _brief(message: BaseMessage) -> dict[str, Any]:
    return {"type": type(message).__name__, "content": message.content,
            "tool_calls": [{"name": c["name"], "args": c["args"], "id": c["id"]}
                           for c in getattr(message, "tool_calls", [])],
            "tool_call_id": getattr(message, "tool_call_id", None)}


def history_view(messages: list[BaseMessage], *, configured_model: str) -> dict[str, Any]:
    """What a replayed history becomes at each outgoing boundary: Responses input, Chat Completions messages (with
    Sage's empty-content patch, which wraps the adapter's module-level converter) and Sage's history sanitizer."""
    import langchain_openai.chat_models.base as adapter

    _apply_bedrock_patch()
    sanitized, changed = _sanitize_model_messages(list(messages), configured_model=configured_model)
    return {
        "responses_input": adapter._construct_responses_api_input(messages, store=False),
        "chat_completions": [adapter._convert_message_to_dict(m) for m in messages],
        "sanitized": [_brief(m) for m in sanitized],
        "sanitized_changed": changed,
    }


def checkpoint_bytes(messages: list[BaseMessage]) -> dict[str, str]:
    """The history as LangGraph's checkpoint serializer stores it."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    kind, data = JsonPlusSerializer().dumps_typed(messages)
    return {"type": kind, "base64": base64.b64encode(data).decode("ascii")}


def restore_checkpoint(frozen: dict[str, str]) -> list[BaseMessage]:
    """Load a frozen history the way the deployed image does: the image and ``.env`` set
    ``LANGGRAPH_STRICT_MSGPACK=true``, which is ``allowed_msgpack_modules=None`` (LangGraph's safe-type allowlist)."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    strict = JsonPlusSerializer(allowed_msgpack_modules=None)
    return strict.loads_typed((frozen["type"], base64.b64decode(frozen["base64"])))


def notice_profile(row: dict) -> SimpleNamespace:
    route = row["route"]
    return SimpleNamespace(provider="openai", model=row["model"], openai_api=route["api"],
                           openai_access_program="", openai_safety_identifier="",
                           reasoning_effort=route.get("reasoning_effort") or "",
                           api_endpoint="https://provider.invalid/v1", source_for=lambda _name: "chat configuration")


def error_view(error: BaseException, row: dict) -> dict[str, Any]:
    import openai

    rejection = provider_configuration_rejection(error)
    profile = notice_profile(row)
    return {
        "sdk_types": [cls.__name__ for cls in type(error).__mro__
                      if cls.__module__.split(".")[0] == "openai" and getattr(openai, cls.__name__, None) is cls],
        "status_code": getattr(error, "status_code", None),
        "code": getattr(error, "code", None),
        "param": getattr(error, "param", None),
        "message": getattr(error, "message", None),
        "body": getattr(error, "body", None),
        "configuration_rejection": rejection is not None,
        "operator_text": operator_error_text(error),
        "configuration_notice": provider_configuration_notice(error, profile),
        "unproven_notice": unproven_model_notice(error, profile),
    }


def outcome(llm) -> AIMessage | BaseException:
    try:
        return llm.invoke(REPLAY_PROMPT)
    except Exception as exc:  # noqa: BLE001 - the error itself is the outcome under test
        return exc

