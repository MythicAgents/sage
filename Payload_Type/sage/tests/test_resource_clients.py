import asyncio
import base64
import io
from types import SimpleNamespace

import aiohttp
import pytest
import requests

from ai import resource_budget, resource_clients


class Ledger:
    def __init__(self):
        self.operations = []

    def reserve(self, amounts, operation, cleanup=False):
        self.operations.append((amounts, operation, cleanup))
        return f"r-{len(self.operations)}"


@pytest.fixture(autouse=True)
def restore_client_methods():
    requests_send = requests.adapters.HTTPAdapter.send
    aiohttp_request = aiohttp.ClientSession._request
    aiohttp_send = aiohttp.ClientRequest.send
    try:
        yield
    finally:
        requests.adapters.HTTPAdapter.send = requests_send
        aiohttp.ClientSession._request = aiohttp_request
        aiohttp.ClientRequest.send = aiohttp_send
        resource_clients._aiohttp_send_credit.set(None)


def test_install_absent_is_noop_and_installed_default_path_still_calls_original(monkeypatch):
    called = []
    incoming_send = requests.adapters.HTTPAdapter.send
    monkeypatch.setattr(resource_clients.resource_budget, "configured_ledger", lambda: None)
    monkeypatch.setattr(
        resource_clients, "_ORIGINAL_REQUESTS_SEND",
        lambda self, request, **kwargs: called.append((request, kwargs)) or "original",
    )
    assert resource_clients.install() is False
    assert requests.adapters.HTTPAdapter.send is incoming_send

    monkeypatch.setattr(resource_clients.resource_budget, "configured_ledger", lambda: Ledger())
    assert resource_clients.install() is True
    monkeypatch.setattr(resource_clients.resource_budget, "configured_ledger", lambda: None)
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    assert adapter.send("request") == "original"
    assert called[0][0] == "request"


def test_requests_reserves_each_send_and_refuses_hidden_retries(monkeypatch):
    ledger, called = Ledger(), []
    monkeypatch.setattr(resource_clients.resource_budget, "configured_ledger", lambda: ledger)
    monkeypatch.setattr(
        resource_clients, "_ORIGINAL_REQUESTS_SEND",
        lambda self, request, **kwargs: called.append(request) or "response",
    )
    assert resource_clients.install() is True
    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    assert adapter.send("first") == "response"
    assert adapter.send("redirect") == "response"
    assert called == ["first", "redirect"]
    assert [amounts for amounts, _operation, _cleanup in ledger.operations] == [
        {"control_requests": 1}, {"control_requests": 1}
    ]

    hidden = requests.adapters.HTTPAdapter(max_retries=1)
    with pytest.raises(resource_budget.ResourceBudgetError, match="zero hidden retries"):
        hidden.send("refused")
    assert called == ["first", "redirect"]


def test_aiohttp_request_credit_redirect_nested_and_concurrent_calls(monkeypatch):
    ledger, sends = Ledger(), []

    async def original_send(self, conn):
        sends.append(conn)
        return f"sent-{conn}"

    async def original_request(self, method, url, **kwargs):
        request = object.__new__(aiohttp.ClientRequest)
        if url == "nested-outer":
            await aiohttp.ClientSession._request(self, method, "nested-inner")
        await aiohttp.ClientRequest.send(request, url)
        if url == "redirect":
            await aiohttp.ClientRequest.send(request, "redirect-2")
        return url

    monkeypatch.setattr(resource_clients.resource_budget, "configured_ledger", lambda: ledger)
    monkeypatch.setattr(resource_clients, "_ORIGINAL_AIOHTTP_REQUEST", original_request)
    monkeypatch.setattr(resource_clients, "_ORIGINAL_AIOHTTP_SEND", original_send)
    assert resource_clients.install() is True

    async def run():
        session = object()
        assert await aiohttp.ClientSession._request(session, "GET", "redirect") == "redirect"
        assert await aiohttp.ClientSession._request(session, "GET", "nested-outer") == "nested-outer"
        await asyncio.gather(
            aiohttp.ClientSession._request(session, "GET", "parallel-a"),
            aiohttp.ClientSession._request(session, "GET", "parallel-b"),
        )

    asyncio.run(run())
    assert len(sends) == 6
    assert len(ledger.operations) == 6
    assert resource_clients._aiohttp_send_credit.get() is None


