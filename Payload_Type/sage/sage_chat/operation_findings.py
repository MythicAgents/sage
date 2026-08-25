"""Deterministic, operation-scoped lifecycle for the assisted findings view.

This module accepts typed candidates and persists only derived state. It does
not decide attacker relevance, call a model, emit to Mythic, or task a callback.
"""

from __future__ import annotations

from collections.abc import Mapping as RuntimeMapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Any, Awaitable, Callable, Iterable, Mapping

from .operation_memory import OperationMemoryStore, _json, _required_text, _utc_now


MAX_ACTIVE_FINDINGS = 5
FINDING_DELIVERY_SINKS = ("mythic_chat", "mythic_eventlog", "slack")
_FINDING_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _typed_text(value: Any, name: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{name} is required")
    return text


def _typed_collection(value: Any, name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray, RuntimeMapping)):
        raise ValueError(f"{name} must be a non-scalar iterable")
    try:
        return tuple(value)
    except TypeError as exc:
        raise ValueError(f"{name} must be a non-scalar iterable") from exc


class FindingState(StrEnum):
    NEW = "new"
    ASSESSING = "assessing"
    RESOLVED = "resolved"
    INVALIDATED = "invalidated"
    STALE = "stale"


ACTIVE_STATES = frozenset(
    {FindingState.NEW, FindingState.ASSESSING, FindingState.STALE}
)


@dataclass(frozen=True, order=True)
class EvidencePointer:
    record_class: str
    source_record_id: str
    revision_sha256: str
    callback_display_id: str = ""
    task_display_id: str = ""
    task_output_id: str = ""

    def __post_init__(self) -> None:
        for name in ("record_class", "source_record_id", "revision_sha256"):
            object.__setattr__(self, name, _typed_text(getattr(self, name), name))
        for name in ("callback_display_id", "task_display_id", "task_output_id"):
            object.__setattr__(
                self,
                name,
                _typed_text(getattr(self, name), name, required=False),
            )
        if _SHA256_RE.fullmatch(self.revision_sha256) is None:
            raise ValueError("revision_sha256 must be 64 lowercase hex characters")

    @classmethod
    def build(
        cls,
        *,
        record_class: Any,
        source_record_id: Any,
        revision_sha256: Any,
        callback_display_id: Any = "",
        task_display_id: Any = "",
        task_output_id: Any = "",
    ) -> "EvidencePointer":
        digest = _required_text(revision_sha256, "revision_sha256").lower()
        if _SHA256_RE.fullmatch(digest) is None:
            raise ValueError("revision_sha256 must be 64 lowercase hex characters")
        return cls(
            record_class=_required_text(record_class, "record_class"),
            source_record_id=_required_text(source_record_id, "source_record_id"),
            revision_sha256=digest,
            callback_display_id=str(callback_display_id or ""),
            task_display_id=str(task_display_id or ""),
            task_output_id=str(task_output_id or ""),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "record_class": self.record_class,
            "source_record_id": self.source_record_id,
            "revision_sha256": self.revision_sha256,
            "callback_display_id": self.callback_display_id,
            "task_display_id": self.task_display_id,
            "task_output_id": self.task_output_id,
        }


def _canonical_evidence(value: Any) -> tuple[EvidencePointer, ...]:
    pointers = _typed_collection(value, "evidence")
    if not pointers or any(
        not isinstance(pointer, EvidencePointer) for pointer in pointers
    ):
        raise ValueError("at least one exact evidence pointer is required")
    return tuple(sorted(set(pointers)))


def _canonical_assumptions(value: Any) -> tuple[str, ...]:
    assumptions = _typed_collection(value, "missing_assumptions")
    return tuple(
        sorted({_typed_text(item, "missing assumption") for item in assumptions})
    )


def stable_finding_id(operation_id: Any, finding_key: Any) -> str:
    operation = _required_text(operation_id, "operation_id")
    key = _required_text(finding_key, "finding_key")
    if _FINDING_KEY_RE.fullmatch(key) is None:
        raise ValueError("finding_key must already be normalized lowercase structured text")
    digest = hashlib.sha256(f"{operation}\0{key}".encode()).hexdigest()
    return f"finding-{digest[:24]}"


