import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
import phoenix_accounting as accounting  # noqa: E402


def _db(tmp_path):
    path = tmp_path / "phoenix.db"
    con = sqlite3.connect(path)
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE traces (id INTEGER PRIMARY KEY, trace_id TEXT);
        CREATE TABLE spans (id INTEGER PRIMARY KEY, trace_rowid INTEGER, span_id TEXT, parent_id TEXT, span_kind TEXT, start_time TEXT, end_time TEXT, status_code TEXT, attributesJSONB TEXT, llm_token_count_prompt INTEGER, llm_token_count_completion INTEGER);
        CREATE TABLE span_costs (id INTEGER PRIMARY KEY, span_rowid INTEGER, trace_rowid INTEGER, total_cost REAL);
        CREATE TABLE span_cost_details (id INTEGER PRIMARY KEY, span_cost_id INTEGER, token_type TEXT, is_prompt INTEGER, tokens REAL);
    """)
    con.executemany("INSERT INTO traces VALUES (?, ?)", [(1, "trace-a"), (2, "trace-b")])
    attrs = json.dumps({"llm": {"model_name": "model-a", "provider": "provider-a"}, "input": {"value": "SECRET"}})
    con.executemany("INSERT INTO spans VALUES (?, ?, ?, ?, 'LLM', ?, ?, ?, ?, ?, ?)", [
        (1, 1, "span-a", None, "1", "2", "OK", attrs, 10, 2),
        (2, 1, "span-child", "chain-a", "3", "4", "ERROR", attrs, None, None),
        (3, 2, "span-b", None, "5", "6", "OK", attrs, 0, 0),
    ])
    con.execute("INSERT INTO spans VALUES (4, 1, 'chain-a', 'span-a', 'CHAIN', '2.5', '2.9', 'OK', ?, NULL, NULL)", (attrs,))
    con.executemany("INSERT INTO span_costs VALUES (?, ?, ?, NULL)", [(1, 1, 1), (2, 2, 1), (3, 3, 2)])
    con.executemany("INSERT INTO span_cost_details VALUES (?, ?, ?, ?, ?)", [
        (1, 1, "input", 1, 10), (2, 1, "output", 0, 2), (3, 1, "cache_read", 1, 0),
        (4, 2, "cache_write", 1, 0), (5, 3, "reasoning", 0, 0),
    ])
    con.commit()
    return path, con


def test_exact_trace_usage_is_allowlisted_and_distinguishes_null_from_zero(tmp_path):
    path, con = _db(tmp_path)
    before = path.read_bytes()
    result = accounting.read_phoenix_usage(path, trace_ids=["trace-b"])
    assert result["selected_trace_ids"] == ["trace-b"]
    assert result["rows"] == [{"trace_id": "trace-b", "span_id": "span-b", "parent_id": None, "start_time": "5", "end_time": "6", "status_code": "OK", "model": "model-a", "provider": "provider-a", "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read_tokens": None, "cache_write_tokens": None, "reasoning_tokens": 0}]
    assert result["usage"]["countable"] is True
    assert result["usage"]["total_tokens"] == 0
    assert result["usage"]["cache_read_tokens"] is None
    assert "SECRET" not in json.dumps(result)
    assert before == path.read_bytes()
    con.close()


def test_missing_failed_and_nested_llm_spans_fail_closed(tmp_path):
    path, con = _db(tmp_path)
    result = accounting.read_phoenix_usage(path, trace_ids=["trace-a", "missing"])
    assert result["missing_trace_ids"] == ["missing"]
    assert result["usage"]["countable"] is False
    assert result["usage"]["total_tokens"] is None
    assert result["coverage"]["error_unknown_usage_count"] == 1
    assert result["coverage"]["duplicated_nested_llm_instrumentation"] is True
    con.close()


def test_attributes_supply_optional_cache_reasoning_without_cost_tables_and_uri_punctuation(tmp_path):
    path = tmp_path / "phoenix?#.db"
    con = sqlite3.connect(path)
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE traces (id INTEGER PRIMARY KEY, trace_id TEXT);
        CREATE TABLE spans (id INTEGER PRIMARY KEY, trace_rowid INTEGER, span_id TEXT, parent_id TEXT, span_kind TEXT, start_time TEXT, end_time TEXT, status_code TEXT, attributesJSONB TEXT, llm_token_count_prompt INTEGER, llm_token_count_completion INTEGER);
    """)
    attrs = json.dumps({"llm": {"model_name": "m", "provider": "p", "token_count": {
        "prompt_details": {"input": 6, "cache_read": 4, "cache_write": 0},
        "completion_details": {"output": 2, "reasoning": 2},
    }}})
    con.execute("INSERT INTO traces VALUES (1, 'cache-trace')")
    con.execute("INSERT INTO spans VALUES (1, 1, 'cached', NULL, 'LLM', '1', '2', 'OK', ?, 10, 4)", (attrs,))
    con.commit()
    result = accounting.read_phoenix_usage(path, trace_ids=["cache-trace"])
    assert result["rows"][0]["cache_read_tokens"] == 4
    assert result["rows"][0]["cache_write_tokens"] == 0
    assert result["rows"][0]["reasoning_tokens"] == 2
    assert result["usage"]["total_tokens"] == 14
    assert result["usage"]["countable"] is True
    con.close()


def test_fingerprint_changes_and_reader_observes_wal_without_mutation(tmp_path):
    path, writer = _db(tmp_path)
    first = accounting.read_phoenix_usage(path, trace_ids=["trace-b"])
    writer.execute("UPDATE spans SET llm_token_count_prompt = 7 WHERE span_id = 'span-b'")
    writer.commit()
    second = accounting.read_phoenix_usage(path, trace_ids=["trace-b"])
    assert second["rows"][0]["prompt_tokens"] == 7
    assert first["selected_rows_fingerprint"] != second["selected_rows_fingerprint"]
    writer.close()


def test_rejects_ambiguous_inputs_conflicts_and_orphan_costs(tmp_path):
    path, con = _db(tmp_path)
    with pytest.raises(accounting.PhoenixAccountingError):
        accounting.read_phoenix_usage(path, trace_ids=["trace-b", "trace-b"])
    con.executemany("INSERT INTO traces VALUES (?, ?)", [(4, "duplicate-trace"), (5, "duplicate-trace")])
    con.commit()
    with pytest.raises(accounting.PhoenixAccountingError):
        accounting.read_phoenix_usage(path, trace_ids=["duplicate-trace"])
    con.execute("INSERT INTO traces VALUES (6, 'duplicate-span')")
    con.executemany("INSERT INTO spans VALUES (?, 6, 'same-span', NULL, 'LLM', '1', '2', 'OK', ?, 1, 1)", [
        (6, json.dumps({"llm": {"model_name": "m", "provider": "p"}})),
        (7, json.dumps({"llm": {"model_name": "m", "provider": "p"}})),
    ])
    con.commit()
    with pytest.raises(accounting.PhoenixAccountingError):
        accounting.read_phoenix_usage(path, trace_ids=["duplicate-span"])
    con.execute("UPDATE span_cost_details SET tokens = 11 WHERE id = 1")
    con.commit()
    with pytest.raises(accounting.PhoenixAccountingError):
        accounting.read_phoenix_usage(path, trace_ids=["trace-a"])
    con.execute("UPDATE span_cost_details SET tokens = 10 WHERE id = 1")
    con.execute("INSERT INTO span_costs VALUES (9, 999, 2, NULL)")
    con.commit()
    with pytest.raises(accounting.PhoenixAccountingError):
        accounting.read_phoenix_usage(path, trace_ids=["trace-b"])
    con.close()
