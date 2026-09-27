from __future__ import annotations

import asyncio
import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor
import threading

import httpx
import pytest

from ai import resource_budget, resource_http
from ai.observation_channel import ObservationChannelError


def _ledger(tmp_path, **limits):
    tmp_path.mkdir(parents=True, exist_ok=True)
    resources = {
        "provider_requests": {"limit": limits.get("provider_requests", 8), "cleanup_reserve": 0},
        "provider_tokens": {"limit": limits.get("provider_tokens", 200000), "cleanup_reserve": 0},
        "control_requests": {"limit": limits.get("control_requests", 8), "cleanup_reserve": 0},
    }
    config = tmp_path / "config.json"
    ledger = tmp_path / "ledger.jsonl"
    config.write_text(json.dumps({"schema": resource_budget.CONFIG_SCHEMA, "resources": resources,
                                  "calendar_seconds": 1000, "cleanup_seconds": 1}))
    return resource_budget.ResourceLedger(config, ledger), ledger


def test_sync_provider_reserves_before_request_settles_usage_and_preserves_bytes(tmp_path):
    ledger, _ = _ledger(tmp_path)
    body = b'{"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}'

    def handler(_request):
        assert ledger.status()["effective_resource_charges"] == {
            "provider_requests": 1, "provider_tokens": 32768, "control_requests": 0,
        }
        return httpx.Response(200, content=body)

    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger, transport=httpx.MockTransport(handler),
    )
    with httpx.Client(transport=transport) as client:
        response = client.get("https://unit.invalid")
        assert response.content == body
    status = ledger.status()
    assert status["effective_resource_charges"]["provider_requests"] == 1
    assert status["effective_resource_charges"]["provider_tokens"] == 7
    assert status["unknown_provider_usage"] is False


def test_async_provider_accepts_sse_usage_and_preserves_bytes(tmp_path):
    ledger, _ = _ledger(tmp_path)
    body = (b'data: {"type":"response.created","response":{"id":"x"}}\n\n'
            b'data: {"type":"response.completed","response":{"usage":'
            b'{"input_tokens":5,"output_tokens":6,"total_tokens":11}}}\n\n'
            b'data: [DONE]\n\n')

    async def handler(_request):
        assert ledger.status()["effective_resource_charges"]["provider_requests"] == 1
        return httpx.Response(200, content=body)

    transport = resource_http.MeteredAsyncHTTPTransport(
        "provider", ledger=ledger, transport=httpx.MockTransport(handler),
    )
    async def exercise():
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://unit.invalid")
            assert response.content == body
    asyncio.run(exercise())
    assert ledger.status()["effective_resource_charges"]["provider_tokens"] == 11


@pytest.mark.parametrize("body", [
    b'{"usage":{"prompt_tokens":3}}',
    b'{"usage":{"total_tokens":7,"input_tokens":3,"output_tokens":5}}',
    b'{"usage":{"total_tokens":true}}',
    b'not-json-or-sse',
])
def test_unknown_or_conflicting_usage_keeps_full_charge_and_blocks_next_request(tmp_path, body):
    ledger, _ = _ledger(tmp_path)
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=body)

    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger, transport=httpx.MockTransport(handler),
    )
    with httpx.Client(transport=transport) as client:
        client.get("https://unit.invalid")
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            client.get("https://unit.invalid")
    assert calls == 1
    status = ledger.status()
    assert status["unknown_provider_usage"] is True
    assert status["effective_resource_charges"]["provider_tokens"] == 32768


def test_provider_http_error_and_network_error_settle_unknown(tmp_path):
    for index, outcome in enumerate((httpx.Response(500, content=b'{"usage":{"total_tokens":1}}'),
                                     httpx.ConnectError("failed"))):
        ledger, _ = _ledger(tmp_path / str(index))
        def handler(request, outcome=outcome):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        transport = resource_http.MeteredHTTPTransport(
            "provider", ledger=ledger, transport=httpx.MockTransport(handler),
        )
        with httpx.Client(transport=transport) as client:
            if isinstance(outcome, Exception):
                with pytest.raises(httpx.ConnectError):
                    client.get("https://unit.invalid")
            else:
                assert client.get("https://unit.invalid").status_code == 500
        assert ledger.status()["unknown_provider_usage"] is True


