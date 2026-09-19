"""Read-only LiteLLM spend-log retrieval for evaluation accounting.

This module intentionally has no import-time configuration or network effect.  Callers must
provide an HTTPS LiteLLM proxy origin (without ``/v1``); only the fixed spend-log route is used.
Rows are returned to the caller in memory and are never written by this transport.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import ssl
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener


_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FIXED_ROUTE = "/spend/logs/v2"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class SpendLogFetchError(RuntimeError):
    """A sanitized failure while obtaining evaluation-only spend records."""


@dataclass(frozen=True)
class SpendLogConfig:
    base_url: str
    api_key_env: str
    start_date: str
    end_date: str
    ca_bundle: str | None = None
    page_size: int = 1000
    max_pages: int = 20
    timeout_seconds: float = 10.0
    api_key_hash: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class SpendLogRequest:
    """A fully-bound request supplied to an injected, offline transport."""

    url: str
    headers: Mapping[str, str]
    timeout_seconds: float
    ca_bundle: str | None


@dataclass(frozen=True)
class SpendLogResponse:
    status_code: int
    payload: object


@dataclass(frozen=True)
class SpendLogFetch:
    """An in-memory snapshot; ``completed`` is true only after an empty page."""

    rows: tuple[Mapping[str, Any], ...]
    pages: int
    completed: bool
    bounds: Mapping[str, str]


Transport = Callable[[SpendLogRequest], SpendLogResponse]


def _fail(message: str) -> None:
    raise SpendLogFetchError(message) from None


def _validated_origin(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        _fail("LiteLLM spend endpoint configuration is invalid")
    parsed = urlsplit(base_url.strip())
    # Deliberately permit only an origin: this prevents a proxy /v1 endpoint or
    # arbitrary supplied route from becoming a credential-bearing request.
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        _fail("LiteLLM spend endpoint configuration is invalid")
    return urlunsplit(("https", parsed.netloc, "", "", ""))


def _validated_datetime(value: object) -> str:
    if not isinstance(value, str):
        _fail("LiteLLM spend time bounds are invalid")
    try:
        parsed = datetime.strptime(value, _DATE_FORMAT)
    except (TypeError, ValueError):
        _fail("LiteLLM spend time bounds are invalid")
    # strptime accepts a few non-canonical forms, so require the documented wire format.
    if parsed.strftime(_DATE_FORMAT) != value:
        _fail("LiteLLM spend time bounds are invalid")
    return value


def _validate_config(config: object) -> tuple[str, str, str, Mapping[str, str]]:
    if not isinstance(config, SpendLogConfig):
        _fail("LiteLLM spend configuration is invalid")
    origin = _validated_origin(config.base_url)
    if not isinstance(config.api_key_env, str) or not _ENV_NAME.fullmatch(config.api_key_env):
        _fail("LiteLLM spend credential configuration is invalid")
    start = _validated_datetime(config.start_date)
    end = _validated_datetime(config.end_date)
    if datetime.strptime(end, _DATE_FORMAT) <= datetime.strptime(start, _DATE_FORMAT):
        _fail("LiteLLM spend time bounds are invalid")
    if type(config.page_size) is not int or not 1 <= config.page_size <= 1000:
        _fail("LiteLLM spend page size is invalid")
    if type(config.max_pages) is not int or config.max_pages < 1:
        _fail("LiteLLM spend page limit is invalid")
    if (
        type(config.timeout_seconds) not in (int, float)
        or isinstance(config.timeout_seconds, bool)
        or config.timeout_seconds <= 0
        or not math.isfinite(config.timeout_seconds)
    ):
        _fail("LiteLLM spend timeout is invalid")
    if config.ca_bundle is not None and (not isinstance(config.ca_bundle, str) or not config.ca_bundle):
        _fail("LiteLLM spend CA configuration is invalid")
    if config.api_key_hash is not None and (
        not isinstance(config.api_key_hash, str) or not _SHA256.fullmatch(config.api_key_hash)
    ):
        _fail("LiteLLM spend API-key hash filter is invalid")
    if config.model is not None and (not isinstance(config.model, str) or not config.model.strip()):
        _fail("LiteLLM spend model filter is invalid")
    bounds = {"start_date": start, "end_date": end}
    if config.api_key_hash is not None:
        bounds["api_key_hash"] = config.api_key_hash.lower()
    if config.model is not None:
        bounds["model"] = config.model
    return origin, start, end, MappingProxyType(bounds)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _stdlib_transport(request: SpendLogRequest) -> SpendLogResponse:
    """Perform one HTTPS GET without following redirects."""
    try:
        context = ssl.create_default_context(cafile=request.ca_bundle)
        opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))
        wire_request = Request(request.url, headers=dict(request.headers), method="GET")
        with opener.open(wire_request, timeout=request.timeout_seconds) as response:
            status = response.getcode()
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                _fail("LiteLLM spend endpoint response was too large")
            payload = json.loads(body.decode("utf-8"))
        return SpendLogResponse(status_code=status, payload=payload)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            _fail("LiteLLM spend endpoint redirect was rejected")
        _fail(f"LiteLLM spend endpoint returned HTTP {exc.code}")
    except (TimeoutError, URLError, OSError, ssl.SSLError):
        _fail("LiteLLM spend endpoint was unavailable")
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("LiteLLM spend endpoint returned invalid JSON")


def _response_from(
    transport: Transport,
    request: SpendLogRequest,
    *,
    sanitize_transport_errors: bool,
) -> SpendLogResponse:
    try:
        response = transport(request)
    except SpendLogFetchError:
        if not sanitize_transport_errors:
            raise
        _fail("LiteLLM spend transport failed")
    except Exception:
        # ``transport`` is injectable.  It may carry arbitrary exception text,
        # including a provider key or a response body, so no exception crosses
        # this public boundary unchanged.
        _fail("LiteLLM spend transport failed")
    if not isinstance(response, SpendLogResponse) or type(response.status_code) is not int:
        _fail("LiteLLM spend transport returned an invalid response")
    if 300 <= response.status_code < 400:
        _fail("LiteLLM spend endpoint redirect was rejected")
    if response.status_code != 200:
        _fail(f"LiteLLM spend endpoint returned HTTP {response.status_code}")
    return response


def _validated_data(payload: object) -> list[object]:
    """Validate data and any present optional pagination metadata before retention."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        _fail("LiteLLM spend endpoint returned an invalid response")
    total = payload.get("total")
    total_pages = payload.get("total_pages")
    capped = payload.get("total_is_capped")
    if (
        (total is not None and (type(total) is not int or total < 0))
        or (total_pages is not None and (type(total_pages) is not int or total_pages < 0))
        or (capped is not None and type(capped) is not bool)
    ):
        _fail("LiteLLM spend endpoint returned an invalid response")
    return payload["data"]


