"""Shared Engagement State Ledger storage in ``sage.db``.

One ledger per bound operation, shared by every session. Rows are addressed individually: a writer changes
only the rows it produced, inside one short SQLite transaction, and every change appends an event. Operator
corrections (retraction, undo, "no longer holds") are events plus row flags; they never delete a row, its
permanent number or its history. Readers obtain an immutable snapshot of hops, graph facts, corrections and
revision from one read transaction.

Pure stdlib plus the runtime proof boundary. No evaluation, range or operator package is imported here.
``engagement_ledger`` keeps the formatting and historical JSON helpers; this module owns live persistence.
"""

from __future__ import annotations

import contextlib
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid as _uuid
from typing import Any, Callable, Iterable, Iterator, Mapping

try:  # package import (runtime) or flat import (tests that put ai/langgraph on sys.path)
    from . import engagement_ledger as _ledger
    from . import task_evidence as _task_evidence
except ImportError:  # pragma: no cover - import-path fallback
    import engagement_ledger as _ledger  # type: ignore
    import task_evidence as _task_evidence  # type: ignore

# Version 2: hop rows carry only achieved or pending. Version 3: rows written before the shared task classification
# for one typed effect are folded into one row and revalidated. Store initialization migrates older operations.
SCHEMA_VERSION = 3
LEGACY_ROW_SCHEMA_VERSION = 1
MIGRATION_VERSION = 1
EXPORT_SCHEMA = "sage.esl.export.v1"
BUSY_TIMEOUT_MS = 5000
MAX_BATCH = 64
MAX_REASON_CHARS = 240
MIGRATION_COMMAND = (
    "skills/sage-goad-reset/scripts/engagement_ledger_migrate.py import --sage-stopped "
    "--database <sage.db> --legacy-dir <legacy state dir>"
)
OBJECTIVE_SETTING = "objective"
OBJECTIVE_SOURCE_SETTING = "objective_source"
_UUID_TAIL = re.compile(
    r"^(?P<name>.+)_(?P<op>\d+)_(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_NUMERIC_ONLY = re.compile(r"^(?P<name>.+)_(?P<op>\d+)$")
_VOLATILE_KEYS = frozenset({"timestamp", "captured_at", "provenance", "updated", "recorded_at"})
_ATTEMPT_STATUSES = frozenset({"failed", "blocked"})
_ROW_STATUSES = frozenset({"achieved", "pending"})
_UNVERIFIED_STATUS = "legacy_unverified"
SCHEMA_MIGRATION_REASON = "attempt record, not progress"
SCHEMA_MIGRATION_PROOF_REASON = "unverified achievement: proof missing"
SCHEMA_MIGRATION_LAPSE_REASON = "attempt record superseded by a verified achievement"
REVALIDATION_ACTOR = _ledger.REVALIDATION_ACTOR
REVALIDATION_REASON = (
    "revalidated under the shared task-evidence rule: a proof recorded before task classification is held pending "
    "until a new observation is admitted"
)


class LedgerUnavailable(RuntimeError):
    """The operation's ledger cannot be read or bound. A database error is never an empty ledger."""


class LedgerWriteFailed(LedgerUnavailable):
    """A ledger mutation was not committed. The observed result is unrecorded, not installed anywhere."""


class LedgerImportError(RuntimeError):
    """An offline import was refused; the destination is unchanged."""


@dataclass(frozen=True)
class OperationBinding:
    """The identity a session's ledger reads and writes are bound to."""

    store_operation_id: int
    engagement_key: str
    mythic_operation_id: int | None = None
    marker_uuid: str = ""
    namespace: str = ""
    display_name: str = ""
    database_path: str = ""


@dataclass(frozen=True)
class LedgerRow:
    row_id: int
    kind: str
    identity: str
    number: int
    payload: dict
    revision: int
    observed_revision: int
    head_event_id: int
    retraction: dict | None
    invalidation: dict | None
    created_at: str
    updated_at: str

    def correction_state(self) -> dict:
        state: dict[str, Any] = {}
        if self.retraction:
            state["retracted"] = dict(self.retraction)
        if self.invalidation:
            state["no_longer_holds"] = dict(self.invalidation)
        return state


@dataclass(frozen=True)
class RowMutation:
    """One produced row: the full observed payload for one identity, plus what it was derived from."""

    kind: str
    identity: str
    payload: dict
    expected_revision: int = 0
    mutation_id: str = ""


@dataclass
class ApplyResult:
    applied: list[int] = field(default_factory=list)
    observations: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    revision: int = 0
    # (identity, row revision) of every head this batch wrote.
    row_revisions: set = field(default_factory=set)
    # Identities whose head this batch changed (a kept observation leaves the head as it was).
    changed_identities: set = field(default_factory=set)


@dataclass(frozen=True)
class ReconciliationState:
    """Where the operation's task-history reconciliation stands.

    Every task at or below ``task_cursor`` has been evaluated, except the still-running ``held_tasks``, which each
    pass reads again. ``callback_baseline`` maps a callback display ID to the integrity level already accounted for;
    None until the operation's first reconciliation stores it.
    """

    task_cursor: int = 0
    held_tasks: tuple[int, ...] = ()
    callback_baseline: dict[str, str] | None = None


@dataclass(frozen=True)
class ReconciliationUpdate:
    """A reconciliation pass's position change, written in the same transaction as its rows.

    The cursor only moves forward. Held tasks are the stored set plus ``held_added`` minus ``held_resolved``, so two
    passes that overlap never drop a running task. ``callback_baseline_initial`` is stored only when the operation
    has no baseline yet; ``callback_baseline_entries`` are merged into it.
    """

    task_cursor: int | None = None
    held_added: tuple[int, ...] = ()
    held_resolved: tuple[int, ...] = ()
    callback_baseline_initial: Mapping[str, str] | None = None
    callback_baseline_entries: Mapping[str, str] | None = None


@dataclass
class CorrectionResult:
    matched: list[int] = field(default_factory=list)
    changed: list[int] = field(default_factory=list)
    revision: int = 0


@dataclass(frozen=True)
class Actor:
    """Who caused a mutation. ``actor_id`` is the authenticated identifier when the request carries one."""

    kind: str
    name: str = ""
    actor_id: str = ""
    channel_id: str = ""
    request_id: str = ""


SAGE_ACTOR = Actor(kind="sage")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _norm(value: Any) -> str:
    return _text(value).strip().casefold()


def _norm_predicate(value: Any) -> str:
    return " ".join(_text(value).strip().casefold().split())


def hop_identity(technique: Any, target: Any) -> str:
    """The logical identity of a hop: the case-normalized ``(technique, target)`` pair used by ``_same_hop``."""
    return f"{_norm(technique)}:{_norm(target)}"


def graph_fact_identity(predicate: Any, source: Any) -> str:
    return f"{_norm_predicate(predicate)}|{_norm(source)}"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _strip_volatile(v) for k, v in value.items() if k not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def mutation_id_for(engagement_key: str, kind: str, identity: str, payload: Mapping[str, Any]) -> str:
    """A stable ID for one observed result: replaying the same evidence is idempotent.

    Observation timestamps are excluded, so an identical reconciliation does not become a new update.
    """
    commitment = hashlib.sha256(_canonical(_strip_volatile(dict(payload))).encode()).hexdigest()
    return hashlib.sha256(f"{engagement_key}|{kind}|{identity}|{commitment}".encode()).hexdigest()


def _bounded_reason(value: Any, default: str) -> str:
    text = " ".join(_text(value).split()) or default
    return text[:MAX_REASON_CHARS]


def legacy_state_dir() -> str:
    """The configured legacy JSON directory. It identifies legacy files only, never a live store."""
    return _ledger.state_dir()


def default_database_path() -> Path:
    try:
        from runtime_db import database_path
    except ImportError:  # pragma: no cover - runtime_db sits at the Sage root on sys.path
        from ...runtime_db import database_path  # type: ignore
    return database_path()


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS esl_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mythic_operation_id INTEGER,
        marker_uuid TEXT,
        namespace TEXT UNIQUE,
        engagement_key TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL DEFAULT '',
        next_hop_number INTEGER NOT NULL DEFAULT 1 CHECK (next_hop_number >= 1),
        revision INTEGER NOT NULL DEFAULT 0,
        graph_generation_revision INTEGER NOT NULL DEFAULT 0,
        graph_cut_revision INTEGER NOT NULL DEFAULT 0,
        graph_facts_updated TEXT,
        schema_version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (mythic_operation_id, marker_uuid),
        CHECK ((namespace IS NULL) <> (marker_uuid IS NULL))
    )""",
    """CREATE TABLE IF NOT EXISTS esl_rows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id INTEGER NOT NULL REFERENCES esl_operations(id),
        kind TEXT NOT NULL CHECK (kind IN ('hop', 'graph_fact')),
        identity TEXT NOT NULL,
        number INTEGER CHECK ((kind = 'hop' AND number >= 1) OR (kind = 'graph_fact' AND number IS NULL)),
        payload TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        observed_revision INTEGER NOT NULL DEFAULT 0,
        head_event_id INTEGER NOT NULL DEFAULT 0,
        retraction_event_id INTEGER,
        invalidation_event_id INTEGER,
        invalidation_scope TEXT CHECK (invalidation_scope IN ('operator', 'automatic')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (operation_id, kind, identity),
        UNIQUE (operation_id, number)
    )""",
    """CREATE TABLE IF NOT EXISTS esl_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id INTEGER NOT NULL REFERENCES esl_operations(id),
        row_id INTEGER REFERENCES esl_rows(id),
        mutation_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN (
            'create', 'update', 'observation', 'retract', 'undo_retraction', 'invalidate', 'graph_cut', 'import'
        )),
        actor_kind TEXT NOT NULL,
        actor_name TEXT NOT NULL DEFAULT '',
        actor_id TEXT,
        channel_id TEXT,
        request_id TEXT,
        recorded_at TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        payload TEXT,
        target_event_id INTEGER REFERENCES esl_events(id),
        row_revision INTEGER,
        operation_revision INTEGER NOT NULL,
        UNIQUE (operation_id, mutation_id)
    )""",
    """CREATE TABLE IF NOT EXISTS esl_imports (
        source_path TEXT PRIMARY KEY,
        source_digest TEXT NOT NULL,
        operation_id INTEGER REFERENCES esl_operations(id),
        migration_version INTEGER NOT NULL,
        imported_at TEXT NOT NULL,
        numbering_mode TEXT NOT NULL CHECK (numbering_mode IN ('preserved', 'excluded'))
    )""",
    """CREATE TABLE IF NOT EXISTS esl_legacy_settings (
        operation_id INTEGER NOT NULL REFERENCES esl_operations(id),
        name TEXT NOT NULL CHECK (name IN ('objective', 'objective_source')),
        value TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (operation_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS esl_reconciliation (
        operation_id INTEGER PRIMARY KEY REFERENCES esl_operations(id),
        task_cursor INTEGER NOT NULL DEFAULT 0 CHECK (task_cursor >= 0),
        held_tasks TEXT NOT NULL DEFAULT '[]',
        callback_baseline TEXT,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS esl_events_row ON esl_events(row_id, id)",
    """CREATE TRIGGER IF NOT EXISTS esl_events_append_only_update BEFORE UPDATE ON esl_events
        BEGIN SELECT RAISE(ABORT, 'esl_events is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS esl_events_append_only_delete BEFORE DELETE ON esl_events
        BEGIN SELECT RAISE(ABORT, 'esl_events is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS esl_rows_never_deleted BEFORE DELETE ON esl_rows
        BEGIN SELECT RAISE(ABORT, 'esl_rows are retracted, never deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS esl_rows_number_permanent BEFORE UPDATE OF number, identity, kind ON esl_rows
        WHEN NEW.number IS NOT OLD.number OR NEW.identity <> OLD.identity OR NEW.kind <> OLD.kind
        BEGIN SELECT RAISE(ABORT, 'row identity and number are permanent'); END""",
)
ESL_TABLES = ("esl_operations", "esl_rows", "esl_events", "esl_imports", "esl_legacy_settings")
# Tables a store from before them may lack. They are copied when present and never required to read.
OPTIONAL_ESL_TABLES = ("esl_reconciliation",)


def _connect(path: str | Path, *, write: bool = True) -> sqlite3.Connection:
    target = Path(path)
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    else:
        # Reads never create the file, its schema, or a journal-mode change.
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000,
                                     isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    if write:
        _ensure_wal(connection)
    return connection


def _ensure_wal(connection: sqlite3.Connection) -> None:
    """Switch to WAL once. The switch needs an exclusive lock and ignores busy_timeout, so concurrent first opens
    retry within the same bounded busy window instead of failing."""
    import time

    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).casefold() == "wal":
                return
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).casefold() or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def ensure_schema(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA:
        connection.execute(statement)


class EngagementStore:
    """Operation-scoped ESL persistence over one SQLite file. Each call uses its own short connection."""

    def __init__(self, path: str | Path | None = None, *, legacy_dir: str | None = None) -> None:
        self.path = Path(path) if path is not None else default_database_path()
        self._legacy_dir = legacy_dir
        self._schema_ready = False
        self._legacy_checked: tuple | None = None

    # ── connections ───────────────────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        error_type = LedgerWriteFailed if write else LedgerUnavailable
        try:
            connection = _connect(self.path, write=write)
        except (OSError, sqlite3.Error) as exc:
            raise error_type(f"engagement ledger database {self.path} unavailable: {exc}") from exc
        try:
            if write and not self._schema_ready:
                connection.execute("BEGIN IMMEDIATE")
                ensure_schema(connection)
                connection.execute("COMMIT")
                migrate_row_statuses(connection)
                self._schema_ready = True
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except (LedgerUnavailable, LedgerImportError):
            _rollback(connection)
            raise
        except sqlite3.Error as exc:
            _rollback(connection)
            raise error_type(f"engagement ledger {'write' if write else 'read'} failed: {exc}") from exc
        except BaseException:
            _rollback(connection)
            raise
        finally:
            connection.close()

    # ── legacy-file guard ─────────────────────────────────────────────────────────────────
    def legacy_dir(self) -> str:
        return self._legacy_dir if self._legacy_dir is not None else legacy_state_dir()

    def pending_legacy_files(self) -> list[str]:
        """Legacy ``state_*.json`` files with no matching import or exclusion record (missing or changed)."""
        files = _legacy_files(self.legacy_dir())
        if not files:
            return []
        recorded = _import_records(self.path)
        pending = []
        for path in files:
            # Matched by file name and content, not absolute path: a host-side migration also counts for a
            # container that mounts the same directory elsewhere. A changed file is pending again.
            if (path.name, _file_digest(path)) not in recorded:
                pending.append(str(path))
        return pending

    def _assert_legacy_imported(self) -> None:
        files = _legacy_files(self.legacy_dir())
        signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
        if signature == self._legacy_checked:
            return
        pending = self.pending_legacy_files()
        if pending:
            raise LedgerUnavailable(
                f"{len(pending)} legacy engagement ledger file(s) in {self.legacy_dir()} are not imported into "
                f"{self.path} (for example {Path(pending[0]).name}). Stop Sage and run {MIGRATION_COMMAND}."
            )
        self._legacy_checked = signature

    # ── binding ───────────────────────────────────────────────────────────────────────────
    def bind_operation(
        self, *, mythic_operation_id: int, marker_uuid: str, display_name: str, proposed_key: str
    ) -> OperationBinding:
        """Bind to one Mythic operation identity (numeric ID plus reset marker). The first canonical key is kept."""
        if not isinstance(mythic_operation_id, int) or isinstance(mythic_operation_id, bool) or mythic_operation_id < 1:
            raise LedgerUnavailable("operation binding requires a positive Mythic operation id")
        marker = _text(marker_uuid).strip().casefold()
        if not marker:
            raise LedgerUnavailable("operation binding requires the Mythic engagement marker")
        self._assert_legacy_imported()
        stamp = now_iso()
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM esl_operations WHERE mythic_operation_id=? AND marker_uuid=?",
                (mythic_operation_id, marker),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO esl_operations (mythic_operation_id, marker_uuid, engagement_key, display_name, "
                    "schema_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (mythic_operation_id, marker, proposed_key, display_name, SCHEMA_VERSION, stamp, stamp),
                )
                row = connection.execute(
                    "SELECT * FROM esl_operations WHERE mythic_operation_id=? AND marker_uuid=?",
                    (mythic_operation_id, marker),
                ).fetchone()
            elif display_name and row["display_name"] != display_name:
                connection.execute(
                    "UPDATE esl_operations SET display_name=? WHERE id=?", (display_name, row["id"])
                )
        return self._binding(row, display_name or row["display_name"])

    def bind_namespace(self, namespace: str) -> OperationBinding:
        """Bind to an explicit isolated namespace (offline/evaluation stores, never a native-chat operation)."""
        key = _text(namespace).strip()
        if not key or key == "default":
            raise LedgerUnavailable("an explicit engagement namespace is required")
        self._assert_legacy_imported()
        stamp = now_iso()
        with self._transaction(write=True) as connection:
            row = connection.execute("SELECT * FROM esl_operations WHERE namespace=?", (key,)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO esl_operations (namespace, engagement_key, display_name, schema_version, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (key, key, key, SCHEMA_VERSION, stamp, stamp),
                )
                row = connection.execute("SELECT * FROM esl_operations WHERE namespace=?", (key,)).fetchone()
        return self._binding(row, row["display_name"])

    def find_binding(self, engagement_key: str) -> OperationBinding | None:
        """Read-only lookup of an existing operation by canonical key (never creates one). For evaluators."""
        key = _text(engagement_key).strip()
        if not key or not self.path.exists():
            return None
        try:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"engagement ledger database {self.path} unavailable: {exc}") from exc
        try:
            connection.row_factory = sqlite3.Row
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='esl_operations'"
            ).fetchone()
            if not present:
                return None
            row = connection.execute("SELECT * FROM esl_operations WHERE engagement_key=?", (key,)).fetchone()
            return self._binding(row, row["display_name"]) if row is not None else None
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"engagement ledger read failed: {exc}") from exc
        finally:
            connection.close()

    def operation_hop_count(self, mythic_operation_id: int, marker_uuid: str) -> int | None:
        """Read-only: hop rows stored for one operation identity, or None when no ESL operation exists yet."""
        if not self.path.exists():
            return None
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='esl_operations'"
            ).fetchone()
            if not present:
                return None
            row = connection.execute(
                "SELECT id FROM esl_operations WHERE mythic_operation_id=? AND marker_uuid=?",
                (int(mythic_operation_id), _text(marker_uuid).strip().casefold()),
            ).fetchone()
            if row is None:
                return None
            return int(connection.execute(
                "SELECT COUNT(*) FROM esl_rows WHERE operation_id=? AND kind='hop'", (row[0],)
            ).fetchone()[0])
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"engagement ledger read failed: {exc}") from exc
        finally:
            connection.close()

    def logical_digest(self, binding: OperationBinding | None = None) -> str:
        """SHA-256 over canonical ESL content and revisions (one operation, or all), read-only.

        Unlike the bytes of the shared SQLite file, this changes only when ledger rows, corrections, history or
        the objective setting change, so evaluators can assert a reader did not mutate what it measured.
        """
        digest = hashlib.sha256()
        if not self.path.exists():
            return digest.hexdigest()
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(ESL_TABLES) <= present:
                return digest.hexdigest()
            rows = connection.execute("SELECT * FROM esl_operations ORDER BY id").fetchall()
            for row in rows:
                if binding is not None and int(row["id"]) != binding.store_operation_id:
                    continue
                op_binding = self._binding(row, row["display_name"])
                snapshot = _read_snapshot(connection, op_binding)
                payload = snapshot.export_dict(_read_events(connection, op_binding.store_operation_id))
                payload["legacy_settings"] = dict(snapshot.settings)
                payload["operation"]["display_name"] = ""
                digest.update(_canonical(payload).encode())
        finally:
            connection.close()
        return digest.hexdigest()

    def _binding(self, row: sqlite3.Row, display_name: str) -> OperationBinding:
        return OperationBinding(
            store_operation_id=int(row["id"]),
            engagement_key=str(row["engagement_key"]),
            mythic_operation_id=row["mythic_operation_id"],
            marker_uuid=row["marker_uuid"] or "",
            namespace=row["namespace"] or "",
            display_name=display_name,
            database_path=str(self.path),
        )

    # ── reads ─────────────────────────────────────────────────────────────────────────────
    def snapshot(self, binding: OperationBinding) -> "LedgerSnapshot":
        with self._transaction(write=False) as connection:
            return _read_snapshot(connection, binding)

    def history(self, binding: OperationBinding, *, row_id: int | None = None) -> list[dict]:
        with self._transaction(write=False) as connection:
            return _read_events(connection, binding.store_operation_id, row_id=row_id)

    def export(self, binding: OperationBinding) -> dict:
        """One consistent operation snapshot with full mutation history. Side-effect free."""
        with self._transaction(write=False) as connection:
            snapshot = _read_snapshot(connection, binding)
            events = _read_events(connection, binding.store_operation_id)
        return snapshot.export_dict(events)

    def rebuild_heads(self, binding: OperationBinding) -> dict[int, dict]:
        """Recompute every row head and its corrections from the retained events alone (read-only check).

        A head is the payload of the row's latest create/import/update event; an ``observation`` never replaces
        it. A retraction stays effective until an undo targets it; the latest ``invalidate`` is the marker.
        """
        with self._transaction(write=False) as connection:
            _operation_row(connection, binding)
            heads: dict[int, dict] = {}
            for event in _read_events(connection, binding.store_operation_id):
                row_id = event.get("row_id")
                if row_id is None:
                    continue
                head = heads.setdefault(int(row_id), {"payload": None, "retraction": None, "invalidation": None})
                kind = event["kind"]
                if kind in {"create", "import", "update"}:
                    head["payload"] = event.get("payload")
                elif kind == "retract":
                    head["retraction"] = int(event["id"])
                elif kind == "undo_retraction" and head["retraction"] == event.get("target_event_id"):
                    head["retraction"] = None
                elif kind == "invalidate":
                    head["invalidation"] = int(event["id"])
            return heads

    def legacy_settings(self, binding: OperationBinding) -> dict[str, str]:
        with self._transaction(write=False) as connection:
            return _read_settings(connection, binding.store_operation_id)

    # ── row mutations ─────────────────────────────────────────────────────────────────────
    def apply(
        self,
        binding: OperationBinding,
        mutations: Iterable[RowMutation],
        *,
        actor: Actor = SAGE_ACTOR,
        complete_graph_refresh: bool = False,
        graph_refresh_time: str = "",
        reconciliation: ReconciliationUpdate | None = None,
    ) -> ApplyResult:
        """Apply a bounded batch of produced rows. Unrelated heads are untouched.

        Each mutation is merged against the current stored head inside the transaction. A new identity takes the
        next permanent number. An admitted ``achieved`` head is historical achievement: a later non-achieved
        observation is kept in the row's history and never replaces it. Retractions and markers are row flags
        that no update clears. ``complete_graph_refresh`` marks a full graph reconcile, which defines the current
        graph-fact generation (facts it did not observe stay stored but are no longer current).
        ``reconciliation`` moves the task-history cursor and callback baseline in the same transaction.
        """
        batch = list(mutations)
        if len(batch) > MAX_BATCH:
            raise LedgerWriteFailed(f"ledger batch of {len(batch)} exceeds the {MAX_BATCH}-row bound")
        result = ApplyResult()
        stamp = now_iso()
        with self._transaction(write=True) as connection:
            op = _operation_row(connection, binding)
            revision = int(op["revision"]) + 1
            changed = False
            if complete_graph_refresh:
                connection.execute(
                    "UPDATE esl_operations SET graph_generation_revision=?, graph_facts_updated=? WHERE id=?",
                    (revision, graph_refresh_time or stamp, op["id"]),
                )
                changed = True
            for mutation in batch:
                outcome = _apply_one(connection, binding, op, mutation, revision, actor, stamp)
                if outcome is None:
                    continue
                kind, row_id = outcome[0], outcome[1]
                if kind == "applied" and len(outcome) > 2:
                    result.row_revisions.add((outcome[2], outcome[3]))
                    result.changed_identities.add(outcome[2])  # a typed effect may resolve to its owning row
                if kind == "unchanged":
                    result.unchanged.append(row_id)
                    continue
                changed = True
                (result.observations if kind == "observation" else result.applied).append(row_id)
                if kind == "applied":
                    result.changed_identities.add(mutation.identity)
            if changed:
                connection.execute(
                    "UPDATE esl_operations SET revision=?, updated_at=? WHERE id=?", (revision, stamp, op["id"])
                )
                result.revision = revision
            else:
                result.revision = int(op["revision"])
            if reconciliation is not None:
                _write_reconciliation(connection, int(op["id"]), reconciliation, stamp)
        return result

    def reconciliation_state(self, binding: OperationBinding) -> ReconciliationState:
        """The operation's reconciliation cursor, held tasks and callback baseline. A store from before them reads
        as a fresh start."""
        with self._transaction(write=False) as connection:
            _operation_row(connection, binding)
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='esl_reconciliation'"
            ).fetchone()
            if not present:
                return ReconciliationState()
            return _read_reconciliation(connection, binding.store_operation_id)

    # ── operator corrections ──────────────────────────────────────────────────────────────
    def retract(self, binding: OperationBinding, selectors: Iterable[str], *, reason: str, actor: Actor) -> CorrectionResult:
        return self._correct(binding, selectors, "retract", reason or "operator retracted via /state remove", actor)

    def restore(self, binding: OperationBinding, selectors: Iterable[str], *, reason: str, actor: Actor) -> CorrectionResult:
        return self._correct(binding, selectors, "undo_retraction", reason or "operator restored via /state restore", actor)

    def invalidate(
        self, binding: OperationBinding, selectors: Iterable[str], *, reason: str, actor: Actor
    ) -> CorrectionResult:
        return self._correct(binding, selectors, "invalidate", reason or "operator marked no longer holds", actor)

    def mark_no_longer_holds(
        self, binding: OperationBinding, identities: Iterable[str], *, reason: str, evidence: Mapping[str, Any],
        actor: Actor = SAGE_ACTOR,
    ) -> CorrectionResult:
        """Automatic "no longer holds" markers with their observed evidence (for example a contradiction)."""
        return self._correct(
            binding, [], "invalidate", reason or "observation contradicts this row", actor,
            identities=list(identities), evidence=dict(evidence), scope="automatic",
        )

    def wipe(self, binding: OperationBinding, *, actor: Actor, reason: str = "") -> CorrectionResult:
        """Retract every hop and cut every graph fact observed so far. Nothing is deleted or renumbered."""
        reason = _bounded_reason(reason, "operator retracted all rows via /state wipe")
        result = CorrectionResult()
        stamp = now_iso()
        with self._transaction(write=True) as connection:
            op = _operation_row(connection, binding)
            revision = int(op["revision"]) + 1
            rows = connection.execute(
                "SELECT id, retraction_event_id FROM esl_rows WHERE operation_id=? AND kind='hop' ORDER BY number",
                (op["id"],),
            ).fetchall()
            batch = _uuid.uuid4().hex
            for row in rows:
                result.matched.append(int(row["id"]))
                if row["retraction_event_id"] is not None:
                    continue
                event_id = _insert_event(
                    connection, op["id"], int(row["id"]), f"wipe:{batch}:{row['id']}", "retract", actor, stamp,
                    reason=reason, revision=revision,
                )
                connection.execute("UPDATE esl_rows SET retraction_event_id=? WHERE id=?", (event_id, row["id"]))
                result.changed.append(int(row["id"]))
            _insert_event(
                connection, op["id"], None, f"wipe:{batch}:graph", "graph_cut", actor, stamp,
                reason=reason, revision=revision,
            )
            connection.execute(
                "UPDATE esl_operations SET revision=?, graph_cut_revision=?, updated_at=? WHERE id=?",
                (revision, revision, stamp, op["id"]),
            )
            result.revision = revision
        return result

    def _correct(
        self,
        binding: OperationBinding,
        selectors: Iterable[str],
        kind: str,
        reason: str,
        actor: Actor,
        *,
        identities: list[str] | None = None,
        evidence: Mapping[str, Any] | None = None,
        scope: str = "operator",
    ) -> CorrectionResult:
        reason = _bounded_reason(reason, kind)
        result = CorrectionResult()
        stamp = now_iso()
        with self._transaction(write=True) as connection:
            op = _operation_row(connection, binding)
            revision = int(op["revision"]) + 1
            snapshot = _read_snapshot(connection, binding)
            if identities is not None:
                wanted = {str(identity) for identity in identities}
                matched = [row for row in snapshot.rows if row.kind == "hop" and row.identity in wanted]
            else:
                matched = select_rows(snapshot, selectors)
            batch = _uuid.uuid4().hex
            for row in matched:
                result.matched.append(row.row_id)
                payload = _canonical(dict(evidence)) if evidence else None
                if kind == "retract":
                    if row.retraction:
                        continue
                    event_id = _insert_event(
                        connection, op["id"], row.row_id, f"{kind}:{batch}:{row.row_id}", kind, actor, stamp,
                        reason=reason, payload=payload, row_revision=row.revision, revision=revision,
                    )
                    connection.execute("UPDATE esl_rows SET retraction_event_id=? WHERE id=?", (event_id, row.row_id))
                elif kind == "undo_retraction":
                    if not row.retraction:
                        continue
                    _insert_event(
                        connection, op["id"], row.row_id, f"{kind}:{batch}:{row.row_id}", kind, actor, stamp,
                        reason=reason, target_event_id=int(row.retraction["event_id"]),
                        row_revision=row.revision, revision=revision,
                    )
                    connection.execute("UPDATE esl_rows SET retraction_event_id=NULL WHERE id=?", (row.row_id,))
                elif kind == "invalidate":
                    current = row.invalidation or {}
                    # An operator marker always records (it may supersede an automatic one, which would lapse on
                    # a later achievement); an automatic marker never displaces an effective one.
                    if current.get("effective") and (current.get("scope") == "operator" or scope == "automatic"):
                        continue
                    event_id = _insert_event(
                        connection, op["id"], row.row_id, f"{kind}:{batch}:{row.row_id}", kind, actor, stamp,
                        reason=reason, payload=payload, row_revision=row.revision, revision=revision,
                    )
                    connection.execute(
                        "UPDATE esl_rows SET invalidation_event_id=?, invalidation_scope=? WHERE id=?",
                        (event_id, scope, row.row_id),
                    )
                else:  # pragma: no cover - internal misuse
                    raise LedgerWriteFailed(f"unknown correction {kind}")
                result.changed.append(row.row_id)
            if result.changed:
                connection.execute(
                    "UPDATE esl_operations SET revision=?, updated_at=? WHERE id=?", (revision, stamp, op["id"])
                )
                result.revision = revision
            else:
                result.revision = int(op["revision"])
        return result

    # ── temporary objective compatibility bridge ──────────────────────────────────────────
    def update_objective(
        self,
        binding: OperationBinding,
        objective: str,
        source: str,
        *,
        replace_if: Callable[[str, str], bool] | None = None,
    ) -> tuple[str, str]:
        """Atomically set the operation-level objective setting (temporary bridge; not part of the ESL).

        ``replace_if(current_objective, current_source)`` runs inside the transaction; returning False keeps the
        current value. Writes only the settings table: never hops, facts, numbering, corrections or evaluator data.
        """
        stamp = now_iso()
        with self._transaction(write=True) as connection:
            op = _operation_row(connection, binding)
            current = _read_settings(connection, op["id"])
            current_objective = current.get(OBJECTIVE_SETTING, "")
            current_source = current.get(OBJECTIVE_SOURCE_SETTING, "")
            if replace_if is not None and not replace_if(current_objective, current_source):
                return current_objective, current_source
            for name, value in ((OBJECTIVE_SETTING, objective), (OBJECTIVE_SOURCE_SETTING, source)):
                connection.execute(
                    "INSERT INTO esl_legacy_settings (operation_id, name, value, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(operation_id, name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (op["id"], name, value, stamp),
                )
        return objective, source


