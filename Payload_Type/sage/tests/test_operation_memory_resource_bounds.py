from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from itertools import combinations
from pathlib import Path

import pytest

from sage_chat.operation_memory import (
    OperationMemoryLimits,
    OperationMemoryStore,
    ResourceDeferral,
    SourceRecord,
)
from sage_chat.operation_memory_runtime import render_findings_markdown
from sage_chat.operation_memory_source import (
    MythicOperationMemoryIngestor,
    MythicOperationMemorySource,
    StreamCursor,
)


def _record(operation_id: str, source_id: str, content: str) -> SourceRecord:
    return SourceRecord.build(
        operation_id=operation_id,
        record_class="task_output",
        source_record_id=source_id,
        observed_at_utc="2026-08-01T00:00:00Z",
        content=content,
        callback_display_id="4",
        task_display_id="8",
        task_output_id=source_id,
    )


def _row(record_id: int, *, operation_id: int = 7, **values):
    return {
        "id": record_id,
        "operation_id": operation_id,
        "timestamp": f"2026-08-01T00:00:{record_id:02d}Z",
        **values,
    }


class RecordedMythic:
    def __init__(self, rows, downloads=None):
        self.rows = rows
        self.downloads = downloads or {}
        self.queries = []
        self.downloaded = []

    async def execute(self, _client, query, variables):
        self.queries.append((query, dict(variables)))
        table = next(name for name in self.rows if f" {name}(" in query)
        after = StreamCursor(variables["after_ts"], variables["after_id"])
        cursor_field = "last_checkin" if table == "callback" else "timestamp"
        selected = [
            row
            for row in self.rows[table]
            if row["operation_id"] == variables["op"]
            and StreamCursor(row[cursor_field], row["id"]) > after
        ]
        selected.sort(key=lambda row: (row[cursor_field], row["id"]))
        return {table: selected[: variables["limit"]]}

    async def download(self, _client, file_uuid, _max_bytes):
        self.downloaded.append(file_uuid)
        return self.downloads[file_uuid]

    def source(self, inline_limit):
        return MythicOperationMemorySource(
            object(),
            max_inline_text_bytes=inline_limit,
            execute_query=self.execute,
            download_file=self.download,
        )


def _empty_rows():
    return {
        "callback": [],
        "task": [],
        "response": [],
        "credential": [],
        "filemeta": [],
    }


def test_frozen_deployed_defaults_and_positive_overrides(monkeypatch):
    expected = {
        "SAGE_OPERATION_MEMORY_MAX_MODEL_INPUT_TOKENS": 100_000,
        "SAGE_OPERATION_MEMORY_MAX_INLINE_TEXT_BYTES": 65_536,
        "SAGE_OPERATION_MEMORY_MAX_MODEL_CALLS_PER_UPDATE": 5,
        "SAGE_OPERATION_MEMORY_BACKFILL_BATCH_SIZE": 500,
        "SAGE_OPERATION_MEMORY_MAX_QUEUED_UPDATES": 100,
    }
    env_text = (Path(__file__).resolve().parents[1] / ".env").read_text()
    for name, value in expected.items():
        monkeypatch.delenv(name, raising=False)
        assert env_text.count(f"#{name}={value}") == 1
    assert OperationMemoryLimits.from_env() == OperationMemoryLimits()

    for index, name in enumerate(expected, start=1):
        monkeypatch.setenv(name, str(index))
    assert OperationMemoryLimits.from_env() == OperationMemoryLimits(1, 2, 3, 4, 5)

    for invalid in ("0", "-1", "not-an-integer"):
        monkeypatch.setenv("SAGE_OPERATION_MEMORY_MAX_QUEUED_UPDATES", invalid)
        with pytest.raises(ValueError, match="positive integer"):
            OperationMemoryLimits.from_env()


