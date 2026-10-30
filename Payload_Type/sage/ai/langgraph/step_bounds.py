"""Per-step bounds for supervised worker turns, and the typed outcome Sage writes when a step ends.

A supervised step is one worker invocation, from a Supervisor handoff until control returns. It ends at its one
guarded action, a tool-call bound, an active-time bound, or a run of tool errors. Time spent waiting for an
operator's approval, and time inside a guarded action (which includes the Mythic task wait, capped separately),
never counts toward the active-time bound: the bound limits the agent's own work, not the operator's decision time.

The outcome is written by Sage from what happened in the step, never by the model. Pure: callers pass the step's
messages, the clock readings and the guarded tool set.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from typing import Any, Iterable, Mapping

STEP_OUTCOME_VERSION = 1
QUEUED_PREFIX = "[Queued]"
DENIED_PREFIX = "[DENIED by operator]"
# Routing results that can appear in a worker's context; they are not work the step did.
CONTROL_TOOLS = frozenset({"handback_to_supervisor", "summarize_and_handback", "request_continuation",
                           "respond_to_user"})
CONTROL_PLANE = "control_plane"


class StepStatus(str, Enum):
    ACTION_EXECUTED = "action_executed"      # its one guarded action ran; a further one needs a new step
    REJECTED = "rejected"                    # the operator denied its guarded action
    REJECTED_BY_SCOPE = "rejected_by_scope"  # it proposed a guarded action outside its brief
    BUDGET_EXHAUSTED = "budget_exhausted"    # a tool-call, active-time or error bound was reached
    PRECONDITION_UNSATISFIABLE = "precondition_unsatisfiable"
    TASK_PENDING = "task_pending"            # a task was accepted and its result is outstanding
    SUBMISSION_UNKNOWN = "submission_unknown"  # it is unknown whether Mythic accepted the task
    ENDED = "ended"                          # the worker ended its own turn


# A step ending in one of these hands control back to the Supervisor to choose the next step; it never ends the
# request by itself. Pending and unknown submissions keep the existing checkpoint end.
CONTINUABLE = frozenset(StepStatus) - {StepStatus.TASK_PENDING, StepStatus.SUBMISSION_UNKNOWN}


def _positive(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(env.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class StepBounds:
    tool_calls: int = 20
    active_seconds: float = 600.0
    consecutive_errors: int = 3
    # One guarded action per step is a decision, not a tunable.
    guarded_actions: int = 1

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "StepBounds":
        env = os.environ if env is None else env
        return cls(
            tool_calls=int(_positive(env, "SAGE_STEP_MAX_TOOL_CALLS", cls.tool_calls)),
            active_seconds=_positive(env, "SAGE_STEP_MAX_ACTIVE_SECONDS", cls.active_seconds),
            consecutive_errors=int(_positive(env, "SAGE_STEP_MAX_CONSECUTIVE_ERRORS", cls.consecutive_errors)),
        )


@dataclass
class StepRecord:
    """Accounting for one step that survives an approval pause, keyed by request and delegated message."""

    request_id: str
    delegation_seq: int
    worker: str
    started: float
    tasks_before: frozenset[str] = frozenset()
    active_seconds: float = 0.0
    action_seconds: float = 0.0
    status: StepStatus | None = None
    bound: str = ""
    detail: str = ""

    def end(self, status: StepStatus, bound: str = "", detail: str = "") -> None:
        if self.status is None:
            self.status, self.bound, self.detail = status, bound, detail


@dataclass(frozen=True)
class StepActivity:
    tool_calls: int = 0
    guarded: tuple[tuple[str, bool], ...] = ()  # (tool name, rejected by the operator) in order
    error_streak: int = 0

    @property
    def executed(self) -> int:
        return sum(not rejected for _, rejected in self.guarded)

    @property
    def rejected(self) -> int:
        return sum(rejected for _, rejected in self.guarded)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content or "")


def step_activity(messages: Iterable[Any], guarded: frozenset[str]) -> StepActivity:
    """Tool results of one step: how many ran, which guarded actions ran or were denied, the trailing error run."""
    tool_calls, streak, results = 0, 0, []
    for message in messages:
        if getattr(message, "type", "") != "tool":
            continue
        name = str(getattr(message, "name", "") or "")
        text = _text(getattr(message, "content", "")).lstrip()
        if text.startswith(QUEUED_PREFIX) or name.startswith("transfer_to_") or name in CONTROL_TOOLS:
            continue  # a deferred proposal or a routing result, not work this step did
        tool_calls += 1
        if name in guarded:
            results.append((name, text.startswith(DENIED_PREFIX)))
        streak = streak + 1 if getattr(message, "status", "") == "error" else 0
    return StepActivity(tool_calls=tool_calls, guarded=tuple(results), error_streak=streak)


def bound_reached(activity: StepActivity, record: StepRecord, bounds: StepBounds) -> tuple[str, str] | None:
    """The bound a step has reached before its next model call, as (bound, detail)."""
    if activity.tool_calls >= bounds.tool_calls:
        return "tool_calls", f"{activity.tool_calls} tool calls (bound {bounds.tool_calls})"
    if activity.error_streak >= bounds.consecutive_errors:
        return "consecutive_errors", f"{activity.error_streak} consecutive tool errors"
    if record.active_seconds >= bounds.active_seconds:
        return "active_time", f"{record.active_seconds:.0f}s active (bound {bounds.active_seconds:.0f}s)"
    return None


def scope_violation(brief: Any, tool_name: str, capability_name: str = "") -> str:
    """Why a guarded call falls outside the step's brief; empty when it is in scope or the brief sets no scope."""
    if not isinstance(brief, Mapping):
        return ""
    capabilities = [str(item) for item in brief.get("allowed_capabilities") or []]
    tools = [str(item) for item in brief.get("allowed_tools") or []]
    if capabilities == [CONTROL_PLANE]:
        return f"`{tool_name}` is a guarded action and this step was briefed as control-plane reads only"
    if tools:
        if tool_name in tools:
            return ""
        return f"`{tool_name}` is not in this step's allowed_tools ({', '.join(tools)})"
    if capabilities:
        if tool_name != "execute_capability":
            return (f"`{tool_name}` is outside this step's capabilities ({', '.join(capabilities)}); "
                    "a catalog procedure runs through execute_capability")
        if capability_name not in capabilities:
            return f"capability `{capability_name or 'unknown'}` is not in this step's allowed_capabilities"
    return ""