def test_each_control_transport_invocation_is_charged_and_limit_denies_before_dispatch(tmp_path):
    ledger, _ = _ledger(tmp_path, control_requests=2)
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        assert ledger.status()["effective_resource_charges"]["control_requests"] == calls
        return httpx.Response(200, content=b"opaque")

    transport = resource_http.MeteredHTTPTransport(
        "control", ledger=ledger, transport=httpx.MockTransport(handler),
    )
    with httpx.Client(transport=transport) as client:
        client.get("https://unit.invalid")
        client.get("https://unit.invalid")
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            client.get("https://unit.invalid")
    assert calls == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("actual", [32768, 32769, None])
def test_capture_response_boundary_retains_usage_and_suppresses_consumer(
    tmp_path, monkeypatch, asynchronous, actual,
):
    ledger, _ = _ledger(tmp_path)
    body = json.dumps({"usage": {"total_tokens": actual}, "choices": [{
        "message": {"content": "synthetic-action-that-must-be-withheld"},
    }]}).encode()
    captured, actions, dispatches = [], [], []
    monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", "/synthetic/fixture")
    def emit(kind, payload):
        captured.append((kind, payload))
        if kind == "model_response":
            assert ledger.status()["effective_resource_charges"]["provider_tokens"] == (
                actual if actual is not None else 32768
            )
    monkeypatch.setattr(resource_http.observation_channel, "emit", emit)
    def handler(request):
        dispatches.append(True)
        return httpx.Response(200, content=body)
    def sync_run():
        transport = resource_http.MeteredHTTPTransport(
            "provider", ledger=ledger, transport=httpx.MockTransport(handler),
        )
        with httpx.Client(transport=transport) as client:
            response = client.post("https://unit.invalid", json={"synthetic": True})
            actions.append(response.json()["choices"][0]["message"]["content"])
    async def async_run():
        transport = resource_http.MeteredAsyncHTTPTransport(
            "provider", ledger=ledger, transport=httpx.MockTransport(handler),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post("https://unit.invalid", json={"synthetic": True})
            actions.append(response.json()["choices"][0]["message"]["content"])
    run = (lambda: asyncio.run(async_run())) if asynchronous else sync_run
    if actual == 32768:
        run()
        assert len(actions) == 1
    else:
        with pytest.raises(resource_http.ProviderResponseBoundaryError) as error:
            run()
        assert error.value.reason == ("usage_unavailable" if actual is None else "per_call_token_overrun")
        assert actions == []
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            run()
    assert dispatches == [True]
    returned = [payload for kind, payload in captured if kind == "model_response"
                and payload.get("outcome") == "returned"]
    assert len(returned) == 1
    assert base64.b64decode(returned[0]["body_base64"]) == body
    reservation_ids = [event["reservation_id"] for line in ledger.ledger_path.read_text().splitlines()
                       for event in [json.loads(line)] if event.get("type") == "reservation"]
    assert returned[0]["reservation_id"] == reservation_ids[0]
    denied = [payload for kind, payload in captured if kind == "model_response"
              and payload.get("outcome") == "denied_before_dispatch"]
    assert all("reservation_id" not in payload for payload in denied)


def test_default_path_does_not_eagerly_read_streaming_request(tmp_path, monkeypatch):
    ledger, _ = _ledger(tmp_path)
    reads = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(True)
            yield b"synthetic-body"

    class Underlying(httpx.BaseTransport):
        def handle_request(self, request):
            assert reads == []
            return httpx.Response(200, json={"usage": {"total_tokens": 1}})

    monkeypatch.delenv("SAGE_OBSERVATION_DIRECTORY", raising=False)
    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger, transport=Underlying(),
    )
    request = httpx.Request("POST", "https://unit.invalid", stream=Stream())
    transport.handle_request(request)
    assert reads == []


