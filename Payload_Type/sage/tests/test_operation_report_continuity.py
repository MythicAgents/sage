"""Exact operation continuity through native paired collection and retained report."""

import asyncio
import base64
import json
import operator
from types import SimpleNamespace
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock
from uuid import uuid4

import aiosqlite
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import StateGraph, START, END
from mythic_container.ChatBase import ChatSlashCommandInvocation

from ai.langgraph.model import Model, MessageCaptureCallback
from ai.langgraph.mythic_tools import MythicTools
from sage_chat.headless import HeadlessSageChat, build_chat_request
from sage_chat.operation_memory import OperationMemoryStore
from sage_chat.operation_memory_runtime import OperationMemoryRuntime
from sage_chat.report_identity import canonical_engagement_uuid
from sage_chat.report_observations import (
    admit_observations, retained_observations, withdraw_source,
)


UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"


def _record(task_id=701, identity="LAB\\analyst", request_id="chat:4:request:1"):
    return {"request_id": request_id, "task_id": task_id,
        "origin": "historical_or_external", "response_text_excerpts": [identity],
        "partial_snapshot": True, "truncated": False, "scoped_content_omitted": False,
        "callback_display_id": 7, "stored_command_name": "whoami",
        "task_timestamp": "2026-10-01T00:00:00Z", "response_timestamps": ["2026-10-01T00:00:01Z"],
        "response_sources": [{"response_id": task_id + 1000,
            "response_timestamp": "2026-10-01T00:00:01Z", "excerpt_sha256": "0" * 64}]}


def _paired(request_id="chat:4:request:1"):
    call = AIMessage(content="FORGED ACCESS CLAIM", tool_calls=[{
        "name": "get_all_task_output_by_task_id", "args": {"task_id": 701},
        "id": "read-701", "type": "tool_call"}])
    tool = ToolMessage(name="get_all_task_output_by_task_id", tool_call_id="read-701",
        content=json.dumps({"provenance": {"task_id": 701,
            "current_request_id": request_id, "origin": "historical_or_external"},
            "task_output": [{"id": 1701, "timestamp": "2026-10-01T00:00:01Z",
            "response_text": "LAB\\analyst", "task": {"id": 901, "operation_id": 7,
                    "display_id": 701, "command_name": "whoami",
                    "timestamp": "2026-10-01T00:00:00Z",
                    "callback": {"display_id": 7}}}]}),
        additional_kwargs={"_task_output_collection_request_id": request_id})
    return call, tool


def test_report_store_dedup_revisions_withdrawal_and_scope(tmp_path):
    async def run():
        store = OperationMemoryStore(tmp_path / "sage.db")
        first = _record()
        assert await admit_observations(store, 7, UUID_A, channel_id=4,
            request_id="chat:4:request:1", checkpoint_thread="4:generation:a",
            records=[first]) == 1
        repeat = {**first, "request_id": "chat:5:request:2", "origin": "prior_sage_request"}
        assert await admit_observations(store, 7, UUID_A, channel_id=5,
            request_id="chat:5:request:2", checkpoint_thread="5:generation:b",
            records=[repeat]) == 1
        rows, omitted, cap = await retained_observations(store, 7, UUID_A)
        assert len(rows) == 1 and omitted == 0 and not cap
        assert len(rows[0]["collection_witnesses"]) == 2
        assert rows[0]["response_sources"][0]["response_id"] == 1701
        assert (await retained_observations(store, 7, UUID_B))[0] == []
        assert (await retained_observations(store, 8, UUID_A))[0] == []
        changed = {**first, "request_id": "chat:6:request:3",
                   "response_text_excerpts": ["LAB\\other"]}
        await admit_observations(store, 7, UUID_A, channel_id=6,
            request_id="chat:6:request:3", checkpoint_thread="6:generation:c",
            records=[changed])
        rows, _, _ = await retained_observations(store, 7, UUID_A)
        assert rows[0]["source_metadata_conflict"] and rows[0]["revision_ambiguity"]
        source_hash = rows[0]["source_revision_sha256"]
        assert await withdraw_source(store, 7, UUID_A, 701, source_hash) == 3
        assert (await retained_observations(store, 7, UUID_A))[0] == []
        assert await admit_observations(store, 7, UUID_A, channel_id=5,
            request_id="chat:5:request:4", checkpoint_thread="5:generation:b",
            records=[{**repeat, "request_id": "chat:5:request:4"}]) == 0
        await store.wipe_operation(7)
        assert (await retained_observations(store, 7, UUID_A))[0] == []
        await store.close()
    asyncio.run(run())


