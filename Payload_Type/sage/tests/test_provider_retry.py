"""Offline producer/consumer checks for the opt-in provider retry boundary."""
from __future__ import annotations

import asyncio
import time

import httpx
import langchain.chat_models
import pytest

from ai import provider_retry
from ai.langgraph import model as model_module
from ai.langgraph.autonomous_controller import AutonomousController, ControllerConfig
from sage_chat.config import build_model_kwargs
from sage_chat.headless import build_chat_request


def _temporary(status=429, code="rate_limit_exceeded", retry_after="0.001"):
    return httpx.Response(
        status,
        headers={"retry-after": retry_after},
        json={"error": {"code": code, "type": "rate_limit_error"}},
    )


def test_policy_record_is_exact_and_opt_in():
    policy = provider_retry.parse_policy("transient-v1")
    assert policy is not None
    assert provider_retry.parse_policy(policy.to_record()) == policy
    assert provider_retry.parse_policy(None) is None
    assert provider_retry.parse_policy("off") is None
    with pytest.raises(ValueError):
        provider_retry.parse_policy({**policy.to_record(), "max_retries_per_request": True})


def test_native_config_explicit_off_suppresses_ambient_preset(monkeypatch):
    monkeypatch.setenv("SAGE_PROVIDER_RETRY_POLICY", "transient-v1")
    off = build_model_kwargs(build_chat_request("Offline", config={
        "provider": "openai", "model": "offline-model", "SAGE_PROVIDER_RETRY_POLICY": "off"
    }))
    assert off["config"]["configurable"]["provider_retry_policy"] == "off"
    enabled = build_model_kwargs(build_chat_request("Offline", config={
        "provider": "openai", "model": "offline-model"
    }))
    assert enabled["config"]["configurable"]["provider_retry_policy"] == "transient-v1"
    with pytest.raises(ValueError, match="requires the openai"):
        build_model_kwargs(build_chat_request("Offline", config={
            "provider": "bedrock", "model": "offline-model",
            "SAGE_PROVIDER_RETRY_POLICY": "transient-v1",
        }))


def test_sync_retry_records_real_responses_wait_and_clean_replay():
    calls = []
    responses = [_temporary(), httpx.Response(200, json={"ok": True})]
    state = provider_retry.RetryState(provider_retry.Policy(), event_sink=lambda e: calls.append(e))
    transport = provider_retry.RetryingHTTPTransport(
        httpx.MockTransport(lambda _request: responses.pop(0)), state
    )
    with httpx.Client(transport=transport) as client:
        assert client.get("https://unit.invalid/v1/chat/completions").json() == {"ok": True}
    snapshot = state.snapshot()
    assert len([e for e in snapshot["events"] if e["event"] == "provider_response_observed"]) == 2
    assert len([e for e in snapshot["events"] if e["event"] == "retry_wait_started"]) == 1
    assert snapshot["wait_pending"] == []
    assert 0 <= state.completed_wait_seconds() <= 0.1
    assert next(e for e in calls if e["event"] == "retry_wait_started")["provider_retry"]["events"]
    assert provider_retry.RetryState.from_snapshot(state.policy, snapshot).snapshot() == snapshot


def test_async_retry_publishes_pending_before_sleep_and_completed_after():
    published = []
    responses = [_temporary(503, "service_unavailable"), httpx.Response(200, json={"ok": True})]
    state = provider_retry.RetryState(provider_retry.Policy())

    async def publish(event):
        published.append(event)

    async def run():
        state.bind_async_event_sink(publish, asyncio.get_running_loop())
        transport = provider_retry.RetryingAsyncHTTPTransport(
            httpx.MockTransport(lambda _request: responses.pop(0)), state
        )
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await client.get("https://unit.invalid/v1/chat/completions")).json() == {"ok": True}

    asyncio.run(run())
    assert [e["event"] for e in published] == [
        "retry_wait_started", "retry_wait_activated", "retry_wait_completed"
    ]
    assert len(published[0]["provider_retry"]["wait_pending"]) == 1
    assert published[0]["provider_retry"]["wait_pending"][0]["sleep_started_at_epoch"] is None
    assert published[1]["provider_retry"]["wait_pending"][0]["sleep_started_at_epoch"] is not None
    assert published[2]["provider_retry"]["wait_pending"] == []


