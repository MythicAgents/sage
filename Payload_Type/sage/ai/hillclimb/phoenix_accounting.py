"""Read-only, trace-bound Phoenix token evidence for evaluation accounting.

This module intentionally retains only an allowlist of tracing metadata and usage
columns.  It never reads prompts, responses, events, or arbitrary span attributes.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Sequence


class PhoenixAccountingError(ValueError):
    """A non-sensitive Phoenix accounting input or evidence error."""


_MAX_TRACE_IDS = 1_000
_PROGRESS_STEPS = 200_000
_ALLOWLIST = (
    "trace_id", "span_id", "parent_id", "start_time", "end_time", "status_code",
    "model", "provider", "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _fingerprint(rows: list[dict[str, Any]]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(rows).encode()).hexdigest()


def _trace_ids(value: Sequence[str]) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PhoenixAccountingError("Phoenix trace selection is malformed")
    result = list(value)
    if not result or len(result) > _MAX_TRACE_IDS:
        raise PhoenixAccountingError("Phoenix trace selection is malformed")
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise PhoenixAccountingError("Phoenix trace selection is malformed")
    result = [item.strip() for item in result]
    if len(set(result)) != len(result):
        raise PhoenixAccountingError("Phoenix trace selection is malformed")
    return result


def _integer(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is float and value.is_integer():
        value = int(value)
    if type(value) is not int or value < 0:
        raise PhoenixAccountingError("Phoenix usage evidence is malformed")
    return value


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PhoenixAccountingError("Phoenix usage evidence is malformed")
    return value


def _read_connection(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
    except (OSError, sqlite3.Error):
        raise PhoenixAccountingError("Phoenix accounting database is unavailable") from None
    steps = 0

    def _limit() -> int:
        nonlocal steps
        steps += 1
        return 1 if steps > _PROGRESS_STEPS else 0

    connection.set_progress_handler(_limit, 1_000)
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        raise PhoenixAccountingError("Phoenix accounting schema is unavailable") from None


def _usage_from_details(connection: sqlite3.Connection, trace_rowids: list[int]) -> dict[int, dict[str, int]]:
    if not trace_rowids:
        return {}
    placeholders = ",".join("?" for _ in trace_rowids)
    try:
        costs = connection.execute(
            "SELECT c.id, c.span_rowid, c.trace_rowid, s.trace_rowid "
            "FROM span_costs AS c LEFT JOIN spans AS s ON s.id = c.span_rowid "
            f"WHERE c.trace_rowid IN ({placeholders})", trace_rowids,
        ).fetchall()
        by_span: dict[int, int] = {}
        for cost_id, span_rowid, trace_rowid, span_trace_rowid in costs:
            if span_trace_rowid is None or int(trace_rowid) != int(span_trace_rowid) or int(span_rowid) in by_span:
                raise PhoenixAccountingError("Phoenix accounting cost evidence is ambiguous")
            by_span[int(span_rowid)] = int(cost_id)
        rows = connection.execute(
            "SELECT c.span_rowid, d.token_type, d.is_prompt, d.tokens "
            "FROM span_costs AS c JOIN span_cost_details AS d ON d.span_cost_id = c.id "
            f"WHERE c.trace_rowid IN ({placeholders})",
            trace_rowids,
        ).fetchall()
    except sqlite3.Error:
        raise PhoenixAccountingError("Phoenix accounting query failed") from None
    result: dict[int, dict[str, int]] = {}
    seen: set[tuple[int, str, int]] = set()
    for span_rowid, token_type, is_prompt, tokens in rows:
        if not isinstance(token_type, str) or not token_type or type(is_prompt) is not int:
            raise PhoenixAccountingError("Phoenix usage evidence is malformed")
        amount = _integer(tokens)
        if amount is None:
            raise PhoenixAccountingError("Phoenix usage evidence is malformed")
        identity = (int(span_rowid), token_type, is_prompt)
        if identity in seen:
            raise PhoenixAccountingError("Phoenix accounting cost evidence is ambiguous")
        seen.add(identity)
        bucket = result.setdefault(int(span_rowid), {})
        bucket[f"{is_prompt}:{token_type}"] = amount
    return result


def _details_value(detail: dict[str, int], token_type: str) -> int | None:
    values = [detail[key] for key in (f"1:{token_type}", f"0:{token_type}") if key in detail]
    return sum(values) if values else None


def _attribute_total(values: tuple[object, object]) -> int | None:
    parsed = [_integer(value) for value in values]
    return sum(value for value in parsed if value is not None) if any(value is not None for value in parsed) else None


def _exact_optional(detail: int | None, attributes: int | None) -> int | None:
    if detail is not None and attributes is not None and detail != attributes:
        raise PhoenixAccountingError("Phoenix usage evidence conflicts")
    return detail if detail is not None else attributes


def read_phoenix_usage(db_path: Path, *, trace_ids: Sequence[str], max_spans: int = 100_000) -> dict:
    """Return a JSON-safe, exact-trace Phoenix usage snapshot without mutating the DB."""
    requested = _trace_ids(trace_ids)
    if type(max_spans) is not int or not 0 < max_spans <= 1_000_000:
        raise PhoenixAccountingError("Phoenix accounting span limit is malformed")
    connection = _read_connection(Path(db_path))
    try:
        span_columns = _table_columns(connection, "spans")
        required = {"id", "trace_rowid", "span_id", "parent_id", "span_kind", "start_time", "end_time", "status_code", "llm_token_count_prompt", "llm_token_count_completion"}
        if not required <= span_columns or not {"id", "trace_id"} <= _table_columns(connection, "traces"):
            raise PhoenixAccountingError("Phoenix accounting schema is unavailable")
        attributes = "attributesJSONB" if "attributesJSONB" in span_columns else "attributes" if "attributes" in span_columns else None
        if attributes is None:
            raise PhoenixAccountingError("Phoenix accounting schema is unavailable")
        cost_tables_available = {"span_costs", "span_cost_details"} <= {
            table for table in ("span_costs", "span_cost_details") if _table_columns(connection, table)
        }
        placeholders = ",".join("?" for _ in requested)
        trace_rows = connection.execute(
            f"SELECT id, trace_id FROM traces WHERE trace_id IN ({placeholders})", requested
        ).fetchall()
        selected_by_id: dict[str, int] = {}
        for rowid, trace_id in trace_rows:
            if trace_id in selected_by_id:
                raise PhoenixAccountingError("Phoenix accounting trace evidence is ambiguous")
            selected_by_id[str(trace_id)] = int(rowid)
        selected = [item for item in requested if item in selected_by_id]
        missing = [item for item in requested if item not in selected_by_id]
        trace_rowids = [selected_by_id[item] for item in selected]
        details = _usage_from_details(connection, trace_rowids) if cost_tables_available else {}
        # JSON extraction is restricted to model/provider paths; no prompt/response attribute is selected.
        sql = (
            "SELECT t.trace_id, s.id, s.span_id, s.parent_id, s.start_time, s.end_time, s.status_code, "
            f"json_extract(s.{attributes}, '$.llm.model_name'), json_extract(s.{attributes}, '$.llm.provider'), "
            "s.llm_token_count_prompt, s.llm_token_count_completion, "
            f"json_extract(s.{attributes}, '$.llm.token_count.prompt_details.cache_read'), "
            f"json_extract(s.{attributes}, '$.llm.token_count.completion_details.cache_read'), "
            f"json_extract(s.{attributes}, '$.llm.token_count.prompt_details.cache_write'), "
            f"json_extract(s.{attributes}, '$.llm.token_count.completion_details.cache_write'), "
            f"json_extract(s.{attributes}, '$.llm.token_count.prompt_details.reasoning'), "
            f"json_extract(s.{attributes}, '$.llm.token_count.completion_details.reasoning') "
            "FROM spans AS s JOIN traces AS t ON t.id = s.trace_rowid "
            f"WHERE s.trace_rowid IN ({','.join('?' for _ in trace_rowids)}) AND s.span_kind = 'LLM' "
            "ORDER BY t.trace_id, s.start_time, s.id LIMIT ?"
            if trace_rowids else None
        )
        raw_rows = connection.execute(sql, [*trace_rowids, max_spans + 1]).fetchall() if sql else []
        if len(raw_rows) > max_spans:
            raise PhoenixAccountingError("Phoenix accounting span limit exceeded")
        rows: list[dict[str, Any]] = []
        seen_span_ids: set[str] = set()
        missing_model_count = missing_count_count = unfinished = failed_unknown = 0
        for raw in raw_rows:
            (trace_id, rowid, span_id, parent_id, start, end, status, model, provider, prompt, completion,
             cache_read_prompt, cache_read_completion, cache_write_prompt, cache_write_completion,
             reasoning_prompt, reasoning_completion) = raw
            if not isinstance(span_id, str) or not span_id or span_id in seen_span_ids:
                raise PhoenixAccountingError("Phoenix accounting span evidence is ambiguous")
            seen_span_ids.add(span_id)
            prompt, completion = _integer(prompt), _integer(completion)
            detail = details.get(int(rowid), {})
            detail_prompt = sum(value for key, value in detail.items() if key.startswith("1:"))
            detail_completion = sum(value for key, value in detail.items() if key.startswith("0:"))
            if (prompt is not None and detail_prompt > prompt) or (completion is not None and detail_completion > completion):
                raise PhoenixAccountingError("Phoenix usage evidence conflicts")
            cache_read = _exact_optional(
                _details_value(detail, "cache_read"), _attribute_total((cache_read_prompt, cache_read_completion)),
            )
            cache_write = _exact_optional(
                _details_value(detail, "cache_write"), _attribute_total((cache_write_prompt, cache_write_completion)),
            )
            reasoning = _exact_optional(
                _details_value(detail, "reasoning"), _attribute_total((reasoning_prompt, reasoning_completion)),
            )
            if prompt is None or completion is None:
                # Detail rows are pricing breakdowns, not an independent aggregate-total source.
                # They can prove auxiliary categories but cannot promote an unknown total.
                missing_count_count += 1
            total = prompt + completion if prompt is not None and completion is not None else None
            row = dict(zip(_ALLOWLIST, (
                str(trace_id), span_id, _text(parent_id), _text(start), _text(end), _text(status),
                _text(model), _text(provider), prompt, completion, total, cache_read, cache_write, reasoning,
            )))
            if not row["model"] or not row["provider"]:
                missing_model_count += 1
            if end is None:
                unfinished += 1
            if str(status or "").upper() == "ERROR" and (prompt is None or completion is None):
                failed_unknown += 1
            rows.append(row)
        metadata_sql = (
            "SELECT span_id, parent_id, span_kind FROM spans WHERE trace_rowid IN ("
            + ",".join("?" for _ in trace_rowids)
            + ") ORDER BY id LIMIT ?"
        ) if trace_rowids else None
        metadata = connection.execute(metadata_sql, [*trace_rowids, max_spans + 1]).fetchall() if metadata_sql else []
        if len(metadata) > max_spans:
            raise PhoenixAccountingError("Phoenix accounting span limit exceeded")
        parents: dict[str, str | None] = {}
        for span_id, parent_id, _kind in metadata:
            if not isinstance(span_id, str) or not span_id or span_id in parents:
                raise PhoenixAccountingError("Phoenix accounting span evidence is ambiguous")
            parents[span_id] = _text(parent_id)
        nested: set[str] = set()
        for span_id in seen_span_ids:
            parent = parents.get(span_id)
            visited: set[str] = set()
            while parent:
                if parent in visited:
                    raise PhoenixAccountingError("Phoenix accounting span evidence is ambiguous")
                visited.add(parent)
                if parent in seen_span_ids:
                    nested.add(parent)
                    break
                parent = parents.get(parent)
        nested_ids = sorted(nested)
        complete = bool(rows) and not missing and not missing_model_count and not missing_count_count and not unfinished and not failed_unknown and not nested_ids
        def _optional_total(field: str) -> int | None:
            values = [row[field] for row in rows]
            return sum(values) if all(value is not None for value in values) else None

        usage = {
            "prompt_tokens": sum(row["prompt_tokens"] for row in rows) if complete else None,
            "completion_tokens": sum(row["completion_tokens"] for row in rows) if complete else None,
            "total_tokens": sum(row["total_tokens"] for row in rows) if complete else None,
            "cache_read_tokens": _optional_total("cache_read_tokens") if complete else None,
            "cache_write_tokens": _optional_total("cache_write_tokens") if complete else None,
            "reasoning_tokens": _optional_total("reasoning_tokens") if complete else None,
            "countable": complete,
        }
        return {
            "schema_version": 1, "requested_trace_ids": requested, "selected_trace_ids": selected,
            "missing_trace_ids": missing, "rows": rows, "usage": usage,
            "coverage": {"empty_selection": not bool(rows), "missing_model_count": missing_model_count,
                         "missing_token_count": missing_count_count, "unfinished_span_count": unfinished,
                         "error_unknown_usage_count": failed_unknown,
                         "duplicated_nested_llm_instrumentation": bool(nested_ids), "nested_parent_span_ids": nested_ids,
                         "countable": complete},
            "selected_rows_fingerprint": _fingerprint(rows),
        }
    except PhoenixAccountingError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        raise PhoenixAccountingError("Phoenix accounting query failed") from None
    finally:
        connection.close()