@dataclass(frozen=True)
class FindingCandidate:
    operation_id: str
    finding_key: str
    finding_id: str
    finding_type: str
    title: str
    state: FindingState
    score: float
    observed_at_utc: str
    confidence: float
    evidence: tuple[EvidencePointer, ...]
    missing_assumptions: tuple[str, ...]
    rationale: str
    suggested_validation: str

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "finding_key",
            "finding_id",
            "finding_type",
            "title",
            "observed_at_utc",
            "rationale",
            "suggested_validation",
        ):
            object.__setattr__(self, name, _typed_text(getattr(self, name), name))
        if self.finding_id != stable_finding_id(self.operation_id, self.finding_key):
            raise ValueError("finding_id must derive only from operation_id and finding_key")
        if not isinstance(self.state, FindingState):
            raise ValueError("state must be a FindingState")
        if isinstance(self.score, bool):
            raise ValueError("score must be numeric")
        try:
            numeric_score = float(self.score)
        except (TypeError, ValueError) as exc:
            raise ValueError("score must be numeric") from exc
        if not math.isfinite(numeric_score):
            raise ValueError("score must be finite")
        object.__setattr__(self, "score", numeric_score)
        if isinstance(self.confidence, bool):
            raise ValueError("confidence must be numeric")
        try:
            numeric_confidence = float(self.confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be numeric") from exc
        if not math.isfinite(numeric_confidence) or not 0 <= numeric_confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", numeric_confidence)
        object.__setattr__(self, "evidence", _canonical_evidence(self.evidence))
        object.__setattr__(
            self,
            "missing_assumptions",
            _canonical_assumptions(self.missing_assumptions),
        )

    @classmethod
    def build(
        cls,
        *,
        operation_id: Any,
        finding_key: Any,
        finding_type: Any,
        title: Any,
        state: FindingState | str,
        score: float,
        observed_at_utc: Any,
        confidence: float,
        evidence: Iterable[EvidencePointer],
        missing_assumptions: Iterable[Any],
        rationale: Any,
        suggested_validation: Any,
    ) -> "FindingCandidate":
        operation = _required_text(operation_id, "operation_id")
        key = _required_text(finding_key, "finding_key")
        finding_id = stable_finding_id(operation, key)
        try:
            typed_state = FindingState(state)
        except ValueError as exc:
            raise ValueError("invalid finding state") from exc
        numeric_score = float(score)
        numeric_confidence = float(confidence)
        if not math.isfinite(numeric_score):
            raise ValueError("score must be finite")
        if not math.isfinite(numeric_confidence) or not 0 <= numeric_confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        pointers = _canonical_evidence(evidence)
        assumptions = _canonical_assumptions(missing_assumptions)
        return cls(
            operation_id=operation,
            finding_key=key,
            finding_id=finding_id,
            finding_type=_required_text(finding_type, "finding_type"),
            title=_required_text(title, "title"),
            state=typed_state,
            score=numeric_score,
            observed_at_utc=_required_text(observed_at_utc, "observed_at_utc"),
            confidence=numeric_confidence,
            evidence=pointers,
            missing_assumptions=assumptions,
            rationale=_required_text(rationale, "rationale"),
            suggested_validation=_required_text(
                suggested_validation, "suggested_validation"
            ),
        )

    def evidence_json(self) -> str:
        return _json([pointer.as_dict() for pointer in self.evidence])

    def assumptions_json(self) -> str:
        return _json(list(self.missing_assumptions))


@dataclass(frozen=True)
class FindingViewItem:
    finding_id: str
    finding_key: str
    finding_type: str
    title: str
    rank: int
    state: FindingState
    score: float
    observed_at_utc: str
    confidence: float
    evidence: tuple[Mapping[str, str], ...]
    missing_assumptions: tuple[str, ...]
    rationale: str
    suggested_validation: str


@dataclass(frozen=True)
class NotificationEvent:
    ledger_id: int
    operation_id: str
    changes: tuple[Mapping[str, Any], ...]
    created_at: str


@dataclass(frozen=True)
class PendingFindingDelivery:
    notification: NotificationEvent
    sink: str
    attempts: int
    last_error: str


@dataclass(frozen=True)
class ReconcileResult:
    operation_id: str
    view: tuple[FindingViewItem, ...]
    notification: NotificationEvent | None


def _candidate_signature(candidate: FindingCandidate) -> tuple[Any, ...]:
    return (
        candidate.finding_id,
        candidate.finding_key,
        candidate.finding_type,
        candidate.title,
        candidate.state.value,
        candidate.score,
        candidate.observed_at_utc,
        candidate.confidence,
        candidate.evidence_json(),
        candidate.assumptions_json(),
        candidate.rationale,
        candidate.suggested_validation,
    )


def _stored_candidate_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row["finding_id"]),
        str(row["finding_key"]),
        str(row["finding_type"]),
        str(row["title"]),
        str(row["state"]),
        float(row["score"]),
        str(row["observed_at_utc"]),
        float(row["confidence"]),
        str(row["evidence_json"]),
        str(row["missing_assumptions_json"]),
        str(row["rationale"]),
        str(row["suggested_validation"]),
    )


