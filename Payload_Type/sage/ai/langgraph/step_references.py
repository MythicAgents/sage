"""Typed references to what a supervised step did, read by Sage from Mythic when the step ends.

The Supervisor learns a step's results from these rather than from the worker's prose: each issued task's own
record (state, command, callback) with an output row count and digest, the credentials Mythic links to those tasks
or that the step added, and the graph reads the step made. References are lifecycle and observation data, never
proof of an effect, and a credential's secret never enters one.

The builders are pure. `collect` reads through MythicTools' internal readers, which never issue, re-issue or
cancel a task and never count as the worker's own reads.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

MAX_TASK_REFERENCES = 16
SAGE_EXCERPT_CHARS = 8000
READ_TIMEOUT_SECONDS = 5.0
_TEXT_LIMIT = 128


def empty() -> dict[str, list]:
    return {"tasks": [], "credentials": [], "graph_reads": []}


def _exact_id(value: Any) -> int | None:
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _short(value: Any) -> str:
    """A Mythic-supplied label made safe to place in a Sage-authored line: one line, bounded."""
    text = " ".join(str(value or "").split())
    return text[:_TEXT_LIMIT]


def task_reference(task_id: int, record: Mapping[str, Any] | None, rows: Any, *, read: str = "ok") -> dict[str, Any]:
    """One issued task as Mythic records it. `read` is `ok`, `failed` or `not_read`."""
    record = record or {}
    reference: dict[str, Any] = {
        "task_id": task_id,
        "callback_display_id": record.get("callback_display_id"),
        "command": _short(record.get("command")) or None,
        "status": _short(record.get("status")) or None,
        "completed": record.get("completed") if type(record.get("completed")) is bool else None,
        "output_rows": None,
        "output_sha256": None,
        "read": read,
    }
    if read != "ok":
        return reference
    if not isinstance(rows, list):
        reference["read"] = "failed"
        return reference
    texts = [row["response_text"] for row in rows if isinstance(row, dict) and isinstance(row.get("response_text"), str)]
    reference["output_rows"] = len(texts)
    reference["output_sha256"] = hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode("utf-8")).hexdigest()
    # Rows carry the task's own identity when the record query could not be read.
    for row in rows:
        task = row.get("task") if isinstance(row, dict) else None
        if not isinstance(task, dict):
            continue
        callback = task.get("callback")
        if reference["callback_display_id"] is None and isinstance(callback, dict):
            reference["callback_display_id"] = _exact_id(callback.get("display_id"))
        if reference["command"] is None and task.get("command_name"):
            reference["command"] = _short(task.get("command_name"))
        break
    return reference


def credential_references(rows: Any, *, task_ids: Iterable[int], added_ids: Iterable[int]) -> list[dict[str, Any]]:
    """Credentials linked to the step's tasks or added by the step. Identity only: the secret is never copied."""
    tasks, added = set(task_ids), set(added_ids)
    references = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            continue
        task = row.get("task")
        source_task = _exact_id(task.get("display_id")) if isinstance(task, dict) else None
        if row["id"] in added:
            source: dict[str, Any] = {"added_by": "add_credential"}
        elif source_task is not None and source_task in tasks:
            source = {"source_task_id": source_task}
        else:
            continue
        references.append({"credential_id": row["id"], "account": _short(row.get("account")),
                           "realm": _short(row.get("realm")), "type": _short(row.get("type")), **source})
    return sorted(references, key=lambda item: item["credential_id"])


def graph_read_references(messages: Iterable[Any], graph_tools: Iterable[str]) -> list[dict[str, Any]]:
    """The graph tool calls a step made that have a paired result."""
    allowed = frozenset(graph_tools)
    calls: dict[str, dict[str, Any]] = {}
    references = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            if call.get("name") in allowed and isinstance(call.get("id"), str):
                calls[call["id"]] = call
        if getattr(message, "type", "") == "tool" and getattr(message, "tool_call_id", None) in calls:
            call = calls.pop(message.tool_call_id)
            arguments = json.dumps(call.get("args") or {}, ensure_ascii=False, sort_keys=True, default=str)
            references.append({"tool": call["name"], "tool_call_id": call["id"][:_TEXT_LIMIT],
                               "args_sha256": hashlib.sha256(arguments.encode("utf-8")).hexdigest()})
    return references


