import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "skills/sage-eval-gauge/scripts/native_inventory.py"
SPEC = importlib.util.spec_from_file_location("m3_native_inventory", SOURCE)
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def _row(table, identity):
    row = {field: None for field in inventory.TABLE_FIELDS[table]}
    row.update(id=identity, operation_id=1)
    return row


def _execute(tables):
    async def execute(_client, query, variables):
        table = next(name for name in inventory.TABLE_FIELDS if f"M3Inventory_{name}" in query)
        rows = tables.get(table, [])
        start = variables["offset"]
        selected = rows[start:start + variables["limit"]]
        return {table: selected, f"{table}_aggregate": {"aggregate": {"count": len(rows)}}}
    return execute


def _collect(tables, **changes):
    args = dict(expected_operation_id=1, boundary="after", boundary_epoch=2.0,
                start_epoch=1.0, end_epoch=3.0, quiescent=True, limit=2, max_pages=3)
    args.update(changes)
    return asyncio.run(inventory.collect_inventory(
        SimpleNamespace(current_operation_id=1), _execute(tables), **args,
    ))


def test_inventory_retains_operation_scoped_lineage_in_memory():
    callback = _row("callback", 10)
    task = _row("task", 20); task.update(callback_id=10)
    response = _row("response", 30); response.update(task_id=20, response_text="raw-output")
    prompt = _row("chat_message", 40); prompt.update(chat_request_id=None)
    request = _row("chat_request", 50); request.update(request_message_id=40)
    reply = _row("chat_message", 41); reply.update(chat_request_id=50)
    result = _collect({"callback": [callback], "task": [task], "response": [response],
                       "chat_request": [request], "chat_message": [prompt, reply]})
    assert result["complete"] is False
    assert result["snapshot_consistency"] == "unproven_paginated_queries"
    assert result["rows"]["response"][0]["response_text"] == "raw-output"


def test_inventory_refuses_scope_mismatch_before_query():
    called = []
    async def execute(*_args): called.append(True)
    with pytest.raises(inventory.InventoryError, match="differs"):
        asyncio.run(inventory.collect_inventory(
            SimpleNamespace(current_operation_id=2), execute, expected_operation_id=1,
            boundary="before", boundary_epoch=1, start_epoch=1, end_epoch=2, quiescent=True,
        ))
    assert called == []


def test_inventory_marks_pagination_gap_and_orphan_incomplete():
    tables = {"task": [_row("task", n) for n in (1, 2, 3)]}
    for row in tables["task"]: row["callback_id"] = 99
    result = _collect(tables, max_pages=1)
    assert result["complete"] is False
    assert any("incomplete" in reason or "orphan" in reason for reason in result["failures"])


def test_inventory_marks_missing_field_incomplete():
    result = _collect({"credential": [{"id": 1, "operation_id": 1}]})
    assert result["complete"] is False
    assert any("malformed" in reason for reason in result["failures"])


def test_inventory_rejects_nonfinite_window_and_flags_unfinished_boundaries():
    with pytest.raises(inventory.InventoryError, match="time boundary"):
        _collect({}, start_epoch=float("-inf"))
    callback = _row("callback", 1)
    task = _row("task", 2); task.update(callback_id=1, completed=False, parent_task_id=99)
    request_message = _row("chat_message", 3); request_message["chat_request_id"] = None
    request = _row("chat_request", 4); request.update(
        request_message_id=3, status="processing", completed_at=None, cancelled_at=None,
    )
    result = _collect({"callback": [callback], "task": [task], "chat_request": [request],
                       "chat_message": [request_message]})
    assert {"task:unfinished", "task:orphan_parent", "chat_request:nonterminal"}.issubset(
        result["failures"]
    )


def test_top_level_queries_keep_table_metadata_and_operation_scope():
    for table, fields in inventory.TABLE_FIELDS.items():
        query = inventory.QUERIES[table]
        assert f"query M3Inventory_{table}" in query
        assert "operation_id: {_eq: $operationId}" in query
        from graphql import parse
        selection = parse(query).definitions[0].selection_set.selections[1]
        observed = {
            (field.alias or field.name).value: field.name.value
            for field in selection.selection_set.selections
        }
        expected = {field: field for field in fields}
        if table == "credential":
            expected["credential"] = "credential_text"
        assert observed == expected