def _schema_migration_actor() -> Actor:
    return Actor(kind="system", name=_ledger.SCHEMA_MIGRATION_ACTOR)


def migrate_row_statuses(connection: sqlite3.Connection) -> dict[int, dict[str, int]]:
    """Upgrade every operation below the current schema, one transaction per operation.

    The caller holds no open transaction. Returns ``{operation id: counts}`` for the operations it upgraded.
    """
    stamp = now_iso()
    upgraded: dict[int, dict[str, int]] = {}
    pending = [
        int(row["id"]) for row in connection.execute(
            "SELECT id FROM esl_operations WHERE schema_version < ? ORDER BY id", (SCHEMA_VERSION,)
        ).fetchall()
    ]
    for operation_id in pending:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT schema_version FROM esl_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None or int(row["schema_version"]) >= SCHEMA_VERSION:
                connection.execute("COMMIT")
                continue
            upgraded[operation_id] = _migrate_operation(connection, operation_id, stamp)
            connection.execute("COMMIT")
        except BaseException:
            _rollback(connection)
            raise
    return upgraded


def _migrate_operation(connection: sqlite3.Connection, operation_id: int, stamp: str) -> dict[str, int]:
    """Append the schema-2 events for one operation inside the caller's transaction. Idempotent; never deletes.

    A ``failed`` or ``blocked`` head is an attempt record, not progress: it gets a system retraction and keeps its
    number, payload and history. A ``legacy_unverified`` head gets an appended update to ``pending`` /
    ``proof_missing``; the original payload stays in history. Then rows for a typed effect written before the shared
    task classification are revalidated (``_revalidate_typed_history``). Corrections, numbers, the high-water mark and
    graph facts are unchanged.
    """
    actor = _schema_migration_actor()
    counts = {"retracted": 0, "pending": 0}
    op = connection.execute("SELECT revision FROM esl_operations WHERE id=?", (operation_id,)).fetchone()
    revision = int(op["revision"]) + 1
    rows = connection.execute(
        "SELECT * FROM esl_rows WHERE operation_id=? AND kind='hop' ORDER BY number", (operation_id,)
    ).fetchall()

    def recorded(mutation_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM esl_events WHERE operation_id=? AND mutation_id=?", (operation_id, mutation_id)
        ).fetchone() is not None

    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        status = _norm(payload.get("status"))
        row_id = int(row["id"])
        if status in _ATTEMPT_STATUSES:
            mutation_id = f"schema-migration:v2:retract:{row_id}"
            if row["retraction_event_id"] is not None or recorded(mutation_id):
                continue
            event_id = _insert_event(
                connection, operation_id, row_id, mutation_id, "retract", actor, stamp,
                reason=SCHEMA_MIGRATION_REASON, row_revision=int(row["revision"]), revision=revision,
            )
            connection.execute("UPDATE esl_rows SET retraction_event_id=? WHERE id=?", (event_id, row_id))
            counts["retracted"] += 1
        elif status == _UNVERIFIED_STATUS:
            mutation_id = f"schema-migration:v2:pending:{row_id}"
            if recorded(mutation_id):
                continue
            upgraded = dict(payload, status="pending", pending_reason="proof_missing")
            encoded = _canonical(upgraded)
            new_revision = int(row["revision"]) + 1
            event_id = _insert_event(
                connection, operation_id, row_id, mutation_id, "update", actor, stamp, payload=encoded,
                reason=SCHEMA_MIGRATION_PROOF_REASON, row_revision=new_revision, revision=revision,
            )
            connection.execute(
                "UPDATE esl_rows SET payload=?, revision=?, head_event_id=?, updated_at=? WHERE id=?",
                (encoded, new_revision, event_id, stamp, row_id),
            )
            counts["pending"] += 1
    counts.update(_revalidate_typed_history(connection, operation_id, revision, stamp))
    if any(counts.values()):
        connection.execute(
            "UPDATE esl_operations SET revision=?, schema_version=?, updated_at=? WHERE id=?",
            (revision, SCHEMA_VERSION, stamp, operation_id),
        )
    else:
        connection.execute(
            "UPDATE esl_operations SET schema_version=? WHERE id=?", (SCHEMA_VERSION, operation_id)
        )
    return counts


