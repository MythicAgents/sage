"""Offline checks for the MCP client and Phoenix live check's local logic (no Mythic, no container).

Mythic rows and span counts below are synthetic, shaped like Mythic's chat rows and Phoenix's spans table. The B1 and
B2 answers are built from the text Sage's `/bloodhound` and `/mcp tools` handlers emit. They prove the pass rules
accept a correct set and reject each falsifier.
"""

from __future__ import annotations

import copy
import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_mcp_client_phoenix.py"
SPEC = importlib.util.spec_from_file_location("check_mcp_client_phoenix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

B1_TEXT = ("**Connection:** Connected to BloodHound MCP (17 tools).\n\n" + check.INGEST_READY)
B2_TEXT = "\n".join([
    "**BloodHound** — 4 tool(s)",
    "- `domain_info` — Domain details",
    "- `cypher_query` — Run a read-only Cypher query",
    "- `file_upload` — Upload collection data or read an ingest job's status",
    "- `list_users`",
])


def _rows(text, status="complete"):
    return {"status": status, "messages": [{"author_type": "user", "message": "prompt"},
                                           {"author_type": "ai", "message": text}]}


def _passing():
    run = {"task_baseline": 41, "started_at": 0.0, "finished_at": 12.0, "phoenix_health": [200, 200, 200],
           "requests": {"B1": {"request_id": 90, "wall_seconds": 6.0}, "B2": {"request_id": 91, "wall_seconds": 1.0}}}
    rows = {"B1": _rows(B1_TEXT), "B2": _rows(B2_TEXT)}
    return run, rows, {"CHAIN": 2}


def test_a_correct_set_passes():
    run, rows, spans = _passing()
    result = check.evaluate(run, rows, spans, 41)
    assert result["verdict"] == "pass", result["criteria"]
    assert result["observations"]["b2_servers"] == {"BloodHound": 4}
    assert result["observations"]["request_ids"] == {"B1": 90, "B2": 91}


def _mutate(change):
    run, rows, spans = copy.deepcopy(_passing())
    tasks = change(run, rows, spans)
    return check.evaluate(run, rows, spans, 41 if tasks is None else tasks)


@pytest.mark.parametrize("change, failing", [
    (lambda run, rows, spans: rows.__setitem__("B1", _rows(
        "**Connection:** Connected.\n\n**Ingest:** NOT ready — Sage will refuse graph collection and ingest.")), "L1"),
    (lambda run, rows, spans: rows.__setitem__("B1", _rows(B1_TEXT, status="error")), "L1"),
    (lambda run, rows, spans: rows.__setitem__("B1", {"status": "complete", "messages": [
        {"author_type": "user", "message": B1_TEXT}]}), "L1"),
    (lambda run, rows, spans: rows.__setitem__("B2", _rows(B2_TEXT.replace("- `cypher_query` — Run a read-only "
                                                                           "Cypher query\n", ""))), "L2"),
    (lambda run, rows, spans: rows.__setitem__("B2", _rows(B2_TEXT + "\n- `file_upload`")), "L2"),
    (lambda run, rows, spans: rows.__setitem__("B2", _rows(B2_TEXT + "\n**BloodHound-2** — 1 tool(s)\n"
                                                                     "- `domain_info`")), "L2"),
    (lambda run, rows, spans: rows.__setitem__("B2", _rows(B2_TEXT.replace("**BloodHound**", "**bloodhound**"))),
     "L2"),
    (lambda run, rows, spans: rows.__setitem__("B2", _rows("No MCP servers connected.")), "L2"),
    (lambda run, rows, spans: rows.__setitem__("B2", _rows(B2_TEXT, status="cancelled")), "L2"),
    (lambda run, rows, spans: 42, "L3"),
    (lambda run, rows, spans: run.__setitem__("stopped_for_task", True), "L3"),
    (lambda run, rows, spans: spans.__setitem__("LLM", 1), "L4"),
    (lambda run, rows, spans: run["requests"]["B2"].__setitem__("timeout", "budget exhausted"), "L5"),
    (lambda run, rows, spans: run.__setitem__("stopped", {"request": "B1", "reason": "x"}), "L5"),
    (lambda run, rows, spans: run.__setitem__("finished_at", 301.0), "L5"),
    (lambda run, rows, spans: run["requests"].pop("B2") and None, "L5"),
], ids=["ingest-not-ready", "b1-error", "ready-text-only-in-prompt", "missing-tool", "duplicate-tool",
        "second-server", "wrong-server-name", "no-server", "b2-cancelled", "task-created", "stopped-for-task",
        "llm-span", "timeout", "stopped", "over-budget", "b2-never-sent"])
def test_each_falsifier_fails_its_rule(change, failing):
    result = _mutate(change)
    assert result["verdict"] == "fail"
    assert result["criteria"][failing] is False
    assert [name for name, ok in result["criteria"].items() if not ok] == [failing]


def test_a_set_whose_first_request_was_never_sent_did_not_run():
    run, rows, spans = _passing()
    run["requests"] = {}
    assert check.evaluate(run, {}, spans, 41)["verdict"] == "did_not_run"


def test_span_counts_are_read_in_place_by_kind(tmp_path, monkeypatch):
    db = tmp_path / "phoenix.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE spans (id INTEGER PRIMARY KEY, span_kind TEXT)")
        conn.executemany("INSERT INTO spans VALUES (?, ?)", [(1, "LLM"), (2, "CHAIN"), (3, "LLM"), (4, "CHAIN")])
    assert check.llm_spans(db, 1, 4) == {"LLM": 1, "CHAIN": 2}


def test_channel_settings_are_reported_by_presence_only(monkeypatch):
    seen = []
    monkeypatch.setattr(check, "http_status", lambda url, timeout=5: seen.append(url) or 200)
    report = check.channel_bloodhound({"BLOODHOUND_URL": "http://ce.invalid:8080", "BLOODHOUND_TOKEN_ID": "token-id-value",
                                       "BLOODHOUND_TOKEN_KEY": ""})
    assert report == {"settings_present": {"BLOODHOUND_URL": True, "BLOODHOUND_TOKEN_ID": True,
                                           "BLOODHOUND_TOKEN_KEY": False}, "ce_status": 200}
    assert seen == ["http://ce.invalid:8080/"]
    assert "token-id-value" not in str(report) and "ce.invalid" not in str(report)