def test_sync_passive_capture_preserves_exact_request_response_bytes_and_settlement_order(tmp_path, monkeypatch):
    ledger, _ = _ledger(tmp_path)
    request_body = b"synthetic request bytes"
    response_body = b'{"usage":{"total_tokens":9},"synthetic":true}'
    events = []

    def emit(kind, payload):
        if kind == "model_response":
            assert ledger.status()["effective_resource_charges"]["provider_tokens"] == 9
        events.append((kind, payload))

    def handler(request):
        assert request.read() == request_body
        return httpx.Response(200, content=response_body, request=request)

    monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", "/synthetic/fixture")
    monkeypatch.setattr(resource_http.observation_channel, "emit", emit)
    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger, transport=httpx.MockTransport(handler),
    )
    with httpx.Client(transport=transport) as client:
        response = client.post("https://unit.invalid/path", content=request_body)
    assert response.content == response_body
    assert [kind for kind, _ in events] == ["model_request", "model_response"]
    assert events[0][1] == {
        "request_id": events[1][1]["request_id"], "method": "POST",
        "url": "https://unit.invalid/path", "body_base64": base64.b64encode(request_body).decode(),
    }
    assert events[1][1]["body_base64"] == base64.b64encode(response_body).decode()
    assert events[1][1]["status_code"] == 200
    assert events[1][1]["outcome"] == "returned"


def test_capture_denial_and_unavailable_observer_prevent_dispatch(tmp_path, monkeypatch):
    ledger, _ = _ledger(tmp_path, control_requests=1)
    ledger.reserve({"control_requests": 1}, "preexisting")
    events, calls = [], []
    monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", "/synthetic/fixture")
    monkeypatch.setattr(resource_http.observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))
    transport = resource_http.MeteredHTTPTransport(
        "control", ledger=ledger,
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            client.post("https://unit.invalid", content=b"fixture")
    assert calls == []
    assert [row[0] for row in events] == ["control_request", "control_response"]
    assert events[1][1]["outcome"] == "denied_before_dispatch"
    assert set(events[1][1]) == {"request_id", "outcome", "exception_type"}

    ledger2, _ = _ledger(tmp_path / "unavailable")
    calls.clear()
    monkeypatch.setattr(
        resource_http.observation_channel, "emit",
        lambda kind, payload: (_ for _ in ()).throw(ObservationChannelError("fixture unavailable")),
    )
    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger2,
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ObservationChannelError):
            client.get("https://unit.invalid")
    assert calls == []
    assert ledger2.status()["effective_resource_charges"]["provider_requests"] == 0


def test_transport_and_response_capture_failures_preserve_provider_settlement(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", "/synthetic/fixture")
    events = []
    monkeypatch.setattr(resource_http.observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))
    ledger, _ = _ledger(tmp_path / "transport")
    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger,
        transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("synthetic"))),
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            client.get("https://unit.invalid")
    assert events[-1][0] == "model_response"
    assert events[-1][1]["outcome"] == "transport_error"
    assert "synthetic" not in json.dumps(events[-1][1])
    assert ledger.status()["unknown_provider_usage"] is True

    ledger2, ledger_path = _ledger(tmp_path / "response")
    def fail_response(kind, payload):
        if kind == "model_response":
            raise ObservationChannelError("fixture unavailable")
    monkeypatch.setattr(resource_http.observation_channel, "emit", fail_response)
    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger2,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"usage": {"total_tokens": 7}}, request=request,
        )),
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ObservationChannelError):
            client.get("https://unit.invalid")
    assert ledger2.status()["effective_resource_charges"]["provider_tokens"] == 7
    assert sum(json.loads(line)["type"] == "provider_settlement"
               for line in ledger_path.read_text().splitlines()) == 1