def test_all_five_exact_and_plus_one_controls_are_visible_and_isolated(tmp_path):
    async def scenario():
        limits = OperationMemoryLimits(
            max_model_input_tokens=10,
            max_inline_text_bytes=4,
            max_model_calls_per_update=2,
            backfill_batch_size=2,
            max_queued_updates=2,
        )
        store = OperationMemoryStore(tmp_path / "bounds.db", limits=limits)
        await store.initialize()

        exact_ingest = await store.ingest_batch(
            "exact",
            [_record("exact", "1", "1234"), _record("exact", "2", "12")],
            stream_key="responses",
            next_cursor="exact",
        )
        exact_budget = await store.reserve_analysis(
            "exact", model_input_tokens=10, model_calls=2
        )
        assert exact_ingest.deferred == 0
        assert exact_budget.degraded is False
        assert await store.enqueue_update("exact", dedupe_key="1", payload={})
        assert await store.enqueue_update("exact", dedupe_key="2", payload={})
        exact = await store.snapshot("exact")
        assert exact["degraded"] is False
        assert exact["deferred_count"] == 0

        over_ingest = await store.ingest_batch(
            "over",
            [
                _record("over", "1", "ééé"),
                _record("over", "2", "1234"),
                _record("over", "3", "deferred by page bound"),
            ],
            stream_key="responses",
            next_cursor="over",
        )
        over_budget = await store.reserve_analysis(
            "over", model_input_tokens=11, model_calls=3
        )
        assert over_ingest.examined == 2
        assert over_ingest.deferred == 1
        assert over_ingest.watermark_advanced is False
        assert over_budget.allowed_tokens == 10
        assert over_budget.allowed_model_calls == 2
        assert await store.enqueue_update("over", dedupe_key="1", payload={})
        assert await store.enqueue_update("over", dedupe_key="2", payload={})
        before_duplicate = await store.snapshot("over")
        assert not await store.enqueue_update("over", dedupe_key="2", payload={})
        assert (await store.snapshot("over"))["deferred_count"] == before_duplicate[
            "deferred_count"
        ]
        assert not await store.enqueue_update("over", dedupe_key="3", payload={})

        over = await store.snapshot("over")
        assert over["degraded"] is True
        assert over["rescan_required"] is True
        assert over["deferred_count"] == 5
        assert {reason["bound"] for reason in over["degraded_reasons"]} == {
            "max_model_input_tokens",
            "max_inline_text_bytes",
            "max_model_calls_per_update",
            "backfill_batch_size",
            "max_queued_updates",
        }
        assert all(
            reason["observed"] > reason["limit"]
            for reason in over["degraded_reasons"]
        )
        inline_reason = next(
            reason
            for reason in over["degraded_reasons"]
            if reason["bound"] == "max_inline_text_bytes"
        )
        assert inline_reason["limit"] == 4
        assert inline_reason["observed"] == 6
        assert (await store.snapshot("other"))["degraded"] is False

        with pytest.raises(ValueError, match="source_has_more must be boolean"):
            await store.ingest_batch(
                "other",
                [],
                stream_key="responses",
                next_cursor="bad",
                source_has_more="yes",  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="complete bounded source page"):
            await store.ingest_batch(
                "other",
                [_record("other", "1", "ok")],
                stream_key="responses",
                next_cursor="bad",
                source_has_more=True,
            )
        with pytest.raises(ValueError, match="positive integer"):
            ResourceDeferral("max_inline_text_bytes", 4, 5, True, "bad")
        await store.close()

    asyncio.run(scenario())


def test_source_page_deferral_is_visible_and_resumes_from_safe_cursor(tmp_path):
    async def scenario():
        rows = _empty_rows()
        rows["callback"] = [
            _row(
                1,
                display_id=4,
                host="ONE",
                active=True,
                last_checkin="2026-08-01T00:00:01Z",
            ),
            _row(
                2,
                display_id=5,
                host="TWO",
                active=True,
                last_checkin="2026-08-01T00:00:02Z",
            ),
        ]
        mythic = RecordedMythic(rows)
        limits = replace(OperationMemoryLimits(), backfill_batch_size=1)
        store = OperationMemoryStore(tmp_path / "page.db", limits=limits)
        await store.initialize()
        ingestor = MythicOperationMemoryIngestor(
            mythic.source(limits.max_inline_text_bytes), store
        )

        first = await ingestor.sync_operation(7)
        assert first["callbacks"].has_more is True
        assert first["callbacks"].ingest.deferred == 1
        snapshot = await store.snapshot("7")
        assert snapshot["record_count"] == 1
        assert snapshot["degraded"] is True
        assert snapshot["rescan_required"] is True
        assert snapshot["deferred_count"] == 1
        assert snapshot["degraded_reasons"][0]["bound"] == "backfill_batch_size"
        assert StreamCursor.parse(snapshot["watermarks"]["callbacks"]).record_id == 1

        second = await ingestor.sync_operation(7)
        assert second["callbacks"].has_more is False
        assert second["callbacks"].ingest.inserted == 1
        resumed = await store.snapshot("7")
        assert resumed["record_count"] == 2
        assert StreamCursor.parse(resumed["watermarks"]["callbacks"]).record_id == 2
        rendered = render_findings_markdown((), resumed)
        assert "**degraded**" in rendered
        assert "`backfill_batch_size`" in rendered
        assert "deferred: 1" in rendered
        assert "rescan required: yes" in rendered
        assert not mythic.downloaded
        await store.close()

    asyncio.run(scenario())


