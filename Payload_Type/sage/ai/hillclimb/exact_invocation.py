"""Shared fail-closed route, request, transport, and stream mechanics for Responses evaluators."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sage_chat.config import resolve_environment_llm_profile


ENVIRONMENT_PROFILE_RESOLVER = resolve_environment_llm_profile


class ExactInvocationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResponsesRoute:
    provider: str
    model: str
    api_endpoint: str
    api_key: str
    reasoning_effort: str = "high"
    service_tier: str = "default"
    store: bool = False
    tools: tuple[Any, ...] = ()
    max_retries: int = 0
    timeout_seconds: float = 600.0
    sdk_method: str = "responses.create"
    stream: bool = True

    def __post_init__(self) -> None:
        required = (self.provider, self.model, self.api_endpoint, self.api_key)
        if any(not str(value or "").strip() for value in required):
            raise ExactInvocationError("provider, model, endpoint, and API key are required")
        if self.sdk_method != "responses.create" or self.stream is not True:
            raise ExactInvocationError("the exact invocation requires streaming responses.create")
        if self.max_retries != 0:
            raise ExactInvocationError("provider retries must be zero")

    def safe_identity(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "endpoint_sha256": hashlib.sha256(self.api_endpoint.encode("utf-8")).hexdigest(),
            "reasoning_effort": self.reasoning_effort,
            "service_tier": self.service_tier,
            "store": self.store,
            "tool_count": len(self.tools),
            "max_retries": self.max_retries,
            "sdk_method": self.sdk_method,
            "stream": self.stream,
        }


@dataclass(frozen=True)
class ResponsesRequest:
    instructions: str
    input: Any
    text: Mapping[str, Any] | None = None
    max_output_tokens: int | None = None
    prompt_cache_key: str | None = None
    extra_headers: Mapping[str, str] | None = None
    extra_body: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.instructions.strip():
            raise ExactInvocationError("request instructions are required")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ExactInvocationError("max_output_tokens must be positive when present")


@dataclass(frozen=True)
class CollectedResponse:
    event_type_counts: Mapping[str, int]
    final_response: Any
    output_text: str
    captured_delta_count: int


@dataclass(frozen=True)
class PreflightReceipt:
    client: Any
    route: ResponsesRoute
    request_projection: tuple[tuple[str, Any], ...]
    request_builder: Any
    stream_collector: Any
    safe_observation: Mapping[str, Any]


def resolve_responses_route(
    environ: Mapping[str, str] | None = None,
    **route_options: Any,
) -> ResponsesRoute:
    """Build the immutable evaluator route from Sage's shipping environment resolver."""
    profile = ENVIRONMENT_PROFILE_RESOLVER(environ)
    return ResponsesRoute(
        provider=profile.provider,
        model=profile.model,
        api_endpoint=profile.api_endpoint,
        api_key=profile.api_key,
        **route_options,
    )


def build_responses_request(
    route: ResponsesRoute,
    request: ResponsesRequest,
) -> dict[str, Any]:
    """Build the only supported Responses request shape."""
    kwargs: dict[str, Any] = {
        "model": route.model,
        "instructions": request.instructions,
        "input": request.input,
        "reasoning": {"effort": route.reasoning_effort},
        "service_tier": route.service_tier,
        "store": route.store,
        "stream": route.stream,
        "tools": list(route.tools),
    }
    optional = {
        "text": dict(request.text) if request.text is not None else None,
        "max_output_tokens": request.max_output_tokens,
        "prompt_cache_key": request.prompt_cache_key,
        "extra_headers": dict(request.extra_headers) if request.extra_headers is not None else None,
        "extra_body": dict(request.extra_body) if request.extra_body is not None else None,
    }
    kwargs.update({key: value for key, value in optional.items() if value is not None})
    return kwargs


