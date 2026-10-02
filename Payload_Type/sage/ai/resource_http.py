"""Opt-in HTTP transports that charge the shared resource ledger at dispatch."""
from __future__ import annotations

import json
import base64
from collections.abc import Mapping
import os
from typing import Any
from uuid import uuid4

import httpx

from . import observation_channel, resource_budget


PROVIDER_TOKEN_RESERVATION = 32_768
_PROVIDER_TOKEN_RESERVATION_ENV = "SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION"
_PROVIDER_OPERATION = "provider-model-request"
_CONTROL_OPERATION = "control-http-request"
_TOKEN_KEYS = {
    "total_tokens", "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"
}


class ResourceHTTPError(RuntimeError):
    pass


def _provider_token_reservation() -> int:
    configured = os.environ.get(_PROVIDER_TOKEN_RESERVATION_ENV)
    if configured is None:
        return PROVIDER_TOKEN_RESERVATION
    if not configured or any(char not in "0123456789" for char in configured):
        raise ResourceHTTPError(f"{_PROVIDER_TOKEN_RESERVATION_ENV} must be a positive integer")
    try:
        amount = int(configured)
    except ValueError as exc:
        raise ResourceHTTPError(
            f"{_PROVIDER_TOKEN_RESERVATION_ENV} must be a positive integer"
        ) from exc
    if amount <= 0:
        raise ResourceHTTPError(f"{_PROVIDER_TOKEN_RESERVATION_ENV} must be a positive integer")
    return amount