def test_source_content_bounds_preserve_provenance_and_report_deferral(tmp_path):
    async def scenario():
        rows = _empty_rows()
        rows["response"] = [
            _row(
                1,
                response_text="ééééé",
                task={
                    "display_id": 8,
                    "command_name": "ps",
                    "callback": {"display_id": 4},
                },
            )
        ]
        rows["filemeta"] = [
            _row(
                record_id,
                agent_file_id=f"file-{record_id}",
                complete=True,
                deleted=False,
                is_download_from_agent=True,
                chunk_size=size,
                total_chunks=1,
                task={"display_id": 8, "callback": {"display_id": 4}},
            )
            for record_id, size in ((2, 9), (3, 8), (4, 8))
        ]
        mythic = RecordedMythic(
            rows,
            downloads={"file-3": b"123456789", "file-4": b"12345678"},
        )
        limits = replace(
            OperationMemoryLimits(), max_inline_text_bytes=8, backfill_batch_size=10
        )
        store = OperationMemoryStore(tmp_path / "content.db", limits=limits)
        await store.initialize()
        await MythicOperationMemoryIngestor(
            mythic.source(limits.max_inline_text_bytes), store
        ).sync_operation(7)

        snapshot = await store.snapshot("7")
        assert snapshot["degraded"] is True
        assert snapshot["rescan_required"] is True
        assert snapshot["deferred_count"] == 3
        assert {reason["bound"] for reason in snapshot["degraded_reasons"]} == {
            "max_inline_text_bytes"
        }
        records = {
            (row["record_class"], row["source_record_id"]): row
            for row in await store.list_records("7")
        }
        response = records[("task_output", "1")]
        assert response["callback_display_id"] == "4"
        assert response["task_display_id"] == "8"
        assert response["task_output_id"] == "1"
        assert response["content_size"] == 10
        assert response["inline_text"] is None

        estimated = records[("file", "2")]
        actual = records[("file", "3")]
        exact = records[("file", "4")]
        assert estimated["metadata"]["content_fetch_status"] == "estimated_oversize"
        assert estimated["metadata"]["estimated_content_bytes"] == 9
        assert actual["metadata"]["content_fetch_status"] == "actual_oversize"
        assert actual["metadata"]["estimated_content_bytes"] == 8
        assert exact["inline_text"] == "12345678"
        assert mythic.downloaded == ["file-3", "file-4"]
        assert all(
            row["callback_display_id"] == "4" and row["task_display_id"] == "8"
            for row in (estimated, actual, exact)
        )
        rendered = render_findings_markdown((), snapshot)
        assert "`max_inline_text_bytes`" in rendered
        assert "deferred: 3" in rendered
        assert "rescan required: yes" in rendered

        exact_rows = _empty_rows()
        exact_rows["response"] = [
            _row(
                1,
                operation_id=8,
                response_text=base64.b64encode(b"12345678").decode(),
                task={"display_id": 9, "callback": {"display_id": 5}},
            )
        ]
        exact_mythic = RecordedMythic(exact_rows)
        await MythicOperationMemoryIngestor(
            exact_mythic.source(limits.max_inline_text_bytes), store
        ).sync_operation(8)
        exact_snapshot = await store.snapshot("8")
        assert exact_snapshot["degraded"] is False
        assert exact_snapshot["record_count"] == 1
        await store.close()

    asyncio.run(scenario())


def test_every_bound_subset_survives_canonical_markdown_without_truncation():
    bounds = (
        "max_model_input_tokens",
        "max_inline_text_bytes",
        "max_model_calls_per_update",
        "backfill_batch_size",
        "max_queued_updates",
    )
    for size in range(1, len(bounds) + 1):
        for subset in combinations(bounds, size):
            snapshot = {
                "operation_id": "7",
                "degraded": True,
                "degraded_reasons": [
                    {
                        "bound": bound,
                        "limit": 1,
                        "observed": 2,
                        "detail": "bounded property probe",
                    }
                    for bound in subset
                ],
                "record_count": 0,
                "queued_update_count": 0,
                "deferred_count": len(subset),
                "rescan_required": True,
            }
            rendered = render_findings_markdown((), snapshot)
            assert "…" not in rendered
            for bound in subset:
                assert bound in rendered
            for bound in set(bounds) - set(subset):
                assert bound not in rendered


