"""Task limits must stop actual native and raw submission seams before network I/O."""
import asyncio
import base64
import httpx
import json
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai import task_budget as tb
from ai import resource_budget
from ai import resource_http
from ai import observation_channel
from ai.hillclimb import bare_mythic_tools as raw
from ai.hillclimb import bare_runner
from ai.langgraph import mythic_tools as native
from ai.langgraph.model import Model


@pytest.mark.parametrize("surface", ["native", "parameter_group", "raw"])
def test_hundred_submissions_then_no_network(surface, monkeypatch):
    calls = []

    async def issue(*args, **kwargs):
        calls.append(kwargs)
        return {"display_id": len(calls)}

    async def post(*args, **kwargs):
        calls.append(kwargs)
        return {"createTask": {"status": "success", "display_id": len(calls)}}

    monkeypatch.setattr(native.mythic, "issue_task", issue)
    monkeypatch.setattr(native.mythic_utilities, "graphql_post", post)
    monkeypatch.setattr(native.mythic, "waitfor_for_task_output", AsyncMock(return_value="ok"))
    tools = object.__new__(native.MythicTools)
    tools.client = object()

    async def submit():
        if surface == "raw":
            return await raw._issue_command(tools.client, 1, "test")
        return await tools._issue_mythic_task(
            "test", "", 1, parameter_group_name="group" if surface == "parameter_group" else None
        )

    async def run():
        budget = tb.TaskBudget(100, time.monotonic() + 30)
        with tb.task_budget_scope(budget):
            for _ in range(100):
                await submit()
            with pytest.raises(tb.TaskBudgetExceeded, match="exhausted"):
                await submit()
        assert budget.closed

    asyncio.run(run())
    assert len(calls) == 100


def test_deadline_and_closed_child_refuse_before_issue(monkeypatch):
    called = AsyncMock()
    monkeypatch.setattr(native.mythic, "issue_task", called)
    tools = object.__new__(native.MythicTools)
    tools.client = object()

    async def run():
        with tb.task_budget_scope(tb.TaskBudget(2, time.monotonic() - 1)):
            with pytest.raises(tb.TaskBudgetExceeded, match="deadline"):
                await tools._issue_mythic_task("test", "", 1)
        ready = asyncio.Event()
        async def late():
            await ready.wait()
            await tools._issue_mythic_task("test", "", 1)
        with tb.task_budget_scope(tb.TaskBudget(2, time.monotonic() + 30)):
            child = asyncio.create_task(late())
        ready.set()
        with pytest.raises(tb.TaskBudgetExceeded, match="closed"):
            await child
    asyncio.run(run())
    called.assert_not_called()


def test_ambiguous_transport_error_consumes_reservation(monkeypatch):
    called = AsyncMock(side_effect=TimeoutError("response lost"))
    monkeypatch.setattr(native.mythic, "issue_task", called)
    tools = object.__new__(native.MythicTools)
    tools.client = object()
    async def run():
        with tb.task_budget_scope(tb.TaskBudget(1, time.monotonic() + 30)):
            with pytest.raises(TimeoutError):
                await tools._issue_mythic_task("test", "", 1)
            with pytest.raises(tb.TaskBudgetExceeded):
                await tools._issue_mythic_task("test", "", 1)
    asyncio.run(run())
    assert called.await_count == 1


def test_disabled_path_and_missing_scope(monkeypatch):
    monkeypatch.delenv(tb.TASK_LIMIT_ENV, raising=False)
    assert tb.configured_budget(10) is None
    tb.admit_task()
    monkeypatch.setenv(tb.TASK_LIMIT_ENV, "100")
    with pytest.raises(tb.TaskBudgetExceeded, match="no active run"):
        tb.admit_task()


def test_absolute_epoch_clamps_configured_task_deadline(monkeypatch):
    monkeypatch.setenv(tb.TASK_LIMIT_ENV, "100")
    budget = tb.configured_budget(2700, deadline_epoch=time.time() + 0.02)
    assert budget is not None
    assert 0 <= budget.remaining_seconds() <= 0.03


