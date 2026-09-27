#!/usr/bin/env python3
"""In-memory Mythic inventory producer for bounded evaluator captures."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Any


LIMIT_DEFAULT = 500
LIMIT_MAX = 1000
PAGES_MAX = 128
SNAPSHOT_CONSISTENCY = "unproven_paginated_queries"
SNAPSHOT_GAP = "configured operation relationship map lacks operation.tasks for one-root six-array query"
TABLE_FIELDS = {
    "task": (
        "id", "operation_id", "display_id", "callback_id", "command_name", "original_params",
        "display_params", "completed", "status", "status_timestamp_preprocessing",
        "status_timestamp_submitted", "status_timestamp_processing", "status_timestamp_processed",
        "timestamp", "parent_task_id", "opsec_pre_blocked", "opsec_pre_bypassed",
        "opsec_post_blocked", "opsec_post_bypassed", "tasking_location", "response_count",
    ),
    "response": ("id", "operation_id", "task_id", "response_text", "timestamp", "sequence_number"),
    "credential": (
        "id", "operation_id", "task_id", "type", "subtype", "account", "realm", "credential",
        "timestamp", "deleted", "metadata", "credential_identity",
    ),
    "callback": (
        "id", "operation_id", "display_id", "init_callback", "last_checkin", "timestamp", "active",
        "dead", "user", "host", "domain", "integrity_level", "pid", "process_name", "os",
        "architecture", "sleep_info",
    ),
    "chat_request": (
        "id", "operation_id", "channel_id", "request_message_id", "chat_container_id", "model",
        "status", "error", "context_snapshot", "retry_of_id", "created_at", "updated_at",
        "completed_at", "cancelled_at",
    ),
    "chat_message": (
        "id", "operation_id", "channel_id", "chat_request_id", "chat_response_key", "author_type",
        "sender_display_name", "message", "tool_output", "edited", "deleted", "status", "metadata",
        "created_at", "updated_at",
    ),
}
OPERATION_RELATIONSHIPS = {
    "callback": "callbacks",
    "response": "responses",
    "credential": "credentials",
    "chat_request": "chat_requests",
    "chat_message": "chat_messages",
}


def _field_selection(table: str, fields: tuple[str, ...]) -> str:
    # Mythic exposes the decoded value as a computed GraphQL field. Keep the
    # evaluator's in-memory key stable without querying a nonexistent column.
    return " ".join(
        "credential: credential_text" if table == "credential" and field == "credential" else field
        for field in fields
    )


def _query(table: str, fields: tuple[str, ...]) -> str:
    return (
        f"query M3Inventory_{table}($operationId: Int!, $limit: Int!, $offset: Int!) {{\n"
        f"  {table}_aggregate(where: {{operation_id: {{_eq: $operationId}}}}) {{ aggregate {{ count }} }}\n"
        f"  {table}(where: {{operation_id: {{_eq: $operationId}}}}, order_by: {{id: asc}}, "
        f"limit: $limit, offset: $offset) {{ {_field_selection(table, fields)} }}\n}}"
    )


QUERIES = {table: _query(table, fields) for table, fields in TABLE_FIELDS.items()}
QUERY_HASHES = {
    table: hashlib.sha256(query.encode("utf-8")).hexdigest() for table, query in QUERIES.items()
}

SNAPSHOT_CAPS = {
    "callback": 1024, "task": 129, "response": 8192,
    "credential": 1024, "chat_request": 16, "chat_message": 4096,
}


def _snapshot_query() -> str:
    callback_fields = " ".join(TABLE_FIELDS["callback"])
    task_fields = " ".join(TABLE_FIELDS["task"])
    relationships = []
    for table in ("response", "credential", "chat_request", "chat_message"):
        relationship = OPERATION_RELATIONSHIPS[table]
        relationships.append(
            f"{relationship}_aggregate {{ aggregate {{ count }} }} "
            f"{relationship}(order_by: {{id: asc}}, limit: ${table}Cap) {{ {_field_selection(table, TABLE_FIELDS[table])} }}"
        )
    return (
        "query M3OperationSnapshot($operationId: Int!, $callbackCap: Int!, $taskCap: Int!, "
        "$responseCap: Int!, $credentialCap: Int!, $chat_requestCap: Int!, $chat_messageCap: Int!) { "
        "operation(where: {id: {_eq: $operationId}}, limit: 2) { id "
        "callbacks_aggregate { aggregate { count } } "
        f"callbacks(order_by: {{id: asc}}, limit: $callbackCap) {{ {callback_fields} "
        f"tasks_aggregate {{ aggregate {{ count }} }} tasks(order_by: {{id: asc}}, limit: $taskCap) {{ {task_fields} }} }} "
        + " ".join(relationships) + " } }"
    )


SNAPSHOT_QUERY = _snapshot_query()
SNAPSHOT_QUERY_SHA256 = hashlib.sha256(SNAPSHOT_QUERY.encode("utf-8")).hexdigest()


class InventoryError(RuntimeError):
    pass


def _lineage_and_terminal_failures(inventory: Mapping[str, list[dict[str, Any]]]) -> list[str]:
    failures = []
    task_ids = {row["id"] for row in inventory.get("task", [])}
    callback_ids = {row["id"] for row in inventory.get("callback", [])}
    request_ids = {row["id"] for row in inventory.get("chat_request", [])}
    message_ids = {row["id"] for row in inventory.get("chat_message", [])}
    if any(row["task_id"] not in task_ids for row in inventory.get("response", [])):
        failures.append("response:orphan_task")
    if any(row["task_id"] is not None and row["task_id"] not in task_ids for row in inventory.get("credential", [])):
        failures.append("credential:orphan_task")
    if any(row["parent_task_id"] is not None and row["parent_task_id"] not in task_ids for row in inventory.get("task", [])):
        failures.append("task:orphan_parent")
    if any(row["callback_id"] not in callback_ids for row in inventory.get("task", [])):
        failures.append("task:orphan_callback")
    if any(row["request_message_id"] not in message_ids for row in inventory.get("chat_request", [])):
        failures.append("chat_request:orphan_message")
    if any(row["chat_request_id"] is not None and row["chat_request_id"] not in request_ids for row in inventory.get("chat_message", [])):
        failures.append("chat_message:orphan_request")
    if any(row["retry_of_id"] is not None and row["retry_of_id"] not in request_ids for row in inventory.get("chat_request", [])):
        failures.append("chat_request:orphan_retry")
    if any(row["completed"] is not True for row in inventory.get("task", [])):
        failures.append("task:unfinished")
    for row in inventory.get("chat_request", []):
        status = str(row.get("status") or "").casefold()
        terminal_time = row.get("cancelled_at") if status == "cancelled" else row.get("completed_at") if status == "complete" else row.get("error") if status == "error" else None
        if status not in {"complete", "cancelled", "error"} or not terminal_time:
            failures.append("chat_request:nonterminal"); break
    return failures


async def collect_operation_snapshot(client: Any, execute_query: Callable, *, expected_operation_id: int) -> dict[str, Any]:
    """One operation-root GraphQL snapshot with nested callback tasks and cap detection."""
    if type(expected_operation_id) is not int or expected_operation_id <= 0 \
            or getattr(client, "current_operation_id", None) != expected_operation_id:
        raise InventoryError("authenticated Mythic operation differs from expected scope")
    variables = {"operationId": expected_operation_id, **{
        f"{table}Cap": cap for table, cap in SNAPSHOT_CAPS.items()
    }}
    result = await execute_query(client, SNAPSHOT_QUERY, variables)
    roots = result.get("operation") if isinstance(result, Mapping) else None
    if not isinstance(roots, list) or len(roots) != 1 or not isinstance(roots[0], Mapping) \
            or roots[0].get("id") != expected_operation_id:
        raise InventoryError("operation-root snapshot is unavailable or ambiguous")
    root = roots[0]; rows = {table: [] for table in TABLE_FIELDS}; failures = []
    for table in ("callback", "response", "credential", "chat_request", "chat_message"):
        relationship = OPERATION_RELATIONSHIPS[table]
        aggregate_name = relationship + "_aggregate"
        aggregate = root.get(aggregate_name)
        count = (aggregate.get("aggregate") or {}).get("count") if isinstance(aggregate, Mapping) else None
        values = root.get(relationship)
        if type(count) is not int or count < 0 or not isinstance(values, list) or count != len(values) \
                or count > SNAPSHOT_CAPS[table]:
            failures.append(f"{table}:truncated_or_missing_count"); values = values if isinstance(values, list) else []
        if any(not isinstance(value, Mapping) for value in values):
            failures.append(f"{table}:malformed_relationship_member")
            values = []
        rows[table] = [dict(value) for value in values]
    flattened = []
    for callback in rows["callback"]:
        aggregate = callback.get("tasks_aggregate")
        count = (aggregate.get("aggregate") or {}).get("count") if isinstance(aggregate, Mapping) else None
        tasks = callback.get("tasks")
        if type(count) is not int or count < 0 or not isinstance(tasks, list) or count != len(tasks) \
                or count > SNAPSHOT_CAPS["task"]:
            failures.append("task:truncated_or_missing_count"); tasks = tasks if isinstance(tasks, list) else []
        if any(not isinstance(task, Mapping) for task in tasks):
            failures.append("task:malformed_relationship_member")
            tasks = []
        flattened.extend(dict(task) for task in tasks)
        callback.pop("tasks", None); callback.pop("tasks_aggregate", None)
    rows["task"] = flattened
    if len(flattened) > 128:
        failures.append("task:operation_total_cap_exceeded")
    for table, values in rows.items():
        ids = set()
        for row in values:
            if any(field not in row for field in TABLE_FIELDS[table]) or row.get("operation_id") != expected_operation_id \
                    or type(row.get("id")) is not int or row["id"] <= 0 or row["id"] in ids:
                failures.append(f"{table}:malformed_duplicate_or_wrong_scope"); break
            ids.add(row["id"])
    failures.extend(_lineage_and_terminal_failures(rows))
    return {"schema": "sage-m3-native-inventory-v1", "operation_id": expected_operation_id,
            "complete": not failures, "failures": failures, "snapshot_consistency": "single_operation_root_query",
            "query_hashes": {"operation_snapshot": SNAPSHOT_QUERY_SHA256}, "rows": rows}


async def collect_with_configured_client(
    login: Callable[[], Awaitable[Any]],
    execute_query: Callable[[Any, str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    **kwargs: Any,
) -> dict[str, Any]:
    """Live driver seam: install actual HTTP meters, then use the caller-approved configured login."""
    from ai import resource_budget, resource_clients
    if resource_budget.configured_ledger() is None:
        raise InventoryError("shared resource ledger is required before Mythic login")
    resource_clients.install()
    client = await login()
    return await collect_inventory(client, execute_query, **kwargs)


async def collect_snapshot_with_configured_client(
    login: Callable[[], Awaitable[Any]],
    execute_query: Callable[[Any, str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    *,
    expected_operation_id: int,
) -> dict[str, Any]:
    """Explicit single-statement producer seam for the future capture driver."""
    from ai import resource_budget, resource_clients
    if resource_budget.configured_ledger() is None:
        raise InventoryError("shared resource ledger is required before Mythic login")
    resource_clients.install()
    client = await login()
    return await collect_operation_snapshot(
        client, execute_query, expected_operation_id=expected_operation_id,
    )


async def collect_inventory(
    client: Any,
    execute_query: Callable[[Any, str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    *,
    expected_operation_id: int,
    boundary: str,
    boundary_epoch: float,
    start_epoch: float,
    end_epoch: float,
    quiescent: bool,
    limit: int = LIMIT_DEFAULT,
    max_pages: int = PAGES_MAX,
) -> dict[str, Any]:
    """Collect exact operation-owned rows; raw values remain in the returned memory object."""
    if type(expected_operation_id) is not int or expected_operation_id <= 0:
        raise InventoryError("expected operation ID must be positive")
    if boundary not in {"before", "after"} or type(quiescent) is not bool or quiescent is not True:
        raise InventoryError("inventory requires a named quiescent boundary")
    if any(type(value) not in (int, float) or not math.isfinite(value)
           for value in (start_epoch, boundary_epoch, end_epoch)) \
            or not start_epoch <= boundary_epoch <= end_epoch:
        raise InventoryError("inventory time boundary is invalid")
    if type(limit) is not int or not 1 <= limit <= LIMIT_MAX \
            or type(max_pages) is not int or not 1 <= max_pages <= PAGES_MAX:
        raise InventoryError("inventory pagination bounds are invalid")
    if getattr(client, "current_operation_id", None) != expected_operation_id:
        raise InventoryError("authenticated Mythic operation differs from expected scope")

    inventory: dict[str, list[dict[str, Any]]] = {}
    failures: list[str] = []
    for table, fields in TABLE_FIELDS.items():
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        declared_count: int | None = None
        for page in range(max_pages):
            try:
                result = await execute_query(client, QUERIES[table], {
                    "operationId": expected_operation_id, "limit": limit, "offset": page * limit,
                })
            except Exception:
                failures.append(f"{table}:query_error")
                break
            aggregate = result.get(f"{table}_aggregate") if isinstance(result, Mapping) else None
            aggregate = aggregate.get("aggregate") if isinstance(aggregate, Mapping) else None
            count = aggregate.get("count") if isinstance(aggregate, Mapping) else None
            page_rows = result.get(table) if isinstance(result, Mapping) else None
            if type(count) is not int or count < 0 or not isinstance(page_rows, list):
                failures.append(f"{table}:missing_count_or_rows")
                break
            if declared_count is None:
                declared_count = count
            elif count != declared_count:
                failures.append(f"{table}:changed_count")
                break
            malformed = False
            for row in page_rows:
                if not isinstance(row, dict) or any(field not in row for field in fields):
                    malformed = True
                    break
                row_id = row.get("id")
                if type(row_id) is not int or row_id <= 0 or row_id in seen \
                        or row.get("operation_id") != expected_operation_id:
                    malformed = True
                    break
                seen.add(row_id)
                rows.append(dict(row))
            if malformed:
                failures.append(f"{table}:malformed_duplicate_or_wrong_scope")
                break
            if len(rows) == declared_count:
                break
            if not page_rows or len(rows) > declared_count:
                failures.append(f"{table}:pagination_gap")
                break
        if declared_count is not None and len(rows) != declared_count:
            failures.append(f"{table}:incomplete_{len(rows)}_of_{declared_count}")
        inventory[table] = rows

    task_ids = {row["id"] for row in inventory.get("task", [])}
    callback_ids = {row["id"] for row in inventory.get("callback", [])}
    request_ids = {row["id"] for row in inventory.get("chat_request", [])}
    message_ids = {row["id"] for row in inventory.get("chat_message", [])}
    if any(row["task_id"] not in task_ids for row in inventory.get("response", [])):
        failures.append("response:orphan_task")
    if any(row["task_id"] is not None and row["task_id"] not in task_ids
           for row in inventory.get("credential", [])):
        failures.append("credential:orphan_task")
    if any(row["parent_task_id"] is not None and row["parent_task_id"] not in task_ids
           for row in inventory.get("task", [])):
        failures.append("task:orphan_parent")
    if any(row["callback_id"] not in callback_ids for row in inventory.get("task", [])):
        failures.append("task:orphan_callback")
    if any(row["request_message_id"] not in message_ids for row in inventory.get("chat_request", [])):
        failures.append("chat_request:orphan_message")
    if any(row["chat_request_id"] is not None and row["chat_request_id"] not in request_ids
           for row in inventory.get("chat_message", [])):
        failures.append("chat_message:orphan_request")
    if any(row["retry_of_id"] is not None and row["retry_of_id"] not in request_ids
           for row in inventory.get("chat_request", [])):
        failures.append("chat_request:orphan_retry")
    if any(row["completed"] is not True for row in inventory.get("task", [])):
        failures.append("task:unfinished")
    for row in inventory.get("chat_request", []):
        status = str(row.get("status") or "").casefold()
        terminal_time = (
            row.get("cancelled_at") if status == "cancelled"
            else row.get("completed_at") if status == "complete"
            else row.get("error") if status == "error"
            else None
        )
        if status not in {"complete", "cancelled", "error"} or not terminal_time:
            failures.append("chat_request:nonterminal")
            break
    # The configured local relationship map has no operation.tasks relationship.
    # Separate top-level/paginated queries therefore cannot prove one database snapshot,
    # regardless of matching aggregate counts or a caller assertion of quiescence.
    failures.append("snapshot_consistency_unproven")
    return {
        "schema": "sage-m3-native-inventory-v1",
        "operation_id": expected_operation_id,
        "boundary": boundary,
        "boundary_epoch": boundary_epoch,
        "window": {"start_epoch": start_epoch, "end_epoch": end_epoch},
        "quiescent": False,
        "caller_quiescent_assertion": quiescent,
        "snapshot_consistency": SNAPSHOT_CONSISTENCY,
        "snapshot_gap": SNAPSHOT_GAP,
        "complete": not failures,
        "failures": failures,
        "query_hashes": QUERY_HASHES,
        "rows": inventory,
    }


def public_receipt(snapshot: Mapping[str, Any], redactor: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> dict:
    if not callable(redactor):
        raise InventoryError("an explicit public redactor is required")
    metadata = redactor(snapshot)
    if not isinstance(metadata, Mapping):
        raise InventoryError("redactor did not return metadata")
    return {
        "schema": "sage-m3-native-inventory-receipt-v1",
        "inventory_sha256": hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest(),
        "metadata": dict(metadata),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    print("DRY RUN: import collect_inventory and inject an authorized PyMythic client/query function.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