def test_completed_collection_cross_chat_restart_and_marker_drift(tmp_path, monkeypatch):
    async def run():
        import sage_chat.report_identity as identity
        import ai.langgraph.mythic_tools as mythic_tools
        marker = [UUID_A]

        async def canonical(client, operation_id, *, create=False):
            assert client is not None and operation_id == 7
            return marker[0]

        async def login(self):
            self.client = object()

        monkeypatch.setattr(identity, "canonical_engagement_uuid", canonical)
        monkeypatch.setattr(mythic_tools.MythicTools, "login", login)
        call, tool = _paired()
        model = Model.__new__(Model)
        model.channel_id = 4
        model.operation_id = 7
        model.mode = "conversation"
        model._thread_id_override = "4:generation:a"
        model._report_engagement_at_turn = UUID_A
        model._report_engagement_uuid = UUID_A
        model.mythic_client = SimpleNamespace(client=object())
        model.graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(
            values={"mythic_operator_messages": [call, tool],
                    "supervisor_messages": [AIMessage(content="FORGED NARRATIVE")]})))
        model._graph_run_config = lambda thread: {"configurable": {"thread_id": thread}}
        model._session_thread_id = lambda: "4:generation:a"
        chat_a = HeadlessSageChat()
        chat_a._operation_memory_runtime = OperationMemoryRuntime(tmp_path / "sage.db")
        request_a = build_chat_request("", operation_id=7, channel_id=4, request_id=1)
        await chat_a._retain_report_collection(request_a, model)
        assert model.graph.aget_state.await_count == 1
        await chat_a._operation_memory_runtime.store.close()

        chat_b = HeadlessSageChat()
        chat_b._operation_memory_runtime = OperationMemoryRuntime(tmp_path / "sage.db")
        request_b = build_chat_request("", operation_id=7, channel_id=5, request_id=2)
        report = await chat_b.handle_identity_report(request_b, None)
        assert "LAB&#92;analyst" in report and "channel 4" in report
        assert "request chat:4:request:1" in report and "response IDs 1701" in report
        assert "Authentication or access to a target | unknown" in report
        marker[0] = UUID_B
        assert "unavailable" in await chat_b.handle_identity_report(request_b, model)
        assert "LAB&#92;analyst" not in await chat_b.handle_identity_report(request_b, None)
        await chat_b._operation_memory_runtime.store.close()
    asyncio.run(run())


def test_collection_rejects_untagged_unpaired_and_failed_tool():
    call, tool = _paired()
    model = Model.__new__(Model)
    model.channel_id = 4
    model.operation_id = 7
    model.mode = "conversation"
    assert len(model.native_report_collection({"mythic_operator_messages": [call, tool]},
                                              "chat:4:request:1")) == 1
    tool.additional_kwargs.clear()
    assert model.native_report_collection({"mythic_operator_messages": [call, tool]},
                                          "chat:4:request:1") == []
    tool.additional_kwargs["_task_output_collection_request_id"] = "chat:4:request:1"
    tool.status = "error"
    assert model.native_report_collection({"mythic_operator_messages": [call, tool]},
                                          "chat:4:request:1") == []


def test_collection_rejects_cross_operation_and_missing_task_operation():
    call, tool = _paired()
    model = Model.__new__(Model)
    model.channel_id = 4
    model.operation_id = 7
    model.mode = "conversation"
    payload = json.loads(tool.content)
    payload["task_output"][0]["task"]["operation_id"] = 8
    tool.content = json.dumps(payload)
    assert model.native_report_collection({"mythic_operator_messages": [call, tool]},
                                          "chat:4:request:1") == []
    del payload["task_output"][0]["task"]["operation_id"]
    tool.content = json.dumps(payload)
    assert model.native_report_collection({"mythic_operator_messages": [call, tool]},
                                          "chat:4:request:1") == []


