from __future__ import annotations

import asyncio
from copy import deepcopy

from sage_chat.operation_findings import (
    EvidencePointer,
    FindingCandidate,
    FindingState,
    current_findings_view,
    list_notification_events,
    reconcile_findings,
)
from sage_chat.operation_memory import OperationMemoryLimits, OperationMemoryStore
from sage_chat.operation_memory_source import (
    MythicOperationMemoryIngestor,
    MythicOperationMemorySource,
    StreamCursor,
)


_TABLES = ("callback", "task", "response", "credential", "filemeta")
_DERIVED_TABLES = (
    "operations",
    "source_records",
    "record_heads",
    "watermarks",
    "update_queue",
    "budget_events",
    "findings",
    "finding_view",
    "finding_notification_ledger",
)


class _OperationScopedMythic:
    def __init__(self) -> None:
        self.rows = {
            "callback": [
                {
                    "id": 1,
                    "operation_id": operation_id,
                    "display_id": 4,
                    "host": f"HOST-{operation_id}",
                    "active": True,
                    "timestamp": "2026-08-01T00:00:00Z",
                    "last_checkin": "2026-08-01T00:00:01Z",
                }
                for operation_id in (7, 8)
            ],
            "task": [],
            "response": [],
            "credential": [],
            "filemeta": [],
        }
        self.query_operations: list[tuple[str, int]] = []

    async def execute(self, _client, query, variables):
        await asyncio.sleep(0)
        table = next(name for name in _TABLES if f" {name}(" in query)
        operation_id = int(variables["op"])
        self.query_operations.append((table, operation_id))
        cursor = StreamCursor(variables["after_ts"], variables["after_id"])
        cursor_field = "last_checkin" if table == "callback" else "timestamp"
        rows = [
            deepcopy(row)
            for row in self.rows[table]
            if row["operation_id"] == operation_id
            and StreamCursor(row[cursor_field], row["id"]) > cursor
        ]
        rows.sort(key=lambda row: (row[cursor_field], row["id"]))
        return {table: rows[: variables["limit"]]}


def _candidate(operation_id: str, revision_sha256: str) -> FindingCandidate:
    return FindingCandidate.build(
        operation_id=operation_id,
        finding_key="shared-key:same-host:same-path",
        finding_type="isolation_probe",
        title=f"Operation {operation_id} finding",
        state=FindingState.NEW,
        score=1.0,
        observed_at_utc="2026-08-02T00:00:00Z",
        confidence=0.8,
        evidence=(
            EvidencePointer.build(
                record_class="callback",
                source_record_id="1",
                revision_sha256=revision_sha256,
                callback_display_id="4",
            ),
        ),
        missing_assumptions=(),
        rationale="The same typed key must remain operation scoped.",
        suggested_validation="Compare the exact operation-scoped source record.",
    )


async def _table_rows(store: OperationMemoryStore, operation_id: str):
    async with store._lock:
        db = store._connection()
        result = {}
        for table in _DERIVED_TABLES:
            rows = await (
                await db.execute(
                    f"SELECT * FROM {table} WHERE operation_id = ? ORDER BY rowid",
                    (operation_id,),
                )
            ).fetchall()
            result[table] = [tuple(row) for row in rows]
        return result


def test_concurrent_operations_remain_isolated_and_wipe_preserves_mythic(tmp_path):
    async def scenario():
        mythic = _OperationScopedMythic()
        mythic_before = deepcopy(mythic.rows)
        store = OperationMemoryStore(
            tmp_path / "memory.db",
            limits=OperationMemoryLimits(max_model_calls_per_update=1),
        )
        await store.initialize()
        source = MythicOperationMemorySource(
            object(),
            max_inline_text_bytes=65_536,
            execute_query=mythic.execute,
        )
        ingestor = MythicOperationMemoryIngestor(source, store)

        first7, first8 = await asyncio.gather(
            ingestor.sync_operation(7),
            ingestor.sync_operation(8),
        )
        assert first7["callbacks"].ingest.inserted == 1
        assert first8["callbacks"].ingest.inserted == 1
        assert await store.enqueue_update("7", dedupe_key="same", payload={"op": 7})
        assert await store.enqueue_update("8", dedupe_key="same", payload={"op": 8})
        await asyncio.gather(
            store.reserve_analysis("7", model_input_tokens=1, model_calls=2),
            store.reserve_analysis("8", model_input_tokens=1, model_calls=2),
        )

        for row in mythic.rows["callback"]:
            row["host"] = f"UPDATED-{row['operation_id']}"
            row["last_checkin"] = "2026-08-02T00:00:00Z"

        async def observe(operation_id: str):
            observations = []
            for _ in range(12):
                records = await store.list_records(operation_id)
                observations.append(records)
                assert all(row["operation_id"] == operation_id for row in records)
                await asyncio.sleep(0)
            return observations

        (_, _), observed7, observed8 = await asyncio.gather(
            asyncio.gather(
                ingestor.sync_operation(7),
                ingestor.sync_operation(8),
            ),
            observe("7"),
            observe("8"),
        )
        assert observed7 and observed8

        records7 = await store.list_records("7")
        records8 = await store.list_records("8")
        assert [row["operation_id"] for row in records7] == ["7"]
        assert [row["operation_id"] for row in records8] == ["8"]
        assert records7[0]["metadata"]["host"] == "UPDATED-7"
        assert records8[0]["metadata"]["host"] == "UPDATED-8"

        candidate7 = _candidate("7", records7[0]["revision_sha256"])
        candidate8 = _candidate("8", records8[0]["revision_sha256"])
        result7, result8 = await asyncio.gather(
            reconcile_findings(store, "7", [candidate7]),
            reconcile_findings(store, "8", [candidate8]),
        )
        assert result7.view[0].finding_id != result8.view[0].finding_id
        assert result7.notification is not None
        assert result8.notification is not None

        replay7, replay8 = await asyncio.gather(
            reconcile_findings(store, "7", [candidate7]),
            reconcile_findings(store, "8", [candidate8]),
        )
        assert replay7.notification is None
        assert replay8.notification is None
        assert len(await list_notification_events(store, "7")) == 1
        assert len(await list_notification_events(store, "8")) == 1

        operation8_before = await _table_rows(store, "8")
        mythic_before_wipe = deepcopy(mythic.rows)
        assert await store.wipe_operation("7") is True
        assert await store.wipe_operation("7") is False
        assert (await store.snapshot("7"))["exists"] is False
        assert await store.list_records("7") == []
        assert await current_findings_view(store, "7") == ()
        assert await list_notification_events(store, "7") == ()
        assert all(not rows for rows in (await _table_rows(store, "7")).values())

        assert await _table_rows(store, "8") == operation8_before
        assert (await store.snapshot("8"))["exists"] is True
        assert len(await store.list_records("8")) == 1
        assert len(await current_findings_view(store, "8")) == 1
        assert len(await list_notification_events(store, "8")) == 1
        assert mythic.rows != mythic_before
        assert mythic.rows == mythic_before_wipe
        assert {(table, op) for table, op in mythic.query_operations} == {
            (table, op) for table in _TABLES for op in (7, 8)
        }
        await store.close()

    asyncio.run(scenario())