def fetch_spend_logs(
    config: SpendLogConfig,
    *,
    transport: Transport | None = None,
    environ: Mapping[str, str] | None = None,
) -> SpendLogFetch:
    """Fetch complete pages until LiteLLM returns an empty ``data`` list.

    The provider's advertised totals may be capped, therefore ``total_pages`` and
    ``total_is_capped`` are intentionally ignored.  No request occurs before all
    configuration is validated and the named operator environment value is present.
    """
    origin, start, end, bounds = _validate_config(config)
    environment = os.environ if environ is None else environ
    api_key = environment.get(config.api_key_env)
    if not isinstance(api_key, str) or not api_key:
        _fail("LiteLLM spend credential is unavailable")

    send = _stdlib_transport if transport is None else transport
    rows: list[Mapping[str, Any]] = []
    for page in range(1, config.max_pages + 1):
        query: dict[str, object] = {
            "start_date": start,
            "end_date": end,
            "page": page,
            "page_size": config.page_size,
        }
        if config.api_key_hash is not None:
            query["api_key"] = config.api_key_hash.lower()
        if config.model is not None:
            query["model"] = config.model
        request = SpendLogRequest(
            url=origin + _FIXED_ROUTE + "?" + urlencode(query),
            headers=MappingProxyType({"Authorization": "Bearer " + api_key, "Accept": "application/json"}),
            timeout_seconds=float(config.timeout_seconds),
            ca_bundle=config.ca_bundle,
        )
        response = _response_from(send, request, sanitize_transport_errors=transport is not None)
        data = _validated_data(response.payload)
        if not data:
            return SpendLogFetch(tuple(rows), page, True, bounds)
        if any(not isinstance(row, Mapping) for row in data):
            _fail("LiteLLM spend endpoint returned malformed rows")
        rows.extend(dict(row) for row in data)
    _fail("LiteLLM spend page limit was exhausted before completion")
