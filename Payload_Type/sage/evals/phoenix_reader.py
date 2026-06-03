"""Pure sqlite readers for Sage Phoenix trace data."""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote


TOOL_OUTPUTS_CHAR_CAP = 200_000


@dataclass(frozen=True)
class TraceSummary:
    """Span-count summary for one Phoenix trace."""

    rowid: int
    trace_id: str
    spans: int
    last_span: str | None


@dataclass(frozen=True)
class Metrics:
    """Aggregated token and error metrics for a set of traces."""

    tokens: int
    model_calls: int
    max_prompt: int
    error_count: int
    errors: list[dict[str, str]]
    recursion_deaths: int


@dataclass(frozen=True)
class TokenBreakdown:
    """Token split and attribution estimates for a set of traces."""

    prompt_tokens: int
    completion_tokens: int
    est_fixed_floor: int
    est_variable: int
    model_calls: int
    tool_calls: int
    per_agent_tokens: dict[str, int]


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    uri = f"file:{quote(str(path), safe='/:')}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _placeholders(values: list[int]) -> str:
    return ",".join("?" for _ in values)


def max_trace_rowid(db_path: str | Path) -> int:
    """Return the current maximum trace rowid, or zero when no traces exist."""

    with _connect(db_path) as conn:
        row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM traces").fetchone()
    if not row or row[0] is None:
        return 0
    return int(row[0])


def new_traces_since(db_path: str | Path, pre_rowid: int) -> list[int]:
    """Return trace rowids greater than the provided pre-run marker."""

    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT rowid FROM traces WHERE rowid > ? ORDER BY rowid",
            (pre_rowid,),
        ).fetchall()
    return [int(row[0]) for row in rows if row and row[0] is not None]


def trace_summaries_since(db_path: str | Path, pre_rowid: int) -> list[TraceSummary]:
    """Return span-count summaries for traces created after pre_rowid."""

    sql = """
        SELECT t.rowid, t.trace_id, COUNT(s.span_id) spans, MAX(s.start_time) last_span
        FROM traces t
        JOIN spans s ON s.trace_rowid = t.rowid
        WHERE t.rowid > ?
        GROUP BY t.rowid
        ORDER BY spans DESC
    """
    with _connect(db_path) as conn:
        rows = conn.execute(sql, (pre_rowid,)).fetchall()
    summaries: list[TraceSummary] = []
    for row in rows:
        if not row or row[0] is None:
            continue
        summaries.append(
            TraceSummary(
                rowid=int(row[0]),
                trace_id="" if row[1] is None else str(row[1]),
                spans=int(row[2] or 0),
                last_span=None if row[3] is None else str(row[3]),
            )
        )
    return summaries


def aggregate_metrics(db_path: str | Path, trace_rowids: list[int]) -> Metrics:
    """Aggregate token counts, model calls, and ERROR spans for trace rowids."""

    if not trace_rowids:
        return Metrics(0, 0, 0, 0, [], 0)

    ph = _placeholders(trace_rowids)
    with _connect(db_path) as conn:
        token_rows = conn.execute(
            f"""
            SELECT COALESCE(llm_token_count_prompt, 0),
                   COALESCE(llm_token_count_completion, 0)
            FROM spans
            WHERE trace_rowid IN ({ph})
            """,
            trace_rowids,
        ).fetchall()
        error_rows = conn.execute(
            f"""
            SELECT name, status_message
            FROM spans
            WHERE trace_rowid IN ({ph}) AND status_code = 'ERROR'
            """,
            trace_rowids,
        ).fetchall()

    total_tokens = 0
    model_calls = 0
    max_prompt = 0
    for prompt, completion in token_rows:
        prompt_tokens = int(prompt or 0)
        completion_tokens = int(completion or 0)
        total_tokens += prompt_tokens + completion_tokens
        if prompt_tokens > 0:
            model_calls += 1
        max_prompt = max(max_prompt, prompt_tokens)

    errors = [
        {"name": "" if row[0] is None else str(row[0]), "status_message": "" if row[1] is None else str(row[1])}
        for row in error_rows
    ]
    recursion_deaths = sum(1 for error in errors if "recursion" in error["status_message"].lower())
    return Metrics(total_tokens, model_calls, max_prompt, len(errors), errors, recursion_deaths)