def test_real_default_downloader_preserves_every_declared_size_state(
    tmp_path, monkeypatch
):
    import aiohttp
    from mythic import mythic_utilities

    class Content:
        def __init__(self, body, *, eof=True, incomplete_partial=None):
            self.body = body
            self.eof = eof
            self.incomplete_partial = incomplete_partial
            self.read_sizes = []

        async def readexactly(self, size):
            self.read_sizes.append(size)
            if self.incomplete_partial is not None:
                raise asyncio.IncompleteReadError(self.incomplete_partial, size)
            return self.body[:size]

        def at_eof(self):
            return self.eof

    class Response:
        def __init__(self, length, body=b"", *, eof=True, incomplete_partial=None):
            self.content_length = length
            self.content = Content(
                body, eof=eof, incomplete_partial=incomplete_partial
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

    response_box = [Response(None)]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            return response_box[0]

    class Client:
        http = "https://"
        server_ip = "mythic.invalid"
        server_port = 7443

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    monkeypatch.setattr(mythic_utilities, "get_headers", lambda _client: {})

    async def scenario():
        cap = 1024
        limits = replace(
            OperationMemoryLimits(), max_inline_text_bytes=cap, backfill_batch_size=10
        )
        store = OperationMemoryStore(tmp_path / "default-download.db", limits=limits)
        await store.initialize()
        cases = (
            (11, None, b"", True, None, "unknown_length", None, False),
            (12, cap - 1, b"a" * (cap - 1), True, None, "inlined", cap - 1, False),
            (13, cap, b"a" * cap, True, None, "inlined", cap, False),
            (14, cap + 1, b"never-read", True, None, "declared_oversize", cap + 1, True),
            (15, cap * 2, b"never-read", True, None, "declared_oversize", cap * 2, True),
            (16, cap, b"a" * (cap - 1), True, None, "incomplete", cap, False),
            (17, cap, b"", True, b"", "incomplete", cap, False),
            (18, cap, b"", True, b"a", "incomplete", cap, False),
            (19, cap, b"", True, b"a" * (cap - 1), "incomplete", cap, False),
        )
        for (
            operation_id,
            declared,
            body,
            eof,
            incomplete_partial,
            status,
            observed,
            degraded,
        ) in cases:
            rows = _empty_rows()
            rows["filemeta"] = [
                _row(
                    1,
                    operation_id=operation_id,
                    agent_file_id=f"file-{operation_id}",
                    complete=True,
                    deleted=False,
                    is_download_from_agent=True,
                    chunk_size=cap,
                    total_chunks=1,
                    task={"display_id": 8, "callback": {"display_id": 4}},
                )
            ]
            mythic = RecordedMythic(rows)
            response_box[0] = Response(
                declared, body, eof=eof, incomplete_partial=incomplete_partial
            )
            source = MythicOperationMemorySource(
                Client(),
                max_inline_text_bytes=cap,
                execute_query=mythic.execute,
            )
            result = await MythicOperationMemoryIngestor(source, store).sync_operation(
                operation_id
            )
            record = (await store.list_records(str(operation_id)))[0]
            snapshot = await store.snapshot(str(operation_id))
            assert record["metadata"]["content_fetch_status"] == status
            assert record["metadata"].get("observed_content_bytes") == observed
            assert record["callback_display_id"] == "4"
            assert record["task_display_id"] == "8"
            assert snapshot["degraded"] is degraded
            if degraded:
                assert result["files"].ingest.deferred == 0
                assert snapshot["deferred_count"] == 1
                assert snapshot["degraded_reasons"] == [
                    {
                        "bound": "max_inline_text_bytes",
                        "limit": cap,
                        "observed": observed,
                        "detail": (
                            f"files:1 content remains authoritative in Mythic and "
                            "requires explicit selection/rescan"
                        ),
                    }
                ]
                assert response_box[0].content.read_sizes == []
            elif status == "inlined":
                assert record["content_size"] == declared
                assert response_box[0].content.read_sizes == [declared]
            elif status == "incomplete":
                assert response_box[0].content.read_sizes == [declared]
            else:
                assert response_box[0].content.read_sizes == []
        await store.close()

    asyncio.run(scenario())