def _revalidate_typed_history(
    connection: sqlite3.Connection, operation_id: int, revision: int, stamp: str
) -> dict[str, int]:
    """Schema 3: fold the rows written before the shared task classification for one typed effect into one row.

    For each typed identity still held by an unretracted non-canonical row, the row ``_typed_owner`` picks gets one
    update that carries a ``legacy:<hop>`` observation from itself and from every other such row. Admission re-runs
    under the current rule, so a proof recorded before task classification (for example an output-only capability
    achievement) is held pending until a new observation is admitted. Each other row is retracted as merged into the
    owner. Operator-retracted and attempt-record rows are never folded into another row; an attempt record never owns
    the effect, while an operator-retracted owner keeps its correction in force (as ``_typed_owner`` does for new
    writes). Typed rows derived before each contribution recorded its admitted writers are re-derived. Earlier events
    are never changed. Idempotent: a revalidated owner is canonical and a merged row is retracted.
    """
    actor = Actor(kind="system", name=REVALIDATION_ACTOR)
    counts = {"revalidated": 0, "merged": 0}
    op = connection.execute("SELECT engagement_key FROM esl_operations WHERE id=?", (operation_id,)).fetchone()
    engagement_key = str(op["engagement_key"]) if op is not None else ""
    groups: dict[str, list[tuple[sqlite3.Row, dict]]] = {}
    for row in connection.execute(
        "SELECT * FROM esl_rows WHERE operation_id=? AND kind='hop' AND retraction_event_id IS NULL ORDER BY number",
        (operation_id,),
    ).fetchall():
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        typed = _task_evidence.payload_typed_identity(payload)
        if typed and not _task_evidence.is_canonical_payload(payload):
            groups.setdefault(typed, []).append((row, payload))

    def recorded(mutation_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM esl_events WHERE operation_id=? AND mutation_id=?", (operation_id, mutation_id)
        ).fetchone() is not None

    for typed, members in groups.items():
        owner_identity, owner_payload = _typed_owner(connection, operation_id, typed, typed)
        owner = connection.execute(
            "SELECT * FROM esl_rows WHERE operation_id=? AND kind='hop' AND identity=?", (operation_id, owner_identity)
        ).fetchone()
        if owner is not None and owner["retraction_event_id"] is not None:
            retraction = connection.execute(
                "SELECT actor_name FROM esl_events WHERE id=?", (owner["retraction_event_id"],)).fetchone()
            if retraction is not None and retraction["actor_name"] == _ledger.SCHEMA_MIGRATION_ACTOR:
                # A retracted attempt record is not progress and owns nothing: the oldest real row does.
                owner, owner_payload = members[0]
        if owner is None or not isinstance(owner_payload, dict):
            continue
        if _task_evidence.is_canonical_payload(owner_payload):
            base = owner_payload
        else:
            own = _task_evidence.legacy_contribution(owner_payload, writer=f"legacy:{owner['number']}")
            base = {"id": owner_payload.get("id"), "technique": owner_payload.get("technique"),
                    "target": owner_payload.get("target"), "canonical_effect": typed,
                    "contributions": [own] if own else []}
        others = [(row, payload) for row, payload in members if int(row["id"]) != int(owner["id"])]
        incoming = [
            contribution for row, payload in others
            if (contribution := _task_evidence.legacy_contribution(payload, writer=f"legacy:{row['number']}"))
        ]
        merged = _payload_without_metadata(_task_evidence.merge_rows(
            base, {"canonical_effect": typed, "contributions": incoming}, engagement_key))
        mutation_id = f"schema-migration:v3:revalidate:{owner['id']}"
        if merged != owner_payload and not recorded(mutation_id):
            encoded = _canonical(merged)
            new_revision = int(owner["revision"]) + 1
            event_id = _insert_event(
                connection, operation_id, int(owner["id"]), mutation_id, "update", actor, stamp, payload=encoded,
                reason=REVALIDATION_REASON, row_revision=new_revision, revision=revision,
            )
            connection.execute(
                "UPDATE esl_rows SET payload=?, revision=?, head_event_id=?, updated_at=? WHERE id=?",
                (encoded, new_revision, event_id, stamp, int(owner["id"])),
            )
            counts["revalidated"] += 1
        for row, _payload in others:
            mutation_id = f"schema-migration:v3:merge:{row['id']}"
            if recorded(mutation_id):
                continue
            event_id = _insert_event(
                connection, operation_id, int(row["id"]), mutation_id, "retract", actor, stamp,
                reason=f"merged into hop {owner['number']}: one row per canonical effect",
                row_revision=int(row["revision"]), revision=revision,
            )
            connection.execute("UPDATE esl_rows SET retraction_event_id=? WHERE id=?", (event_id, int(row["id"])))
            counts["merged"] += 1
    # A typed row derived before views needed each task's admitted writers is re-derived under the current rule.
    for row in connection.execute(
        "SELECT * FROM esl_rows WHERE operation_id=? AND kind='hop' ORDER BY number", (operation_id,)
    ).fetchall():
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        if not _task_evidence.is_canonical_payload(payload) or all(
                isinstance(c, dict) and "admitted_writers" in c for c in payload["contributions"]):
            continue
        mutation_id = f"schema-migration:v3:revalidate:{row['id']}"
        if recorded(mutation_id):
            continue
        rederived = _payload_without_metadata(_task_evidence.derive_row(payload, engagement_key))
        encoded = _canonical(rederived)
        new_revision = int(row["revision"]) + 1
        event_id = _insert_event(
            connection, operation_id, int(row["id"]), mutation_id, "update", actor, stamp, payload=encoded,
            reason=REVALIDATION_REASON, row_revision=new_revision, revision=revision,
        )
        connection.execute(
            "UPDATE esl_rows SET payload=?, revision=?, head_event_id=?, updated_at=? WHERE id=?",
            (encoded, new_revision, event_id, stamp, int(row["id"])),
        )
        counts["revalidated"] += 1
    return counts


