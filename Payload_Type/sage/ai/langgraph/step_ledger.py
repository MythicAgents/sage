"""Callback context gains Mythic recorded, ready for the engagement ledger.

The ledger records progress, not events: a row is a capability the engagement now holds, keyed by its effect, so a
repeat updates its row rather than adding one. This module finds callback context gains: a new session on a host,
or a rise in a callback's integrity, measured against the operation's stored callback baseline. Each gain is
credited to the operation task that caused it, whoever issued it. A gain no task can be credited with is proven by a
completed task on the gained callback itself. Losing a context, a token or impersonation change, and a task that
merely ran produce nothing; a gain with neither a credited task nor a completed task on its callback is reported as
unattributed and recorded nowhere.

Pure: callers pass Mythic's callback and task rows and the stored baseline.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
from typing import Any, Iterable, Mapping

try:
    from .access_reconciler import _normalize_integrity
except ImportError:  # pragma: no cover
    from access_reconciler import _normalize_integrity  # type: ignore

ATTRIBUTION_WINDOW_SECONDS = 600.0


def attribution_window(env: Mapping[str, str] | None = None) -> float:
    """Seconds after a task during which a callback's first check-in is credited to it (configurable)."""
    env = os.environ if env is None else env
    try:
        value = float(env.get("SAGE_STEP_LEDGER_ATTRIBUTION_SECONDS", ""))
    except (TypeError, ValueError):
        return ATTRIBUTION_WINDOW_SECONDS
    return value if value > 0 else ATTRIBUTION_WINDOW_SECONDS
TECHNIQUE = "callback-context"
VERIFIER_ID = "mythic_callback:context_observed"
_RANK = {"low": 1, "medium": 2, "high": 3, "admin": 3, "administrator": 3, "elevated": 3, "system": 4}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _when(value: Any) -> datetime | None:
    """A Mythic timestamp (stored without a zone, in UTC) as an aware datetime."""
    text = _text(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CallbackContext:
    display_id: int
    agent_callback_id: str
    host: str
    user: str
    integrity: str
    init_callback: str

    def row(self) -> dict[str, Any]:
        """The canonical callback row a proof commits to."""
        return asdict(self)


def callback_context(row: Mapping[str, Any]) -> CallbackContext | None:
    display_id = row.get("display_id")
    if type(display_id) is not int or display_id < 0 or not _text(row.get("agent_callback_id")):
        return None
    return CallbackContext(
        display_id=display_id, agent_callback_id=_text(row.get("agent_callback_id")),
        host=_text(row.get("host")).casefold(), user=_text(row.get("user")).casefold(),
        integrity=_normalize_integrity(dict(row)), init_callback=_text(row.get("init_callback")),
    )


_TERMINAL_SUCCESS = frozenset({"completed", "complete", "success", "succeeded"})


@dataclass(frozen=True)
class OperationTask:
    """A Mythic task in the operation, from any issuer.

    ``issued_at`` is when Mythic created the task record (``status_timestamp_preprocessing``, UTC). It is not the
    task's mutable ``timestamp``, which is its last update, nor when the payload ran it.
    """

    task_id: int
    callback_display_id: int | None
    command: str
    status: str
    issued_at: str

    @property
    def succeeded(self) -> bool:
        return _text(self.status).casefold() in _TERMINAL_SUCCESS


@dataclass(frozen=True)
class ContextGain:
    kind: str  # new_callback | integrity_rise
    context: CallbackContext
    effects: tuple[str, ...]
    task: OperationTask | None  # the task credited with causing the gain
    proof_task: OperationTask | None = None  # the latest completed task on the gained callback


def _privilege_effects(context: CallbackContext) -> list[str]:
    if not context.host:
        return []
    if context.integrity == "system":
        return [f"system:{context.host}"]
    if _RANK.get(context.integrity, 0) >= 3:
        return [f"admin:{context.host}"]
    return []


def _credit_new_callback(context: CallbackContext, tasks: Iterable[OperationTask], window: float) -> OperationTask | None:
    """The latest task created before the callback first checked in, within the window (inclusive at both
    ends). A task without a valid creation time gets no time-based credit; no other clock stands in for it.
    """
    checked_in = _when(context.init_callback)
    if checked_in is None:
        return None
    best: tuple[datetime, OperationTask] | None = None
    for task in tasks:
        issued = _when(task.issued_at)
        if issued is None or not (0.0 <= (checked_in - issued).total_seconds() <= window):
            continue
        if best is None or issued > best[0]:
            best = (issued, task)
    return best[1] if best else None


def checked_in(context: CallbackContext) -> datetime | None:
    """When the callback first checked in, per Mythic."""
    return _when(context.init_callback)


def baseline_of(callbacks: Iterable[CallbackContext]) -> dict[str, str]:
    """The stored form of a callback baseline: display ID to integrity level."""
    return {str(context.display_id): context.integrity for context in callbacks}


def context_gains(callbacks: Iterable[CallbackContext], baseline: Mapping[str, str],
                  tasks: Iterable[OperationTask], *, window: float = ATTRIBUTION_WINDOW_SECONDS) -> list[ContextGain]:
    """Gains between the stored baseline and Mythic's current callbacks.

    `tasks` are operation tasks from any issuer, with their Mythic creation times. A new callback is credited by
    time, so a payload that checks in after its task finished is still credited at the next reconciliation. A rise
    in integrity is credited to the latest task on that same callback. Each gain also names the latest completed
    task on the gained callback, which proves the gain when no task can be credited.
    """
    tasks = list(tasks)
    gains: list[ContextGain] = []
    for context in callbacks:
        prior = baseline.get(str(context.display_id))
        on_callback = [task for task in tasks if task.callback_display_id == context.display_id]
        succeeded = [task for task in on_callback if task.succeeded]
        proof = max(succeeded, key=lambda task: task.task_id) if succeeded else None
        if prior is None:
            effects = ([f"session:{context.user}@{context.host}"] if context.user and context.host else []) \
                + _privilege_effects(context)
            if effects:
                gains.append(ContextGain("new_callback", context, tuple(effects),
                                         _credit_new_callback(context, tasks, window), proof))
            continue
        if _RANK.get(context.integrity, 0) > _RANK.get(prior, 0):
            effects = _privilege_effects(context)
            if effects:
                gains.append(ContextGain("integrity_rise", context, tuple(effects),
                                         max(on_callback, key=lambda task: task.task_id) if on_callback else None,
                                         proof))
    return gains


def unattributed(gains: Iterable[ContextGain]) -> list[dict[str, Any]]:
    """Gains with neither a credited task nor a completed task on the gained callback."""
    return [{"callback_display_id": gain.context.display_id, "kind": gain.kind, "effects": list(gain.effects)}
            for gain in gains if gain.task is None and gain.proof_task is None]