@pytest.mark.parametrize("status,code", [
    (429, "insufficient_quota"), (429, "billing_hard_limit_reached"),
    (400, "rate_limit_exceeded"), (401, "rate_limit_exceeded"),
    (500, "server_error"),
])
def test_permanent_and_other_responses_are_never_retried(status, code):
    seen = []
    state = provider_retry.RetryState(provider_retry.Policy())
    response = httpx.Response(status, json={"error": {"code": code}})
    transport = provider_retry.RetryingHTTPTransport(
        httpx.MockTransport(lambda _request: seen.append(True) or response), state
    )
    with httpx.Client(transport=transport) as client:
        assert client.get("https://unit.invalid").status_code == status
    assert seen == [True]
    assert state.completed_wait_seconds() == 0


def test_permanent_type_overrides_transient_code():
    response = httpx.Response(429, json={"error": {
        "code": "rate_limit_exceeded", "type": "insufficient_quota"
    }})
    assert not provider_retry.retryable_response(response)


def test_transport_error_is_not_retried():
    seen = []
    def fail(_request):
        seen.append(True)
        raise httpx.ReadTimeout("offline timeout")
    state = provider_retry.RetryState(provider_retry.Policy())
    with httpx.Client(transport=provider_retry.RetryingHTTPTransport(httpx.MockTransport(fail), state)) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.get("https://unit.invalid")
    assert seen == [True]
    assert state.snapshot()["events"] == []


def test_pending_snapshot_is_provisional_only_and_recovery_rejects_it():
    state = provider_retry.RetryState(provider_retry.Policy())
    event_id = state._reserve_wait(10)
    assert event_id is not None
    snapshot = state.snapshot()
    with pytest.raises(ValueError, match="unresolved"):
        provider_retry.RetryState.from_snapshot(state.policy, snapshot)
    observer = provider_retry.RetryState.from_snapshot(state.policy, snapshot, allow_pending=True)
    start = snapshot["wait_pending"][0]["started_at_epoch"]
    assert observer.credited_wait_seconds(start + 100) == 0
    state._begin_wait(event_id)
    active_snapshot = state.snapshot()
    active_observer = provider_retry.RetryState.from_snapshot(state.policy, active_snapshot, allow_pending=True)
    began = active_snapshot["wait_pending"][0]["sleep_started_at_epoch"]
    assert active_observer.credited_wait_seconds(began + 100) == pytest.approx(10)
    assert observer.completed_wait_seconds(start + 100) == 0


def test_arm_deadline_prevents_another_physical_attempt_after_late_429():
    seen = []
    state = provider_retry.RetryState(provider_retry.Policy())
    state.bind_active_deadline(time.time() + 0.003)
    def respond(_request):
        seen.append(True)
        time.sleep(0.01)
        return _temporary()
    with httpx.Client(transport=provider_retry.RetryingHTTPTransport(httpx.MockTransport(respond), state)) as client:
        assert client.get("https://unit.invalid").status_code == 429
    assert seen == [True]
    assert state.completed_wait_seconds() == 0


def test_provider_retry_wait_cap_prevents_sleep():
    state = provider_retry.RetryState(provider_retry.Policy())
    assert state._reserve_wait(901) is None
    assert state.snapshot()["events"] == []


def test_retry_attempt_and_request_wait_caps(monkeypatch):
    monkeypatch.setattr(provider_retry.time, "sleep", lambda _seconds: None)
    seen = []
    state = provider_retry.RetryState(provider_retry.Policy())
    def always_limited(_request):
        seen.append(True)
        return _temporary(retry_after="0")
    with httpx.Client(transport=provider_retry.RetryingHTTPTransport(
        httpx.MockTransport(always_limited), state
    )) as client:
        assert client.get("https://unit.invalid").status_code == 429
    assert len(seen) == 4
    assert len([e for e in state.snapshot()["events"] if e["event"] == "retry_wait_started"]) == 3

    seen.clear()
    response = _temporary(retry_after="301")
    with httpx.Client(transport=provider_retry.RetryingHTTPTransport(
        httpx.MockTransport(lambda _request: seen.append(True) or response),
        provider_retry.RetryState(provider_retry.Policy()),
    )) as client:
        assert client.get("https://unit.invalid").status_code == 429
    assert len(seen) == 1

    seen.clear()
    with httpx.Client(transport=provider_retry.RetryingHTTPTransport(
        httpx.MockTransport(lambda _request: seen.append(True) or _temporary(retry_after=(
            "100" if len(seen) < 3 else "101"
        ))), provider_retry.RetryState(provider_retry.Policy()),
    )) as client:
        assert client.get("https://unit.invalid").status_code == 429
    assert len(seen) == 3


