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


GRAPH_MAX_RECORDS = 12
GRAPH_EXCERPT_CHARS = 1500
GRAPH_TOTAL_CHARS = 12_000
_GRAPH_ARGUMENT_CHARS = 600
_CALL_ID_LIMIT = 128


def graph_citation(request_id: str, tool_call_id: str) -> str:
    """The stable token a worker cites for one graph observation; resolved by exact lookup, never parsed."""
    return f"graph:{request_id}:{tool_call_id}"


def graph_citation_suffix(token: str) -> str:
    """The line Sage appends to a BloodHound worker's own graph result so the worker can cite it."""
    return f"\n\n[Sage source {token}]"


def _without_citation_suffix(text: str) -> str:
    """The tool's own output: a trailing Sage citation line is not part of the observation."""
    marker = text.rfind("\n\n[Sage source graph:")
    if marker >= 0 and text.endswith("]") and "\n" not in text[marker + 2:]:
        return text[:marker]
    return text


def graph_observation_records(
    messages: Iterable[Any], graph_tools: Iterable[str], *, request_id: str, generation: str = "",
    is_error: Any = None, max_records: int = GRAPH_MAX_RECORDS, total_chars: int = GRAPH_TOTAL_CHARS,
    excerpt_chars: int = GRAPH_EXCERPT_CHARS,
) -> list[dict[str, Any]]:
    """The graph queries a step made, each with its paired result, in call order. Pure.

    A record is usable support only when one allowlisted call and one result share its call ID and tool name.
    Ambiguous pairs stay visible without an excerpt or citation; a result with no allowlisted call, or a call with
    no result, produces nothing. An error result (`ToolMessage.status` or the structured `is_error` detector) is an
    observation of an error. The most recent records are selected first, within the record and excerpt budgets;
    the digest always covers the full result.
    """
    allowed = frozenset(graph_tools)
    messages = list(messages)
    calls: dict[str, list[dict[str, Any]]] = {}
    results: dict[str, list[Any]] = {}
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            call_id = call.get("id")
            if call.get("name") in allowed and isinstance(call_id, str) and 0 < len(call_id) <= _CALL_ID_LIMIT:
                calls.setdefault(call_id, []).append(call)
        if getattr(message, "type", "") == "tool" and getattr(message, "tool_call_id", None) in calls:
            results.setdefault(message.tool_call_id, []).append(message)
    paired = [call_id for call_id in calls if call_id in results]
    records: list[dict[str, Any]] = []
    remaining = total_chars
    for call_id in reversed(paired):
        if len(records) >= max_records:
            break
        call, result = calls[call_id][0], results[call_id][0]
        arguments = json.dumps(call.get("args") or {}, ensure_ascii=False, sort_keys=True, default=str)
        content = result.content
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        text = _without_citation_suffix(text)
        pairing = ("duplicate_call" if len(calls[call_id]) > 1 else
                   "duplicate_result" if len(results[call_id]) > 1 else
                   "tool_name_mismatch" if getattr(result, "name", None) not in {None, call["name"]} else "paired")
        error = getattr(result, "status", None) == "error" or bool(callable(is_error) and is_error(text))
        record: dict[str, Any] = {
            "request_id": request_id,
            "source_tool": call["name"],
            "source_tool_call_id": call_id,
            "arguments": arguments[:_GRAPH_ARGUMENT_CHARS],
            "arguments_sha256": hashlib.sha256(arguments.encode("utf-8")).hexdigest(),
            "arguments_truncated": len(arguments) > _GRAPH_ARGUMENT_CHARS,
            "result_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "disposition": "error" if error else "success",
            "pairing": pairing,
            "usable": pairing == "paired",
        }
        if generation:
            record["generation"] = generation
        if pairing == "paired":
            excerpt = text[:max(0, min(excerpt_chars, remaining))]
            remaining -= len(excerpt)
            record.update({"citation": graph_citation(request_id, call_id), "result_excerpt": excerpt,
                           "result_truncated": len(excerpt) < len(text)})
        record["truncated"] = bool(record.get("result_truncated") or record["arguments_truncated"]
                                   or pairing != "paired")
        records.append(record)
    records.reverse()
    return records


def graph_read_references(messages: Iterable[Any], graph_tools: Iterable[str], *,
                          is_error: Any = None) -> list[dict[str, Any]]:
    """The graph tool calls a step made that have a paired result, from the shared graph record builder."""
    return [{"tool": record["source_tool"], "tool_call_id": record["source_tool_call_id"][:_TEXT_LIMIT],
             "args_sha256": record["arguments_sha256"], "result_sha256": record["result_sha256"],
             "disposition": record["disposition"], "pairing": record["pairing"]}
            for record in graph_observation_records(messages, graph_tools, request_id="-", is_error=is_error,
                                                    max_records=10**6, total_chars=0)]


async def _bounded(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, timeout=READ_TIMEOUT_SECONDS)


async def collect(client: Any, *, task_ids: Iterable[Any], credentials_added: Iterable[Any],
                  messages: Iterable[Any], graph_tools: Iterable[str],
                  graph_is_error: Any = None) -> tuple[dict[str, list], dict[int, list]]:
    """Read the step's references from Mythic. Returns the references and the output rows read for each task.

    A failed read keeps the reference with `read: failed`; nothing here raises to the caller.
    """
    references = empty()
    references["graph_reads"] = graph_read_references(messages, graph_tools, is_error=graph_is_error)
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
        state = {"error": ", returned an error"}.get(read.get("disposition"), "")
        if read.get("pairing") not in {None, "paired"}:
            state += f", ambiguous result pairing ({read['pairing']}); not usable support"
        lines.append(f"- graph read `{read['tool']}` (call {read['tool_call_id']}){state}.")
    if not lines:
        return []
    return ["References read by Sage from Mythic (the worker's summary is its interpretation of these):", *lines]