@pytest.mark.parametrize("setting", ["0", "-1", "invalid", "1.5"])
def test_bad_config_cannot_disable_guard(monkeypatch, setting):
    monkeypatch.setenv(tb.TASK_LIMIT_ENV, setting)
    with pytest.raises(ValueError):
        tb.configured_budget(10)


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_deadline_cannot_disable_guard(deadline):
    with pytest.raises(ValueError, match="finite"):
        tb.TaskBudget(100, deadline)


def test_controller_installs_and_closes_actual_task_scope(monkeypatch):
    monkeypatch.setenv(tb.TASK_LIMIT_ENV, "1")
    model = object.__new__(Model)
    model.set_active_agent = lambda name: None
    observed = []
    async def kernel(prompt):
        tb.admit_task()
        observed.append(tb.current_snapshot())
        with pytest.raises(tb.TaskBudgetExceeded):
            tb.admit_task()
        return "bounded"
    model._run_autonomous_controller_kernel = kernel
    assert asyncio.run(model._run_autonomous_controller("test")) == "bounded"
    assert observed == [{
        "limit": 1,
        "reservations": 1,
        "submissions": 1,
        "closed": False,
        "confirmed_task_ids": [],
        "task_ids": [],
    }]
    assert tb.current_snapshot() is None


def test_real_operator_stop_cancels_controller_and_closes_inherited_budget(monkeypatch):
    monkeypatch.setenv(tb.TASK_LIMIT_ENV, "100")
    issued = AsyncMock(return_value={"display_id": 1})
    monkeypatch.setattr(native.mythic, "issue_task", issued)
    tools = object.__new__(native.MythicTools)
    tools.client = object()
    model = object.__new__(Model)
    model.task_id = 1
    model._running_tasks = set()
    model._stop_requested = False
    model.set_active_agent = lambda name: None

    async def run():
        ready, release_late = asyncio.Event(), asyncio.Event()
        children = []
        async def late():
            await release_late.wait()
            await tools._issue_mythic_task("test", "", 1)
        async def kernel(prompt):
            await tools._issue_mythic_task("test", "", 1)
            children.append(asyncio.create_task(late()))
            ready.set()
            await asyncio.Event().wait()
        model._run_autonomous_controller_kernel = kernel
        task = asyncio.create_task(model._run_autonomous_controller("offline fixture"))
        model._register_running_task(task)
        await ready.wait()
        model.request_stop("operator")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert model._stop_requested is True
        release_late.set()
        with pytest.raises(tb.TaskBudgetExceeded, match="closed"):
            await children[0]
        assert tb.current_snapshot() is None
    asyncio.run(run())
    assert issued.await_count == 1


def _shared_task_config(tmp_path, monkeypatch, limit=2):
    config, ledger = tmp_path / "resource-config.json", tmp_path / "resource-ledger.jsonl"
    config.write_text(json.dumps({
        "schema": resource_budget.CONFIG_SCHEMA,
        "resources": {"payload_task_attempts": {"limit": limit, "cleanup_reserve": 0}},
        "calendar_seconds": 100,
        "cleanup_seconds": 10,
    }))
    monkeypatch.setenv(resource_budget._CONFIG_ENV, str(config))
    monkeypatch.setenv(resource_budget._LEDGER_ENV, str(ledger))
    monkeypatch.delenv(tb.TASK_LIMIT_ENV, raising=False)
    return resource_budget.ResourceLedger(config, ledger)


def test_shared_cap_stops_native_and_raw_task_paths_through_one_admission_seam(tmp_path, monkeypatch):
    shared = _shared_task_config(tmp_path, monkeypatch)
    calls = []

    async def issue(*args, **kwargs):
        calls.append(kwargs)
        return {"display_id": len(calls)}

    monkeypatch.setattr(native.mythic, "issue_task", issue)
    monkeypatch.setattr(native.mythic, "waitfor_for_task_output", AsyncMock(return_value="ok"))
    tools = object.__new__(native.MythicTools)
    tools.client = object()

    async def run():
        await tools._issue_mythic_task("native-test", "", 1)
        await raw._issue_command(tools.client, 1, "raw-test")
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            await tools._issue_mythic_task("refused", "", 1)

    asyncio.run(run())
    assert len(calls) == 2
    assert shared.status()["effective_resource_charges"] == {"payload_task_attempts": 2}