def _row_to_view(row: Mapping[str, Any]) -> FindingViewItem:
    return FindingViewItem(
        finding_id=str(row["finding_id"]),
        finding_key=str(row["finding_key"]),
        finding_type=str(row["finding_type"]),
        title=str(row["title"]),
        rank=int(row["rank"]),
        state=FindingState(row["state"]),
        score=float(row["score"]),
        observed_at_utc=str(row["observed_at_utc"]),
        confidence=float(row["confidence"]),
        evidence=tuple(json.loads(row["evidence_json"])),
        missing_assumptions=tuple(json.loads(row["missing_assumptions_json"])),
        rationale=str(row["rationale"]),
        suggested_validation=str(row["suggested_validation"]),
    )


async def _read_view(db, operation_id: str) -> tuple[FindingViewItem, ...]:
    rows = await (
        await db.execute(
            """SELECT f.*, v.rank FROM finding_view v JOIN findings f
               ON f.operation_id = v.operation_id AND f.finding_id = v.finding_id
               WHERE v.operation_id = ? ORDER BY v.rank""",
            (operation_id,),
        )
    ).fetchall()
    return tuple(_row_to_view(row) for row in rows)


async def reconcile_findings(
    store: OperationMemoryStore,
    operation_id: Any,
    candidates: Iterable[FindingCandidate],
    *,
    admission_guard: Callable[[], Awaitable[None]] | None = None,
) -> ReconcileResult:
    """Upsert typed candidates and atomically refresh the canonical top five."""
    operation = _required_text(operation_id, "operation_id")
    unique: dict[str, FindingCandidate] = {}
    for candidate in candidates:
        if candidate.operation_id != operation:
            raise ValueError("every candidate must match operation_id")
        prior = unique.get(candidate.finding_id)
        if prior is not None and _candidate_signature(prior) != _candidate_signature(candidate):
            raise ValueError("conflicting duplicate finding candidate")
        unique[candidate.finding_id] = candidate

    async with store._lock:
        db = store._connection()
        await db.execute("BEGIN IMMEDIATE")
        try:
            await store._ensure_operation(db, operation)
            for candidate in unique.values():
                for pointer in candidate.evidence:
                    resolved = await (
                        await db.execute(
                            """SELECT 1 FROM source_records
                               WHERE operation_id = ? AND record_class = ?
                               AND source_record_id = ? AND revision_sha256 = ?
                               LIMIT 1""",
                            (
                                operation,
                                pointer.record_class,
                                pointer.source_record_id,
                                pointer.revision_sha256,
                            ),
                        )
                    ).fetchone()
                    if resolved is None:
                        raise ValueError(
                            "finding evidence pointer does not resolve under the same operation"
                        )
            before_view = await _read_view(db, operation)
            before_ids = [item.finding_id for item in before_view]
            before_members = {item.finding_id for item in before_view}
            state_changes: list[dict[str, Any]] = []
            evidence_changes: list[dict[str, Any]] = []
            now = _utc_now()
            for candidate in unique.values():
                existing = await (
                    await db.execute(
                        """SELECT finding_id, finding_key, finding_type, title, state,
                                  score, observed_at_utc, confidence, evidence_json,
                                  missing_assumptions_json, rationale, suggested_validation
                           FROM findings
                           WHERE operation_id = ? AND finding_id = ?""",
                        (operation, candidate.finding_id),
                    )
                ).fetchone()
                evidence_json = candidate.evidence_json()
                if existing is not None and existing["state"] != candidate.state.value:
                    state_changes.append(
                        {
                            "kind": "state",
                            "finding_id": candidate.finding_id,
                            "old": str(existing["state"]),
                            "new": candidate.state.value,
                        }
                    )
                if existing is not None and existing["evidence_json"] != evidence_json:
                    evidence_changes.append(
                        {"kind": "evidence", "finding_id": candidate.finding_id}
                    )
                if (
                    existing is not None
                    and _stored_candidate_signature(existing)
                    == _candidate_signature(candidate)
                ):
                    continue
                await db.execute(
                    """INSERT INTO findings(
                       operation_id, finding_id, finding_key, finding_type, title,
                       state, score, observed_at_utc, confidence, evidence_json,
                       missing_assumptions_json, rationale, suggested_validation,
                       created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(operation_id, finding_id) DO UPDATE SET
                         finding_key=excluded.finding_key,
                         finding_type=excluded.finding_type,
                         title=excluded.title,
                         state=excluded.state,
                         score=excluded.score,
                         observed_at_utc=excluded.observed_at_utc,
                         confidence=excluded.confidence,
                         evidence_json=excluded.evidence_json,
                         missing_assumptions_json=excluded.missing_assumptions_json,
                         rationale=excluded.rationale,
                         suggested_validation=excluded.suggested_validation,
                         updated_at=excluded.updated_at""",
                    (
                        operation,
                        candidate.finding_id,
                        candidate.finding_key,
                        candidate.finding_type,
                        candidate.title,
                        candidate.state.value,
                        candidate.score,
                        candidate.observed_at_utc,
                        candidate.confidence,
                        evidence_json,
                        candidate.assumptions_json(),
                        candidate.rationale,
                        candidate.suggested_validation,
                        now,
                        now,
                    ),
                )

            active_values = tuple(state.value for state in ACTIVE_STATES)
            placeholders = ",".join("?" for _ in active_values)
            ranked = await (
                await db.execute(
                    f"""SELECT finding_id FROM findings
                        WHERE operation_id = ? AND state IN ({placeholders})
                        ORDER BY score DESC, observed_at_utc DESC, finding_id ASC
                        LIMIT ?""",
                    (operation, *active_values, MAX_ACTIVE_FINDINGS),
                )
            ).fetchall()
            after_ids = [str(row["finding_id"]) for row in ranked]
            after_members = set(after_ids)
            if after_ids != before_ids:
                await db.execute("DELETE FROM finding_view WHERE operation_id = ?", (operation,))
                for rank, finding_id in enumerate(after_ids, start=1):
                    await db.execute(
                        """INSERT INTO finding_view(operation_id, finding_id, rank, updated_at)
                           VALUES (?, ?, ?, ?)""",
                        (operation, finding_id, rank, now),
                    )

            membership_changes = [
                {"kind": "membership", "finding_id": finding_id, "old": "absent", "new": "present"}
                for finding_id in sorted(after_members - before_members)
            ] + [
                {"kind": "membership", "finding_id": finding_id, "old": "present", "new": "absent"}
                for finding_id in sorted(before_members - after_members)
            ]
            changes = tuple(
                sorted(
                    membership_changes + state_changes + evidence_changes,
                    key=lambda row: (str(row["finding_id"]), str(row["kind"])),
                )
            )
            notification = None
            if changes:
                cursor = await db.execute(
                    """INSERT INTO finding_notification_ledger(
                       operation_id, changes_json, created_at) VALUES (?, ?, ?)""",
                    (operation, _json(changes), now),
                )
                notification = NotificationEvent(
                    ledger_id=int(cursor.lastrowid),
                    operation_id=operation,
                    changes=changes,
                    created_at=now,
                )
                for sink in FINDING_DELIVERY_SINKS:
                    await db.execute(
                        """INSERT INTO finding_delivery_outbox(
                           operation_id, notification_id, sink, updated_at)
                           VALUES (?, ?, ?, ?)""",
                        (operation, notification.ledger_id, sink, now),
                    )
            view = await _read_view(db, operation)
            # Ownership is external Mythic authority.  Revalidate at the actual
            # persistence boundary, while every derived finding/view/notification/
            # outbox mutation is still rollbackable in this transaction.
            if admission_guard is not None:
                await admission_guard()
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return ReconcileResult(operation_id=operation, view=view, notification=notification)


