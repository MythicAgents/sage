"""Offline probes for the matched eval retry clock and the real raw SDK path."""

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
import time

import httpx
import pytest

from ai import provider_retry
from ai.hillclimb import bare_runner, live_seams


NATIVE_PATH = Path(__file__).resolve().parents[3] / "skills/sage-live-runner/scripts/native_chat.py"
NATIVE_SPEC = importlib.util.spec_from_file_location("retry_native_chat", NATIVE_PATH)
native_chat = importlib.util.module_from_spec(NATIVE_SPEC)
assert NATIVE_SPEC and NATIVE_SPEC.loader
NATIVE_SPEC.loader.exec_module(native_chat)

PILOT_PATH = Path(__file__).resolve().parents[3] / "skills/sage-eval-gauge/scripts/run_trust_walker_pilot.py"
PILOT_SPEC = importlib.util.spec_from_file_location("retry_pilot", PILOT_PATH)
pilot_cli = importlib.util.module_from_spec(PILOT_SPEC)
assert PILOT_SPEC and PILOT_SPEC.loader
PILOT_SPEC.loader.exec_module(pilot_cli)


def test_raw_sdk_retries_temporary_429_and_excludes_actual_wait(monkeypatch):
    requests = []
    secret = "synthetic-secret-do-not-retain"

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "0.03"},
                                  json={"error": {"code": "rate_limit_exceeded", "message": secret}})
        return httpx.Response(200, json={
            "id": "chatcmpl-offline", "object": "chat.completion", "created": 1,
            "model": "offline-model", "choices": [{"index": 0, "message": {
                "role": "assistant", "content": "STUCK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        })

    mock = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "HTTPTransport", lambda *args, **kwargs: mock)
    events = []
    policy = provider_retry.parse_policy("transient-v1")
    state = provider_retry.RetryState(policy, event_sink=events.append)
    started = time.time()
    model = live_seams.make_model_fn(
        "openai", "offline-model", api_key="offline-placeholder",
        base_url="https://example.invalid/v1", max_retries=0, retry_state=state,
    )
    result = bare_runner.BareModelRunner(model, lambda _: "unused",
                                          elapsed_seconds_fn=lambda: state.active_elapsed_seconds(started),
                                          timeout=1).run("offline objective")
    wall = time.time() - started
    assert result.model_calls == result.model_attempts == 1
    assert result.final_text == "STUCK"
    assert len(requests) == 2
    assert state.credited_wait_seconds() >= 0.02
    assert wall - result.wall_seconds >= 0.02
    assert [event["event"] for event in state.snapshot()["events"]] == [
        "provider_response_observed", "retry_wait_started", "retry_wait_completed",
        "provider_response_observed",
    ]
    assert secret not in str(state.snapshot())


def test_native_observer_credits_completed_wait_for_active_clock(monkeypatch):
    policy = provider_retry.parse_policy("transient-v1")
    state = provider_retry.RetryState(policy)
    base = time.time() - 0.14
    snapshot = state.snapshot()
    snapshot["events"] = [
        {"event": "retry_wait_started", "event_id": "a" * 32,
         "started_at_epoch": base + 0.03, "planned_seconds": 0.10},
        {"event": "retry_wait_completed", "event_id": "a" * 32,
         "finished_at_epoch": base + 0.13, "actual_seconds": 0.10},
    ]
    snapshot["wait_completed_seconds"] = 0.10
    calls = 0

    async def fetch(_client, _request_id):
        nonlocal calls
        calls += 1
        return {"request": {"status": "complete" if calls >= 2 else "processing"},
                "messages": [{"metadata": {"runtime_telemetry": {"provider_retry": snapshot}}}]}

    monkeypatch.setattr(native_chat, "fetch_request_snapshot", fetch)
    result = asyncio.run(native_chat.wait_for_request(
        object(), 1, timeout_seconds=0.1, poll_interval_seconds=0.001,
        retry_clock={"policy": policy.to_record(), "started_at_epoch": base,
                     "active_seconds": 0.1},
    ))
    assert result["request"]["status"] == "complete"
    assert calls == 2


def test_raw_pilot_adapter_timer_extends_only_during_actual_retry_sleep(tmp_path):
    policy = provider_retry.parse_policy("transient-v1")
    pilot = object.__new__(pilot_cli.RuntimePilot)
    pilot.artifact_dir = tmp_path
    pilot.provider_retry_policy = policy
    pilot.matrix_wall_seconds = 0.30
    pilot.current_started_at = datetime.now(UTC)
    pilot._resume_arm_active = False
    pilot._raw_active_adapter_timer = False
    events = []
    state = pilot._raw_retry_state(events.append)
    pilot.current_retry_state = state
    state.bind_active_deadline((pilot.current_started_at + timedelta(seconds=0.30)).timestamp())
    observed = []

    def respond(request):
        observed.append(request)
        if len(observed) == 1:
            return httpx.Response(429, headers={"retry-after": "0.35"},
                                  json={"error": {"code": "rate_limit_exceeded"}})
        return httpx.Response(200, json={"ok": True})

    transport = provider_retry.RetryingHTTPTransport(httpx.MockTransport(respond), state)
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions", json={})
    hard_deadline = time.monotonic() + 1.0
    response = pilot._call_raw_adapter(hard_deadline, transport.handle_request, request)
    assert response.status_code == 200
    assert len(observed) == 2
    assert state.completed_wait_seconds() >= 0.34
    assert state.active_elapsed_seconds(pilot.current_started_at.timestamp()) < 0.30
    assert [event["event"] for event in events if event["event"].startswith("retry_wait_")] == [
        "retry_wait_started", "retry_wait_completed",
    ]


def test_native_observer_stops_at_active_limit_without_retry_wait(monkeypatch):
    async def fetch(_client, _request_id):
        return {"request": {"status": "processing"}, "messages": []}

    monkeypatch.setattr(native_chat, "fetch_request_snapshot", fetch)
    started = time.monotonic()
    with pytest.raises(native_chat.RequestWaitTimeout):
        asyncio.run(native_chat.wait_for_request(
            object(), 2, poll_interval_seconds=0.005,
            retry_clock={"policy": provider_retry.parse_policy("transient-v1").to_record(),
                         "started_at_epoch": time.time(), "active_seconds": 0.03},
        ))
    assert time.monotonic() - started < 0.2