def schema_status(database: str | Path) -> list[dict[str, Any]]:
    """Read-only: each operation's schema version and how many rows the schema migration changed."""
    target = Path(database)
    if not target.exists():
        return []
    actor = _schema_migration_actor()
    revalidation = Actor(kind="system", name=REVALIDATION_ACTOR)
    with contextlib.closing(_connect(target, write=False)) as connection:
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"esl_operations", "esl_events"} <= present:
            return []
        out = []
        for op in connection.execute("SELECT id, engagement_key, schema_version FROM esl_operations ORDER BY id"):
            counts = {
                row["kind"]: int(row["n"]) for row in connection.execute(
                    "SELECT kind, COUNT(*) AS n FROM esl_events WHERE operation_id=? AND actor_kind=? "
                    "AND actor_name=? GROUP BY kind", (op["id"], actor.kind, actor.name),
                )
            }
            revalidated = {
                row["kind"]: int(row["n"]) for row in connection.execute(
                    "SELECT kind, COUNT(*) AS n FROM esl_events WHERE operation_id=? AND actor_kind=? "
                    "AND actor_name=? GROUP BY kind", (op["id"], revalidation.kind, revalidation.name),
                )
            }
            out.append({
                "engagement_key": op["engagement_key"],
                "schema_version": int(op["schema_version"]),
                "migrated_rows": {"retracted": counts.get("retract", 0), "pending": counts.get("update", 0)},
                "revalidated_rows": {"updated": revalidated.get("update", 0), "merged": revalidated.get("retract", 0)},
            })
        return out


