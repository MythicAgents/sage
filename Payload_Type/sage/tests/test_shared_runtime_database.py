"""The application stores share a physical SQLite file and preserve offline legacy data."""

import asyncio
from collections import Counter
from pathlib import Path
import sqlite3

import aiosqlite
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
import pytest
import runtime_db

from ai.langgraph.controller_recovery import ControllerRecoveryStore
from ai.langgraph.tool_cache import ToolCache
from runtime_db import (
    assert_legacy_migrated,
    database_path,
    migrate_legacy_operation_memory,
)
from sage_chat.operation_memory import OperationMemoryStore, SourceRecord
from sage_chat.operation_findings import EvidencePointer, FindingCandidate, reconcile_findings
from sage_chat.operation_memory_runtime import OperationMemoryRuntime


def test_canonical_path_is_absolute_and_cwd_independent(monkeypatch, tmp_path):
    monkeypatch.delenv("SAGE_DATABASE_PATH")
    expected = Path(__file__).resolve().parents[1] / "sage.db"
    monkeypatch.chdir(tmp_path)
    assert database_path() == expected
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(tmp_path / "test.db"))
    assert database_path() == tmp_path / "test.db"
    monkeypatch.setenv("SAGE_DATABASE_PATH", "relative.db")
    with pytest.raises(ValueError, match="absolute"):
        database_path()


def test_real_saver_cache_operation_store_and_recovery_share_file(tmp_path, monkeypatch):
    db_path = tmp_path / "sage.db"
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(db_path))

    async def scenario():
        connection = aiosqlite.connect(database_path(), check_same_thread=False)
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        checkpoint = empty_checkpoint()
        config = {"configurable": {"thread_id": "channel-a", "checkpoint_ns": ""}}
        saved = await saver.aput(config, checkpoint, {"source": "input", "step": 0, "writes": {}}, {})
        cache = ToolCache()
        await cache.set("tool", {"operation": 7}, {"value": 1})
        store = OperationMemoryRuntime()
        await store.initialize()
        await store.store.snapshot("7")
        await store.store.close()
        await connection.close()

        reopened = aiosqlite.connect(database_path(), check_same_thread=False)
        resumed = AsyncSqliteSaver(reopened)
        assert (await resumed.aget_tuple(saved)).checkpoint["id"] == checkpoint["id"]
        assert await ToolCache().get("tool", {"operation": 7}) == {"value": 1}
        await reopened.close()

    asyncio.run(scenario())
    recovery = ControllerRecoveryStore(7, 9)
    recovery.acquire()
    try:
        recovery.write({"operation": 7}, "clean", {"original_request_id": "r"})
    finally:
        recovery.release()
    assert recovery.load()["phase"] == "clean"
    with sqlite3.connect(db_path) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"checkpoints", "tool_cache", "operations", "sage_auto_controller_recovery"} <= names


def test_offline_migration_preserves_history_and_fails_closed(tmp_path, monkeypatch):
    target = tmp_path / "sage.db"
    legacy = tmp_path / "sage_operation_memory.db"
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(target))
    monkeypatch.setattr(runtime_db, "_ROOT", tmp_path)

    async def seed():
        store = OperationMemoryStore(legacy)
        await store.initialize()
        record = SourceRecord.build(
            operation_id="7",
            record_class="task_output",
            source_record_id="output-7",
            observed_at_utc="2026-08-01T12:00:00Z",
            content="persisted source evidence",
            callback_display_id="1",
            task_display_id="2",
            task_output_id="output-7",
            metadata={"source": "mythic"},
        )
        await store.ingest_batch("7", [record], stream_key="responses", next_cursor="cursor-7")
        await store.apply_watcher_profile(
            "7", owner_channel_id=9, owner_channel_name="watch", provider="provider",
            model="model", config_sources={}, interval_seconds=30, generation=1,
        )
        candidate = FindingCandidate.build(
            operation_id="7", finding_key="source:output-7", finding_type="source_fact",
            title="Finding 7", state="new", score=0.8,
            observed_at_utc="2026-08-01T12:00:00Z", confidence=0.9,
            evidence=(EvidencePointer.build(
                record_class="task_output", source_record_id="output-7",
                revision_sha256=record.content_sha256,
            ),),
            missing_assumptions=(), rationale="Source evidence persists.",
            suggested_validation="Review the source task.",
        )
        result = await reconcile_findings(store, "7", [candidate])
        assert result.notification is not None
        await store.close()
        return candidate.finding_id, result.notification.ledger_id

    finding_id, notification_id = asyncio.run(seed())
    with sqlite3.connect(legacy) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO finding_notification_ledger(operation_id, changes_json, created_at) "
            "VALUES ('7', '{}', 'b')"
        )
        db.execute("DELETE FROM finding_notification_ledger WHERE id > ?", (notification_id,))
        sequence = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='finding_notification_ledger'"
        ).fetchone()[0]
    original = legacy.read_bytes()
    with pytest.raises(RuntimeError, match="requires offline migration"):
        OperationMemoryRuntime()
    with sqlite3.connect(target) as db:
        db.execute("CREATE TABLE checkpoints (thread_id TEXT)")
    assert migrate_legacy_operation_memory() is True
    assert legacy.read_bytes() == original
    with sqlite3.connect(legacy) as source, sqlite3.connect(target) as destination:
        for table in (
            "operations", "watcher_profiles", "source_records", "record_heads", "watermarks",
            "findings", "finding_view", "finding_notification_ledger", "finding_delivery_outbox",
        ):
            assert Counter(source.execute(f"SELECT * FROM {table}")) == Counter(
                destination.execute(f"SELECT * FROM {table}")
            )
    with sqlite3.connect(target) as db:
        db.execute("INSERT INTO checkpoints VALUES ('new-channel')")
        db.execute("UPDATE operations SET updated_at='later' WHERE operation_id='7'")
    assert migrate_legacy_operation_memory() is False
    assert_legacy_migrated()
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert db.execute("SELECT thread_id FROM checkpoints").fetchall() == [('new-channel',)]
        assert db.execute("SELECT generation FROM watcher_profiles").fetchone()[0] == 1
        assert db.execute("SELECT cursor FROM watermarks WHERE stream_key='responses'").fetchone()[0] == "cursor-7"
        assert db.execute("SELECT source_record_id FROM source_records").fetchone()[0] == "output-7"
        assert db.execute("SELECT source_record_id FROM record_heads").fetchone()[0] == "output-7"
        assert db.execute("SELECT finding_id FROM finding_view").fetchone()[0] == finding_id
        assert {row[0] for row in db.execute("SELECT notification_id FROM finding_delivery_outbox")} == {notification_id}
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='finding_notification_ledger'"
        ).fetchone()[0] == sequence
    with sqlite3.connect(legacy) as db:
        db.execute(
            "UPDATE sqlite_sequence SET seq=seq+1 WHERE name='finding_notification_ledger'"
        )
    with pytest.raises(RuntimeError, match="requires offline migration"):
        assert_legacy_migrated()