def token_breakdown(db_path: str | Path, trace_rowids: list[int]) -> TokenBreakdown:
    """Estimate token floor, variable prompt tokens, tool calls, and root-agent totals."""

    if not trace_rowids:
        return TokenBreakdown(0, 0, 0, 0, 0, 0, {})

    ph = _placeholders(trace_rowids)
    with _connect(db_path) as conn:
        span_kind_expr = _span_kind_select_expr(conn)
        root_names = _root_names(conn, trace_rowids)
        rows = conn.execute(
            f"""
            SELECT trace_rowid,
                   name,
                   attributes,
                   COALESCE(llm_token_count_prompt, 0),
                   COALESCE(llm_token_count_completion, 0),
                   {span_kind_expr}
            FROM spans
            WHERE trace_rowid IN ({ph})
            ORDER BY trace_rowid, id
            """,
            trace_rowids,
        ).fetchall()

    prompt_tokens = 0
    completion_tokens = 0
    model_calls = 0
    prompt_counts: list[int] = []
    tool_calls = 0
    per_agent_tokens: dict[str, int] = {}

    for trace_rowid, name, attributes, prompt, completion, span_kind in rows:
        prompt_count = int(prompt or 0)
        completion_count = int(completion or 0)
        prompt_tokens += prompt_count
        completion_tokens += completion_count
        if prompt_count > 0:
            model_calls += 1
            prompt_counts.append(prompt_count)

        if _is_tool_span("" if name is None else str(name), "" if span_kind is None else str(span_kind), attributes):
            tool_calls += 1

        agent = root_names.get(int(trace_rowid), "")
        if agent:
            per_agent_tokens[agent] = per_agent_tokens.get(agent, 0) + prompt_count + completion_count

    est_fixed_floor = min(prompt_counts) if prompt_counts else 0
    est_variable = max(0, prompt_tokens - est_fixed_floor * model_calls)
    return TokenBreakdown(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        est_fixed_floor=est_fixed_floor,
        est_variable=est_variable,
        model_calls=model_calls,
        tool_calls=tool_calls,
        per_agent_tokens=per_agent_tokens,
    )


def span_rows(db_path: str | Path, trace_rowids: list[int]) -> list[dict[str, str]]:
    """Return deterministic per-span rows with owning root-agent attribution."""

    if not trace_rowids:
        return []

    ph = _placeholders(trace_rowids)
    with _connect(db_path) as conn:
        span_kind_expr = _span_kind_select_expr(conn)
        root_names = _root_names(conn, trace_rowids)
        trace_ids = _trace_ids(conn, trace_rowids)
        rows = conn.execute(
            f"""
            SELECT trace_rowid,
                   name,
                   attributes,
                   COALESCE(llm_token_count_prompt, 0),
                   COALESCE(llm_token_count_completion, 0),
                   status_code,
                   {span_kind_expr}
            FROM spans
            WHERE trace_rowid IN ({ph})
            ORDER BY trace_rowid, id
            """,
            trace_rowids,
        ).fetchall()

    spans: list[dict[str, str]] = []
    for trace_rowid, name, attributes, prompt, completion, status_code, span_kind in rows:
        raw_span_kind = "" if span_kind is None else str(span_kind)
        spans.append(
            {
                "agent": root_names.get(int(trace_rowid), ""),
                "trace_id": trace_ids.get(int(trace_rowid), ""),
                "name": "" if name is None else str(name),
                "span_kind": raw_span_kind or _span_kind_from_attributes(attributes),
                "prompt": str(int(prompt or 0)),
                "completion": str(int(completion or 0)),
                "status_code": "" if status_code is None else str(status_code),
            }
        )
    return spans


