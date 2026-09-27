"""Optional per-run task admission limits, shared by native and raw-tool execution."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import math
import os
from threading import Lock
import time
from typing import Any, Callable
from uuid import uuid4

from . import resource_budget


TASK_LIMIT_ENV = "SAGE_EVAL_PAYLOAD_TASK_BUDGET"


class TaskBudgetExceeded(RuntimeError):
    """A task was refused before any submission to Mythic."""


@dataclass
class TaskBudget:
    limit: int
    deadline: float
    submissions: int = 0
    closed: bool = False
    event_sink: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False)
    task_ids: list[int] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def __post_init__(self) -> None:
        if type(self.limit) is not int or self.limit <= 0:
            raise ValueError("task limit must be a positive integer")
        if not math.isfinite(self.deadline):
            raise ValueError("task deadline must be finite")

    def remaining_seconds(self) -> float:
        """Return time left in the fixed task-admission window."""
        with self._lock:
            return max(0.0, self.deadline - time.monotonic())

    def admit(self) -> float:
        with self._lock:
            if self.closed:
                raise TaskBudgetExceeded("task budget scope is closed")
            if time.monotonic() >= self.deadline:
                raise TaskBudgetExceeded("task execution deadline reached")
            if self.submissions >= self.limit:
                raise TaskBudgetExceeded("payload task submission budget exhausted")
            # Reserve before I/O; a response-loss failure may still have created a task.
            self.submissions += 1
            remaining = max(0.0, self.deadline - time.monotonic())
            if self.event_sink is not None:
                self.event_sink({
                    "event": "payload_task_reserved",
                    "submission_index": self.submissions,
                    "limit": self.limit,
                    "remaining_seconds": remaining,
                })
            return remaining

    def record_task_id(self, task_id: int) -> None:
        with self._lock:
            if type(task_id) is not int or task_id <= 0 or task_id in self.task_ids:
                raise ValueError("payload task identity is missing or duplicated")
            self.task_ids.append(task_id)
            if self.event_sink is not None:
                self.event_sink({
                    "event": "payload_task_submitted",
                    "task_id": task_id,
                    "submission_index": len(self.task_ids),
                })

    def record_task_result(self, task_id: int) -> None:
        if self.event_sink is not None:
            self.event_sink({"event": "payload_task_result_observed", "task_id": task_id})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "limit": self.limit,
                "reservations": self.submissions,
                "submissions": self.submissions,
                "closed": self.closed,
                "confirmed_task_ids": list(self.task_ids),
                "task_ids": list(self.task_ids),
            }

    def close(self) -> None:
        with self._lock:
            self.closed = True


_active: ContextVar[TaskBudget | None] = ContextVar("sage_task_budget", default=None)


def _capture(payload: dict[str, Any]) -> None:
    from . import observation_channel
    if os.environ.get(observation_channel.ENV_NAME, ""):
        observation_channel.emit("tool_event", payload)


def configured_budget(wall_seconds: float, *, deadline_epoch: float | None = None) -> TaskBudget | None:
    raw = os.environ.get(TASK_LIMIT_ENV, "").strip()
    if not raw:
        return None
    remaining = float(wall_seconds)
    if deadline_epoch is not None:
        remaining = min(remaining, max(0.0, float(deadline_epoch) - time.time()))
    return TaskBudget(int(raw), time.monotonic() + remaining)


@contextmanager
def task_budget_scope(budget: TaskBudget | None):
    token = _active.set(budget)
    try:
        yield budget
    finally:
        if budget is not None:
            budget.close()
        _active.reset(token)


def admit_task() -> float | None:
    attempt_id = uuid4().hex
    _capture({"event": "payload_task_admission_attempt", "attempt_id": attempt_id})
    try:
        budget = _active.get()
        if budget is not None:
            remaining = budget.admit()
        elif os.environ.get(TASK_LIMIT_ENV, "").strip():
            raise TaskBudgetExceeded("configured task budget has no active run scope")
        else:
            remaining = None
        resource_budget.reserve_configured({"payload_task_attempts": 1}, "payload-task-attempt")
    except BaseException as exc:
        _capture({"event": "payload_task_admission_result", "attempt_id": attempt_id,
                  "outcome": "denied_before_dispatch", "exception_type": type(exc).__name__})
        raise
    _capture({"event": "payload_task_admission_result", "attempt_id": attempt_id, "outcome": "admitted"})
    return remaining


def current_snapshot():
    budget = _active.get()
    return budget.snapshot() if budget is not None else None


def current_remaining_seconds() -> float | None:
    budget = _active.get()
    return budget.remaining_seconds() if budget is not None else None


def record_task_id(task_id: int) -> None:
    budget = _active.get()
    if budget is not None:
        budget.record_task_id(task_id)
    _capture({"event": "payload_task_identity_returned", "task_id": task_id})


def record_task_result(task_id: int) -> None:
    budget = _active.get()
    if budget is not None:
        budget.record_task_result(task_id)
