"""Bounded native task observations in the existing operation-memory connection."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from .operation_memory import OperationMemoryStore


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_MAX_ROWS = 512
_MAX_RECORD_BYTES = 32_768
_MAX_REPORT_SOURCES = 8
_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_observations (
 operation_id TEXT NOT NULL,
 engagement_uuid TEXT NOT NULL,
 task_id INTEGER NOT NULL,
 source_sha256 TEXT NOT NULL,
 witness_sha256 TEXT NOT NULL,
 channel_id INTEGER NOT NULL,
 request_id TEXT NOT NULL,
 checkpoint_thread TEXT NOT NULL,
 collected_at TEXT NOT NULL,
 record_json TEXT NOT NULL,
 withdrawn INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(operation_id, engagement_uuid, witness_sha256),
 FOREIGN KEY(operation_id) REFERENCES operations(operation_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS report_observations_scope ON report_observations
 (operation_id, engagement_uuid, task_id, withdrawn);
"""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _scope(operation_id: Any, engagement_uuid: Any) -> tuple[str, str]:
    if type(operation_id) is not int or operation_id <= 0:
        raise ValueError("exact operation ID required")
    if not isinstance(engagement_uuid, str) or not _UUID.fullmatch(engagement_uuid):
        raise ValueError("canonical engagement UUID required")
    return str(operation_id), engagement_uuid


async def _table(store: OperationMemoryStore) -> None:
    await store.initialize()
    async with store._lock:
        await store._connection().executescript(_SCHEMA)
        await store._connection().commit()