def step_outcome(record: StepRecord, activity: StepActivity, *, now: float, tasks_issued: Iterable[str] = (),
                 status: StepStatus | None = None) -> dict[str, Any]:
    final = status or record.status or StepStatus.ENDED
    wall = max(0.0, now - record.started)
    return {
        "version": STEP_OUTCOME_VERSION,
        "worker": record.worker,
        "status": final.value,
        "bound": record.bound if final == record.status else "",
        "detail": record.detail if final == record.status else "",
        "tool_calls": activity.tool_calls,
        "guarded_executed": activity.executed,
        "guarded_rejected": activity.rejected,
        "tasks_issued": sorted(tasks_issued, key=lambda item: (len(item), item)),
        "active_seconds": round(record.active_seconds, 1),
        "action_seconds": round(record.action_seconds, 1),
        "waiting_seconds": round(max(0.0, wall - record.active_seconds - record.action_seconds), 1),
    }


def render_outcome(outcome: Mapping[str, Any]) -> str:
    """The Sage-authored line the Supervisor reads when a step returns."""
    reason = outcome["status"] + (f" ({outcome['detail']})" if outcome.get("detail") else "")
    tasks = ", ".join(str(task) for task in outcome.get("tasks_issued") or []) or "none"
    lines = [
        f"[step outcome] {outcome['worker']} step ended: {reason}.",
        f"Guarded actions: {outcome['guarded_executed']} executed, {outcome['guarded_rejected']} rejected by the "
        f"operator. Mythic tasks issued: {tasks}. Tool calls: {outcome['tool_calls']}.",
        f"Time: {outcome['active_seconds']:.0f}s active, {outcome['action_seconds']:.0f}s inside the guarded "
        f"action, {outcome['waiting_seconds']:.0f}s waiting (approval and other).",
    ]
    if StepStatus(outcome["status"]) in CONTINUABLE:
        lines.append("Control is back with the Supervisor: choose the next step or answer the operator."
                     + (" Returning control did not cancel any target-side task." if outcome.get("tasks_issued")
                        else ""))
    return "\n".join(lines)
