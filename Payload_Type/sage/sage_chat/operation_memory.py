"""Operation-scoped persistence primitives for Sage's assisted findings memory.

Mythic remains authoritative.  This module stores an erasable index, exact source
lineage, incremental cursors, and visible deferred-work state; it never tasks a
callback and has no model or action interface.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

import aiosqlite


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _required_text(value: Any, name: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class OperationMemoryLimits:
    """The five release-frozen, deployed runtime controls."""

    max_model_input_tokens: int = 100_000
    max_inline_text_bytes: int = 65_536
    max_model_calls_per_update: int = 5
    backfill_batch_size: int = 500
    max_queued_updates: int = 100

    @classmethod
    def from_env(cls) -> "OperationMemoryLimits":
        return cls(
            max_model_input_tokens=_positive_env(
                "SAGE_OPERATION_MEMORY_MAX_MODEL_INPUT_TOKENS", 100_000
            ),
            max_inline_text_bytes=_positive_env(
                "SAGE_OPERATION_MEMORY_MAX_INLINE_TEXT_BYTES", 65_536
            ),
            max_model_calls_per_update=_positive_env(
                "SAGE_OPERATION_MEMORY_MAX_MODEL_CALLS_PER_UPDATE", 5
            ),
            backfill_batch_size=_positive_env(
                "SAGE_OPERATION_MEMORY_BACKFILL_BATCH_SIZE", 500
            ),
            max_queued_updates=_positive_env(
                "SAGE_OPERATION_MEMORY_MAX_QUEUED_UPDATES", 100
            ),
        )

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ResourceDeferral:
    """Typed notice that authoritative source content exceeded one frozen bound."""

    bound_name: str
    limit_value: int
    observed_value: int
    deferred_units: int
    detail: str

    def __post_init__(self) -> None:
        _required_text(self.bound_name, "bound_name")
        _required_text(self.detail, "detail")
        for name in ("limit_value", "observed_value", "deferred_units"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class SourceRecord:
    operation_id: str
    record_class: str
    source_record_id: str
    observed_at_utc: str
    content: bytes
    content_kind: str = "text"
    callback_display_id: str = ""
    task_display_id: str = ""
    task_output_id: str = ""
    metadata: Mapping[str, Any] | None = None
    deferrals: tuple[ResourceDeferral, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        operation_id: Any,
        record_class: Any,
        source_record_id: Any,
        observed_at_utc: Any,
        content: str | bytes | Mapping[str, Any] | list[Any],
        content_kind: str = "text",
        callback_display_id: Any = "",
        task_display_id: Any = "",
        task_output_id: Any = "",
        metadata: Mapping[str, Any] | None = None,
        deferrals: Iterable[ResourceDeferral] = (),
    ) -> "SourceRecord":
        if isinstance(content, bytes):
            encoded = content
        elif isinstance(content, str):
            encoded = content.encode("utf-8")
        else:
            encoded = _json(content).encode("utf-8")
            if content_kind == "text":
                content_kind = "json"
        typed_deferrals = tuple(deferrals)
        if any(not isinstance(row, ResourceDeferral) for row in typed_deferrals):
            raise ValueError("deferrals must contain ResourceDeferral values")
        return cls(
            operation_id=_required_text(operation_id, "operation_id"),
            record_class=_required_text(record_class, "record_class"),
            source_record_id=_required_text(source_record_id, "source_record_id"),
            observed_at_utc=_required_text(observed_at_utc, "observed_at_utc"),
            content=encoded,
            content_kind=_required_text(content_kind, "content_kind"),
            callback_display_id=str(callback_display_id or ""),
            task_display_id=str(task_display_id or ""),
            task_output_id=str(task_output_id or ""),
            metadata=deepcopy(dict(metadata or {})),
            deferrals=typed_deferrals,
        )

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class IngestResult:
    received: int
    examined: int
    inserted: int
    revised: int
    unchanged: int
    deferred: int
    watermark_advanced: bool


@dataclass(frozen=True)
class BudgetDecision:
    requested_tokens: int
    allowed_tokens: int
    requested_model_calls: int
    allowed_model_calls: int
    degraded: bool


class WatcherOwnerConflict(RuntimeError):
    """A different active Watcher owner already controls this operation."""


@dataclass(frozen=True)
class WatcherProfileRecord:
    operation_id: str
    owner_channel_id: int
    owner_channel_name: str
    generation: int
    provider: str
    model: str
    config_sources: Mapping[str, str]
    profile_binding_sha256: str
    interval_seconds: int
    paused: bool
    lifecycle_state: str
    credentials_required: bool
    updated_at: str


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    degraded INTEGER NOT NULL DEFAULT 0,
    rescan_required INTEGER NOT NULL DEFAULT 0,
    degraded_reasons_json TEXT NOT NULL DEFAULT '[]',
    deferred_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watcher_profiles (
    operation_id TEXT PRIMARY KEY,
    owner_channel_id INTEGER NOT NULL,
    owner_channel_name TEXT NOT NULL,
    generation INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    config_sources_json TEXT NOT NULL,
    profile_binding_sha256 TEXT NOT NULL DEFAULT '',
    interval_seconds INTEGER NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    lifecycle_state TEXT NOT NULL,
    credentials_required INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS source_records (
    operation_id TEXT NOT NULL,
    record_class TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    revision_sha256 TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    callback_display_id TEXT NOT NULL,
    task_display_id TEXT NOT NULL,
    task_output_id TEXT NOT NULL,
    content_kind TEXT NOT NULL,
    content_size INTEGER NOT NULL,
    inline_text TEXT,
    metadata_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, record_class, source_record_id, revision_sha256),
    FOREIGN KEY (operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS record_heads (
    operation_id TEXT NOT NULL,
    record_class TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    revision_sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, record_class, source_record_id),
    FOREIGN KEY (operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS analysis_heads (
    operation_id TEXT NOT NULL,
    record_class TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    revision_sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, record_class, source_record_id),
    FOREIGN KEY (operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS watermarks (
    operation_id TEXT NOT NULL,
    stream_key TEXT NOT NULL,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, stream_key),
    FOREIGN KEY (operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS update_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    UNIQUE (operation_id, dedupe_key),
    FOREIGN KEY (operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS budget_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    bound_name TEXT NOT NULL,
    limit_value INTEGER NOT NULL,
    observed_value INTEGER NOT NULL,
    deferred_units INTEGER NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS findings (
    operation_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    finding_key TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    score REAL NOT NULL,
    observed_at_utc TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    missing_assumptions_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    suggested_validation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, finding_id),
    UNIQUE (operation_id, finding_key),
    FOREIGN KEY (operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS finding_view (
    operation_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, finding_id),
    UNIQUE (operation_id, rank),
    FOREIGN KEY (operation_id, finding_id)
        REFERENCES findings(operation_id, finding_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS finding_notification_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS finding_delivery_outbox (
    operation_id TEXT NOT NULL,
    notification_id INTEGER NOT NULL,
    sink TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, notification_id, sink),
    FOREIGN KEY (operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE,
    FOREIGN KEY (notification_id) REFERENCES finding_notification_ledger(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS source_records_by_operation_time
    ON source_records(operation_id, observed_at_utc, record_class);
CREATE INDEX IF NOT EXISTS analysis_heads_by_operation
    ON analysis_heads(operation_id, record_class, source_record_id);
CREATE INDEX IF NOT EXISTS queue_by_operation_state
    ON update_queue(operation_id, state, id);
CREATE INDEX IF NOT EXISTS findings_by_operation_state_score
    ON findings(operation_id, state, score DESC, observed_at_utc DESC, finding_id);
CREATE INDEX IF NOT EXISTS finding_notifications_by_operation
    ON finding_notification_ledger(operation_id, id);
CREATE INDEX IF NOT EXISTS finding_delivery_pending_by_operation
    ON finding_delivery_outbox(operation_id, delivered_at, notification_id);
"""