class ProviderResponseBoundaryError(ResourceHTTPError):
    """A retained provider response that must not reach the policy consumer."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.code = "context_length_exceeded" if reason == "per_call_token_overrun" else "usage_unavailable"
        self.termination_cause = (
            "per_request_context_limit" if reason == "per_call_token_overrun"
            else "other_declared_boundary"
        )
        self.termination_subtype = "completed_response_boundary"
        super().__init__(f"provider response withheld: {reason}")


def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _one_usage_total(value: Mapping[str, Any]) -> int | None:
    present = _TOKEN_KEYS.intersection(value)
    if any(not _nonnegative_int(value[key]) for key in present):
        return None
    totals: list[int] = []
    if "total_tokens" in value:
        totals.append(value["total_tokens"])
    for first, second in (("prompt_tokens", "completion_tokens"), ("input_tokens", "output_tokens")):
        count = int(first in value) + int(second in value)
        if count == 1:
            return None
        if count == 2:
            totals.append(value[first] + value[second])
    return totals[0] if totals and len(set(totals)) == 1 else None


def _usage_mappings(value: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        usage = value.get("usage")
        if isinstance(usage, Mapping):
            found.append(usage)
        for nested in value.values():
            found.extend(_usage_mappings(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_usage_mappings(nested))
    return found


def provider_usage_total(body: bytes) -> int | None:
    """Return one unambiguous OpenAI JSON/SSE token total; unknown stays ``None``."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    documents: list[Any] = []
    try:
        documents.append(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if not data or data == "[DONE]":
                continue
            try:
                documents.append(json.loads(data))
            except json.JSONDecodeError:
                return None
    usage = [item for document in documents for item in _usage_mappings(document)]
    if not usage:
        return None
    totals = [_one_usage_total(item) for item in usage]
    return totals[0] if all(item is not None for item in totals) and len(set(totals)) == 1 else None


class _MeteredBase:
    def __init__(self, kind: str, *, ledger: resource_budget.ResourceLedger,
                 operation: str | None = None, allow_non2xx: bool = False) -> None:
        if kind not in {"provider", "control"}:
            raise ResourceHTTPError("HTTP resource kind must be provider or control")
        self.kind = kind
        self.ledger = ledger
        self.operation = operation or (_PROVIDER_OPERATION if kind == "provider" else _CONTROL_OPERATION)
        self.allow_non2xx = allow_non2xx
        self.provider_token_reservation = _provider_token_reservation() if kind == "provider" else None

    def _reserve(self) -> str:
        if self.kind == "provider":
            return self.ledger.reserve(
                {"provider_requests": 1, "provider_tokens": self.provider_token_reservation},
                self.operation,
            )
        return self.ledger.reserve({"control_requests": 1}, self.operation)

    def _settle(self, reservation: str, response: httpx.Response | None, body: bytes | None) -> None:
        if self.kind != "provider":
            return
        actual = (
            provider_usage_total(body)
            if response is not None and 200 <= response.status_code < 300 and body is not None
            else None
        )
        self.ledger.settle_provider(reservation, actual)

    def _capture_enabled(self) -> bool:
        return bool(os.environ.get(observation_channel.ENV_NAME, ""))

    def _capture_request(self, request: httpx.Request, body: bytes) -> str:
        request_id = uuid4().hex
        observation_channel.emit(
            "model_request" if self.kind == "provider" else "control_request",
            {"request_id": request_id, "method": request.method, "url": str(request.url),
             "body_base64": base64.b64encode(body).decode("ascii")},
        )
        return request_id

    def _capture_response(self, request_id: str, payload: Mapping[str, Any]) -> None:
        observation_channel.emit(
            "model_response" if self.kind == "provider" else "control_response",
            {"request_id": request_id, **payload},
        )

    def _consumer_boundary(self, response: httpx.Response, body: bytes) -> None:
        # The approved accounting rule permits an in-flight call to cross its
        # reservation. Preserve and settle that response, but do not let its
        # content authorize a subsequent action. The route's approximate token
        # counter must not replace actual completed-call usage.
        if self.kind != "provider" or not self._capture_enabled():
            return
        if self.allow_non2xx and not 200 <= response.status_code < 300:
            return
        actual = provider_usage_total(body) if 200 <= response.status_code < 300 else None
        if actual is None:
            raise ProviderResponseBoundaryError("usage_unavailable")
        if actual > self.provider_token_reservation:
            raise ProviderResponseBoundaryError("per_call_token_overrun")


class MeteredHTTPTransport(_MeteredBase, httpx.BaseTransport):
    """Sync HTTPX transport charging every transport invocation."""

    def __init__(self, kind: str, *, ledger: resource_budget.ResourceLedger,
                 transport: httpx.BaseTransport | None = None, operation: str | None = None,
                 allow_non2xx: bool = False) -> None:
        _MeteredBase.__init__(self, kind, ledger=ledger, operation=operation, allow_non2xx=allow_non2xx)
        self.transport = transport or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        capture = self._capture_enabled()
        request_id = self._capture_request(request, request.read()) if capture else ""
        try:
            reservation = self._reserve()
        except resource_budget.ResourceBudgetError as exc:
            if capture:
                self._capture_response(request_id, {
                    "outcome": "denied_before_dispatch", "exception_type": type(exc).__name__,
                })
            raise
        try:
            response = self.transport.handle_request(request)
            body = response.read()
        except BaseException as exc:
            self._settle(reservation, None, None)
            if capture:
                self._capture_response(request_id, {
                    "outcome": "transport_error", "exception_type": type(exc).__name__,
                    "reservation_id": reservation,
                })
            raise
        self._settle(reservation, response, body)
        if capture:
            self._capture_response(request_id, {
                "status_code": response.status_code,
                "body_base64": base64.b64encode(body).decode("ascii"),
                "outcome": "returned", "reservation_id": reservation,
            })
        self._consumer_boundary(response, body)
        return response

    def close(self) -> None:
        self.transport.close()


class MeteredAsyncHTTPTransport(_MeteredBase, httpx.AsyncBaseTransport):
    """Async HTTPX transport charging every transport invocation."""

    def __init__(self, kind: str, *, ledger: resource_budget.ResourceLedger,
                 transport: httpx.AsyncBaseTransport | None = None, operation: str | None = None,
                 allow_non2xx: bool = False) -> None:
        _MeteredBase.__init__(self, kind, ledger=ledger, operation=operation, allow_non2xx=allow_non2xx)
        self.transport = transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        capture = self._capture_enabled()
        request_id = self._capture_request(request, await request.aread()) if capture else ""
        try:
            reservation = self._reserve()
        except resource_budget.ResourceBudgetError as exc:
            if capture:
                self._capture_response(request_id, {
                    "outcome": "denied_before_dispatch", "exception_type": type(exc).__name__,
                })
            raise
        try:
            response = await self.transport.handle_async_request(request)
            body = await response.aread()
        except BaseException as exc:
            self._settle(reservation, None, None)
            if capture:
                self._capture_response(request_id, {
                    "outcome": "transport_error", "exception_type": type(exc).__name__,
                    "reservation_id": reservation,
                })
            raise
        self._settle(reservation, response, body)
        if capture:
            self._capture_response(request_id, {
                "status_code": response.status_code,
                "body_base64": base64.b64encode(body).decode("ascii"),
                "outcome": "returned", "reservation_id": reservation,
            })
        self._consumer_boundary(response, body)
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()