async def current_findings_view(
    store: OperationMemoryStore, operation_id: Any
) -> tuple[FindingViewItem, ...]:
    operation = _required_text(operation_id, "operation_id")
    async with store._lock:
        return await _read_view(store._connection(), operation)


async def list_notification_events(
    store: OperationMemoryStore, operation_id: Any
) -> tuple[NotificationEvent, ...]:
    operation = _required_text(operation_id, "operation_id")
    async with store._lock:
        rows = await (
            await store._connection().execute(
                """SELECT id, changes_json, created_at
                   FROM finding_notification_ledger
                   WHERE operation_id = ? ORDER BY id""",
                (operation,),
            )
        ).fetchall()
    return tuple(
        NotificationEvent(
            ledger_id=int(row["id"]),
            operation_id=operation,
            changes=tuple(json.loads(row["changes_json"])),
            created_at=str(row["created_at"]),
        )
        for row in rows
    )


async def list_pending_finding_deliveries(
    store: OperationMemoryStore, operation_id: Any
) -> tuple[PendingFindingDelivery, ...]:
    operation = _required_text(operation_id, "operation_id")
    async with store._lock:
        rows = await (
            await store._connection().execute(
                """SELECT o.sink, o.attempts, o.last_error, n.id,
                          n.changes_json, n.created_at
                   FROM finding_delivery_outbox o
                   JOIN finding_notification_ledger n ON n.id = o.notification_id
                   WHERE o.operation_id = ? AND n.operation_id = ?
                     AND o.delivered_at IS NULL
                   ORDER BY n.id, o.sink""",
                (operation, operation),
            )
        ).fetchall()
    return tuple(
        PendingFindingDelivery(
            notification=NotificationEvent(
                ledger_id=int(row["id"]),
                operation_id=operation,
                changes=tuple(json.loads(row["changes_json"])),
                created_at=str(row["created_at"]),
            ),
            sink=str(row["sink"]),
            attempts=int(row["attempts"]),
            last_error=str(row["last_error"]),
        )
        for row in rows
    )


async def record_finding_delivery_attempt(
    store: OperationMemoryStore,
    operation_id: Any,
    notification_id: int,
    sink: Any,
    *,
    delivered: bool,
    error: str = "",
) -> None:
    operation = _required_text(operation_id, "operation_id")
    sink_name = _required_text(sink, "sink")
    if sink_name not in FINDING_DELIVERY_SINKS:
        raise ValueError("unknown finding delivery sink")
    if not isinstance(notification_id, int) or isinstance(notification_id, bool):
        raise ValueError("notification_id must be an integer")
    now = _utc_now()
    safe_error = " ".join(str(error or "").split())[:500]
    async with store._lock:
        db = store._connection()
        cursor = await db.execute(
            """UPDATE finding_delivery_outbox
               SET attempts = attempts + 1,
                   delivered_at = CASE WHEN ? THEN ? ELSE NULL END,
                   last_error = ?, updated_at = ?
               WHERE operation_id = ? AND notification_id = ? AND sink = ?
                 AND delivered_at IS NULL""",
            (
                int(delivered),
                now,
                "" if delivered else safe_error,
                now,
                operation,
                notification_id,
                sink_name,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise ValueError("finding delivery outbox row is missing or already delivered")
        await db.commit()