def test_aiohttp_failure_cancellation_and_missing_context_preserve_reservations(monkeypatch):
    ledger, original_sends = Ledger(), []

    async def original_request(self, method, url, **kwargs):
        if url == "connect-failure":
            raise TimeoutError("connect failed")
        await asyncio.Event().wait()

    async def original_send(self, conn):
        original_sends.append(conn)

    monkeypatch.setattr(resource_clients.resource_budget, "configured_ledger", lambda: ledger)
    monkeypatch.setattr(resource_clients, "_ORIGINAL_AIOHTTP_REQUEST", original_request)
    monkeypatch.setattr(resource_clients, "_ORIGINAL_AIOHTTP_SEND", original_send)
    resource_clients.install()

    async def run():
        with pytest.raises(TimeoutError):
            await aiohttp.ClientSession._request(object(), "GET", "connect-failure")
        task = asyncio.create_task(aiohttp.ClientSession._request(object(), "GET", "cancelled"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(resource_budget.ResourceBudgetError, match="no enclosing"):
            await aiohttp.ClientRequest.send(object.__new__(aiohttp.ClientRequest), "direct")

    asyncio.run(run())
    assert len(ledger.operations) == 2
    assert original_sends == []
    assert resource_clients._aiohttp_send_credit.get() is None


def test_requests_passive_capture_links_materialized_json_and_stream_response(monkeypatch):
    ledger, events = Ledger(), []

    def emit(kind, payload):
        events.append((kind, payload))

    def original_send(self, request, **kwargs):
        response = requests.Response()
        response.status_code = 201
        response._content = b"synthetic-response"
        return response

    prepared = requests.Request(
        "POST", "https://control.invalid/path", json={"synthetic": True},
        headers={"Authorization": "SYNTHETIC_HEADER_SENTINEL"},
    ).prepare()
    monkeypatch.setenv(resource_clients.observation_channel.ENV_NAME, "/synthetic/observer")
    monkeypatch.setattr(resource_clients.observation_channel, "emit", emit)
    monkeypatch.setattr(resource_clients.resource_budget, "configured_ledger", lambda: ledger)
    monkeypatch.setattr(resource_clients, "_ORIGINAL_REQUESTS_SEND", original_send)
    resource_clients.install()
    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    response = adapter.send(prepared)
    assert response.content == b"synthetic-response"
    assert [kind for kind, _ in events] == ["control_request", "control_response"]
    assert set(events[0][1]) == {"request_id", "transport", "method", "url", "body_base64"}
    assert events[0][1]["request_id"] == events[1][1]["request_id"]
    assert events[0][1]["transport"] == "requests"
    assert base64.b64decode(events[0][1]["body_base64"]) == prepared.body
    assert base64.b64decode(events[1][1]["body_base64"]) == b"synthetic-response"
    assert events[1][1]["reservation_id"] == "r-1"

    events.clear()
    adapter.send(prepared, stream=True)
    assert events[1][1]["body_unavailable"] is True
    assert "body_base64" not in events[1][1]
    assert events[1][1]["reservation_id"] == "r-2"


def test_requests_opaque_body_is_not_read_and_denial_or_error_is_redacted(monkeypatch):
    class Opaque:
        def read(self):
            raise AssertionError("opaque body was consumed")

    prepared = requests.Request("POST", "https://control.invalid/path").prepare()
    prepared.body = Opaque()
    events, calls = [], []
    monkeypatch.setenv(resource_clients.observation_channel.ENV_NAME, "/synthetic/observer")
    monkeypatch.setattr(resource_clients.observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))

    class DenyLedger(Ledger):
        def reserve(self, *args, **kwargs):
            raise resource_budget.ResourceBudgetExceeded("SYNTHETIC_EXCEPTION_SENTINEL")

    monkeypatch.setattr(resource_clients.resource_budget, "configured_ledger", lambda: DenyLedger())
    monkeypatch.setattr(
        resource_clients, "_ORIGINAL_REQUESTS_SEND",
        lambda *args, **kwargs: calls.append(True),
    )
    resource_clients.install()
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        requests.adapters.HTTPAdapter(max_retries=0).send(prepared)
    assert calls == []
    assert events[0][1]["body_unavailable"] is True
    assert set(events[1][1]) == {"request_id", "outcome", "exception_type"}
    assert "SYNTHETIC_EXCEPTION_SENTINEL" not in str(events[1][1])

    ledger, events = Ledger(), []
    monkeypatch.setattr(resource_clients.resource_budget, "configured_ledger", lambda: ledger)
    monkeypatch.setattr(resource_clients.observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(
        resource_clients, "_ORIGINAL_REQUESTS_SEND",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("SYNTHETIC_EXCEPTION_SENTINEL")),
    )
    with pytest.raises(TimeoutError):
        requests.adapters.HTTPAdapter(max_retries=0).send(prepared)
    assert events[-1][1]["outcome"] == "transport_error"
    assert events[-1][1]["reservation_id"] == "r-1"
    assert "SYNTHETIC_EXCEPTION_SENTINEL" not in str(events[-1][1])


def test_aiohttp_passive_capture_uses_only_materialized_payload_and_never_reads_opaque(monkeypatch):
    ledger, events = Ledger(), []

    class Reader(io.BytesIO):
        reads = 0

        def read(self, *args, **kwargs):
            self.reads += 1
            return super().read(*args, **kwargs)

    opaque_reader = Reader(b"opaque")

    async def original_request(self, method, url, **kwargs):
        payload = aiohttp.payload.JsonPayload({"synthetic": True}) if url == "json" else \
            aiohttp.payload.IOBasePayload(opaque_reader)
        request = SimpleNamespace(method=method, url=f"https://control.invalid/{url}", body=payload)
        return await aiohttp.ClientRequest.send(request, None)

    async def original_send(self, conn):
        return SimpleNamespace(status=202)

    monkeypatch.setenv(resource_clients.observation_channel.ENV_NAME, "/synthetic/observer")
    monkeypatch.setattr(resource_clients.observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(resource_clients.resource_budget, "configured_ledger", lambda: ledger)
    monkeypatch.setattr(resource_clients, "_ORIGINAL_AIOHTTP_REQUEST", original_request)
    monkeypatch.setattr(resource_clients, "_ORIGINAL_AIOHTTP_SEND", original_send)
    resource_clients.install()

    async def run():
        await aiohttp.ClientSession._request(object(), "POST", "json")
        await aiohttp.ClientSession._request(object(), "POST", "opaque")
    asyncio.run(run())
    requests_events = [payload for kind, payload in events if kind == "control_request"]
    response_events = [payload for kind, payload in events if kind == "control_response"]
    assert base64.b64decode(requests_events[0]["body_base64"]) == aiohttp.payload.JsonPayload(
        {"synthetic": True}
    )._value
    assert requests_events[1]["body_unavailable"] is True
    assert opaque_reader.reads == 0
    assert all(row["body_unavailable"] is True and row["status_code"] == 202 for row in response_events)
    assert [row["request_id"] for row in requests_events] == [row["request_id"] for row in response_events]
    assert [row["reservation_id"] for row in response_events] == ["r-1", "r-2"]
