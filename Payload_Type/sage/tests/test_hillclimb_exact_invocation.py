"""Offline contract tests for the shared Responses exact-invocation gate."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import openai

from ai.hillclimb import exact_invocation as exact
from sage_chat.config import resolve_environment_llm_profile
from ssl_bootstrap import configure_sage_ssl_cert_file


class _Stream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        self._iterator = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _completed(*, output_text="READY", model="exact-model", service_tier="default"):
    response = SimpleNamespace(
        status="completed",
        output_text=output_text,
        model=model,
        service_tier=service_tier,
        usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
    )
    return SimpleNamespace(type="response.completed", response=response)


class _Responses:
    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.streams.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _Stream(item)


class _Client:
    def __init__(self, *streams):
        self.responses = _Responses(streams)


def _route(**updates):
    values = {
        "provider": "openai",
        "model": "exact-model",
        "api_endpoint": "https://provider.invalid/v1",
        "api_key": "secret",
        "reasoning_effort": "high",
        "service_tier": "default",
        "store": False,
        "tools": (),
        "max_retries": 0,
    }
    values.update(updates)
    return exact.ResponsesRoute(**values)


def _request(**updates):
    values = {
        "instructions": "Reply READY.",
        "input": [{"role": "user", "content": "probe"}],
        "max_output_tokens": 8,
    }
    values.update(updates)
    return exact.ResponsesRequest(**values)


def test_exact_runner_imports_the_shipping_environment_resolver():
    assert exact.ENVIRONMENT_PROFILE_RESOLVER is resolve_environment_llm_profile
    route = exact.resolve_responses_route({
        "provider": "OpenAI",
        "model": "exact-model",
        "API_ENDPOINT": "https://provider.invalid/v1",
        "API_KEY": "secret",
    })
    assert route == _route()


def test_request_builder_carries_the_exact_route_configuration():
    route = _route(tools=({"type": "function", "name": "read"},))
    request = _request(
        text={"format": {"type": "text"}},
        prompt_cache_key="thread",
        extra_headers={"thread-id": "thread"},
        extra_body={"client_metadata": {"source": "test"}},
    )
    assert exact.build_responses_request(route, request) == {
        "model": "exact-model",
        "instructions": "Reply READY.",
        "input": [{"role": "user", "content": "probe"}],
        "reasoning": {"effort": "high"},
        "service_tier": "default",
        "store": False,
        "stream": True,
        "tools": [{"type": "function", "name": "read"}],
        "text": {"format": {"type": "text"}},
        "max_output_tokens": 8,
        "prompt_cache_key": "thread",
        "extra_headers": {"thread-id": "thread"},
        "extra_body": {"client_metadata": {"source": "test"}},
    }


def test_client_factory_carries_auth_endpoint_headers_and_zero_retries(monkeypatch):
    seen = {}
    events = []

    def fake_client(**kwargs):
        events.append("client")
        seen.update(kwargs)
        return object()

    assert exact.configure_sage_ssl_cert_file is configure_sage_ssl_cert_file
    monkeypatch.setattr(exact, "configure_sage_ssl_cert_file", lambda: events.append("tls"))
    monkeypatch.setattr(openai, "AsyncOpenAI", fake_client)
    exact.make_responses_client(_route(), default_headers={"x-source": "preflight"})
    assert events == ["tls", "client"]
    assert seen["api_key"] == "secret"
    assert str(seen["base_url"]) == "https://provider.invalid/v1"
    assert seen["default_headers"] == {"x-source": "preflight"}
    assert seen["max_retries"] == 0


def test_route_rejects_incomplete_or_noncanonical_transport():
    with pytest.raises(exact.ExactInvocationError, match="required"):
        _route(api_key="")
    with pytest.raises(exact.ExactInvocationError, match="streaming responses.create"):
        _route(sdk_method="chat.completions.create")
    with pytest.raises(exact.ExactInvocationError, match="retries"):
        _route(max_retries=1)


def test_delta_only_completed_stream_retains_every_ordered_delta():
    final = _completed(output_text="")
    collected = asyncio.run(exact.collect_responses_stream(_Stream([
        SimpleNamespace(type="response.output_text.delta", delta="RE"),
        SimpleNamespace(type="response.output_text.delta", delta="ADY"),
        final,
    ])))
    assert collected.output_text == "READY"
    assert collected.captured_delta_count == 2
    assert collected.event_type_counts["response.output_text.delta"] == 2


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([SimpleNamespace(type="response.output_text.delta", delta="lost")], "exactly one terminal"),
        ([_completed(output_text="")], "nonempty captured output"),
        ([SimpleNamespace(
            type="response.failed",
            response=SimpleNamespace(status="failed", output_text="failure"),
        )], "terminated as response.failed"),
    ],
)
def test_collector_fails_closed_on_inadmissible_terminal_shapes(events, message):
    with pytest.raises(exact.ExactInvocationError, match=message):
        asyncio.run(exact.collect_responses_stream(_Stream(events)))


def test_canonical_symbol_guard_rejects_a_separately_assembled_path():
    def alternate_builder(*args, **kwargs):
        return exact.build_responses_request(*args, **kwargs)

    with pytest.raises(exact.ExactInvocationError, match="canonical invocation symbols"):
        exact.assert_canonical_invocation_symbols(alternate_builder, exact.STREAM_COLLECTOR)


def test_existing_result_path_blocks_before_provider_contact(tmp_path):
    path = tmp_path / "result.json"
    path.write_text("occupied")
    route = _route()
    request = _request()
    client = _Client([_completed()])
    receipt = exact.PreflightReceipt(
        client=client,
        route=route,
        request_projection=exact._request_projection(route, request),
        request_builder=exact.REQUEST_BUILDER,
        stream_collector=exact.STREAM_COLLECTOR,
        safe_observation={},
    )

    with pytest.raises(exact.ExactInvocationError, match="refusing to overwrite"):
        asyncio.run(exact.run_gated_responses(
            client, route, request, receipt=receipt, result_path=path,
        ))
    assert client.responses.calls == []


def test_smoke_and_production_share_route_client_builder_and_collector(tmp_path):
    route = _route()
    smoke = _request(input="small smoke")
    production = _request(input="large production")
    client = _Client([_completed()], [_completed(output_text="PRODUCTION")])

    receipt = asyncio.run(exact.run_exact_route_smoke(client, route, smoke))
    collected = asyncio.run(exact.run_gated_responses(
        client,
        route,
        production,
        receipt=receipt,
        result_path=tmp_path / "absent.json",
    ))

    assert receipt.route is route and receipt.client is client
    assert receipt.request_builder is exact.REQUEST_BUILDER
    assert receipt.stream_collector is exact.STREAM_COLLECTOR
    assert collected.output_text == "PRODUCTION"
    assert len(client.responses.calls) == 2
    for key in ("model", "reasoning", "service_tier", "store", "stream", "tools", "max_output_tokens"):
        assert client.responses.calls[0][key] == client.responses.calls[1][key]


def test_gated_request_rejects_route_client_and_request_configuration_drift(tmp_path):
    route = _route()
    request = _request()
    client = _Client([_completed()])
    receipt = asyncio.run(exact.run_exact_route_smoke(client, route, request))

    with pytest.raises(exact.ExactInvocationError, match="authenticated client"):
        asyncio.run(exact.run_gated_responses(
            _Client([_completed()]), route, request, receipt=receipt,
            result_path=tmp_path / "one.json",
        ))
    with pytest.raises(exact.ExactInvocationError, match="route object"):
        asyncio.run(exact.run_gated_responses(
            client, _route(), request, receipt=receipt,
            result_path=tmp_path / "two.json",
        ))
    with pytest.raises(exact.ExactInvocationError, match="configuration differ"):
        asyncio.run(exact.run_gated_responses(
            client, route, exact.ResponsesRequest(
                instructions="Reply READY.", input="changed", max_output_tokens=None,
            ), receipt=receipt, result_path=tmp_path / "three.json",
        ))
    assert len(client.responses.calls) == 1


def test_unsupported_field_failure_is_observed_by_the_exact_smoke():
    route = _route()
    client = _Client(TypeError("unsupported max_output_tokens"))
    with pytest.raises(TypeError, match="unsupported max_output_tokens"):
        asyncio.run(exact.run_exact_route_smoke(client, route, _request()))
    assert client.responses.calls[0]["max_output_tokens"] == 8


def test_failed_smoke_never_reaches_a_production_request(tmp_path):
    route = _route()
    client = _Client(TypeError("unsupported field"), [_completed(output_text="forbidden")])
    with pytest.raises(TypeError, match="unsupported field"):
        asyncio.run(exact.run_exact_route_smoke(client, route, _request()))
    assert len(client.responses.calls) == 1
    assert not (tmp_path / "result.json").exists()


def test_module_imports_in_clean_process_from_production_working_directory():
    payload = Path(__file__).resolve().parents[1]
    command = (
        "from ai.hillclimb import exact_invocation as e; "
        "assert e.REQUEST_BUILDER is e.build_responses_request; "
        "assert e.STREAM_COLLECTOR is e.collect_responses_stream; "
        "print(3)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=payload,
        env={**os.environ, "PYTHONPATH": str(payload)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert int(completed.stdout.strip()) >= 3