def _read_reconciliation(connection: sqlite3.Connection, operation_id: int) -> ReconciliationState:
    row = connection.execute(
        "SELECT task_cursor, held_tasks, callback_baseline FROM esl_reconciliation WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return ReconciliationState()
    held = json.loads(row["held_tasks"] or "[]")
    baseline = json.loads(row["callback_baseline"]) if row["callback_baseline"] is not None else None
    return ReconciliationState(
        task_cursor=int(row["task_cursor"]),
        held_tasks=tuple(sorted({int(item) for item in held})),
        callback_baseline={str(k): str(v) for k, v in baseline.items()} if isinstance(baseline, dict) else None,
    )


def _write_reconciliation(
    connection: sqlite3.Connection, operation_id: int, update: ReconciliationUpdate, stamp: str
) -> None:
    current = _read_reconciliation(connection, operation_id)
    cursor = current.task_cursor
    if update.task_cursor is not None:
        cursor = max(cursor, int(update.task_cursor))
    held = (set(current.held_tasks) | {int(i) for i in update.held_added}) - {int(i) for i in update.held_resolved}
    baseline = None if current.callback_baseline is None else dict(current.callback_baseline)
    if baseline is None and update.callback_baseline_initial is not None:
        baseline = {str(k): str(v) for k, v in update.callback_baseline_initial.items()}
    if update.callback_baseline_entries:
        baseline = dict(baseline or {})
        baseline.update({str(k): str(v) for k, v in update.callback_baseline_entries.items()})
    connection.execute(
        "INSERT INTO esl_reconciliation (operation_id, task_cursor, held_tasks, callback_baseline, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(operation_id) DO UPDATE SET task_cursor=excluded.task_cursor, "
        "held_tasks=excluded.held_tasks, callback_baseline=excluded.callback_baseline, updated_at=excluded.updated_at",
        (operation_id, cursor, json.dumps(sorted(held)),
         json.dumps(baseline, sort_keys=True) if baseline is not None else None, stamp),
    )


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _operation_row(connection: sqlite3.Connection, binding: OperationBinding) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM esl_operations WHERE id=?", (binding.store_operation_id,)).fetchone()
    if row is None or str(row["engagement_key"]) != binding.engagement_key:
        raise LedgerUnavailable("bound operation is not present in the engagement ledger store")
    return row


def _insert_event(
    connection: sqlite3.Connection,
    operation_id: int,
    row_id: int | None,
    mutation_id: str,
    kind: str,
    actor: Actor,
    stamp: str,
    *,
    reason: str = "",
    payload: str | None = None,
    target_event_id: int | None = None,
    row_revision: int | None = None,
    revision: int,
) -> int:
    cursor = connection.execute(
        "INSERT INTO esl_events (operation_id, row_id, mutation_id, kind, actor_kind, actor_name, actor_id, "
        "channel_id, request_id, recorded_at, reason, payload, target_event_id, row_revision, operation_revision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            operation_id, row_id, mutation_id, kind, actor.kind, actor.name, actor.actor_id or None,
            actor.channel_id or None, actor.request_id or None, stamp, reason, payload, target_event_id,
            row_revision, revision,
        ),
    )
    return int(cursor.lastrowid)


def _is_admitted_achieved(payload: Mapping[str, Any], engagement_key: str) -> bool:
    if _norm(payload.get("status")) != "achieved":
        return False
    admitted, _reason = _ledger.hop_proof_admission(dict(payload), engagement_key)
    return admitted


def _apply_one(
    connection: sqlite3.Connection,
    binding: OperationBinding,
    op: sqlite3.Row,
    mutation: RowMutation,
    revision: int,
    actor: Actor,
    stamp: str,
) -> tuple | None:
    kind = mutation.kind
    revalidated = False
    if kind not in {"hop", "graph_fact"}:
        raise LedgerWriteFailed(f"unknown ledger row kind {kind!r}")
    payload = _payload_without_metadata(mutation.payload)
    identity = mutation.identity or _identity_for(kind, payload)
    if not identity or identity in {":", "|"}:
        raise LedgerWriteFailed("ledger mutation has no row identity")
    if kind == "hop" and _task_evidence.is_canonical_payload(payload):
        # A typed-effect row: resolve the row that owns the effect, then merge this writer's contributions onto
        # its stored head inside the transaction, so a concurrent writer's observation, an admitted proof, earlier
        # rejected evidence and the row's number and corrections all survive.
        identity, current = _typed_owner(connection, int(op["id"]), identity, str(payload["canonical_effect"]))
        # Adopting a row written before the shared task classification revalidates its stored proof.
        revalidated = isinstance(current, dict) and not _task_evidence.is_canonical_payload(current)
        payload = _payload_without_metadata(_task_evidence.merge_rows(current, payload, binding.engagement_key))
    if kind == "hop" and _norm(payload.get("status")) not in _ROW_STATUSES:
        # The ledger records progress only; a failed or blocked attempt belongs to the request attempt memory.
        raise LedgerWriteFailed(f"hop status {payload.get('status')!r} is not a ledger row status")
    head = connection.execute(
        "SELECT * FROM esl_rows WHERE operation_id=? AND kind=? AND identity=?", (op["id"], kind, identity)
    ).fetchone()
    if mutation.mutation_id and connection.execute(
        "SELECT 1 FROM esl_events WHERE operation_id=? AND mutation_id=?", (op["id"], mutation.mutation_id)
    ).fetchone():
        return ("unchanged", int(head["id"])) if head is not None else None
    encoded = _canonical(payload)
    if head is None:
        mutation_id = mutation.mutation_id or mutation_id_for(binding.engagement_key, kind, identity, payload) + ":0"
        number = None
        if kind == "hop":
            number = int(connection.execute(
                "SELECT next_hop_number FROM esl_operations WHERE id=?", (op["id"],)
            ).fetchone()[0])
            connection.execute(
                "UPDATE esl_operations SET next_hop_number=? WHERE id=?", (number + 1, op["id"])
            )
        cursor = connection.execute(
            "INSERT INTO esl_rows (operation_id, kind, identity, number, payload, revision, observed_revision, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (op["id"], kind, identity, number, encoded, revision, stamp, stamp),
        )
        row_id = int(cursor.lastrowid)
        event_id = _insert_event(
            connection, op["id"], row_id, mutation_id, "create", actor, stamp, payload=encoded,
            row_revision=1, revision=revision,
        )
        connection.execute("UPDATE esl_rows SET head_event_id=? WHERE id=?", (event_id, row_id))
        return ("applied", row_id, identity, 1)
    row_id = int(head["id"])
    current = json.loads(head["payload"])
    if _strip_volatile(current) == _strip_volatile(payload):
        # Replaying the same evidence (only observation times differ) neither duplicates history nor
        # renumbers. A re-observed fact still belongs to the current graph generation.
        if kind == "graph_fact":
            connection.execute("UPDATE esl_rows SET observed_revision=? WHERE id=?", (revision, row_id))
        return ("unchanged", row_id)
    mutation_id = mutation.mutation_id or (
        mutation_id_for(binding.engagement_key, kind, identity, payload) + f":{int(head['revision'])}"
    )
    if kind == "hop" and not _task_evidence.is_canonical_payload(payload) and _is_admitted_achieved(
        current, binding.engagement_key
    ) and not _is_admitted_achieved(payload, binding.engagement_key):
        if connection.execute(
            "SELECT 1 FROM esl_events WHERE operation_id=? AND mutation_id=?", (op["id"], mutation_id)
        ).fetchone():
            return ("unchanged", row_id)
        _insert_event(
            connection, op["id"], row_id, mutation_id, "observation", actor, stamp, payload=encoded,
            reason="kept historical achievement; later observation retained in history",
            row_revision=int(head["revision"]), revision=revision,
        )
        return ("observation", row_id)
    new_revision = int(head["revision"]) + 1
    event_id = _insert_event(
        connection, op["id"], row_id, mutation_id, "update", actor, stamp, payload=encoded,
        reason=REVALIDATION_REASON if revalidated else "" if int(mutation.expected_revision or 0) in {
            0, int(head["revision"])}
        else f"merged onto current revision {head['revision']} (derived from {mutation.expected_revision})",
        row_revision=new_revision, revision=revision,
    )
    connection.execute(
        "UPDATE esl_rows SET payload=?, revision=?, observed_revision=?, head_event_id=?, updated_at=? WHERE id=?",
        (encoded, new_revision, revision, event_id, stamp, row_id),
    )
    if kind == "hop" and head["retraction_event_id"] and _is_admitted_achieved(payload, binding.engagement_key):
        _lapse_attempt_record_retraction(connection, op["id"], row_id, int(head["retraction_event_id"]),
                                         new_revision, revision, stamp)
    return ("applied", row_id, identity, new_revision)


def _typed_owner(connection: sqlite3.Connection, operation_id: int, proposed: str, typed: str) -> tuple[str, Any]:
    """(identity, stored payload) of the row that owns a typed effect, or (proposed, None) for a new row.

    The proposed row when it exists; else the row already carrying the typed effect; else the lowest-numbered earlier
    row whose primary effect it is, retracted or not, so operator corrections on that effect stay in force.
    """
    row = connection.execute(
        "SELECT identity, payload FROM esl_rows WHERE operation_id=? AND kind='hop' AND identity=?",
        (operation_id, proposed),
    ).fetchone()
    if row is not None:
        return proposed, json.loads(row["payload"])
    legacy = None
    for row in connection.execute(
        "SELECT identity, payload FROM esl_rows WHERE operation_id=? AND kind='hop' ORDER BY number", (operation_id,)
    ):
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        if payload.get("canonical_effect") == typed:
            return str(row["identity"]), payload
        if legacy is None and _task_evidence.payload_typed_identity(payload) == typed:
            legacy = (str(row["identity"]), payload)
    return legacy if legacy is not None else (proposed, None)