def test_snapshot_relationships_use_operation_scoped_names_with_singular_metadata_fixture():
    operation_relationship_fixture = {
        "callbacks": "callback", "responses": "response", "credentials": "credential",
        "chat_requests": "chat_request", "chat_messages": "chat_message",
    }
    assert {relationship: table for table, relationship in inventory.OPERATION_RELATIONSHIPS.items()} \
           == operation_relationship_fixture
    assert all(f"{relationship}_aggregate" in inventory.SNAPSHOT_QUERY
               for relationship in inventory.OPERATION_RELATIONSHIPS.values())


def test_single_operation_root_snapshot_is_complete_without_pagination():
    callback = _row("callback", 1); callback.update(
        tasks=[], tasks_aggregate={"aggregate": {"count": 0}},
    )
    root = {
        "id": 1, "callbacks": [callback], "callbacks_aggregate": {"aggregate": {"count": 1}},
    }
    for table in ("response", "credential", "chat_request", "chat_message"):
        relationship = inventory.OPERATION_RELATIONSHIPS[table]
        root[relationship] = []; root[f"{relationship}_aggregate"] = {"aggregate": {"count": 0}}
    async def execute(_client, query, variables):
        assert query == inventory.SNAPSHOT_QUERY
        assert variables["operationId"] == 1
        return {"operation": [root]}
    result = asyncio.run(inventory.collect_operation_snapshot(
        SimpleNamespace(current_operation_id=1), execute, expected_operation_id=1,
    ))
    assert result["complete"] is True
    assert result["snapshot_consistency"] == "single_operation_root_query"


@pytest.mark.parametrize("table", tuple(inventory.TABLE_FIELDS))
def test_snapshot_rejects_nonmapping_relationship_member(table):
    callback = _row("callback", 1); callback.update(
        tasks=[None] if table == "task" else [],
        tasks_aggregate={"aggregate": {"count": 1 if table == "task" else 0}},
    )
    root = {"id": 1, "callbacks": [None] if table == "callback" else [callback],
            "callbacks_aggregate": {"aggregate": {"count": 1}}}
    for member in ("response", "credential", "chat_request", "chat_message"):
        relationship = inventory.OPERATION_RELATIONSHIPS[member]
        root[relationship] = [None] if member == table else []
        root[f"{relationship}_aggregate"] = {"aggregate": {"count": 1 if member == table else 0}}
    async def execute(*_args): return {"operation": [root]}
    result = asyncio.run(inventory.collect_operation_snapshot(
        SimpleNamespace(current_operation_id=1), execute, expected_operation_id=1,
    ))
    assert result["complete"] is False
    assert f"{table}:malformed_relationship_member" in result["failures"]


def test_configured_snapshot_seam_selects_one_operation_query(monkeypatch):
    from ai import resource_budget, resource_clients
    installs, queries = [], []
    monkeypatch.setattr(resource_budget, "configured_ledger", lambda: object())
    monkeypatch.setattr(resource_clients, "install", lambda: installs.append(True))
    callback = _row("callback", 1); callback.update(tasks=[], tasks_aggregate={"aggregate": {"count": 0}})
    root = {"id": 1, "callbacks": [callback], "callbacks_aggregate": {"aggregate": {"count": 1}}}
    for table in ("response", "credential", "chat_request", "chat_message"):
        relationship = inventory.OPERATION_RELATIONSHIPS[table]
        root[relationship] = []; root[f"{relationship}_aggregate"] = {"aggregate": {"count": 0}}
    async def login(): return SimpleNamespace(current_operation_id=1)
    async def execute(_client, query, _variables): queries.append(query); return {"operation": [root]}
    result = asyncio.run(inventory.collect_snapshot_with_configured_client(
        login, execute, expected_operation_id=1,
    ))
    assert result["complete"] is True and installs == [True]
    assert queries == [inventory.SNAPSHOT_QUERY]