def extract_answer(db_path: str | Path, trace_rowids: list[int]) -> str:
    """Extract the latest respond_to_user final answer for trace rowids."""

    if not trace_rowids:
        return ""

    ph = _placeholders(trace_rowids)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT attributes
            FROM spans
            WHERE trace_rowid IN ({ph}) AND name LIKE 'respond_to_user%'
            ORDER BY COALESCE(end_time, '') ASC, id ASC
            """,
            trace_rowids,
        ).fetchall()

    for row in reversed(rows):
        raw_attributes = "" if not row or row[0] is None else str(row[0])
        answer = _answer_from_attributes(raw_attributes)
        if answer:
            return answer
    return ""


def extract_answer_with_fallback(db_path: str | Path, trace_rowids: list[int]) -> str:
    """Extract the final answer, falling back to the last assistant message in model output spans."""

    answer = extract_answer(db_path, trace_rowids)
    if answer:
        return answer
    if not trace_rowids:
        return ""

    ph = _placeholders(trace_rowids)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT attributes
            FROM spans
            WHERE trace_rowid IN ({ph})
              AND (
                  COALESCE(llm_token_count_prompt, 0) > 0
                  OR COALESCE(llm_token_count_completion, 0) > 0
                  OR LOWER(COALESCE(name, '')) LIKE '%llm%'
                  OR LOWER(COALESCE(name, '')) LIKE '%model%'
              )
            ORDER BY COALESCE(end_time, '') ASC, id ASC
            """,
            trace_rowids,
        ).fetchall()

    for row in reversed(rows):
        raw_attributes = "" if not row or row[0] is None else str(row[0])
        answer = _last_assistant_message(raw_attributes)
        if answer:
            return answer
    return ""


def tool_outputs(db_path: str | Path, trace_rowids: list[int]) -> str:
    """Return newline-joined TOOL span outputs, capped at TOOL_OUTPUTS_CHAR_CAP characters."""

    if not trace_rowids:
        return ""

    ph = _placeholders(trace_rowids)
    with _connect(db_path) as conn:
        span_kind_expr = _span_kind_select_expr(conn)
        rows = conn.execute(
            f"""
            SELECT name, attributes, {span_kind_expr}
            FROM spans
            WHERE trace_rowid IN ({ph})
            ORDER BY trace_rowid, id
            """,
            trace_rowids,
        ).fetchall()

    pieces: list[str] = []
    total_chars = 0
    for name, attributes, span_kind in rows:
        raw_name = "" if name is None else str(name)
        raw_span_kind = "" if span_kind is None else str(span_kind)
        raw_attributes = "" if attributes is None else str(attributes)
        if not _is_tool_span(raw_name, raw_span_kind, raw_attributes):
            continue

        text = _tool_output_text(raw_attributes)
        if not text:
            continue

        newline_chars = 1 if pieces else 0
        remaining = TOOL_OUTPUTS_CHAR_CAP - total_chars - newline_chars
        if remaining <= 0:
            break
        pieces.append(text[:remaining])
        total_chars += newline_chars + len(pieces[-1])

    return "\n".join(pieces)