class OperationMemoryStore:
    """One async, operation-keyed SQLite store for erasable derived state."""

    def __init__(
        self,
        db_path: str | Path = "operation_memory.db",
        *,
        limits: OperationMemoryLimits | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.limits = limits or OperationMemoryLimits.from_env()
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._db is not None:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            db = await aiosqlite.connect(self.db_path)
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(SCHEMA)
            columns = {
                str(row[1])
                for row in await (await db.execute("PRAGMA table_info(watcher_profiles)")).fetchall()
            }
            if "profile_binding_sha256" not in columns:
                await db.execute(
                    "ALTER TABLE watcher_profiles ADD COLUMN profile_binding_sha256 TEXT NOT NULL DEFAULT ''"
                )
            await db.commit()
            self._db = db

    async def close(self) -> None:
        async with self._lock:
            if self._db is None:
                return
            await self._db.close()
            self._db = None

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("OperationMemoryStore.initialize() must be called first")
        return self._db

    async def _ensure_operation(self, db: aiosqlite.Connection, operation_id: str) -> None:
        now = _utc_now()
        await db.execute(
            """INSERT INTO operations(operation_id, created_at, updated_at)
               VALUES (?, ?, ?) ON CONFLICT(operation_id) DO NOTHING""",
            (operation_id, now, now),
        )

    async def _mark_degraded(
        self,
        db: aiosqlite.Connection,
        operation_id: str,
        *,
        bound_name: str,
        limit_value: int,
        observed_value: int,
        deferred_units: int,
        detail: str,
    ) -> None:
        await self._ensure_operation(db, operation_id)
        row = await (
            await db.execute(
                "SELECT degraded_reasons_json FROM operations WHERE operation_id = ?",
                (operation_id,),
            )
        ).fetchone()
        reasons = json.loads(row[0]) if row else []
        reason = {
            "bound": bound_name,
            "limit": limit_value,
            "observed": observed_value,
            "detail": detail,
        }
        if reason not in reasons:
            reasons.append(reason)
        now = _utc_now()
        await db.execute(
            """UPDATE operations SET degraded = 1, rescan_required = 1,
               deferred_count = deferred_count + ?, degraded_reasons_json = ?, updated_at = ?
               WHERE operation_id = ?""",
            (max(0, deferred_units), _json(reasons), now, operation_id),
        )
        await db.execute(
            """INSERT INTO budget_events(
               operation_id, bound_name, limit_value, observed_value,
               deferred_units, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                operation_id,
                bound_name,
                limit_value,
                observed_value,
                max(0, deferred_units),
                detail,
                now,
            ),
        )

    @staticmethod
    def _watcher_profile_from_row(row: Any) -> WatcherProfileRecord:
        return WatcherProfileRecord(
            operation_id=str(row["operation_id"]),
            owner_channel_id=int(row["owner_channel_id"]),
            owner_channel_name=str(row["owner_channel_name"]),
            generation=int(row["generation"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            config_sources=json.loads(row["config_sources_json"]),
            profile_binding_sha256=str(row["profile_binding_sha256"]),
            interval_seconds=int(row["interval_seconds"]),
            paused=bool(row["paused"]),
            lifecycle_state=str(row["lifecycle_state"]),
            credentials_required=bool(row["credentials_required"]),
            updated_at=str(row["updated_at"]),
        )

    async def watcher_profile(self, operation_id: Any) -> WatcherProfileRecord | None:
        op = _required_text(operation_id, "operation_id")
        await self.initialize()
        async with self._lock:
            row = await (
                await self._connection().execute(
                    "SELECT * FROM watcher_profiles WHERE operation_id = ?", (op,)
                )
            ).fetchone()
        return self._watcher_profile_from_row(row) if row is not None else None

    async def apply_watcher_profile(
        self,
        operation_id: Any,
        *,
        owner_channel_id: int,
        owner_channel_name: str,
        provider: str,
        model: str,
        config_sources: Mapping[str, str],
        interval_seconds: int,
        profile_binding_sha256: str = "",
        credentials_required: bool = False,
        expected_generation: int | None = None,
        generation: int | None = None,
    ) -> WatcherProfileRecord:
        """Claim or advance one exact owner generation without persisting credentials."""

        op = _required_text(operation_id, "operation_id")
        channel_id = int(owner_channel_id)
        if channel_id <= 0:
            raise ValueError("owner_channel_id must be positive")
        if not 5 <= int(interval_seconds) <= 86_400:
            raise ValueError("watcher interval must be between 5 and 86400 seconds")
        source_labels = {
            str(key): str(value)
            for key, value in config_sources.items()
            if str(value) in {"ui-config", "user-secret", "environment", "default"}
        }
        now = _utc_now()
        await self.initialize()
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._ensure_operation(db, op)
                existing = await (
                    await db.execute(
                        "SELECT * FROM watcher_profiles WHERE operation_id = ?", (op,)
                    )
                ).fetchone()
                current_generation = int(existing["generation"]) if existing is not None else 0
                if (
                    expected_generation is not None
                    and current_generation != int(expected_generation)
                ):
                    raise WatcherOwnerConflict(
                        "Watcher generation changed before profile apply"
                    )
                if (
                    existing is not None
                    and int(existing["owner_channel_id"]) != channel_id
                    and str(existing["lifecycle_state"]) != "controller-missing"
                ):
                    raise WatcherOwnerConflict(
                        "a different active locked Watcher owner already controls this operation"
                    )
                next_generation = current_generation + 1
                if generation is not None and int(generation) != next_generation:
                    raise WatcherOwnerConflict(
                        "Watcher prospective generation is not the next generation"
                    )
                next_generation = int(generation) if generation is not None else next_generation
                paused = bool(existing["paused"]) if existing is not None else False
                await db.execute(
                    """INSERT INTO watcher_profiles(
                           operation_id, owner_channel_id, owner_channel_name, generation,
                           provider, model, config_sources_json, profile_binding_sha256, interval_seconds, paused,
                           lifecycle_state, credentials_required, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(operation_id) DO UPDATE SET
                           owner_channel_id = excluded.owner_channel_id,
                           owner_channel_name = excluded.owner_channel_name,
                           generation = excluded.generation,
                           provider = excluded.provider,
                           model = excluded.model,
                           config_sources_json = excluded.config_sources_json,
                           profile_binding_sha256 = excluded.profile_binding_sha256,
                           interval_seconds = excluded.interval_seconds,
                           paused = excluded.paused,
                           lifecycle_state = excluded.lifecycle_state,
                           credentials_required = excluded.credentials_required,
                           updated_at = excluded.updated_at""",
                    (
                        op,
                        channel_id,
                        str(owner_channel_name or "").strip(),
                        next_generation,
                        str(provider).strip(),
                        str(model).strip(),
                        _json(source_labels),
                        str(profile_binding_sha256),
                        int(interval_seconds),
                        int(paused),
                        "credentials-required" if credentials_required else ("paused" if paused else "starting"),
                        int(credentials_required),
                        now,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        record = await self.watcher_profile(op)
        if record is None:  # pragma: no cover - fail closed on impossible storage loss
            raise RuntimeError("applied Watcher profile was not retained")
        return record

    async def update_watcher_profile_state(
        self,
        operation_id: Any,
        *,
        expected_generation: int,
        paused: bool | None = None,
        interval_seconds: int | None = None,
        lifecycle_state: str | None = None,
        credentials_required: bool | None = None,
    ) -> WatcherProfileRecord:
        op = _required_text(operation_id, "operation_id")
        await self.initialize()
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT * FROM watcher_profiles WHERE operation_id = ?", (op,)
                    )
                ).fetchone()
                if row is None or int(row["generation"]) != int(expected_generation):
                    raise WatcherOwnerConflict("Watcher generation changed before lifecycle update")
                next_interval = int(interval_seconds) if interval_seconds is not None else int(row["interval_seconds"])
                if not 5 <= next_interval <= 86_400:
                    raise ValueError("watcher interval must be between 5 and 86400 seconds")
                next_paused = bool(paused) if paused is not None else bool(row["paused"])
                next_required = (
                    bool(credentials_required)
                    if credentials_required is not None
                    else bool(row["credentials_required"])
                )
                next_state = str(lifecycle_state or ("paused" if next_paused else row["lifecycle_state"]))
                await db.execute(
                    """UPDATE watcher_profiles SET interval_seconds = ?, paused = ?,
                       lifecycle_state = ?, credentials_required = ?, updated_at = ?
                       WHERE operation_id = ? AND generation = ?""",
                    (next_interval, int(next_paused), next_state, int(next_required), _utc_now(), op, int(expected_generation)),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        record = await self.watcher_profile(op)
        if record is None:  # pragma: no cover
            raise RuntimeError("Watcher profile disappeared after lifecycle update")
        return record

    async def ingest_batch(
        self,
        operation_id: Any,
        records: Iterable[SourceRecord],
        *,
        stream_key: Any,
        next_cursor: Any,
        source_has_more: bool = False,
    ) -> IngestResult:
        op = _required_text(operation_id, "operation_id")
        stream = _required_text(stream_key, "stream_key")
        cursor = _required_text(next_cursor, "next_cursor")
        if not isinstance(source_has_more, bool):
            raise ValueError("source_has_more must be boolean")
        rows = list(records)
        for record in rows:
            if record.operation_id != op:
                raise ValueError("every SourceRecord must match the batch operation_id")
        received = len(rows)
        accepted = rows[: self.limits.backfill_batch_size]
        deferred = max(0, received - len(accepted))
        if source_has_more and received != self.limits.backfill_batch_size:
            raise ValueError(
                "source_has_more requires one complete bounded source page"
            )
        inserted = revised = unchanged = 0
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._ensure_operation(db, op)
                if deferred:
                    await self._mark_degraded(
                        db,
                        op,
                        bound_name="backfill_batch_size",
                        limit_value=self.limits.backfill_batch_size,
                        observed_value=received,
                        deferred_units=deferred,
                        detail="remaining source records require a subsequent backfill batch",
                    )
                elif source_has_more:
                    await self._mark_degraded(
                        db,
                        op,
                        bound_name="backfill_batch_size",
                        limit_value=self.limits.backfill_batch_size,
                        observed_value=received + 1,
                        deferred_units=1,
                        detail=(
                            "at least one additional authoritative Mythic source record "
                            "requires a subsequent bounded backfill page"
                        ),
                    )
                for record in accepted:
                    digest = record.content_sha256
                    head = await (
                        await db.execute(
                            """SELECT revision_sha256 FROM record_heads
                               WHERE operation_id = ? AND record_class = ? AND source_record_id = ?""",
                            (op, record.record_class, record.source_record_id),
                        )
                    ).fetchone()
                    if head is not None and head[0] == digest:
                        unchanged += 1
                        continue
                    for notice in record.deferrals:
                        await self._mark_degraded(
                            db,
                            op,
                            bound_name=notice.bound_name,
                            limit_value=notice.limit_value,
                            observed_value=notice.observed_value,
                            deferred_units=notice.deferred_units,
                            detail=notice.detail,
                        )
                    inline_text: str | None = None
                    if record.content_kind in {"text", "json"}:
                        if len(record.content) <= self.limits.max_inline_text_bytes:
                            try:
                                inline_text = record.content.decode("utf-8")
                            except UnicodeDecodeError:
                                inline_text = None
                        elif not any(
                            notice.bound_name == "max_inline_text_bytes"
                            for notice in record.deferrals
                        ):
                            await self._mark_degraded(
                                db,
                                op,
                                bound_name="max_inline_text_bytes",
                                limit_value=self.limits.max_inline_text_bytes,
                                observed_value=len(record.content),
                                deferred_units=1,
                                detail=(
                                    f"{record.record_class}:{record.source_record_id} content remains "
                                    "authoritative in Mythic and requires explicit selection/rescan"
                                ),
                            )
                    now = _utc_now()
                    await db.execute(
                        """INSERT OR IGNORE INTO source_records(
                           operation_id, record_class, source_record_id, revision_sha256,
                           observed_at_utc, callback_display_id, task_display_id,
                           task_output_id, content_kind, content_size, inline_text,
                           metadata_json, first_seen_at, last_seen_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            op,
                            record.record_class,
                            record.source_record_id,
                            digest,
                            record.observed_at_utc,
                            record.callback_display_id,
                            record.task_display_id,
                            record.task_output_id,
                            record.content_kind,
                            len(record.content),
                            inline_text,
                            _json(record.metadata or {}),
                            now,
                            now,
                        ),
                    )
                    await db.execute(
                        """INSERT INTO record_heads(
                           operation_id, record_class, source_record_id, revision_sha256, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(operation_id, record_class, source_record_id)
                           DO UPDATE SET revision_sha256 = excluded.revision_sha256,
                                         updated_at = excluded.updated_at""",
                        (op, record.record_class, record.source_record_id, digest, now),
                    )
                    if head is None:
                        inserted += 1
                    else:
                        revised += 1
                watermark = await (
                    await db.execute(
                        """SELECT cursor FROM watermarks
                           WHERE operation_id = ? AND stream_key = ?""",
                        (op, stream),
                    )
                ).fetchone()
                watermark_advanced = not deferred and (
                    watermark is None or str(watermark[0]) != cursor
                )
                if watermark_advanced:
                    await db.execute(
                        """INSERT INTO watermarks(operation_id, stream_key, cursor, updated_at)
                           VALUES (?, ?, ?, ?) ON CONFLICT(operation_id, stream_key)
                           DO UPDATE SET cursor = excluded.cursor, updated_at = excluded.updated_at""",
                        (op, stream, cursor, _utc_now()),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return IngestResult(
            received=received,
            examined=len(accepted),
            inserted=inserted,
            revised=revised,
            unchanged=unchanged,
            deferred=deferred + int(source_has_more),
            watermark_advanced=watermark_advanced,
        )

    async def reserve_analysis(
        self,
        operation_id: Any,
        *,
        model_input_tokens: int,
        model_calls: int,
    ) -> BudgetDecision:
        op = _required_text(operation_id, "operation_id")
        if model_input_tokens < 0 or model_calls < 0:
            raise ValueError("analysis budget requests cannot be negative")
        allowed_tokens = min(model_input_tokens, self.limits.max_model_input_tokens)
        allowed_calls = min(model_calls, self.limits.max_model_calls_per_update)
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._ensure_operation(db, op)
                if model_input_tokens > allowed_tokens:
                    await self._mark_degraded(
                        db,
                        op,
                        bound_name="max_model_input_tokens",
                        limit_value=self.limits.max_model_input_tokens,
                        observed_value=model_input_tokens,
                        deferred_units=model_input_tokens - allowed_tokens,
                        detail="excess model-input tokens require a later bounded analysis",
                    )
                if model_calls > allowed_calls:
                    await self._mark_degraded(
                        db,
                        op,
                        bound_name="max_model_calls_per_update",
                        limit_value=self.limits.max_model_calls_per_update,
                        observed_value=model_calls,
                        deferred_units=model_calls - allowed_calls,
                        detail="excess model calls require a later bounded analysis",
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return BudgetDecision(
            requested_tokens=model_input_tokens,
            allowed_tokens=allowed_tokens,
            requested_model_calls=model_calls,
            allowed_model_calls=allowed_calls,
            degraded=(allowed_tokens != model_input_tokens or allowed_calls != model_calls),
        )

    async def enqueue_update(
        self,
        operation_id: Any,
        *,
        dedupe_key: Any,
        payload: Mapping[str, Any],
    ) -> bool:
        op = _required_text(operation_id, "operation_id")
        key = _required_text(dedupe_key, "dedupe_key")
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._ensure_operation(db, op)
                existing = await (
                    await db.execute(
                        "SELECT 1 FROM update_queue WHERE operation_id = ? AND dedupe_key = ?",
                        (op, key),
                    )
                ).fetchone()
                if existing is not None:
                    await db.commit()
                    return False
                count_row = await (
                    await db.execute(
                        "SELECT COUNT(*) FROM update_queue WHERE operation_id = ? AND state = 'pending'",
                        (op,),
                    )
                ).fetchone()
                count = int(count_row[0])
                if count >= self.limits.max_queued_updates:
                    await self._mark_degraded(
                        db,
                        op,
                        bound_name="max_queued_updates",
                        limit_value=self.limits.max_queued_updates,
                        observed_value=count + 1,
                        deferred_units=1,
                        detail="source-derived update was not queued; rescan from Mythic source records is required",
                    )
                    await db.commit()
                    return False
                await db.execute(
                    """INSERT INTO update_queue(operation_id, dedupe_key, payload_json, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (op, key, _json(payload), _utc_now()),
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def list_records(self, operation_id: Any) -> list[dict[str, Any]]:
        op = _required_text(operation_id, "operation_id")
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                """SELECT r.* FROM source_records r JOIN record_heads h
                   ON h.operation_id = r.operation_id AND h.record_class = r.record_class
                   AND h.source_record_id = r.source_record_id
                   AND h.revision_sha256 = r.revision_sha256
                   WHERE r.operation_id = ?
                   ORDER BY r.observed_at_utc, r.record_class, r.source_record_id""",
                (op,),
            )
            rows = await cursor.fetchall()
        return [
            {
                **dict(row),
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    async def list_unanalyzed_records(
        self,
        operation_id: Any,
        *,
        record_classes: Iterable[Any],
    ) -> list[dict[str, Any]]:
        """Return current heads whose exact revision has not been analyzed."""
        op = _required_text(operation_id, "operation_id")
        classes = tuple(
            sorted({_required_text(item, "record_class") for item in record_classes})
        )
        if not classes:
            raise ValueError("record_classes must not be empty")
        placeholders = ",".join("?" for _ in classes)
        async with self._lock:
            db = self._connection()
            rows = await (
                await db.execute(
                    f"""SELECT r.* FROM source_records r JOIN record_heads h
                        ON h.operation_id = r.operation_id
                        AND h.record_class = r.record_class
                        AND h.source_record_id = r.source_record_id
                        AND h.revision_sha256 = r.revision_sha256
                        LEFT JOIN analysis_heads a
                        ON a.operation_id = h.operation_id
                        AND a.record_class = h.record_class
                        AND a.source_record_id = h.source_record_id
                        WHERE r.operation_id = ?
                        AND r.record_class IN ({placeholders})
                        AND (a.revision_sha256 IS NULL
                             OR a.revision_sha256 != h.revision_sha256)
                        ORDER BY r.observed_at_utc, r.record_class,
                                 r.source_record_id""",
                    (op, *classes),
                )
            ).fetchall()
        return [
            {
                **dict(row),
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    async def mark_records_analyzed(
        self,
        operation_id: Any,
        records: Iterable[Mapping[str, Any]],
    ) -> None:
        """Checkpoint only exact current heads under the supplied operation."""
        op = _required_text(operation_id, "operation_id")
        rows = tuple(records)
        normalized: list[tuple[str, str, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("analyzed records must be mappings")
            if _required_text(row.get("operation_id"), "record operation_id") != op:
                raise ValueError("analyzed record does not resolve to a current head")
            normalized.append(
                (
                    _required_text(row.get("record_class"), "record_class"),
                    _required_text(row.get("source_record_id"), "source_record_id"),
                    _required_text(row.get("revision_sha256"), "revision_sha256"),
                )
            )
        if len(set(normalized)) != len(normalized):
            raise ValueError("analyzed records contain duplicate current heads")
        if not normalized:
            return

        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                now = _utc_now()
                for record_class, source_record_id, revision_sha256 in normalized:
                    current = await (
                        await db.execute(
                            """SELECT 1 FROM record_heads
                               WHERE operation_id = ? AND record_class = ?
                               AND source_record_id = ? AND revision_sha256 = ?""",
                            (op, record_class, source_record_id, revision_sha256),
                        )
                    ).fetchone()
                    if current is None:
                        raise ValueError(
                            "analyzed record does not resolve to a current head"
                        )
                    await db.execute(
                        """INSERT INTO analysis_heads(
                           operation_id, record_class, source_record_id,
                           revision_sha256, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(operation_id, record_class, source_record_id)
                           DO UPDATE SET revision_sha256 = excluded.revision_sha256,
                                         updated_at = excluded.updated_at""",
                        (op, record_class, source_record_id, revision_sha256, now),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def snapshot(self, operation_id: Any) -> dict[str, Any]:
        op = _required_text(operation_id, "operation_id")
        async with self._lock:
            db = self._connection()
            operation = await (
                await db.execute("SELECT * FROM operations WHERE operation_id = ?", (op,))
            ).fetchone()
            if operation is None:
                return {
                    "operation_id": op,
                    "exists": False,
                    "degraded": False,
                    "rescan_required": False,
                    "deferred_count": 0,
                    "record_count": 0,
                    "queued_update_count": 0,
                    "pending_delivery_count": 0,
                    "watermarks": {},
                    "bounds": self.limits.__dict__.copy(),
                }
            record_count = int(
                (await (await db.execute(
                    "SELECT COUNT(*) FROM record_heads WHERE operation_id = ?", (op,)
                )).fetchone())[0]
            )
            queue_count = int(
                (await (await db.execute(
                    "SELECT COUNT(*) FROM update_queue WHERE operation_id = ? AND state = 'pending'", (op,)
                )).fetchone())[0]
            )
            pending_delivery_count = int(
                (await (await db.execute(
                    """SELECT COUNT(*) FROM finding_delivery_outbox
                       WHERE operation_id = ? AND delivered_at IS NULL""",
                    (op,),
                )).fetchone())[0]
            )
            watermark_rows = await (
                await db.execute(
                    "SELECT stream_key, cursor FROM watermarks WHERE operation_id = ? ORDER BY stream_key",
                    (op,),
                )
            ).fetchall()
        return {
            "operation_id": op,
            "exists": True,
            "degraded": bool(operation["degraded"]),
            "rescan_required": bool(operation["rescan_required"]),
            "deferred_count": int(operation["deferred_count"]),
            "degraded_reasons": json.loads(operation["degraded_reasons_json"]),
            "record_count": record_count,
            "queued_update_count": queue_count,
            "pending_delivery_count": pending_delivery_count,
            "watermarks": {row["stream_key"]: row["cursor"] for row in watermark_rows},
            "bounds": self.limits.__dict__.copy(),
        }

    async def wipe_operation(self, operation_id: Any) -> bool:
        op = _required_text(operation_id, "operation_id")
        async with self._lock:
            db = self._connection()
            cursor = await db.execute("DELETE FROM operations WHERE operation_id = ?", (op,))
            await db.commit()
            return bool(cursor.rowcount)