@pytest.mark.parametrize("argument", ["", "identity"])
def test_actual_task_producer_callback_checkpoint_to_native_report(tmp_path, monkeypatch, argument):
    async def run():
        import sage_chat.report_identity as identity
        import ai.langgraph.mythic_tools as tools_module
        request_id = "chat:4:request:1"
        query_count = []

        async def upstream(*args, **kwargs):
            query_count.append(kwargs.get("task_display_id"))
            return [{"id": 1701, "timestamp": "2026-10-01T00:00:01Z",
                "response_text": base64.b64encode(b"LAB\\analyst").decode(),
                "task": {"id": 901, "operation_id": 7, "display_id": 701, "agent_task_id": "agent-task",
                    "command_name": "whoami", "timestamp": "2026-10-01T00:00:00Z",
                    "callback": {"display_id": 7}}}]

        async def canonical(client, operation_id, *, create=False):
            assert client is not None and operation_id == 7
            return UUID_A

        async def login(self):
            self.client = object()

        monkeypatch.setattr(tools_module.mythic, "get_all_task_output_by_id", upstream)
        monkeypatch.setattr(identity, "canonical_engagement_uuid", canonical)
        monkeypatch.setattr(MythicTools, "login", login)
        tools = MythicTools(operation_id=7, channel_id=4, preauth_client=object())
        await tools.login()
        tools._request_contract = SimpleNamespace(request_id=request_id)
        tools._is_task_completed = AsyncMock(return_value=False)
        result = await tools.get_all_task_output_by_task_id(701)
        assert query_count == [701]
        assert json.loads(result)["task_output"][0]["response_text"] == "LAB\\analyst"
        callback = MessageCaptureCallback("Mythic_Operator", observation_request_id=request_id)
        callback._tool_call_to_name["read-701"] = "get_all_task_output_by_task_id"
        call = AIMessage(content="CLAIM: access proven", tool_calls=[{
            "name": "get_all_task_output_by_task_id", "args": {"task_id": 701},
            "id": "read-701", "type": "tool_call"}])
        await callback.on_tool_end(ToolMessage(name="get_all_task_output_by_task_id",
            tool_call_id="read-701", content=result), run_id=uuid4())
        tagged = callback.captured_messages[0]
        assert tagged.additional_kwargs["_task_output_collection_request_id"] == request_id

        class State(TypedDict):
            mythic_operator_messages: Annotated[list, operator.add]

        builder = StateGraph(State)
        builder.add_node("collect", lambda _: {"mythic_operator_messages": [call, tagged]})
        builder.add_edge(START, "collect")
        builder.add_edge("collect", END)
        connection = aiosqlite.connect(tmp_path / "sage.db")
        saver = AsyncSqliteSaver(connection)
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "4:generation:a"}}
        try:
            await graph.ainvoke({"mythic_operator_messages": []}, config)
            model = Model.__new__(Model)
            model.channel_id = 4
            model.operation_id = 7
            model.mode = "conversation"
            model._thread_id_override = "4:generation:a"
            model._report_engagement_at_turn = UUID_A
            model._report_engagement_uuid = UUID_A
            model.mythic_client = SimpleNamespace(client=object())
            model.graph = graph
            model._graph_run_config = lambda thread: {"configurable": {"thread_id": thread}}
            model._session_thread_id = lambda: "4:generation:a"
            chat_a = HeadlessSageChat()
            chat_a._operation_memory_runtime = OperationMemoryRuntime(tmp_path / "sage.db")
            request_a = build_chat_request("", operation_id=7, channel_id=4, request_id=1)
            await chat_a._retain_report_collection(request_a, model)
            await chat_a._operation_memory_runtime.store.close()
            chat_b = HeadlessSageChat()
            chat_b._operation_memory_runtime = OperationMemoryRuntime(tmp_path / "sage.db")
            request_b = build_chat_request("", operation_id=7, channel_id=5, request_id=2)
            request_b.SlashCommand = ChatSlashCommandInvocation(name="report", argument=argument)
            await chat_b.chat(request_b)
            assert len(chat_b.terminal_emissions) == 1
            report = chat_b.terminal_emissions[0]["content"]
            assert "LAB&#92;analyst" in report
            assert "channel 4" in report and "response IDs 1701" in report
            assert "Authentication or access to a target | unknown" in report
            assert query_count == [701]
            await chat_b._operation_memory_runtime.store.close()
        finally:
            await saver.conn.close()
    asyncio.run(run())


def test_marker_read_is_exact_authenticated_and_concurrent_creation_converges(monkeypatch):
    async def run():
        import sage_chat.report_identity as identity
        rows = []
        seen_queries = []
        async def execute(client, query, variables=None):
            seen_queries.append((query, variables))
            return {"whoami": {"status": "success", "active": True, "deleted": False,
                    "current_operation_id": client["operation_id"]},
                    "operationeventlog": [{"message":
                        "Sage durable engagement ledger id: " + rows[0]}] if rows else []}
        async def create(client, operation_id):
            rows.append(UUID_A if not rows else UUID_B)
            return rows[-1]
        monkeypatch.setattr(identity.mythic, "execute_custom_query", execute)
        monkeypatch.setattr(identity, "get_or_create_engagement_uuid", create)
        same_op = {"operation_id": 7}
        values = await asyncio.gather(
            canonical_engagement_uuid(same_op, 7, create=True),
            canonical_engagement_uuid(same_op, 7, create=True))
        assert values == [UUID_A, UUID_A]
        assert await canonical_engagement_uuid({"operation_id": 8}, 7) is None
        assert all(item[1] == {"op": 7} for item in seen_queries)
        assert all("whoami" in item[0] and "operationeventlog" in item[0]
                   for item in seen_queries)
    asyncio.run(run())
