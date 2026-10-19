"""Storage limits and competing connections preserve conservative report semantics."""

import asyncio
import hashlib
import json

import aiosqlite
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_core.messages import AIMessage, ToolMessage

from ai.langgraph.assisted_reporting import render_identity_report
from ai.langgraph.model import Model
from sage_chat.operation_memory import OperationMemoryStore
from sage_chat.report_observations import admit_observations, retained_observations
import sage_chat.report_observations as repository


ENGAGEMENT = "11111111-1111-4111-8111-111111111111"


def record(task, request, value=None):
    text = value or f"LAB\\operator{task}"
    return {"request_id": request, "task_id": task, "task_internal_id": task + 1000,
            "task_operation_id": 7, "origin": "historical_or_external",
            "stored_command_name": "whoami", "callback_display_id": 2,
            "response_text_excerpts": [text], "partial_snapshot": True,
            "truncated": False, "scoped_content_omitted": False,
            "response_sources": [{"response_id": task + 2000,
                                  "excerpt_sha256": hashlib.sha256(text.encode()).hexdigest(),
                                  "response_timestamp": "2026-10-06T00:00:00Z"}]}


async def admit(store, task, channel=4, request_number=1, value=None):
    request = f"chat:{channel}:request:{request_number}"
    return await admit_observations(store, 7, ENGAGEMENT, channel_id=channel,
                                   request_id=request, checkpoint_thread=f"{channel}:generation:probe",
                                   records=[record(task, request, value)])


def test_storage_capacity_is_disclosed_and_no_partial_extra_source(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "_MAX_ROWS", 3)

    async def run():
        store = OperationMemoryStore(tmp_path / "sage.db")
        try:
            for task in range(1, 4):
                assert await admit(store, task) == 1
            assert await admit(store, 4) == 0
            rows, omitted, capacity = await retained_observations(store, 7, ENGAGEMENT)
            assert len(rows) == 3 and omitted == 0 and capacity
            output = render_identity_report(rows, operation_scope=(7, ENGAGEMENT),
                                            omitted_sources=omitted, capacity_reached=capacity)
            assert "capacity" in output.lower()
            assert "operator4" not in output
            assert "| LAB&#92;operator1 |" not in output.split("### Bounded task output excerpts")[0]
        finally:
            await store.close()
    asyncio.run(run())


def test_conflicting_revision_cannot_hide_behind_display_limit(tmp_path):
    async def run():
        store = OperationMemoryStore(tmp_path / "sage.db")
        try:
            for task in range(1, 11):
                await admit(store, task)
            await admit(store, 1, channel=5, request_number=2, value="LAB\\changed")
            rows, omitted, capacity = await retained_observations(store, 7, ENGAGEMENT)
            assert len(rows) == 8 and omitted == 2 and not capacity
            conflicted = next(row for row in rows if row["task_id"] == 1)
            assert conflicted["source_metadata_conflict"]
            output = render_identity_report(rows, operation_scope=(7, ENGAGEMENT),
                                            omitted_sources=omitted)
            observation_table = output.split("### Bounded task output excerpts")[0]
            assert "| LAB&#92;operator1 |" not in observation_table
            assert "| LAB&#92;changed |" not in observation_table
            assert "2 additional task" in output
        finally:
            await store.close()
    asyncio.run(run())


def test_two_report_stores_checkpoint_writers_and_readers_share_file(tmp_path):
    async def run():
        path = tmp_path / "sage.db"
        stores = [OperationMemoryStore(path), OperationMemoryStore(path)]
        connections = [aiosqlite.connect(path), aiosqlite.connect(path)]
        savers = [AsyncSqliteSaver(connection) for connection in connections]
        try:
            for store in stores:
                await store.initialize()
            for saver in savers:
                await saver.setup()

            async def operator(index):
                config = {"configurable": {"thread_id": f"operator-{index}", "checkpoint_ns": ""}}
                last = None
                for step in range(6):
                    last = await savers[index].aput(config, empty_checkpoint(),
                                                   {"source": "input", "step": step, "writes": {}}, {})
                    await admit(stores[index], index * 10 + step + 1, channel=index + 4,
                                request_number=step + 1)
                    rows, _, _ = await retained_observations(stores[index], 7, ENGAGEMENT)
                    assert len(rows) <= 8
                return last

            saved = await asyncio.gather(operator(0), operator(1))
            for saver, config in zip(savers, saved, strict=True):
                assert await saver.aget_tuple(config) is not None
            rows, omitted, capacity = await retained_observations(stores[0], 7, ENGAGEMENT)
            assert len(rows) == 8 and omitted == 4 and not capacity
            count = await (await stores[0]._connection().execute(
                "SELECT count(*) FROM report_observations")).fetchone()
            assert count[0] == 12
        finally:
            for store in stores:
                await store.close()
            for connection in connections:
                await connection.close()
    asyncio.run(run())


def test_matched_ambiguous_metadata_revision_suppresses_prior_supported_value(tmp_path):
    async def run():
        store = OperationMemoryStore(tmp_path / "sage.db")
        request = "chat:4:request:2"
        call = AIMessage(content="", tool_calls=[{
            "name": "get_all_task_output_by_task_id", "args": {"task_id": 1},
            "id": "read", "type": "tool_call"}])
        rows = [{"id": response, "response_text": "LAB\\operator1",
                 "task": {"id": 1001, "display_id": 1, "operation_id": 7,
                          "command_name": "whoami", "callback": {"display_id": callback}}}
                for response, callback in ((2001, 2), (2002, 3))]
        tool = ToolMessage(name="get_all_task_output_by_task_id", tool_call_id="read",
                           content=json.dumps({"provenance": {"task_id": 1,
                             "current_request_id": request, "origin": "historical_or_external"},
                             "task_output": rows}),
                           additional_kwargs={"_task_output_collection_request_id": request})
        model = Model.__new__(Model)
        model.channel_id, model.operation_id, model.mode = 4, 7, "conversation"
        try:
            await admit(store, 1)
            collection = model.native_report_collection(
                {"mythic_operator_messages": [call, tool]}, request)
            assert len(collection) == 1 and collection[0]["source_metadata_conflict"]
            await admit_observations(store, 7, ENGAGEMENT, channel_id=4, request_id=request,
                                     checkpoint_thread="4:generation:probe", records=collection)
            retained, _, _ = await retained_observations(store, 7, ENGAGEMENT)
            assert retained[0]["revision_ambiguity"]
            output = render_identity_report(retained).split("### Bounded task output excerpts")[0]
            assert "| LAB&#92;operator1 |" not in output
        finally:
            await store.close()
    asyncio.run(run())