def test_migration_rejects_table_collision_without_import(tmp_path):
    legacy = tmp_path / "sage_operation_memory.db"
    target = tmp_path / "sage.db"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE operations (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO operations VALUES (1)")
    with sqlite3.connect(target) as db:
        db.execute("CREATE TABLE operations (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO operations VALUES (2)")
    with pytest.raises(RuntimeError, match="conflicts"):
        migrate_legacy_operation_memory(target, legacy_path=legacy)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT id FROM operations").fetchall() == [(2,)]


def test_concurrent_channel_checkpoints_and_operation_wipe_preserve_other_state(tmp_path, monkeypatch):
    db_path = tmp_path / "sage.db"
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(db_path))

    async def scenario():
        store = OperationMemoryStore(database_path())
        await store.initialize()
        cache = ToolCache()
        await cache.initialize()

        async def channel(channel_id):
            connection = aiosqlite.connect(database_path(), check_same_thread=False)
            saver = AsyncSqliteSaver(connection)
            await saver.setup()
            config = {"configurable": {"thread_id": f"channel-{channel_id}", "checkpoint_ns": ""}}
            last = None
            for _ in range(4):
                checkpoint = empty_checkpoint()
                last = await saver.aput(
                    config, checkpoint, {"source": "input", "step": 0, "writes": {}}, {}
                )
            await connection.close()
            return last

        async def operation(operation_id):
            record = SourceRecord.build(
                operation_id=operation_id,
                record_class="task_output",
                source_record_id=f"output-{operation_id}",
                observed_at_utc="2026-08-01T12:00:00Z",
                content="bounded evidence",
                callback_display_id="1",
                task_display_id="2",
                task_output_id=f"output-{operation_id}",
                metadata={},
            )
            await store.ingest_batch(operation_id, [record], stream_key="responses", next_cursor="1")

        first, second, _, _ = await asyncio.gather(channel(1), channel(2), operation("a"), operation("b"))
        connection = aiosqlite.connect(database_path(), check_same_thread=False)
        saver = AsyncSqliteSaver(connection)
        await cache.set("tool", "key", {"ok": True})
        assert (await saver.aget_tuple(first)).checkpoint["id"] == first["configurable"]["checkpoint_id"]
        assert (await saver.aget_tuple(second)).checkpoint["id"] == second["configurable"]["checkpoint_id"]
        assert await store.wipe_operation("a") is True
        assert (await store.snapshot("b"))["record_count"] == 1
        assert await cache.get("tool", "key") == {"ok": True}
        assert await saver.aget_tuple(first) is not None
        await store.close()
        await connection.close()

    asyncio.run(scenario())


def test_writer_lock_times_out_without_partial_cache_write(tmp_path, monkeypatch):
    db_path = tmp_path / "sage.db"
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(db_path))

    async def scenario():
        cache = ToolCache()
        await cache.initialize()
        blocker = sqlite3.connect(db_path, timeout=1)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                await asyncio.wait_for(cache.set("tool", "blocked", {"ok": True}), timeout=6)
        finally:
            blocker.rollback()
            blocker.close()
        assert await cache.get("tool", "blocked") is None
        await cache.set("tool", "recovered", {"ok": True})
        assert await cache.get("tool", "recovered") == {"ok": True}

    asyncio.run(scenario())
