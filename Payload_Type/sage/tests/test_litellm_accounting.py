"""Offline contract tests for the eval-only LiteLLM spend transport."""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.hillclimb.litellm_accounting import (  # noqa: E402
    SpendLogConfig,
    SpendLogFetchError,
    SpendLogResponse,
    fetch_spend_logs,
)
from ai.hillclimb import litellm_accounting as accounting  # noqa: E402


SECRET = "do-not-leak-this-secret"


def config(**overrides):
    values = {
        "base_url": "https://proxy.example.test",
        "api_key_env": "LITELLM_EVAL_KEY",
        "start_date": "2026-01-01 00:00:00",
        "end_date": "2026-01-02 00:00:00",
    }
    values.update(overrides)
    return SpendLogConfig(**values)


def test_paginate_until_empty_ignoring_capped_metadata():
    requests = []
    replies = iter([
        {"data": [{"id": 1}], "total": 10000, "total_pages": 1, "total_is_capped": True},
        {"data": [{"id": 2}], "total": 10000, "total_pages": 1, "total_is_capped": True},
        {"data": [], "total": 10000, "total_pages": 1, "total_is_capped": True},
    ])

    def transport(request):
        requests.append(request)
        return SpendLogResponse(200, next(replies))

    result = fetch_spend_logs(config(api_key_hash="a" * 64, model="chosen-model"), transport=transport,
                              environ={"LITELLM_EVAL_KEY": SECRET})
    assert [row["id"] for row in result.rows] == [1, 2]
    assert result.pages == 3 and result.completed is True
    assert [parse_qs(urlsplit(item.url).query)["page"] for item in requests] == [["1"], ["2"], ["3"]]
    assert "api_key=" + "a" * 64 in requests[0].url
    assert SECRET not in requests[0].url


def test_empty_first_page_completes():
    result = fetch_spend_logs(config(), transport=lambda _: SpendLogResponse(200, {
        "data": [], "total": 0, "total_pages": 0, "total_is_capped": False,
    }),
                              environ={"LITELLM_EVAL_KEY": SECRET})
    assert result.rows == () and result.pages == 1 and result.bounds["start_date"].startswith("2026")


def test_older_v2_envelope_without_capped_count_metadata_is_accepted():
    result = fetch_spend_logs(config(), transport=lambda request: SpendLogResponse(200, (
        {"data": [{"id": 1}], "total": 1, "page": 1, "page_size": 100}
        if "page=1" in request.url
        else {"data": [], "total": 1, "page": 2, "page_size": 100}
    )), environ={"LITELLM_EVAL_KEY": SECRET})
    assert [row["id"] for row in result.rows] == [1]
    assert result.pages == 2 and result.completed is True


@pytest.mark.parametrize("reply", [SpendLogResponse(401, {}), SpendLogResponse(302, {})])
def test_http_and_redirect_fail_without_secret_leak(reply):
    with pytest.raises(SpendLogFetchError) as error:
        fetch_spend_logs(config(), transport=lambda _: reply, environ={"LITELLM_EVAL_KEY": SECRET})
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("payload", [{}, {"data": "bad"}, {"data": ["not-a-row"]}])
def test_malformed_payload_or_rows_fail_closed(payload):
    with pytest.raises(SpendLogFetchError):
        fetch_spend_logs(config(), transport=lambda _: SpendLogResponse(200, payload),
                         environ={"LITELLM_EVAL_KEY": SECRET})


def test_timeout_and_transport_exception_are_sanitized():
    def timeout(_request):
        raise TimeoutError(SECRET)

    with pytest.raises(SpendLogFetchError) as error:
        fetch_spend_logs(config(), transport=timeout, environ={"LITELLM_EVAL_KEY": SECRET})
    assert SECRET not in str(error.value)


def test_injected_spend_error_cannot_bypass_sanitization():
    def unsafe_transport(_request):
        raise SpendLogFetchError(SECRET)

    with pytest.raises(SpendLogFetchError) as error:
        fetch_spend_logs(config(), transport=unsafe_transport, environ={"LITELLM_EVAL_KEY": SECRET})
    assert SECRET not in str(error.value)


def test_stdlib_transport_builds_https_opener_and_rejects_oversize_body(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return 200

        def read(self, limit):
            assert limit == accounting._MAX_RESPONSE_BYTES + 1
            return b"x" * limit

    def fake_open(opener, _request, data=None, timeout=0):
        assert data is None and timeout == 10.0
        https_handlers = [handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPSHandler)]
        assert len(https_handlers) == 1
        return Response()

    # Leave ``build_opener`` real: intercept only the assembled opener's final
    # transport call, so this remains an offline test.
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    with pytest.raises(SpendLogFetchError, match="too large") as error:
        fetch_spend_logs(config(), environ={"LITELLM_EVAL_KEY": SECRET})
    assert SECRET not in str(error.value)


def test_max_pages_exhaustion_returns_no_partial_snapshot():
    with pytest.raises(SpendLogFetchError, match="page limit"):
        fetch_spend_logs(config(max_pages=2), transport=lambda _: SpendLogResponse(200, {
            "data": [{"id": 1}], "total": 10000, "total_pages": 1, "total_is_capped": True,
        }),
                         environ={"LITELLM_EVAL_KEY": SECRET})


@pytest.mark.parametrize("base_url", ["http://proxy.test", "https://u:p@proxy.test", "https://proxy.test/x",
                                        "https://proxy.test/?q=1", "https://proxy.test/#part"])
def test_non_origin_https_base_is_rejected_before_transport(base_url):
    invoked = False

    def transport(_request):
        nonlocal invoked
        invoked = True
        return SpendLogResponse(200, {"data": [], "total": 0, "total_pages": 0, "total_is_capped": False})

    with pytest.raises(SpendLogFetchError):
        fetch_spend_logs(config(base_url=base_url), transport=transport, environ={"LITELLM_EVAL_KEY": SECRET})
    assert invoked is False


def test_missing_named_credential_does_not_invoke_transport():
    with pytest.raises(SpendLogFetchError, match="credential"):
        fetch_spend_logs(config(), transport=lambda _: (_ for _ in ()).throw(AssertionError("network")), environ={})


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_timeout_is_rejected_before_transport(timeout):
    with pytest.raises(SpendLogFetchError, match="timeout"):
        fetch_spend_logs(config(timeout_seconds=timeout), transport=lambda _: (_ for _ in ()).throw(AssertionError("network")),
                         environ={"LITELLM_EVAL_KEY": SECRET})
