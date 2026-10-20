"""Location and offline migration of Sage's application SQLite database."""

from __future__ import annotations

from collections import Counter
import hashlib
import os
from pathlib import Path
import sqlite3


DATABASE_ENV = "SAGE_DATABASE_PATH"
_ROOT = Path(__file__).resolve().parent
_LEGACY_NAME = "sage_operation_memory.db"
_MIGRATION_TABLE = "sage_operation_memory_migrations"


def database_path() -> Path:
    """Return one absolute path, independent of the process working directory."""
    configured = os.environ.get(DATABASE_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{DATABASE_ENV} must be an absolute path")
        return path.resolve()
    return _ROOT / "sage.db"


class DatabaseUnavailable(sqlite3.OperationalError):
    """sage.db cannot be opened or locked; the message names the path and the setting to change."""


def unavailable_message(path: str | Path, error: BaseException) -> str:
    # SQLite's own text ("unable to open database file") names neither the file nor the fix.
    return (
        f"Sage database {path}: {error}. If SQLite cannot create or lock files there (for example a "
        f"macOS Docker Desktop or OrbStack bind mount), set {DATABASE_ENV} to an absolute path on a "
        "filesystem that supports SQLite locking."
    )


def probe_database(path: str | Path | None = None) -> Path:
    """Open sage.db the way the runtime will, so an unusable location fails at startup, not on a chat turn.

    Creates the parent directory, then enables WAL (as the checkpointer and operation memory do) and
    takes a write lock, which is where an unlockable filesystem fails.
    """
    target = Path(path) if path is not None else database_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target, timeout=5)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseUnavailable(unavailable_message(target, exc)) from exc
    return target


def legacy_operation_memory_path(target: str | Path | None = None) -> Path:
    # The old store was fixed at the source root, even when Sage ran from a
    # different cwd. A configured destination must not conceal that file.
    return _ROOT / _LEGACY_NAME


def _snapshot(connection: sqlite3.Connection) -> tuple[str, dict[str, Counter]]:
    schema = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'index', 'trigger', 'view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    tables = {}
    digest = hashlib.sha256(repr(schema).encode()).copy()
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        sequences = connection.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name").fetchall()
        digest.update(repr(sequences).encode())
    for kind, name, _ in schema:
        if kind != "table":
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        rows = Counter(connection.execute(f"SELECT * FROM {quoted}").fetchall())
        tables[name] = rows
        for row, count in sorted(rows.items(), key=lambda item: repr(item[0])):
            digest.update(repr((name, row, count)).encode())
    return digest.hexdigest(), tables


def assert_legacy_migrated(target: str | Path | None = None) -> None:
    """Refuse startup if a canonical legacy file has unimported or changed data."""
    destination = Path(target or database_path()).resolve()
    legacy = legacy_operation_memory_path(destination)
    if not legacy.exists():
        return
    with sqlite3.connect(f"file:{legacy}?mode=ro", uri=True, timeout=5) as source:
        source.execute("BEGIN")
        fingerprint, _ = _snapshot(source)
    if not destination.is_file():
        raise RuntimeError(f"Legacy operation memory at {legacy} requires offline migration")
    with sqlite3.connect(f"file:{destination}?mode=ro", uri=True, timeout=5) as db:
        try:
            row = db.execute(
                f"SELECT source_digest FROM {_MIGRATION_TABLE} WHERE source_path=?", (str(legacy),)
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
    if row is None or row[0] != fingerprint:
        raise RuntimeError(f"Legacy operation memory at {legacy} requires offline migration")


def migrate_legacy_operation_memory(
    target: str | Path | None = None, *, legacy_path: str | Path | None = None
) -> bool:
    """Explicit offline import. Keep the source file and reject destination conflicts.

    Sage must be stopped before calling this function. SQLite write locks then bound
    concurrent source/destination writers during the copy and verification.
    """
    destination = Path(target or database_path()).resolve()
    legacy = Path(legacy_path or legacy_operation_memory_path(destination)).resolve()
    if not legacy.is_file():
        return False
    if destination == legacy:
        raise ValueError("Legacy and destination paths must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(legacy, timeout=5) as source:
        source.execute("PRAGMA busy_timeout=5000")
        source.execute("BEGIN IMMEDIATE")
        fingerprint, source_rows = _snapshot(source)
        source_schema = source.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        ).fetchall()
        with sqlite3.connect(destination, timeout=5) as dest:
            dest.execute("PRAGMA busy_timeout=5000")
            dest.execute("PRAGMA foreign_keys=ON")
            dest.execute("BEGIN IMMEDIATE")
            dest.execute("PRAGMA defer_foreign_keys=ON")
            dest.execute(
                f"CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} "
                "(source_path TEXT PRIMARY KEY, source_digest TEXT NOT NULL)"
            )
            existing = dest.execute(
                f"SELECT source_digest FROM {_MIGRATION_TABLE} WHERE source_path=?", (str(legacy),)
            ).fetchone()
            if existing is not None:
                if existing[0] != fingerprint:
                    raise RuntimeError("Legacy operation memory changed after migration")
                return False
            names = {row[0] for row in dest.execute("SELECT name FROM sqlite_master")}
            collisions = {name for _, name, _ in source_schema} & names
            if collisions:
                raise RuntimeError(f"Destination schema conflicts with legacy memory: {sorted(collisions)}")
            for kind, _, sql in source_schema:
                if kind == "table":
                    dest.execute(sql)
            for name, rows in source_rows.items():
                quoted = '"' + name.replace('"', '""') + '"'
                for row, count in rows.items():
                    placeholders = ",".join("?" for _ in row)
                    dest.executemany(f"INSERT INTO {quoted} VALUES ({placeholders})", [row] * count)
            for kind, _, sql in source_schema:
                if kind != "table":
                    dest.execute(sql)
            source_sequences = source.execute("SELECT name, seq FROM sqlite_sequence").fetchall() if source.execute(
                "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
            ).fetchone() else []
            for name, seq in source_sequences:
                dest.execute("DELETE FROM sqlite_sequence WHERE name=?", (name,))
                dest.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (name, seq))
            _, target_rows = _snapshot(dest)
            imported_schema = dest.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
            ).fetchall()
            schema_by_name = {name: (kind, sql) for kind, name, sql in imported_schema}
            schema_matches = all(schema_by_name.get(name) == (kind, sql) for kind, name, sql in source_schema)
            target_sequences = dict(dest.execute("SELECT name, seq FROM sqlite_sequence")) if source_sequences else {}
            if any(target_rows.get(name) != rows for name, rows in source_rows.items()) or dest.execute(
                "PRAGMA foreign_key_check"
            ).fetchone() or not schema_matches or any(target_sequences.get(name) != seq for name, seq in source_sequences):
                raise RuntimeError("Legacy operation memory verification failed")
            dest.execute(
                f"INSERT INTO {_MIGRATION_TABLE} VALUES (?, ?)", (str(legacy), fingerprint)
            )
        source.rollback()
    return True