def test_local_task_rejection_precedes_aggregate_reservation(tmp_path, monkeypatch):
    shared = _shared_task_config(tmp_path, monkeypatch)
    async def run():
        with tb.task_budget_scope(tb.TaskBudget(1, time.monotonic() + 10)):
            tb.admit_task()
            with pytest.raises(tb.TaskBudgetExceeded):
                tb.admit_task()
    asyncio.run(run())
    assert shared.status()["effective_resource_charges"] == {"payload_task_attempts": 1}


def test_passive_admission_events_cover_local_shared_and_success(tmp_path, monkeypatch):
    shared = _shared_task_config(tmp_path, monkeypatch, limit=1); events = []
    monkeypatch.setenv(observation_channel.ENV_NAME, "/configured")
    monkeypatch.setattr(observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))
    with tb.task_budget_scope(tb.TaskBudget(1, time.monotonic() + 10)):
        tb.admit_task()
        with pytest.raises(tb.TaskBudgetExceeded): tb.admit_task()
    monkeypatch.delenv(tb.TASK_LIMIT_ENV, raising=False)
    with pytest.raises(resource_budget.ResourceBudgetExceeded): tb.admit_task()
    attempts = [payload for kind, payload in events if payload["event"] == "payload_task_admission_attempt"]
    results = [payload for kind, payload in events if payload["event"] == "payload_task_admission_result"]
    assert len(attempts) == len(results) == 3
    assert [row["outcome"] for row in results] == ["admitted", "denied_before_dispatch", "denied_before_dispatch"]
    assert {row["attempt_id"] for row in attempts} == {row["attempt_id"] for row in results}
    assert [row.get("exception_type") for row in results] == [None, "TaskBudgetExceeded", "ResourceBudgetExceeded"]
    assert shared.status()["effective_resource_charges"]["payload_task_attempts"] == 1


def test_capture_records_missing_scope_and_returned_id_without_join(monkeypatch):
    events = []; monkeypatch.setenv(observation_channel.ENV_NAME, "/configured")
    monkeypatch.setenv(tb.TASK_LIMIT_ENV, "1")
    monkeypatch.setattr(observation_channel, "emit", lambda kind, payload: events.append(payload))
    with pytest.raises(tb.TaskBudgetExceeded): tb.admit_task()
    tb.record_task_id(31)
    assert events[-1] == {"event": "payload_task_identity_returned", "task_id": 31}
    assert "attempt_id" not in events[-1]


def test_no_observation_environment_has_no_emitter_call(monkeypatch):
    monkeypatch.delenv(observation_channel.ENV_NAME, raising=False)
    monkeypatch.setattr(observation_channel, "emit", lambda *_args: pytest.fail("unexpected observation"))
    tb.admit_task(); tb.record_task_id(9)


def _shared_provider_task_config(tmp_path, monkeypatch):
    config, ledger_path = tmp_path / "provider-task.json", tmp_path / "provider-task.jsonl"
    config.write_text(json.dumps({
        "schema": resource_budget.CONFIG_SCHEMA,
        "resources": {
            "provider_requests": {"limit": 10, "cleanup_reserve": 0},
            "provider_tokens": {"limit": 100_000, "cleanup_reserve": 0},
            "payload_task_attempts": {"limit": 10, "cleanup_reserve": 0},
            "control_requests": {"limit": 10, "cleanup_reserve": 2},
        }, "calendar_seconds": 100, "cleanup_seconds": 10,
    }))
    monkeypatch.setenv(resource_budget._CONFIG_ENV, str(config))
    monkeypatch.setenv(resource_budget._LEDGER_ENV, str(ledger_path))
    monkeypatch.delenv(tb.TASK_LIMIT_ENV, raising=False)
    return resource_budget.ResourceLedger(config, ledger_path)