def _lapse_attempt_record_retraction(
    connection: sqlite3.Connection, operation_id: int, row_id: int, retraction_event_id: int, row_revision: int,
    revision: int, stamp: str,
) -> None:
    """A verified achievement supersedes the schema migration's retraction of a former attempt record.

    Only that retraction lapses, as an appended undo event; an operator's retraction stays in force.
    """
    event = connection.execute(
        "SELECT actor_kind, actor_name FROM esl_events WHERE id=?", (retraction_event_id,)
    ).fetchone()
    actor = _schema_migration_actor()
    if event is None or (event["actor_kind"], event["actor_name"]) != (actor.kind, actor.name):
        return
    _insert_event(
        connection, operation_id, row_id, f"schema-migration:lapse:{row_id}:{retraction_event_id}",
        "undo_retraction", actor, stamp, reason=SCHEMA_MIGRATION_LAPSE_REASON,
        target_event_id=retraction_event_id, row_revision=row_revision, revision=revision,
    )
    connection.execute("UPDATE esl_rows SET retraction_event_id=NULL WHERE id=?", (row_id,))


_METADATA_KEYS = ("number", "row_id", "revision", "corrections")


def _payload_without_metadata(payload: Mapping[str, Any]) -> dict:
    out = {k: v for k, v in dict(payload).items() if k not in _METADATA_KEYS}
    if not out.get("pending_reason"):
        # Only pending rows carry a reason; an empty one is not part of the stored row.
        out.pop("pending_reason", None)
    if not out.get("canonical_effect") and not out.get("contributions"):
        # Only rows keyed by a typed effect carry contributions.
        out.pop("canonical_effect", None)
        out.pop("contributions", None)
    return out


def _identity_for(kind: str, payload: Mapping[str, Any]) -> str:
    if kind == "hop":
        return hop_identity(payload.get("technique"), payload.get("target"))
    return graph_fact_identity(payload.get("predicate"), payload.get("source"))


def _read_settings(connection: sqlite3.Connection, operation_id: int) -> dict[str, str]:
    return {
        row["name"]: row["value"] or ""
        for row in connection.execute(
            "SELECT name, value FROM esl_legacy_settings WHERE operation_id=?", (operation_id,)
        )
    }


def _event_summary(row: sqlite3.Row | None, *, effective: bool = True) -> dict | None:
    if row is None:
        return None
    summary = {
        "event_id": int(row["id"]),
        "kind": row["kind"],
        "actor": row["actor_name"] or row["actor_kind"],
        "actor_kind": row["actor_kind"],
        "actor_id": row["actor_id"] or "",
        "channel_id": row["channel_id"] or "",
        "request_id": row["request_id"] or "",
        "recorded_at": row["recorded_at"],
        "reason": row["reason"] or "",
        "effective": effective,
    }
    if row["payload"]:
        try:
            summary["evidence"] = json.loads(row["payload"])
        except ValueError:
            summary["evidence"] = {}
    return summary


def _read_snapshot(connection: sqlite3.Connection, binding: OperationBinding) -> "LedgerSnapshot":
    op = _operation_row(connection, binding)
    event_rows = {
        int(row["id"]): row
        for row in connection.execute(
            "SELECT e.* FROM esl_events e JOIN esl_rows r ON e.id IN (r.retraction_event_id, r.invalidation_event_id) "
            "WHERE r.operation_id=?",
            (op["id"],),
        )
    }
    rows = []
    for row in connection.execute(
        "SELECT * FROM esl_rows WHERE operation_id=? ORDER BY kind, COALESCE(number, 0), id", (op["id"],)
    ):
        retraction = _event_summary(event_rows.get(row["retraction_event_id"])) if row["retraction_event_id"] else None
        invalidation = None
        if row["invalidation_event_id"]:
            event = event_rows.get(row["invalidation_event_id"])
            effective = row["invalidation_scope"] == "operator" or int(row["invalidation_event_id"]) > int(
                row["head_event_id"] or 0
            )
            invalidation = _event_summary(event, effective=effective)
            if invalidation is not None:
                invalidation["scope"] = row["invalidation_scope"] or "operator"
        rows.append(LedgerRow(
            row_id=int(row["id"]),
            kind=row["kind"],
            identity=row["identity"],
            number=int(row["number"] or 0),
            payload=json.loads(row["payload"]),
            revision=int(row["revision"]),
            observed_revision=int(row["observed_revision"]),
            head_event_id=int(row["head_event_id"] or 0),
            retraction=retraction,
            invalidation=invalidation,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        ))
    return LedgerSnapshot(
        binding=binding,
        revision=int(op["revision"]),
        next_hop_number=int(op["next_hop_number"]),
        graph_generation_revision=int(op["graph_generation_revision"]),
        graph_cut_revision=int(op["graph_cut_revision"]),
        graph_facts_updated=op["graph_facts_updated"] or "",
        updated=op["updated_at"],
        rows=tuple(rows),
        settings=_read_settings(connection, op["id"]),
    )


def _read_events(connection: sqlite3.Connection, operation_id: int, *, row_id: int | None = None) -> list[dict]:
    query = "SELECT * FROM esl_events WHERE operation_id=?"
    params: list[Any] = [operation_id]
    if row_id is not None:
        query += " AND row_id=?"
        params.append(row_id)
    events = []
    for row in connection.execute(query + " ORDER BY id", params):
        event = {key: row[key] for key in row.keys() if key != "payload"}
        if row["payload"]:
            try:
                event["payload"] = json.loads(row["payload"])
            except ValueError:
                event["payload"] = row["payload"]
        events.append(event)
    return events


@dataclass(frozen=True)
class LedgerSnapshot:
    """An immutable operation snapshot from one read transaction. Hold it for one calculation only."""

    binding: OperationBinding
    revision: int
    next_hop_number: int
    graph_generation_revision: int
    graph_cut_revision: int
    graph_facts_updated: str
    updated: str
    rows: tuple[LedgerRow, ...]
    settings: dict = field(default_factory=dict)

    @property
    def engagement_key(self) -> str:
        return self.binding.engagement_key

    def hop_rows(self) -> list[LedgerRow]:
        return sorted((row for row in self.rows if row.kind == "hop"), key=lambda row: row.number)

    def fact_rows(self) -> list[LedgerRow]:
        return [row for row in self.rows if row.kind == "graph_fact"]

    def display_hops(self) -> list[dict]:
        """Every hop row (corrected rows included) with its permanent number and correction state."""
        out = []
        for row in self.hop_rows():
            payload = dict(row.payload)
            payload.update({
                "number": row.number,
                "row_id": row.row_id,
                "revision": row.revision,
                "corrections": row.correction_state(),
            })
            out.append(payload)
        return out

    def runtime_hop_dicts(self, *, ttl_hours: float = 0, now: str = "") -> tuple[list[dict], list[dict]]:
        """(eligible, ineligible) hop dicts for runtime use.

        Achieved rows without an admissible proof envelope read as ``pending`` / ``proof_missing`` in this view
        only. A row with an effective retraction or "no longer holds" marker, or past the configured hop TTL, is
        ineligible: it stays visible to renderers, but no gate, selector or completion decision may use it.
        """
        eligible: list[dict] = []
        ineligible: list[dict] = []
        cutoff = _ttl_cutoff(ttl_hours, now)
        for hop in self.display_hops():
            _ledger.quarantine_hop(hop, self.engagement_key)
            corrections = hop.get("corrections") or {}
            blocked = bool(corrections.get("retracted")) or bool(
                (corrections.get("no_longer_holds") or {}).get("effective")
            )
            expired = cutoff is not None and _before(hop.get("timestamp"), cutoff)
            if expired:
                hop["expired"] = True
            (ineligible if blocked or expired else eligible).append(hop)
        return eligible, ineligible

    def runtime_fact_dicts(self) -> tuple[list[dict], list[dict]]:
        """(current, stored-but-not-current) graph facts.

        Current facts belong to the latest complete graph refresh (or a later partial addition) and were observed
        after the last ``/state wipe`` cut. A partial or empty refresh never removes a stored fact.
        """
        floor = max(self.graph_generation_revision, self.graph_cut_revision + 1)
        current: list[dict] = []
        stale: list[dict] = []
        for row in self.fact_rows():
            payload = dict(row.payload)
            payload.update({"row_id": row.row_id, "revision": row.revision, "corrections": row.correction_state()})
            corrections = payload["corrections"]
            blocked = bool(corrections.get("retracted")) or bool(
                (corrections.get("no_longer_holds") or {}).get("effective")
            )
            (stale if blocked or row.observed_revision < floor else current).append(payload)
        return current, stale

    def objective_setting(self) -> tuple[str, str]:
        return self.settings.get(OBJECTIVE_SETTING, ""), self.settings.get(OBJECTIVE_SOURCE_SETTING, "")

    def change_signature(self) -> tuple:
        """Changes whenever any row, revision or correction changes (end-of-turn "unchanged" check)."""
        return tuple(
            (row.kind, row.identity, row.number, row.revision,
             bool(row.retraction), bool(row.invalidation and row.invalidation.get("effective")))
            for row in self.rows
            if row.kind == "hop"
        )

    def export_dict(self, events: list[dict] | None = None) -> dict:
        facts = []
        for row in self.fact_rows():
            payload = dict(row.payload)
            payload.update({
                "row_id": row.row_id,
                "revision": row.revision,
                "current": row.observed_revision >= max(self.graph_generation_revision, self.graph_cut_revision + 1),
                "corrections": row.correction_state(),
            })
            facts.append(payload)
        identity = {
            "store_operation_id": self.binding.store_operation_id,
            "mythic_operation_id": self.binding.mythic_operation_id,
            "marker_uuid": self.binding.marker_uuid,
            "namespace": self.binding.namespace,
            "display_name": self.binding.display_name,
        }
        return {
            "schema": EXPORT_SCHEMA,
            "engagement_id": self.engagement_key,
            "operation": identity,
            "revision": self.revision,
            "updated": self.updated,
            "next_hop_number": self.next_hop_number,
            "graph_facts_updated": self.graph_facts_updated,
            "graph_generation_revision": self.graph_generation_revision,
            "graph_cut_revision": self.graph_cut_revision,
            "hops": self.display_hops(),
            "graph_facts": facts,
            "history": list(events or []),
        }


