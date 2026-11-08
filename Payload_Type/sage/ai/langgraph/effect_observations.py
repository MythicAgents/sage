"""Normalized, secret-free observations of Mythic records, and the joins that wait for a later record.

Reconciliation fetches Mythic's own records (tasks, their callbacks, credential-store entries). This module keeps
the parts a verifier needs as a normalized observation: the source record, the task and callback identity (the
callback's agent ID beside its display ID, so a reused display ID cannot join two callback lifetimes), a stable
subject, the observation time, the exact structured values, references to supporting records, and commitments over
them. Values are copied from an allow-list of fields; a secret (credential text, task parameters, response text)
never enters an observation.

A *join* is an observation whose proof is expected from a later record: for example a terminal credential task whose
Mythic credential-store entry is not there yet. The store keeps it beside the ledger, so a later pass resolves it
after the task cursor has moved on and across process restarts. Observations are evidence indexing, not progress:
nothing here admits a proof or writes a ledger row.

Pure: no transport, no model call, no task issue. No evaluation, range or operator package is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

try:
    from . import credential_artifacts, task_evidence
except ImportError:  # pragma: no cover - flat import for tests that put ai/langgraph on sys.path
    import credential_artifacts  # type: ignore
    import task_evidence  # type: ignore


VERIFIER_VERSION = "v1"
NO_JOIN = "none"
JOIN_AWAITING = "awaiting"
JOIN_RESOLVED = "resolved"
JOIN_EXPIRED = "expired"
JOIN_STATES = (NO_JOIN, JOIN_AWAITING, JOIN_RESOLVED, JOIN_EXPIRED)

# A terminal credential task whose credential-store entry has not appeared yet.
CREDENTIAL_ENTRY_JOIN = "credential_entry"
CREDENTIAL_ENTRY_VERIFIER = "effect_observations:credential_entry"
# How long a pass keeps looking for that entry. Expiry stops the polling, not the proof: citing the entry with
# `/state reconcile cred:<id>` still records it.
CREDENTIAL_JOIN_WINDOW_SECONDS = 3600
# Awaiting joins one pass reads, oldest first.
JOIN_LIMIT = 50
MAX_REASON_CHARS = 200

# Task fields an observation may carry. Parameters and output are excluded: they can hold secrets.
_TASK_FIELDS = ("operation_id", "command_name", "status_timestamp_processed")
# Credential-store fields an observation may carry. ``credential_text`` and ``comment`` are excluded.
_CREDENTIAL_FIELDS = ("id", "operation_id", "account", "realm", "type")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _bounded(value: Any) -> str:
    return _text(value)[:MAX_REASON_CHARS]


@dataclass(frozen=True)
class Observation:
    """One normalized observation. ``observation_id`` is stable per (kind, source, subject), so recording the same
    observation again is a no-op in the store; ``values_sha256`` commits to what the verifier will read."""

    kind: str
    source_kind: str
    source_id: str
    subject: str
    observed_at: str
    values: Mapping[str, Any] = field(default_factory=dict)
    task_id: str = ""
    callback_id: str = ""
    agent_callback_id: str = ""
    supporting: tuple[str, ...] = ()
    verifier_id: str = ""
    verifier_version: str = VERIFIER_VERSION
    reason: str = ""
    join_state: str = NO_JOIN

    @property
    def observation_id(self) -> str:
        return hashlib.sha256(
            f"observation|{self.kind}|{self.source_kind}|{self.source_id}|{self.subject}".encode()).hexdigest()

    @property
    def values_sha256(self) -> str:
        return _sha256(dict(self.values))

    def payload(self) -> dict[str, Any]:
        return {
            "values": dict(self.values),
            "supporting": list(self.supporting),
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "values_sha256": self.values_sha256,
        }

    def record(self) -> dict[str, Any]:
        """Keyword arguments for the store's observation record."""
        return {
            "observation_id": self.observation_id,
            "kind": self.kind,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "subject": self.subject,
            "task_id": self.task_id,
            "callback_id": self.callback_id,
            "agent_callback_id": self.agent_callback_id,
            "observed_at": self.observed_at,
            "payload": self.payload(),
            "join_state": self.join_state,
            "join_reason": _bounded(self.reason),
        }


def normalize_task(task: Mapping[str, Any] | None) -> dict[str, Any]:
    """The task's identity and shared classification from its Mythic record; {} without a display ID."""
    if not isinstance(task, Mapping):
        return {}
    record = task_evidence.task_record_from_row(task)
    if record is None:
        return {}
    callback = task.get("callback") if isinstance(task.get("callback"), Mapping) else {}
    values: dict[str, Any] = {key: task.get(key) for key in _TASK_FIELDS if task.get(key) not in (None, "")}
    values.update({
        "task_id": record.task_id,
        "callback_id": record.callback_id,
        "agent_callback_id": _text(callback.get("agent_callback_id")),
        "task_classification": task_evidence.classify_task(record),
    })
    return values


def normalize_credential(credential: Mapping[str, Any] | None) -> dict[str, Any]:
    """A credential-store entry's identity, account, realm, type and producing task; never its secret."""
    if not isinstance(credential, Mapping) or credential.get("id") in (None, ""):
        return {}
    values = {key: credential.get(key) for key in _CREDENTIAL_FIELDS if credential.get(key) not in (None, "")}
    for key in ("account", "realm"):
        if key in values:
            values[key] = _text(values[key]).casefold()
    task = credential.get("task") if isinstance(credential.get("task"), Mapping) else {}
    if task.get("display_id") is not None:
        values["task_id"] = _text(task.get("display_id"))
        values["task_operation_id"] = task.get("operation_id")
    return values


def credential_join(task: Mapping[str, Any] | None, technique: str, target: str, observed_at: str) -> Observation | None:
    """An awaiting join for a terminal credential task with no credential-store entry yet, else None.

    Only a task the shared classifier calls terminal success qualifies: a failed task's entry will not come, and a
    task Mythic is still finishing is held by the cursor instead. ``technique``/``target`` are the task's modeled
    intent, which names the subject the entry is expected to prove.
    """
    if _text(technique) not in credential_artifacts.CREDENTIAL_TECHNIQUES:
        return None
    values = normalize_task(task)
    if not values or values.get("task_classification") != task_evidence.TERMINAL_SUCCESS:
        return None
    subject = f"{_text(technique).casefold()}:{_text(target).casefold()}"
    values.update({"technique": _text(technique).casefold(), "target": _text(target).casefold()})
    task_id = str(values["task_id"])
    return Observation(
        kind=CREDENTIAL_ENTRY_JOIN, source_kind="task", source_id=task_id, subject=subject,
        observed_at=_text(observed_at), values=values, task_id=task_id, callback_id=str(values["callback_id"]),
        agent_callback_id=str(values["agent_callback_id"]), supporting=(f"task:{task_id}",),
        verifier_id=CREDENTIAL_ENTRY_VERIFIER, reason="awaiting the task's Mythic credential-store entry",
        join_state=JOIN_AWAITING,
    )


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def join_expired(observed_at: Any, now: Any, window_seconds: int | None = None) -> bool:
    """Whether an awaiting join observed at ``observed_at`` is past its window at ``now``. An unreadable time never
    expires a join by itself (it stays bounded by the per-pass join limit)."""
    start, end = _parse_time(observed_at), _parse_time(now)
    if start is None or end is None:
        return False
    window = CREDENTIAL_JOIN_WINDOW_SECONDS if window_seconds is None else window_seconds
    return (end - start).total_seconds() > window