@pytest.mark.parametrize("actual", (None, resource_http.PROVIDER_TOKEN_RESERVATION + 1))
@pytest.mark.parametrize("surface", ("native", "raw"))
def test_provider_boundary_state_denies_native_and_raw_tasks_before_dispatch(
    tmp_path, monkeypatch, actual, surface,
):
    ledger = _shared_provider_task_config(tmp_path, monkeypatch)
    reservation = ledger.reserve({"provider_requests": 1, "provider_tokens": 32768}, "model")
    if actual is None:
        ledger.settle_provider(reservation, None)
    else:
        ledger.settle_provider(reservation, actual)
    issued = AsyncMock()
    monkeypatch.setattr(native.mythic, "issue_task", issued)
    tools = object.__new__(native.MythicTools); tools.client = object()
    async def run():
        with pytest.raises(resource_budget.ResourceBudgetExceeded, match="payload task"):
            if surface == "native":
                await tools._issue_mythic_task("blocked", "", 1)
            else:
                await raw._issue_command(tools.client, 1, "blocked")
    asyncio.run(run())
    issued.assert_not_awaited()
    assert ledger.status()["effective_resource_charges"]["provider_tokens"] == (
        32768 if actual is None else actual
    )


def test_exact_provider_boundary_allows_task_and_cleanup_control(tmp_path, monkeypatch):
    ledger = _shared_provider_task_config(tmp_path, monkeypatch)
    reservation = ledger.reserve({"provider_requests": 1, "provider_tokens": 32768}, "model")
    ledger.settle_provider(reservation, 32768)
    assert tb.admit_task() is None
    cleanup = resource_budget.ResourceLedger(
        ledger.config_path, ledger.ledger_path, default_cleanup=True,
    )
    assert cleanup.reserve({"control_requests": 1}, "cleanup-observation")


def test_raw_runner_withholds_overrun_toolcall_and_retains_cost(monkeypatch, tmp_path):
    ledger = _shared_provider_task_config(tmp_path, monkeypatch)
    marker = "SYNTHETIC-TOOL-BODY"
    body = json.dumps({"usage": {"total_tokens": 32769},
                      "decision": {"tool": "issue", "args": {"value": marker}}}).encode()
    observations, tool_calls, logs = [], [], []
    monkeypatch.setenv(resource_http.observation_channel.ENV_NAME, "/synthetic-observer")
    monkeypatch.setattr(
        resource_http.observation_channel, "emit",
        lambda kind, payload: observations.append((kind, payload)) or True,
    )
    dispatches = []
    transport = resource_http.MeteredHTTPTransport(
        "provider", ledger=ledger,
        transport=httpx.MockTransport(
            lambda request: dispatches.append((request.method, str(request.url)))
            or httpx.Response(200, content=body)
        ),
    )
    def model_fn(*_args):
        with httpx.Client(transport=transport) as client:
            return client.post("https://model.invalid/private-route", content=b"request-body").json()["decision"]
    result = bare_runner.BareModelRunner(
        model_fn, lambda call: tool_calls.append(call), max_steps=1, logger=logs.append,
    ).run("objective")
    assert result.stopped == "error" and tool_calls == [] and dispatches == [("POST", "https://model.invalid/private-route")]
    assert result.termination == {
        "cause": "per_request_context_limit", "subtype": "completed_response_boundary",
    }
    assert ledger.status()["effective_resource_charges"]["provider_tokens"] == 32769
    assert ledger.status()["provider_overrun"] is True
    returned = json.dumps(result.__dict__, default=str) + json.dumps(logs)
    assert marker not in returned and "private-route" not in returned and "request-body" not in returned
    response = next(payload for kind, payload in observations if kind == "model_response")
    assert base64.b64decode(response["body_base64"]) == body
