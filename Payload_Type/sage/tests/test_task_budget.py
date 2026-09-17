"""Task limits must stop actual native and raw submission seams before network I/O."""
import asyncio
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai import task_budget as tb
from ai.hillclimb import bare_mythic_tools as raw
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