def test_async_passive_capture_preserves_exact_linked_bytes(tmp_path, monkeypatch):
    ledger, _ = _ledger(tmp_path)
    events = []
    monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", "/synthetic/fixture")
    monkeypatch.setattr(resource_http.observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))
    request_body = b"async request fixture"
    response_body = b'{"usage":{"total_tokens":5}}'

    async def handler(request):
        assert await request.aread() == request_body
        return httpx.Response(200, content=response_body, request=request)

    transport = resource_http.MeteredAsyncHTTPTransport(
        "provider", ledger=ledger, transport=httpx.MockTransport(handler),
    )
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post("https://unit.invalid/async", content=request_body)
            assert response.content == response_body
    asyncio.run(run())
    assert [kind for kind, _ in events] == ["model_request", "model_response"]
    assert events[0][1]["request_id"] == events[1][1]["request_id"]
    assert events[0][1]["body_base64"] == base64.b64encode(request_body).decode()
    assert events[1][1]["body_base64"] == base64.b64encode(response_body).decode()


def test_concurrent_provider_calls_dispatch_when_both_full_reservations_fit(tmp_path):
    ledger, _ = _ledger(tmp_path, provider_tokens=65536)
    barrier = threading.Barrier(2)

    def call(total):
        transport = resource_http.MeteredHTTPTransport(
            "provider", ledger=ledger,
            transport=httpx.MockTransport(lambda request: (
                barrier.wait(2), httpx.Response(200, json={"usage": {"total_tokens": total}}, request=request)
            )[1]),
        )
        with httpx.Client(transport=transport) as client:
            return client.get("https://unit.invalid").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call, 10), pool.submit(call, 20)]
        assert sorted(future.result(timeout=5) for future in futures) == [200, 200]
    assert ledger.status()["effective_resource_charges"] == {
        "provider_requests": 2, "provider_tokens": 30, "control_requests": 0,
    }


def test_concurrent_provider_call_is_denied_when_only_one_full_reservation_fits(tmp_path):
    ledger, _ = _ledger(tmp_path, provider_tokens=32768)
    entered, release = threading.Event(), threading.Event()

    def handler(request):
        entered.set()
        assert release.wait(3)
        return httpx.Response(200, json={"usage": {"total_tokens": 10}}, request=request)

    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger, transport=httpx.MockTransport(handler),
    )
    with httpx.Client(transport=transport) as client, ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.get, "https://unit.invalid/first")
        assert entered.wait(2)
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            client.get("https://unit.invalid/second")
        release.set()
        assert first.result(3).status_code == 200


def test_custom_provider_reservation_allows_three_evolving_known_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION", "1002048")
    monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", "/synthetic/fixture")
    monkeypatch.setattr(resource_http.observation_channel, "emit", lambda *_: None)
    ledger, _ = _ledger(tmp_path, provider_tokens=1500000)
    actuals = iter((300000, 120000, 60000))
    reserved_charges = []

    def handler(request):
        reserved_charges.append(ledger.status()["effective_resource_charges"]["provider_tokens"])
        return httpx.Response(200, json={"usage": {"total_tokens": next(actuals)}}, request=request)

    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger, transport=httpx.MockTransport(handler),
    )
    assert transport.provider_token_reservation == 1002048
    with httpx.Client(transport=transport) as client:
        for _ in range(3):
            assert client.get("https://unit.invalid").status_code == 200
    assert reserved_charges == [1002048, 1302048, 1422048]
    assert ledger.status()["effective_resource_charges"]["provider_tokens"] == 480000
    assert ledger.status()["effective_resource_charges"]["provider_requests"] == 3