async def admit_observations(
    store: OperationMemoryStore, operation_id: int, engagement_uuid: str,
    *, channel_id: int, request_id: str, checkpoint_thread: str,
    records: list[Mapping[str, Any]],
) -> int:
    """Persist only caller-matched bounded records with a collection witness."""
    operation, engagement = _scope(operation_id, engagement_uuid)
    if (type(channel_id) is not int or channel_id <= 0 or not isinstance(request_id, str)
            or not request_id.startswith(f"chat:{channel_id}:request:")
            or len(request_id) > 256 or not isinstance(checkpoint_thread, str)
            or len(checkpoint_thread) > 256 or not checkpoint_thread
            or not isinstance(records, list) or len(records) > 8):
        raise ValueError("invalid report collection witness")
    prepared = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        row = dict(record)
        if (row.get("request_id") != request_id or type(row.get("task_id")) is not int
                or row["task_id"] < 0):
            continue
        source = {key: value for key, value in row.items() if key not in {"request_id", "origin"}}
        encoded = _canonical(row)
        if len(encoded.encode("utf-8")) > _MAX_RECORD_BYTES:
            continue
        source_hash = hashlib.sha256(_canonical(source).encode("utf-8")).hexdigest()
        witness = hashlib.sha256(_canonical([source_hash, channel_id, request_id,
                                                checkpoint_thread]).encode("utf-8")).hexdigest()
        prepared.append((row["task_id"], source_hash, witness, encoded))
    if not prepared:
        return 0
    await _table(store)
    async with store._lock:
        db = store._connection()
        await db.execute("BEGIN IMMEDIATE")
        try:
            await store._ensure_operation(db, operation)
            count = (await (await db.execute(
                "SELECT count(*) FROM report_observations WHERE operation_id=? AND engagement_uuid=?",
                (operation, engagement))).fetchone())[0]
            written = 0
            for task_id, source_hash, witness, encoded in prepared:
                if count >= _MAX_ROWS:
                    break
                withdrawn = await (await db.execute(
                    "SELECT 1 FROM report_observations WHERE operation_id=? AND engagement_uuid=? AND task_id=? AND withdrawn=1 LIMIT 1",
                    (operation, engagement, task_id))).fetchone()
                if withdrawn:
                    continue
                existing = await (await db.execute(
                    "SELECT 1 FROM report_observations WHERE operation_id=? AND engagement_uuid=? AND witness_sha256=?",
                    (operation, engagement, witness))).fetchone()
                if existing:
                    continue
                await db.execute("""INSERT INTO report_observations VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                    (operation, engagement, task_id, source_hash, witness, channel_id,
                     request_id, checkpoint_thread,
                     datetime.now(timezone.utc).isoformat(), encoded))
                count += 1
                written += 1
            await db.commit()
            return written
        except Exception:
            await db.rollback()
            raise


async def withdraw_source(store: OperationMemoryStore, operation_id: int,
                          engagement_uuid: str, task_id: int, source_sha256: str) -> int:
    """Explicit source-bound withdrawal also suppresses all older task revisions."""
    operation, engagement = _scope(operation_id, engagement_uuid)
    if type(task_id) is not int or task_id < 0 or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("exact source required")
    await _table(store)
    async with store._lock:
        db = store._connection()
        exists = await (await db.execute(
            "SELECT 1 FROM report_observations WHERE operation_id=? AND engagement_uuid=? AND task_id=? AND source_sha256=?",
            (operation, engagement, task_id, source_sha256))).fetchone()
        if not exists:
            return 0
        cursor = await db.execute(
            "UPDATE report_observations SET withdrawn=1 WHERE operation_id=? AND engagement_uuid=? AND task_id=?",
            (operation, engagement, task_id))
        await db.commit()
        return cursor.rowcount


async def retained_observations(store: OperationMemoryStore, operation_id: int,
                                engagement_uuid: str) -> tuple[list[dict], int, bool]:
    """Return eight task sources plus omitted-task count; revisions stay ambiguous."""
    operation, engagement = _scope(operation_id, engagement_uuid)
    if not store.db_path.is_file():
        raise RuntimeError("retained observation store is unavailable")
    await store.initialize()
    async with store._lock:
        db = store._connection()
        table = await (await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_observations'"
        )).fetchone()
        if not table:
            return [], 0, False
        tasks = await (await db.execute("""SELECT task_id, max(rowid) AS latest FROM report_observations
            WHERE operation_id=? AND engagement_uuid=? AND withdrawn=0 GROUP BY task_id
            ORDER BY latest DESC LIMIT ?""", (operation, engagement, _MAX_REPORT_SOURCES + 1))).fetchall()
        chosen = [row[0] for row in tasks[:_MAX_REPORT_SOURCES]]
        omitted = max(0, len(tasks) - _MAX_REPORT_SOURCES)
        total_tasks = (await (await db.execute("""SELECT count(DISTINCT task_id) FROM report_observations
            WHERE operation_id=? AND engagement_uuid=? AND withdrawn=0""", (operation, engagement))).fetchone())[0]
        omitted = max(omitted, total_tasks - _MAX_REPORT_SOURCES)
        retained_count = (await (await db.execute(
            "SELECT count(*) FROM report_observations WHERE operation_id=? AND engagement_uuid=?",
            (operation, engagement))).fetchone())[0]
        capacity_reached = retained_count >= _MAX_ROWS
        if not chosen:
            return [], omitted, capacity_reached
        marks = ",".join("?" for _ in chosen)
        rows = await (await db.execute(f"""SELECT task_id, source_sha256, channel_id,
            request_id, checkpoint_thread, collected_at, record_json FROM report_observations
            WHERE operation_id=? AND engagement_uuid=? AND withdrawn=0 AND task_id IN ({marks})
            ORDER BY rowid ASC LIMIT ?""", (operation, engagement, *chosen, _MAX_ROWS + 1))).fetchall()
    by_task: dict[int, dict[str, tuple[dict, list[str], int]]] = {}
    for row in rows[:_MAX_ROWS]:
        try:
            record = json.loads(row[6])
        except (TypeError, ValueError):
            continue
        witnesses = by_task.setdefault(row[0], {})
        if row[1] not in witnesses:
            witnesses[row[1]] = (record, [], 0)
        first, shown_witnesses, count = witnesses[row[1]]
        witness = f"channel {row[2]}; request {row[3]}; thread {row[4]}; collected {row[5]}"
        if witness not in shown_witnesses:
            if len(shown_witnesses) < 8:
                shown_witnesses.append(witness)
            witnesses[row[1]] = (first, shown_witnesses, count + 1)
    result = []
    for task_id in chosen:
        revisions = by_task.get(task_id, {})
        if not revisions:
            continue
        record, witnesses, witness_count = next(iter(revisions.values()))
        record["collection_witnesses"] = witnesses
        record["collection_witnesses_omitted"] = max(0, witness_count - len(witnesses))
        record["source_revision_sha256"] = next(iter(revisions))
        if len(revisions) != 1:
            record["source_metadata_conflict"] = True
            record["revision_ambiguity"] = True
        if capacity_reached:
            # Once writes are refused, a later contradicting revision may be
            # absent from this store. Keep excerpts visible but do not promote
            # any retained row as a supported current-scope fact.
            record["source_metadata_conflict"] = True
            record["retention_capacity_exhausted"] = True
        result.append(record)
    return result, omitted, capacity_reached