async def collect_responses_stream(stream: Any) -> CollectedResponse:
    """Collect ordered text deltas and require exactly one completed terminal response."""
    event_types: Counter[str] = Counter()
    terminals: list[tuple[str, Any]] = []
    deltas: list[str] = []
    async for event in stream:
        event_type = str(getattr(event, "type", type(event).__name__))
        event_types[event_type] += 1
        if event_type == "response.output_text.delta":
            delta = getattr(event, "delta", None)
            if not isinstance(delta, str):
                raise ExactInvocationError("response.output_text.delta lacked a string delta")
            deltas.append(delta)
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            terminals.append((event_type, getattr(event, "response", None)))
    if len(terminals) != 1 or terminals[0][1] is None:
        raise ExactInvocationError("stream did not contain exactly one terminal response")
    terminal_type, final_response = terminals[0]
    status = str(getattr(final_response, "status", "") or "").strip().casefold()
    if terminal_type != "response.completed" or status not in {"", "completed"}:
        raise ExactInvocationError(f"provider stream terminated as {terminal_type}")
    terminal_text = getattr(final_response, "output_text", None)
    output_text = terminal_text if isinstance(terminal_text, str) and terminal_text.strip() else "".join(deltas)
    if not output_text.strip():
        raise ExactInvocationError("provider stream completed without nonempty captured output text")
    return CollectedResponse(
        event_type_counts=dict(sorted(event_types.items())),
        final_response=final_response,
        output_text=output_text,
        captured_delta_count=event_types.get("response.output_text.delta", 0),
    )


REQUEST_BUILDER = build_responses_request
STREAM_COLLECTOR = collect_responses_stream


def make_responses_client(
    route: ResponsesRoute,
    *,
    default_headers: Mapping[str, str] | None = None,
) -> Any:
    """Construct the canonical zero-retry client from the resolved route."""
    if route.provider.strip().casefold() != "openai":
        raise ExactInvocationError("Responses transport currently requires provider=openai")
    import httpx
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=route.api_key,
        base_url=route.api_endpoint,
        default_headers=dict(default_headers or {}),
        timeout=httpx.Timeout(timeout=route.timeout_seconds, connect=min(10.0, route.timeout_seconds)),
        max_retries=route.max_retries,
    )


def assert_canonical_invocation_symbols(builder: Any, collector: Any) -> None:
    if builder is not REQUEST_BUILDER or collector is not STREAM_COLLECTOR:
        raise ExactInvocationError("smoke and production must import the canonical invocation symbols")


def _request_projection(route: ResponsesRoute, request: ResponsesRequest) -> tuple[tuple[str, Any], ...]:
    built = REQUEST_BUILDER(route, request)
    return tuple(sorted(
        (key, "present" if key == "max_output_tokens" else json.dumps(value, sort_keys=True, default=str))
        for key, value in built.items()
        if key not in {"input", "instructions", "prompt_cache_key", "extra_headers", "extra_body"}
    ))


async def _execute(client: Any, route: ResponsesRoute, request: ResponsesRequest) -> CollectedResponse:
    if route.sdk_method != "responses.create":
        raise ExactInvocationError("unsupported SDK method")
    stream = await client.responses.create(**REQUEST_BUILDER(route, request))
    return await STREAM_COLLECTOR(stream)


async def run_exact_route_smoke(
    client: Any,
    route: ResponsesRoute,
    smoke_request: ResponsesRequest,
) -> PreflightReceipt:
    """Run the one bounded transport probe and return an in-process admission receipt."""
    assert_canonical_invocation_symbols(REQUEST_BUILDER, STREAM_COLLECTOR)
    collected = await _execute(client, route, smoke_request)
    response = collected.final_response
    usage = getattr(response, "usage", None)
    usage_dict = usage.model_dump(mode="json") if hasattr(usage, "model_dump") else usage
    return PreflightReceipt(
        client=client,
        route=route,
        request_projection=_request_projection(route, smoke_request),
        request_builder=REQUEST_BUILDER,
        stream_collector=STREAM_COLLECTOR,
        safe_observation={
            "route": route.safe_identity(),
            "response_status": str(getattr(response, "status", "") or ""),
            "observed_model": str(getattr(response, "model", "") or ""),
            "observed_service_tier": str(getattr(response, "service_tier", "") or ""),
            "usage": usage_dict if isinstance(usage_dict, Mapping) else {},
        },
    )


async def run_gated_responses(
    client: Any,
    route: ResponsesRoute,
    request: ResponsesRequest,
    *,
    receipt: PreflightReceipt,
    result_path: str | Path,
) -> CollectedResponse:
    """Execute production only after the exact in-process smoke symbols and route passed."""
    path = Path(result_path)
    if path.exists():
        raise ExactInvocationError(f"refusing to overwrite existing result: {path}")
    assert_canonical_invocation_symbols(receipt.request_builder, receipt.stream_collector)
    if receipt.client is not client:
        raise ExactInvocationError("production did not receive the smoke probe's authenticated client")
    if receipt.route is not route:
        raise ExactInvocationError("production did not receive the smoke probe's route object")
    if receipt.request_projection != _request_projection(route, request):
        raise ExactInvocationError("smoke and production request configuration differ")
    return await _execute(client, route, request)