def test_custom_provider_reservation_denies_simultaneous_second_call(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION", "1002048")
    ledger, _ = _ledger(tmp_path, provider_tokens=1500000)
    entered, release = threading.Event(), threading.Event()
    dispatched = []

    def handler(request):
        dispatched.append(str(request.url))
        entered.set()
        assert release.wait(3)
        return httpx.Response(200, json={"usage": {"total_tokens": 10}}, request=request)

    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger, transport=httpx.MockTransport(handler),
    )
    with httpx.Client(transport=transport) as client, ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.get, "https://unit.invalid/first")
        assert entered.wait(2)
        with pytest.raises(resource_budget.ResourceBudgetExceeded, match="provider_tokens"):
            client.get("https://unit.invalid/second")
        release.set()
        assert first.result(3).status_code == 200
    assert dispatched == ["https://unit.invalid/first"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("actual", [None, 1002048, 1002049])
def test_custom_provider_reservation_controls_capture_boundary_and_later_dispatch(
    tmp_path, monkeypatch, asynchronous, actual,
):
    monkeypatch.setenv("SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION", "1002048")
    monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", "/synthetic/fixture")
    monkeypatch.setattr(resource_http.observation_channel, "emit", lambda *_: None)
    ledger, _ = _ledger(tmp_path, provider_tokens=1500000)
    dispatches = []

    def handler(request):
        dispatches.append(True)
        return httpx.Response(200, json={"usage": {"total_tokens": actual}}, request=request)

    transport_type = (
        resource_http.MeteredAsyncHTTPTransport if asynchronous
        else resource_http.MeteredHTTPTransport
    )
    transport = transport_type("provider", ledger=ledger, transport=httpx.MockTransport(handler))
    # The limit is frozen on the transport; changing the environment cannot change its reserve or boundary.
    monkeypatch.setenv("SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION", "32768")

    async def async_run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.get("https://unit.invalid")

    def sync_run():
        with httpx.Client(transport=transport) as client:
            return client.get("https://unit.invalid")

    run = (lambda: asyncio.run(async_run())) if asynchronous else sync_run
    if actual == 1002048:
        assert run().status_code == 200
    else:
        with pytest.raises(resource_http.ProviderResponseBoundaryError) as error:
            run()
        assert error.value.reason == (
            "usage_unavailable" if actual is None else "per_call_token_overrun"
        )
    status = ledger.status()
    assert status["effective_resource_charges"]["provider_tokens"] == (
        1002048 if actual is None else actual
    )
    if actual != 1002048:
        later = resource_http.MeteredHTTPTransport(
            "provider", ledger=ledger,
            transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("dispatched"))),
        )
        with httpx.Client(transport=later) as client:
            with pytest.raises(resource_budget.ResourceBudgetExceeded):
                client.get("https://unit.invalid/later")
    assert dispatches == [True]


@pytest.mark.parametrize("value", ["", "0", "-1", "+1", "1.5", " 2 ", "abc"])
def test_invalid_custom_provider_reservation_fails_at_transport_creation(
    tmp_path, monkeypatch, value,
):
    monkeypatch.setenv("SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION", value)
    ledger, ledger_path = _ledger(tmp_path)
    for transport_type in (resource_http.MeteredHTTPTransport, resource_http.MeteredAsyncHTTPTransport):
        with pytest.raises(resource_http.ResourceHTTPError, match="SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION"):
            transport_type("provider", ledger=ledger)
    assert not ledger_path.exists()
    control = resource_http.MeteredHTTPTransport(
        "control", ledger=ledger,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    )
    with httpx.Client(transport=control) as client:
        assert client.get("https://unit.invalid").status_code == 200
    assert ledger.status()["effective_resource_charges"]["control_requests"] == 1


def test_unknown_settlement_blocks_later_dispatch_but_not_already_dispatched_call(tmp_path):
    ledger, _ = _ledger(tmp_path, provider_tokens=65536)
    barrier = threading.Barrier(2)
    dispatched = []

    def call(body):
        def handler(request):
            dispatched.append(True)
            barrier.wait(2)
            return httpx.Response(200, content=body, request=request)
        transport = resource_http.MeteredHTTPTransport(
            "provider", ledger=ledger, transport=httpx.MockTransport(handler),
        )
        with httpx.Client(transport=transport) as client:
            return client.get("https://unit.invalid").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call, b"unknown"), pool.submit(call, b'{"usage":{"total_tokens":12}}')]
        assert [future.result(5) for future in futures] == [200, 200]
    assert len(dispatched) == 2
    assert ledger.status()["unknown_provider_usage"] is True
    later = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger,
        transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("dispatched"))),
    )
    with httpx.Client(transport=later) as client:
        with pytest.raises(resource_budget.ResourceBudgetExceeded, match="unknown provider"):
            client.get("https://unit.invalid/later")