def test_async_cancellation_records_only_completed_wait_and_no_retry_dispatch():
    seen = []
    started = asyncio.Event()
    state = provider_retry.RetryState(provider_retry.Policy())

    async def publish(event):
        if event["event"] == "retry_wait_started":
            started.set()

    async def run():
        state.bind_async_event_sink(publish, asyncio.get_running_loop())
        transport = provider_retry.RetryingAsyncHTTPTransport(httpx.MockTransport(
            lambda _request: seen.append(True) or _temporary(retry_after="10")
        ), state)
        async with httpx.AsyncClient(transport=transport) as client:
            task = asyncio.create_task(client.get("https://unit.invalid"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert seen == [True]
    assert state.snapshot()["wait_pending"] == []
    assert state.completed_wait_seconds() < 1


def test_controller_seam_uses_active_clock_across_old_deadline():
    state = provider_retry.RetryState(provider_retry.Policy())
    started = time.time()

    async def delayed_result():
        event_id = state._reserve_wait(0.06)
        assert event_id is not None
        sleep_started = time.monotonic()
        state._begin_wait(event_id)
        await asyncio.sleep(0.06)
        state._complete_wait(event_id, sleep_started)
        await asyncio.sleep(0.005)
        return "done"

    controller = AutonomousController(
        observe=lambda: None, execute=lambda _action: None,
        clock=lambda: state.active_elapsed_seconds(started),
        dynamic_wall_clock=True,
        config=ControllerConfig(wall_clock_budget_s=0.03, seam_timeout_s=0.03),
    )
    assert asyncio.run(controller._seam(delayed_result, "policy_select")) == ("ok", "done")
    assert time.time() - started > 0.03
    assert state.active_elapsed_seconds(started) < 0.03


def test_slow_retry_publication_does_not_buy_another_provider_request():
    state = provider_retry.RetryState(provider_retry.Policy())
    began = time.time()
    state.bind_active_deadline(time.time() + 0.01)
    seen = []

    async def slow_publish(event):
        if event["event"] == "retry_wait_started":
            await asyncio.sleep(0.03)
            assert state.active_elapsed_seconds(began) >= 0.02

    async def run():
        state.bind_async_event_sink(slow_publish, asyncio.get_running_loop())
        transport = provider_retry.RetryingAsyncHTTPTransport(httpx.MockTransport(
            lambda _request: seen.append(True) or _temporary(retry_after="0.05")
        ), state)
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await client.get("https://unit.invalid")).status_code == 429

    asyncio.run(run())
    assert seen == [True]
    assert state.completed_wait_seconds() < 0.005


def test_native_model_retry_configuration_reaches_actual_chat_completion(monkeypatch):
    responses = [_temporary(), httpx.Response(200, json={
        "id": "offline", "object": "chat.completion", "created": 0,
        "model": "offline-model", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "READY"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })]
    paths = []
    monkeypatch.setattr(model_module, "init_chat_model", lambda **kwargs: kwargs)
    monkeypatch.setattr(model_module, "_patch_model_for_bedrock", lambda value: value)
    monkeypatch.setattr(model_module.resource_budget, "configured_ledger", lambda: None)
    instance = model_module.Model.__new__(model_module.Model)
    instance.provider = "openai"
    instance.model = "offline-model"
    instance._eval_arm_deadline_epoch = None
    instance._provider_retry_state = None
    instance._controller_runtime_telemetry = {}
    instance.config = {"configurable": {
        "api_key": "offline-placeholder", "base_url": "https://unit.invalid/v1",
        "provider_retry_policy": "transient-v1",
    }}
    kwargs = instance._get_base_chat_model()
    assert kwargs["max_retries"] == 0
    sync_client = kwargs["http_client"]
    assert isinstance(sync_client._transport, provider_retry.RetryingHTTPTransport)
    sync_client._transport.transport = httpx.MockTransport(
        lambda request: paths.append(request.url.path) or responses.pop(0)
    )
    llm = langchain.chat_models.init_chat_model(
        model_provider="openai", model="offline-model", api_key="offline-placeholder",
        base_url="https://unit.invalid/v1", http_client=sync_client, max_retries=0,
    )
    assert llm.invoke("Offline request").content == "READY"
    assert paths == ["/v1/chat/completions", "/v1/chat/completions"]
    assert instance._provider_retry_state.completed_wait_seconds() > 0