def command_histogram(db_path: str | Path, trace_rowids: list[int]) -> dict[str, int]:
    """Count Mythic command names from issue_task tool spans."""

    if not trace_rowids:
        return {}

    ph = _placeholders(trace_rowids)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT attributes
            FROM spans
            WHERE trace_rowid IN ({ph}) AND name LIKE 'issue_task_and_waitfor_task_output%'
            """,
            trace_rowids,
        ).fetchall()

    counts: dict[str, int] = {}
    for row in rows:
        raw_attributes = "" if not row or row[0] is None else str(row[0])
        for command in _commands_from_attributes(raw_attributes):
            counts[command] = counts.get(command, 0) + 1
    return counts


def _root_names(conn: sqlite3.Connection, trace_rowids: list[int]) -> dict[int, str]:
    if not trace_rowids:
        return {}

    ph = _placeholders(trace_rowids)
    rows = conn.execute(
        f"""
        SELECT trace_rowid, name
        FROM spans
        WHERE trace_rowid IN ({ph})
        ORDER BY trace_rowid, COALESCE(start_time, ''), id
        """,
        trace_rowids,
    ).fetchall()

    roots: dict[int, str] = {}
    for trace_rowid, name in rows:
        rowid = int(trace_rowid)
        if rowid not in roots:
            roots[rowid] = "" if name is None else str(name)
    return roots


def _trace_ids(conn: sqlite3.Connection, trace_rowids: list[int]) -> dict[int, str]:
    if not trace_rowids:
        return {}

    ph = _placeholders(trace_rowids)
    rows = conn.execute(
        f"""
        SELECT rowid, trace_id
        FROM traces
        WHERE rowid IN ({ph})
        """,
        trace_rowids,
    ).fetchall()
    return {int(row[0]): "" if row[1] is None else str(row[1]) for row in rows}


def _span_kind_select_expr(conn: sqlite3.Connection) -> str:
    column = _span_kind_column(conn)
    if column:
        return f"COALESCE({_quote_identifier(column)}, '')"
    return "''"


def _span_kind_column(conn: sqlite3.Connection) -> str:
    rows = conn.execute("PRAGMA table_info(spans)").fetchall()
    for row in rows:
        name = "" if row[1] is None else str(row[1])
        if name.lower() == "span_kind":
            return name
    return ""


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _is_tool_span(name: str, span_kind: str, raw_attributes: Any) -> bool:
    if name.startswith("issue_task_and_waitfor_task_output"):
        return True
    if name.endswith(".tool"):
        return True
    if span_kind.upper() == "TOOL":
        return True
    return _span_kind_from_attributes(raw_attributes).upper() == "TOOL"


def _span_kind_from_attributes(raw_attributes: Any) -> str:
    attrs = _parse_json_object("" if raw_attributes is None else str(raw_attributes))
    for key in ("span_kind", "span.kind", "openinference.span.kind"):
        value = attrs.get(key)
        if isinstance(value, str):
            return value
    return ""


def _answer_from_attributes(raw_attributes: str) -> str:
    attrs = _parse_json_object(raw_attributes)
    if attrs:
        input_value = _nested_value(attrs, "input", "value")
        answer = _final_response_from_repr(input_value)
        if answer:
            return answer

        output_value = _nested_value(attrs, "output", "value")
        answer = _messages_from_output(output_value)
        if answer:
            return answer

    return _final_response_from_repr(raw_attributes)


def _tool_output_text(raw_attributes: str) -> str:
    try:
        attrs = _parse_json_object(raw_attributes)
        if not attrs:
            return _regex_output_text(raw_attributes)

        candidate = _first_tool_output_candidate(attrs)
        if not candidate:
            return ""
        return _human_text_from_inner_output(candidate)
    except (TypeError, ValueError, RecursionError):
        return ""


def _first_tool_output_candidate(attrs: dict[str, Any]) -> str:
    output_value = _nested_value(attrs, "output", "value")
    if output_value:
        return output_value

    traceloop_output = _nested_string(attrs, "traceloop", "entity", "output")
    if traceloop_output:
        return traceloop_output

    entity_output = _nested_string(attrs, "entity", "output")
    if entity_output:
        return entity_output

    output = attrs.get("output")
    return output if isinstance(output, str) else ""


def _human_text_from_inner_output(raw: str) -> str:
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()

    parts = _string_values_for_keys(parsed, {"content", "outputs"}, 0)
    if parts:
        return "\n".join(parts).strip()
    return raw.strip()


def _regex_output_text(raw_attributes: str) -> str:
    patterns = (
        r'"(?:output|value)"\s*:\s*"((?:\\.|[^"\\])*)"',
        r"'(?:output|value)'\s*:\s*'((?:\\.|[^'\\])*)'",
    )
    for pattern in patterns:
        values = [_decode_regex_string(match.group(1)) for match in re.finditer(pattern, raw_attributes)]
        text = "\n".join(value.strip() for value in values if value.strip()).strip()
        if text:
            return text
    return ""


def _decode_regex_string(raw: str) -> str:
    try:
        decoded = json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw
    return decoded if isinstance(decoded, str) else raw


def _last_assistant_message(raw_attributes: str) -> str:
    attrs = _parse_json_object(raw_attributes)
    if not attrs:
        return ""

    for candidate in _entity_output_candidates(attrs):
        message = _last_assistant_message_from_output(candidate)
        if message:
            return message
    return ""


def _entity_output_candidates(attrs: dict[str, Any]) -> list[str]:
    candidates = [
        _nested_string(attrs, "traceloop", "entity", "output"),
        _nested_string(attrs, "entity", "output"),
        _nested_value(attrs, "output", "value"),
    ]
    output = attrs.get("output")
    if isinstance(output, str):
        candidates.append(output)
    return [candidate for candidate in candidates if candidate]


def _last_assistant_message_from_output(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""

    messages = _output_messages(parsed)
    for message in reversed(messages):
        if _is_assistant_message(message):
            return _message_content(message)
    return ""


def _output_messages(parsed: dict[str, Any]) -> list[Any]:
    for key in ("outputs", "update"):
        container = parsed.get(key)
        messages = container.get("messages") if isinstance(container, dict) else None
        if isinstance(messages, list):
            return messages

    messages = parsed.get("messages")
    return messages if isinstance(messages, list) else []


def _is_assistant_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False

    for value in (message.get("role"), message.get("type")):
        if isinstance(value, str) and value.lower() in {"assistant", "ai"}:
            return True

    kwargs = message.get("kwargs")
    if isinstance(kwargs, dict):
        for value in (kwargs.get("role"), kwargs.get("type")):
            if isinstance(value, str) and value.lower() in {"assistant", "ai"}:
                return True

    identifier = message.get("id")
    if isinstance(identifier, list) and identifier:
        tail = identifier[-1]
        return isinstance(tail, str) and tail.lower().endswith("aimessage")
    if isinstance(identifier, str):
        return identifier.lower().endswith("aimessage")
    return False


def _message_content(message: dict[str, Any]) -> str:
    kwargs = message.get("kwargs")
    if isinstance(kwargs, dict):
        content = kwargs.get("content")
        if isinstance(content, str):
            return content.strip()

    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def _commands_from_attributes(raw_attributes: str) -> list[str]:
    attrs = _parse_json_object(raw_attributes)
    input_value = _nested_value(attrs, "input", "value") if attrs else ""
    candidates = [input_value] if input_value else [raw_attributes]

    commands: list[str] = []
    for candidate in candidates:
        for pattern in (r"'command':\s*'([^']+)'", r'"command":\s*"([^"]+)"'):
            commands.extend(match.group(1) for match in re.finditer(pattern, candidate))
    return commands


def _parse_json_object(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nested_value(data: dict[str, Any], first: str, second: str) -> str:
    outer = data.get(first)
    if not isinstance(outer, dict):
        return ""
    value = outer.get(second)
    return value if isinstance(value, str) else ""


def _nested_string(data: dict[str, Any], *keys: str) -> str:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def _string_values_for_keys(data: Any, keys: set[str], depth: int) -> list[str]:
    if depth > 20:
        return []
    if isinstance(data, dict):
        parts: list[str] = []
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in keys and isinstance(value, str):
                stripped = value.strip()
                if stripped:
                    parts.append(stripped)
            parts.extend(_string_values_for_keys(value, keys, depth + 1))
        return parts
    if isinstance(data, list):
        parts = []
        for item in data:
            parts.extend(_string_values_for_keys(item, keys, depth + 1))
        return parts
    return []


def _final_response_from_repr(raw: str) -> str:
    if not raw:
        return ""

    try:
        parsed = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        value = parsed.get("final_response")
        if value is not None:
            return str(value).strip()

    patterns = (
        r"'final_response':\s*'(.*?)'(?:\s*[},])",
        r'"final_response":\s*"(.*?)"(?:\s*[},])',
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.DOTALL)
        if match:
            return match.group(1).strip()
    return ""


def _messages_from_output(raw: str) -> str:
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ""

    update = parsed.get("update") if isinstance(parsed, dict) else None
    messages = update.get("messages") if isinstance(update, dict) else None
    if not isinstance(messages, list):
        return ""

    parts: list[str] = []
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            parts.append(message["content"])
    return "\n".join(part for part in parts if part).strip()
