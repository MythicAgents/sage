"""Opt-in process-level accounting at existing control HTTP client seams."""
from __future__ import annotations

from contextvars import ContextVar
import base64
import os
from typing import Any
from uuid import uuid4

import requests

from . import observation_channel, resource_budget


try:
    import aiohttp as _aiohttp
except ModuleNotFoundError:
    _aiohttp = None

_ORIGINAL_REQUESTS_SEND = requests.adapters.HTTPAdapter.send
_ORIGINAL_AIOHTTP_REQUEST = None if _aiohttp is None else _aiohttp.ClientSession._request
_ORIGINAL_AIOHTTP_SEND = None if _aiohttp is None else _aiohttp.ClientRequest.send
_aiohttp_send_credit: ContextVar[tuple[int, str] | None] = ContextVar(
    "sage_resource_aiohttp_send_credit", default=None
)


def _capture_enabled() -> bool:
    return bool(os.environ.get(observation_channel.ENV_NAME, ""))


def _materialized_body(value: Any, *, aiohttp_payload: bool = False) -> dict[str, Any]:
    if value is None:
        raw = b""
    elif isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    elif aiohttp_payload and _aiohttp is not None \
            and isinstance(value, _aiohttp.payload.BytesPayload) \
            and isinstance(getattr(value, "_value", None), bytes):
        raw = value._value
    else:
        return {"body_unavailable": True}
    return {"body_base64": base64.b64encode(raw).decode("ascii")}


def _capture_request(transport: str, method: str, url: str, body: Any, *, aiohttp_payload=False) -> str:
    request_id = uuid4().hex
    observation_channel.emit("control_request", {
        "request_id": request_id, "transport": transport,
        "method": str(method), "url": str(url),
        **_materialized_body(body, aiohttp_payload=aiohttp_payload),
    })
    return request_id


def _capture_response(request_id: str, payload: dict[str, Any]) -> None:
    observation_channel.emit("control_response", {"request_id": request_id, **payload})


def _requests_send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
    ledger = resource_budget.configured_ledger()
    if ledger is None:
        return _ORIGINAL_REQUESTS_SEND(
            self, request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies
        )
    capture = _capture_enabled()
    request_id = _capture_request(
        "requests", request.method, request.url, request.body,
    ) if capture else ""
    try:
        retries = getattr(self, "max_retries", None)
        retry_total = getattr(retries, "total", retries)
        if retry_total not in (0, False):
            raise resource_budget.ResourceBudgetError(
                "metered requests adapters require zero hidden retries"
            )
        reservation = ledger.reserve({"control_requests": 1}, "control-requests-send")
    except resource_budget.ResourceBudgetError as exc:
        if capture:
            _capture_response(request_id, {
                "outcome": "denied_before_dispatch", "exception_type": type(exc).__name__,
            })
        raise
    try:
        response = _ORIGINAL_REQUESTS_SEND(
            self, request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies
        )
    except BaseException as exc:
        if capture:
            _capture_response(request_id, {
                "outcome": "transport_error", "exception_type": type(exc).__name__,
                "reservation_id": reservation,
            })
        raise
    if capture:
        body = {"body_unavailable": True} if stream else _materialized_body(response.content)
        _capture_response(request_id, {
            "outcome": "returned", "status_code": response.status_code, "reservation_id": reservation, **body,
        })
    return response


async def _aiohttp_request(self, method: str, str_or_url: Any, **kwargs):
    ledger = resource_budget.configured_ledger()
    if ledger is None:
        return await _ORIGINAL_AIOHTTP_REQUEST(self, method, str_or_url, **kwargs)
    reservation = ledger.reserve({"control_requests": 1}, "control-aiohttp-request")
    token = _aiohttp_send_credit.set((1, reservation))
    try:
        return await _ORIGINAL_AIOHTTP_REQUEST(self, method, str_or_url, **kwargs)
    finally:
        _aiohttp_send_credit.reset(token)


async def _aiohttp_send(self, conn):
    ledger = resource_budget.configured_ledger()
    if ledger is None:
        return await _ORIGINAL_AIOHTTP_SEND(self, conn)
    capture = _capture_enabled()
    request_id = _capture_request(
        "aiohttp", self.method, self.url, self.body, aiohttp_payload=True,
    ) if capture else ""
    try:
        credit = _aiohttp_send_credit.get()
        if credit is None:
            raise resource_budget.ResourceBudgetError(
                "metered aiohttp send has no enclosing request reservation"
            )
        if credit[0]:
            reservation = credit[1]
            _aiohttp_send_credit.set((credit[0] - 1, credit[1]))
        else:
            reservation = ledger.reserve({"control_requests": 1}, "control-aiohttp-send")
    except resource_budget.ResourceBudgetError as exc:
        if capture:
            _capture_response(request_id, {
                "outcome": "denied_before_dispatch", "exception_type": type(exc).__name__,
            })
        raise
    try:
        response = await _ORIGINAL_AIOHTTP_SEND(self, conn)
    except BaseException as exc:
        if capture:
            _capture_response(request_id, {
                "outcome": "transport_error", "exception_type": type(exc).__name__,
                "reservation_id": reservation,
            })
        raise
    if capture:
        _capture_response(request_id, {
            "outcome": "returned", "status_code": response.status, "body_unavailable": True,
            "reservation_id": reservation,
        })
    return response


def install(*, clients: tuple[str, ...] = ("requests", "aiohttp")) -> bool:
    """Install only explicitly requested client families; both remain the default."""
    if not clients or any(name not in {"requests", "aiohttp"} for name in clients):
        raise resource_budget.ResourceBudgetError("metered client selection is invalid")
    if resource_budget.configured_ledger() is None:
        return False
    if "requests" in clients:
        requests.adapters.HTTPAdapter.send = _requests_send
    if "aiohttp" in clients:
        if _aiohttp is None:
            raise resource_budget.ResourceBudgetError("requested aiohttp meter is unavailable")
        _aiohttp.ClientSession._request = _aiohttp_request
        _aiohttp.ClientRequest.send = _aiohttp_send
    return True