async def _bounded(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, timeout=READ_TIMEOUT_SECONDS)


async def collect(client: Any, *, task_ids: Iterable[Any], credentials_added: Iterable[Any],
                  messages: Iterable[Any], graph_tools: Iterable[str]) -> tuple[dict[str, list], dict[int, list]]:
    """Read the step's references from Mythic. Returns the references and the output rows read for each task.

    A failed read keeps the reference with `read: failed`; nothing here raises to the caller.
    """
    references = empty()
    references["graph_reads"] = graph_read_references(messages, graph_tools)
    ids = sorted({i for i in (_exact_id(t) for t in task_ids) if i is not None})
    added = sorted({i for i in (_exact_id(c) for c in credentials_added) if i is not None})
    read_ids, unread_ids = ids[:MAX_TASK_REFERENCES], ids[MAX_TASK_REFERENCES:]
    rows_by_task: dict[int, list] = {}
    if read_ids:
        try:
            records = await _bounded(client._step_task_records(read_ids))
        except Exception as exc:
            logger.debug("step task records unreadable: %s", exc)
            records = {}
        records = records if isinstance(records, dict) else {}
        async def output(task_id: int) -> Any:
            return await _bounded(client._task_output_rows(task_id))

        outputs = await asyncio.gather(*(output(task_id) for task_id in read_ids), return_exceptions=True)
        for task_id, result in zip(read_ids, outputs):
            if isinstance(result, BaseException) or not isinstance(result, tuple):
                references["tasks"].append(task_reference(task_id, records.get(task_id), None, read="failed"))
                continue
            rows = result[0]
            reference = task_reference(task_id, records.get(task_id), rows)
            if reference["read"] == "ok":
                rows_by_task[task_id] = rows
            references["tasks"].append(reference)
    references["tasks"].extend(task_reference(task_id, None, None, read="not_read") for task_id in unread_ids)
    if ids or added:
        try:
            rows = await _bounded(client._fetch_credentials_cached(datetime.now(timezone.utc).isoformat(), force=True))
        except Exception as exc:
            logger.debug("credential store unreadable for step references: %s", exc)
            rows = []
        references["credentials"] = credential_references(rows, task_ids=ids, added_ids=added)
    return references, rows_by_task


def render_lines(references: Mapping[str, Any] | None) -> list[str]:
    """Lines the Supervisor reads, one per reference; the worker's prose is its interpretation of these."""
    if not isinstance(references, Mapping):
        return []
    lines = []
    for task in references.get("tasks") or []:
        where = f" on callback {task['callback_display_id']}" if task.get("callback_display_id") is not None else ""
        command = f" (`{task['command']}`)" if task.get("command") else ""
        if task.get("read") == "not_read":
            state = "not read (reference limit reached)"
        elif task.get("read") == "failed":
            state = "its Mythic record could not be read"
        else:
            completed = {True: "completed", False: "not completed"}.get(task.get("completed"), "completion unknown")
            status = f", status `{task['status']}`" if task.get("status") else ""
            state = f"{completed}{status}, {task.get('output_rows') or 0} output rows"
        excerpt = {"existing": "; output excerpt in the observations above",
                   "sage_read": "; output excerpt read by Sage in the observations above"}.get(task.get("excerpt"), "")
        if task.get("excerpt") == "omitted":
            excerpt = f"; no excerpt ({task.get('omitted_reason')})"
        lines.append(f"- task {task['task_id']}{where}{command}: {state}{excerpt}.")
    for credential in references.get("credentials") or []:
        principal = "\\".join(part for part in (credential.get("realm"), credential.get("account")) if part)
        source = (f"from task {credential['source_task_id']}" if "source_task_id" in credential
                  else "added with add_credential")
        lines.append(f"- credential {credential['credential_id']}: `{principal or '-'}` "
                     f"({credential.get('type') or 'unknown type'}), {source}; secret not shown.")
    for read in references.get("graph_reads") or []:
        lines.append(f"- graph read `{read['tool']}` (call {read['tool_call_id']}).")
    if not lines:
        return []
    return ["References read by Sage from Mythic (the worker's summary is its interpretation of these):", *lines]