def _ttl_cutoff(ttl_hours: float, now: str):
    try:
        if not ttl_hours or float(ttl_hours) <= 0:
            return None
        current = _parse_time(now) if now else datetime.now(timezone.utc)
        if current is None:
            return None
        from datetime import timedelta
        return current - timedelta(hours=float(ttl_hours))
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any):
    text = _text(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _before(value: Any, cutoff) -> bool:
    parsed = _parse_time(value)
    return parsed is not None and parsed < cutoff


def select_rows(snapshot: LedgerSnapshot, selectors: Iterable[str]) -> list[LedgerRow]:
    """Hop rows matching any selector: an exact permanent number, else an exact label/effect/technique."""
    hops = snapshot.hop_rows()
    chosen: dict[int, LedgerRow] = {}
    for selector in selectors:
        text = _text(selector).strip()
        if not text:
            continue
        if text.isdigit():
            for row in hops:
                if row.number == int(text):
                    chosen[row.row_id] = row
            continue
        wanted = text.casefold()
        for row in hops:
            if _ledger._hop_matches(row.payload, wanted):
                chosen[row.row_id] = row
    return sorted(chosen.values(), key=lambda row: row.number)


# ── offline migration ─────────────────────────────────────────────────────────────────────────


def _import_records(database: Path) -> set[tuple[str, str]]:
    """(file name, digest) of every import/exclusion record, read-only; empty for a ledger-less database."""
    if not Path(database).exists():
        return set()
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as exc:
        raise LedgerUnavailable(f"engagement ledger database {database} unavailable: {exc}") from exc
    try:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='esl_imports'"
        ).fetchone():
            return set()
        return {(Path(path).name, digest) for path, digest in connection.execute(
            "SELECT source_path, source_digest FROM esl_imports")}
    except sqlite3.Error as exc:
        raise LedgerUnavailable(f"engagement ledger read failed: {exc}") from exc
    finally:
        connection.close()


def _legacy_files(directory: str | Path) -> list[Path]:
    root = Path(directory)
    try:
        return sorted(
            path for path in root.iterdir()
            if path.is_file() and path.name.startswith("state_") and path.name.endswith(".json")
        )
    except OSError:
        return []


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_engagement_key(key: str) -> dict | None:
    """``<Name>_<op>_<uuid>`` → operation identity; None for keys that need an explicit mapping."""
    match = _UUID_TAIL.match(_text(key).strip())
    if not match:
        return None
    return {"name": match.group("name"), "operation_id": int(match.group("op")), "marker_uuid": match.group("uuid").casefold()}


def classify_legacy_key(key: str) -> str:
    """``operation`` (full key), ``mapping-required`` (default / numeric-only / pre-rename), or ``namespace``."""
    text = _text(key).strip()
    if parse_engagement_key(text):
        return "operation"
    if not text or text == "default" or _NUMERIC_ONLY.match(text):
        return "mapping-required"
    return "namespace"


def _validate_legacy(data: Any, source: Path) -> tuple[list[dict], list[dict]]:
    if not isinstance(data, dict) or not isinstance(data.get("hops", []), list):
        raise LedgerImportError(f"{source.name}: not a ledger object with a hops list")
    hops = []
    seen_ids: set[str] = set()
    seen_numbers: set[int] = set()
    for index, hop in enumerate(data.get("hops") or []):
        if not isinstance(hop, dict):
            raise LedgerImportError(f"{source.name}: hop {index} is not an object")
        identity = hop_identity(hop.get("technique"), hop.get("target"))
        if identity == ":":
            raise LedgerImportError(f"{source.name}: hop {index} has no technique/target identity")
        if identity in seen_ids:
            raise LedgerImportError(f"{source.name}: duplicate hop identity {identity}")
        seen_ids.add(identity)
        number = hop.get("number")
        if number is not None:
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise LedgerImportError(f"{source.name}: hop {identity} has invalid number {number!r}")
            if number in seen_numbers:
                raise LedgerImportError(f"{source.name}: duplicate hop number {number}")
            seen_numbers.add(number)
        hops.append(hop)
    facts = []
    seen_facts: set[str] = set()
    for index, fact in enumerate(data.get("graph_facts") or []):
        if not isinstance(fact, dict) or not _norm_predicate(fact.get("predicate")):
            raise LedgerImportError(f"{source.name}: graph fact {index} has no predicate")
        identity = graph_fact_identity(fact.get("predicate"), fact.get("source"))
        if identity in seen_facts:
            raise LedgerImportError(f"{source.name}: duplicate graph fact identity {identity}")
        seen_facts.add(identity)
        facts.append(fact)
    return hops, facts


def import_legacy_file(
    source: str | Path,
    *,
    database: str | Path | None = None,
    engagement_key: str | None = None,
    exclude: bool = False,
) -> str:
    """Import one legacy ``state_*.json`` file offline. Returns ``imported``, ``unchanged`` or ``excluded``.

    The source is never modified. The import binds by the file's full ``engagement_id`` (or an explicit
    ``engagement_key`` mapping), preserves rows, evidence, proof envelopes, graph facts, timestamps, numbers and
    the high-water mark, and grants no new proof. ``ground_truth_probes`` and other unknown fields stay in the
    retained source. It verifies counts, identities, payloads and numbering before committing; any conflict
    raises :class:`LedgerImportError` and leaves the destination unchanged.
    """
    path = Path(source).resolve()
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    store = EngagementStore(database, legacy_dir=str(path.parent))
    stamp = now_iso()
    with store._transaction(write=True) as connection:
        records = [
            (Path(row["source_path"]).name, row["source_digest"])
            for row in connection.execute("SELECT source_path, source_digest FROM esl_imports")
        ]
        if (path.name, digest) in records:
            return "unchanged"
        if any(name == path.name for name, _digest in records):
            raise LedgerImportError(f"{path.name} changed after it was imported; repair it explicitly")
        if exclude:
            connection.execute(
                "INSERT INTO esl_imports (source_path, source_digest, operation_id, migration_version, imported_at, "
                "numbering_mode) VALUES (?, ?, NULL, ?, ?, 'excluded')",
                (str(path), digest, MIGRATION_VERSION, stamp),
            )
            return "excluded"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise LedgerImportError(f"{path.name}: malformed JSON: {exc}") from exc
        hops, facts = _validate_legacy(data, path)
        key = _text(engagement_key or data.get("engagement_id")).strip()
        kind = classify_legacy_key(key)
        if kind == "mapping-required":
            raise LedgerImportError(
                f"{path.name}: engagement id {key!r} does not identify an operation; pass an explicit mapping"
            )
        identity = parse_engagement_key(key)
        if identity is not None:
            existing = connection.execute(
                "SELECT id FROM esl_operations WHERE (mythic_operation_id=? AND marker_uuid=?) OR engagement_key=?",
                (identity["operation_id"], identity["marker_uuid"], key),
            ).fetchone()
        else:
            existing = connection.execute(
                "SELECT id FROM esl_operations WHERE namespace=? OR engagement_key=?", (key, key)
            ).fetchone()
        if existing is not None:
            raise LedgerImportError(f"{path.name}: destination already holds operation {key}; not merging")
        if identity is not None:
            cursor = connection.execute(
                "INSERT INTO esl_operations (mythic_operation_id, marker_uuid, engagement_key, display_name, "
                "schema_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (identity["operation_id"], identity["marker_uuid"], key, identity["name"].replace("_", " "),
                 LEGACY_ROW_SCHEMA_VERSION, stamp, stamp),
            )
        else:
            cursor = connection.execute(
                "INSERT INTO esl_operations (namespace, engagement_key, display_name, schema_version, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key, key, key, LEGACY_ROW_SCHEMA_VERSION, stamp, stamp),
            )
        op_id = int(cursor.lastrowid)
        revision = 1
        high = 0
        try:
            high = max(high, int(data.get("next_hop_number") or 1) - 1)
        except (TypeError, ValueError):
            raise LedgerImportError(f"{path.name}: invalid next_hop_number {data.get('next_hop_number')!r}")
        claimed = {hop["number"] for hop in hops if hop.get("number") is not None}
        high = max([high, *claimed]) if claimed else high
        actor = Actor(kind="migration", name=f"legacy import {path.name}")
        expected_numbers: dict[str, int] = {}
        for hop in hops:
            number = hop.get("number")
            if number is None:
                high += 1
                number = high
            identity_text = hop_identity(hop.get("technique"), hop.get("target"))
            payload = _payload_without_metadata(hop)
            encoded = _canonical(payload)
            created = _text(hop.get("timestamp")) or stamp
            row = connection.execute(
                "INSERT INTO esl_rows (operation_id, kind, identity, number, payload, revision, observed_revision, "
                "created_at, updated_at) VALUES (?, 'hop', ?, ?, ?, 1, ?, ?, ?)",
                (op_id, identity_text, number, encoded, revision, created, created),
            )
            event_id = _insert_event(
                connection, op_id, int(row.lastrowid), f"import:{digest}:hop:{identity_text}", "import", actor,
                stamp, payload=encoded, row_revision=1, revision=revision,
                reason=f"imported from {path.name} without granting new proof",
            )
            connection.execute("UPDATE esl_rows SET head_event_id=? WHERE id=?", (event_id, int(row.lastrowid)))
            expected_numbers[identity_text] = number
        for fact in facts:
            identity_text = graph_fact_identity(fact.get("predicate"), fact.get("source"))
            encoded = _canonical(_payload_without_metadata(fact))
            created = _text(fact.get("timestamp")) or stamp
            row = connection.execute(
                "INSERT INTO esl_rows (operation_id, kind, identity, number, payload, revision, observed_revision, "
                "created_at, updated_at) VALUES (?, 'graph_fact', ?, NULL, ?, 1, ?, ?, ?)",
                (op_id, identity_text, encoded, revision, created, created),
            )
            event_id = _insert_event(
                connection, op_id, int(row.lastrowid), f"import:{digest}:fact:{identity_text}", "import", actor,
                stamp, payload=encoded, row_revision=1, revision=revision,
                reason=f"imported from {path.name}",
            )
            connection.execute("UPDATE esl_rows SET head_event_id=? WHERE id=?", (event_id, int(row.lastrowid)))
        connection.execute(
            "UPDATE esl_operations SET next_hop_number=?, revision=?, graph_generation_revision=?, "
            "graph_facts_updated=?, updated_at=? WHERE id=?",
            (high + 1, revision, revision, _text(data.get("graph_facts_updated") or data.get("updated")) or None,
             _text(data.get("updated")) or stamp, op_id),
        )
        for name in (OBJECTIVE_SETTING, OBJECTIVE_SOURCE_SETTING):
            value = data.get(name)
            if value is not None and _text(value).strip():
                connection.execute(
                    "INSERT INTO esl_legacy_settings (operation_id, name, value, updated_at) VALUES (?, ?, ?, ?)",
                    (op_id, name, _text(value), stamp),
                )
        # Verify before commit: counts, identities, payloads, numbers and high-water.
        stored = connection.execute(
            "SELECT kind, identity, number, payload FROM esl_rows WHERE operation_id=?", (op_id,)
        ).fetchall()
        stored_hops = {row["identity"]: row for row in stored if row["kind"] == "hop"}
        stored_facts = {row["identity"]: row for row in stored if row["kind"] == "graph_fact"}
        if len(stored_hops) != len(hops) or len(stored_facts) != len(facts):
            raise LedgerImportError(f"{path.name}: imported row count mismatch")
        for hop in hops:
            identity_text = hop_identity(hop.get("technique"), hop.get("target"))
            row = stored_hops.get(identity_text)
            if row is None or row["number"] != expected_numbers[identity_text] or json.loads(row["payload"]) != json.loads(
                _canonical(_payload_without_metadata(hop))
            ):
                raise LedgerImportError(f"{path.name}: verification failed for hop {identity_text}")
        next_number = connection.execute(
            "SELECT next_hop_number FROM esl_operations WHERE id=?", (op_id,)
        ).fetchone()[0]
        if next_number <= max([0, *expected_numbers.values()]):
            raise LedgerImportError(f"{path.name}: high-water verification failed")
        connection.execute(
            "INSERT INTO esl_imports (source_path, source_digest, operation_id, migration_version, imported_at, "
            "numbering_mode) VALUES (?, ?, ?, ?, ?, 'preserved')",
            (str(path), digest, op_id, MIGRATION_VERSION, stamp),
        )
        # The imported rows keep their historical statuses in the import events; the schema migration then
        # appends the current-schema events in the same transaction.
        _migrate_operation(connection, op_id, stamp)
    return "imported"


def import_legacy_directory(
    legacy_dir: str | Path | None = None,
    *,
    database: str | Path | None = None,
    mapping: Mapping[str, str] | None = None,
    exclude: Iterable[str] = (),
) -> dict[str, str]:
    """Import every top-level ``state_*.json`` in a legacy directory. Each file is its own transaction.

    ``mapping`` maps a file name to an explicit engagement key; ``exclude`` records named files as explicitly
    not imported. Files needing a mapping are reported, not attached to any operation.
    """
    directory = Path(legacy_dir or legacy_state_dir())
    mapping = dict(mapping or {})
    excluded = set(exclude)
    report: dict[str, str] = {}
    for path in _legacy_files(directory):
        try:
            report[path.name] = import_legacy_file(
                path, database=database, engagement_key=mapping.get(path.name), exclude=path.name in excluded,
            )
        except LedgerImportError as exc:
            report[path.name] = f"refused: {exc}"
    return report


def export_operation(
    binding: OperationBinding, destination_dir: str | Path, *, database: str | Path | None = None,
    include_legacy_settings: bool = False,
) -> Path:
    """Write one operation export atomically under a collision-safe name and update the directory manifest."""
    store = EngagementStore(database or binding.database_path or None)
    with store._transaction(write=False) as connection:
        snapshot = _read_snapshot(connection, binding)
        events = _read_events(connection, binding.store_operation_id)
    payload = snapshot.export_dict(events)
    if include_legacy_settings:
        payload["legacy_settings"] = dict(snapshot.settings)
    return _write_export(Path(destination_dir), payload)


def export_database(
    database: str | Path, destination_dir: str | Path, *, include_legacy_settings: bool = True
) -> dict[str, str]:
    """Export every operation in a (typically archived) database from one read-only snapshot.

    Opens the database read-only, so exporting never adds tables to an archive. A database without ESL tables
    exports nothing.
    """
    destination = Path(destination_dir)
    written: dict[str, str] = {}
    payloads = []
    with contextlib.closing(sqlite3.connect(f"file:{Path(database)}?mode=ro", uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(ESL_TABLES) <= present:
            return {}
        store = EngagementStore(database, legacy_dir=os.devnull)
        for row in connection.execute("SELECT * FROM esl_operations ORDER BY id").fetchall():
            binding = store._binding(row, row["display_name"])
            snapshot = _read_snapshot(connection, binding)
            payload = snapshot.export_dict(_read_events(connection, binding.store_operation_id))
            if include_legacy_settings:
                payload["legacy_settings"] = dict(snapshot.settings)
            payloads.append(payload)
    for payload in payloads:
        written[payload["engagement_id"]] = str(_write_export(destination, payload))
    return written


def export_filename(engagement_key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", engagement_key)[:96]
    digest = hashlib.sha256(engagement_key.encode()).hexdigest()[:12]
    return f"state_{safe}.{digest}.esl.json"


MANIFEST_NAME = "esl_exports.json"


def _write_export(destination: Path, payload: dict) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / export_filename(payload["engagement_id"])
    _atomic_write_json(target, payload)
    manifest_path = destination / MANIFEST_NAME
    manifest: dict = {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    exports = manifest.get("exports") if isinstance(manifest.get("exports"), dict) else {}
    exports[payload["engagement_id"]] = {
        "path": target.name,
        "schema": payload["schema"],
        "revision": payload["revision"],
        "operation": payload["operation"],
    }
    _atomic_write_json(manifest_path, {"schema": "sage.esl.manifest.v1", "exports": exports})
    return target


def _atomic_write_json(target: Path, payload: Any) -> None:
    fd, temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def manifest_exports(directory: str | Path) -> dict[str, Path]:
    """Engagement key → export path from a directory's manifest (never the truncated legacy filename rule)."""
    root = Path(directory)
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    exports = manifest.get("exports") if isinstance(manifest, dict) else None
    if not isinstance(exports, dict):
        return {}
    return {key: root / str(entry.get("path")) for key, entry in exports.items() if isinstance(entry, dict)}


def copy_ledger_tables(source: str | Path, destination: str | Path) -> dict[str, int]:
    """Carry the complete ESL tables, their sequences, import records and settings into a fresh database.

    Used by the reset archive helper's default (ledger kept between arms) mode. Refuses a destination that
    already has ESL rows. Returns per-table row counts after verification.
    """
    src = Path(source)
    dst = Path(destination)
    counts: dict[str, int] = {}
    with contextlib.closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5)) as reader:
        reader.execute("BEGIN")
        present = {
            row[0] for row in reader.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not set(ESL_TABLES) <= present:
            return {}
        data = {table: reader.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in ESL_TABLES}
        columns = {
            table: [row[1] for row in reader.execute(f"PRAGMA table_info({table})")] for table in ESL_TABLES
        }
        optional = [table for table in OPTIONAL_ESL_TABLES if table in present]
        for table in optional:
            data[table] = reader.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            columns[table] = [row[1] for row in reader.execute(f"PRAGMA table_info({table})")]
        sequences = reader.execute(
            "SELECT name, seq FROM sqlite_sequence WHERE name IN (%s)" % ",".join("?" * len(ESL_TABLES)),
            ESL_TABLES,
        ).fetchall()
    with contextlib.closing(_connect(dst)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            ensure_schema(writer)
            for table in ESL_TABLES:
                if writer.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    raise LedgerImportError(f"destination {dst} already holds {table} rows")
            for table in (*ESL_TABLES, *optional):
                cols = columns[table]
                placeholders = ",".join("?" * len(cols))
                writer.executemany(
                    f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", [tuple(r) for r in data[table]]
                )
                counts[table] = len(data[table])
            for name, seq in sequences:
                writer.execute("DELETE FROM sqlite_sequence WHERE name=?", (name,))
                writer.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (name, seq))
            ensure_schema(writer)
            for table in ESL_TABLES:
                copied = writer.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if copied != counts[table]:
                    raise LedgerImportError(f"carry-forward verification failed for {table}")
            if writer.execute("PRAGMA foreign_key_check").fetchone():
                raise LedgerImportError("carry-forward foreign key verification failed")
            writer.execute("COMMIT")
        except BaseException:
            _rollback(writer)
            raise
    return counts


def hops_from_rows(rows: Iterable[Mapping[str, Any]]):
    """Hop dataclasses carrying permanent number, row id, revision and corrections."""
    try:
        from . import engagement_state
    except ImportError:  # pragma: no cover
        import engagement_state  # type: ignore
    return engagement_state.hops_from_dicts(list(rows))


def graph_facts_from_rows(rows: Iterable[Mapping[str, Any]]):
    try:
        from . import engagement_state
    except ImportError:  # pragma: no cover
        import engagement_state  # type: ignore
    return engagement_state.graph_facts_from_dicts(list(rows))


def hop_mutations(before: Iterable[Any], after: Iterable[Any]) -> list[RowMutation]:
    """Rows a pure reducer actually produced: hops in ``after`` that differ from the same identity in ``before``.

    Store metadata (number, revision, corrections) is excluded from the comparison, and the expected revision is
    the revision the reducer started from. Untouched rows never become write input.
    """
    def as_dict(hop: Any) -> dict:
        return dataclasses.asdict(hop) if dataclasses.is_dataclass(hop) and not isinstance(hop, type) else dict(hop)

    prior: dict[str, dict] = {}
    for hop in before or []:
        data = as_dict(hop)
        prior[hop_identity(data.get("technique"), data.get("target"))] = data
    produced: list[RowMutation] = []
    for hop in after or []:
        data = as_dict(hop)
        identity = hop_identity(data.get("technique"), data.get("target"))
        old = prior.get(identity)
        payload = _payload_without_metadata(data)
        if old is not None and _payload_without_metadata(old) == payload:
            continue
        mutation_id = ""
        if _task_evidence.is_canonical_payload(payload):
            # Stable per (task, typed effect, writer, observation): replaying the same observation is idempotent,
            # whatever else changed on the row meanwhile.
            added = _task_evidence.observation_keys(payload) - _task_evidence.observation_keys(old)
            if added:
                mutation_id = _task_evidence.contribution_mutation_id("", identity, added)
        produced.append(RowMutation(
            kind="hop", identity=identity, payload=payload,
            expected_revision=int((old or {}).get("revision") or 0), mutation_id=mutation_id,
        ))
    return produced


def fact_mutations(facts: Iterable[Any]) -> list[RowMutation]:
    out: list[RowMutation] = []
    for fact in facts or []:
        data = dataclasses.asdict(fact) if dataclasses.is_dataclass(fact) and not isinstance(fact, type) else dict(fact)
        payload = _payload_without_metadata(data)
        payload["predicate"] = _norm_predicate(payload.get("predicate"))
        if not payload["predicate"]:
            continue
        out.append(RowMutation(kind="graph_fact", identity=graph_fact_identity(payload["predicate"], payload.get("source")), payload=payload))
    return out
