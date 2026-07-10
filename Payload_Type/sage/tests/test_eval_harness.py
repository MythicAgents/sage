import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import harness, phoenix_reader  # noqa: E402


REQUIRED_CASE_IDS = {
    "list_callbacks",
    "whoami_cb17",
    "domain_controllers",
    "domain_users",
    "shares",
    "bh_domains",
    "shortest_path_da",
    "gpo_abuse",
    "kerberoastable",
    "adcs_templates",
}


def make_phoenix_db(tmp_path):
    db_path = tmp_path / "phoenix.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE traces (
            id INTEGER PRIMARY KEY,
            trace_id VARCHAR
        );
        CREATE TABLE spans (
            id INTEGER PRIMARY KEY,
            trace_rowid INTEGER,
            span_id VARCHAR,
            name VARCHAR,
            start_time VARCHAR,
            end_time VARCHAR,
            attributes TEXT,
            status_code VARCHAR,
            status_message VARCHAR,
            llm_token_count_prompt INTEGER,
            llm_token_count_completion INTEGER
        );
        """
    )
    conn.execute("INSERT INTO traces(id, trace_id) VALUES(1, 'old')")
    conn.execute("INSERT INTO traces(id, trace_id) VALUES(2, 'trace-a')")
    conn.execute("INSERT INTO traces(id, trace_id) VALUES(3, 'trace-b')")
    answer_attrs = json.dumps({"input": {"value": "{'final_response': 'SYSVOL and NETLOGON are accessible.'}"}})
    command_attrs = json.dumps(
        {
            "input": {
                "value": "{'callback_display_id': 17, 'command': 'net_shares', 'parameters': {}, 'timeout': 60}"
            }
        }
    )
    conn.executemany(
        """
        INSERT INTO spans(
            id, trace_rowid, span_id, name, start_time, end_time, attributes, status_code,
            status_message, llm_token_count_prompt, llm_token_count_completion
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, 1, "old-span", "old", "0", "0", "{}", None, None, None, None),
            (2, 2, "s1", "respond_to_user.tool", "1", "2", answer_attrs, None, None, 10, 5),
            (3, 2, "s2", "issue_task_and_waitfor_task_output.tool", "2", "3", command_attrs, None, None, 20, 7),
            (4, 3, "s3", "broken", "3", "4", "{}", "ERROR", "Recursion limit hit", None, 4),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


def make_breakdown_db(tmp_path):
    db_path = tmp_path / "breakdown.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE traces (
            id INTEGER PRIMARY KEY,
            trace_id VARCHAR
        );
        CREATE TABLE spans (
            id INTEGER PRIMARY KEY,
            trace_rowid INTEGER,
            span_id VARCHAR,
            name VARCHAR,
            start_time VARCHAR,
            end_time VARCHAR,
            attributes TEXT,
            span_kind VARCHAR,
            status_code VARCHAR,
            status_message VARCHAR,
            llm_token_count_prompt INTEGER,
            llm_token_count_completion INTEGER
        );
        """
    )
    conn.execute("INSERT INTO traces(id, trace_id) VALUES(10, 'trace-supervisor')")
    conn.execute("INSERT INTO traces(id, trace_id) VALUES(11, 'trace-mythic')")
    conn.executemany(
        """
        INSERT INTO spans(
            id, trace_rowid, span_id, name, start_time, end_time, attributes, span_kind, status_code,
            status_message, llm_token_count_prompt, llm_token_count_completion
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, 10, "root-a", "Supervisor", "1", "2", "{}", "", None, None, 0, 0),
            (2, 10, "llm-a", "Supervisor.llm", "2", "3", "{}", "", None, None, 100, 20),
            (3, 10, "tool-a", "MCP_Manager.tool", "3", "4", "{}", "TOOL", None, None, 0, 0),
            (4, 10, "llm-b", "Supervisor.llm_retry", "4", "5", "{}", "", None, None, 140, 10),
            (5, 11, "root-b", "Mythic_Operator", "1", "2", "{}", "", None, None, 0, 0),
            (6, 11, "llm-c", "Mythic_Operator.llm", "2", "3", "{}", "", None, None, 80, 5),
            (7, 11, "tool-b", "net_shares.tool", "3", "4", "{}", "TOOL", "ERROR", "tool failed", 0, 0),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


def make_tool_output_db(tmp_path):
    db_path = tmp_path / "tool-output.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE traces (
            id INTEGER PRIMARY KEY,
            trace_id VARCHAR
        );
        CREATE TABLE spans (
            id INTEGER PRIMARY KEY,
            trace_rowid INTEGER,
            span_id VARCHAR,
            name VARCHAR,
            start_time VARCHAR,
            end_time VARCHAR,
            attributes TEXT,
            span_kind VARCHAR,
            status_code VARCHAR,
            status_message VARCHAR,
            llm_token_count_prompt INTEGER,
            llm_token_count_completion INTEGER
        );
        """
    )
    live_output = json.dumps({"output": {"kwargs": {"content": "live kwargs content from tool"}}})
    conn.execute("INSERT INTO traces(id, trace_id) VALUES(20, 'trace-tools')")
    conn.executemany(
        """
        INSERT INTO spans(
            id, trace_rowid, span_id, name, start_time, end_time, attributes, span_kind, status_code,
            status_message, llm_token_count_prompt, llm_token_count_completion
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                1,
                20,
                "simple-tool",
                "simple.tool",
                "1",
                "2",
                json.dumps({"output": {"value": "simple output value"}}),
                "",
                None,
                None,
                0,
                0,
            ),
            (
                2,
                20,
                "live-tool",
                "issue_task_and_waitfor_task_output.tool",
                "2",
                "3",
                json.dumps({"traceloop": {"entity": {"output": live_output}}}),
                "",
                None,
                None,
                0,
                0,
            ),
            (
                3,
                20,
                "malformed-tool",
                "malformed.tool",
                "3",
                "4",
                '"output": "malformed fallback text", trailing',
                "",
                None,
                None,
                0,
                0,
            ),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


def make_answer_fallback_db(tmp_path):
    db_path = tmp_path / "answer-fallback.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE traces (
            id INTEGER PRIMARY KEY,
            trace_id VARCHAR
        );
        CREATE TABLE spans (
            id INTEGER PRIMARY KEY,
            trace_rowid INTEGER,
            span_id VARCHAR,
            name VARCHAR,
            start_time VARCHAR,
            end_time VARCHAR,
            attributes TEXT,
            status_code VARCHAR,
            status_message VARCHAR,
            llm_token_count_prompt INTEGER,
            llm_token_count_completion INTEGER
        );
        """
    )
    answer_attrs = json.dumps({"input": {"value": "{'final_response': 'respond_to_user answer wins'}"}})
    older_output = json.dumps(
        {
            "update": {
                "messages": [
                    {"id": ["langchain", "schema", "messages", "AIMessage"], "kwargs": {"content": "older AI answer"}}
                ]
            }
        }
    )
    final_output = json.dumps(
        {
            "outputs": {
                "messages": [
                    {"id": ["langchain", "schema", "messages", "HumanMessage"], "kwargs": {"content": "question"}},
                    {
                        "id": ["langchain", "schema", "messages", "AIMessage"],
                        "kwargs": {"content": "fallback final AI answer"},
                    },
                ]
            }
        }
    )
    conn.executemany(
        "INSERT INTO traces(id, trace_id) VALUES(?, ?)",
        [(30, "trace-answer"), (31, "trace-fallback"), (32, "trace-empty")],
    )
    conn.executemany(
        """
        INSERT INTO spans(
            id, trace_rowid, span_id, name, start_time, end_time, attributes, status_code,
            status_message, llm_token_count_prompt, llm_token_count_completion
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, 30, "respond", "respond_to_user.tool", "1", "2", answer_attrs, None, None, 0, 0),
            (
                2,
                30,
                "later-llm",
                "Supervisor.llm",
                "2",
                "3",
                json.dumps({"entity": {"output": final_output}}),
                None,
                None,
                10,
                2,
            ),
            (
                3,
                31,
                "older-llm",
                "Supervisor.llm",
                "1",
                "2",
                json.dumps({"traceloop": {"entity": {"output": older_output}}}),
                None,
                None,
                10,
                2,
            ),
            (
                4,
                31,
                "final-llm",
                "Supervisor.llm",
                "2",
                "3",
                json.dumps({"entity": {"output": final_output}}),
                None,
                None,
                10,
                2,
            ),
            (5, 32, "empty", "Supervisor.llm", "1", "2", "{}", None, None, 10, 2),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


def test_phoenix_reader_extracts_traces_metrics_answer_and_commands(tmp_path):
    db_path = make_phoenix_db(tmp_path)

    assert phoenix_reader.max_trace_rowid(db_path) == 3
    assert phoenix_reader.new_traces_since(db_path, 1) == [2, 3]

    summaries = phoenix_reader.trace_summaries_since(db_path, 1)
    assert {summary.rowid for summary in summaries} == {2, 3}
    assert sum(summary.spans for summary in summaries) == 3

    metrics = phoenix_reader.aggregate_metrics(db_path, [2, 3])
    assert metrics.tokens == 46
    assert metrics.model_calls == 2
    assert metrics.max_prompt == 20
    assert metrics.error_count == 1
    assert metrics.recursion_deaths == 1

    assert phoenix_reader.extract_answer(db_path, [2, 3]) == "SYSVOL and NETLOGON are accessible."
    assert phoenix_reader.command_histogram(db_path, [2, 3]) == {"net_shares": 1}


def test_token_breakdown_and_span_rows_attribute_tokens_by_root_agent(tmp_path):
    db_path = make_breakdown_db(tmp_path)

    empty = phoenix_reader.token_breakdown(db_path, [])
    assert empty == phoenix_reader.TokenBreakdown(0, 0, 0, 0, 0, 0, {})
    assert phoenix_reader.span_rows(db_path, []) == []

    breakdown = phoenix_reader.token_breakdown(db_path, [10, 11])
    assert breakdown.prompt_tokens == 320
    assert breakdown.completion_tokens == 35
    assert breakdown.model_calls == 3
    assert breakdown.est_fixed_floor == 80
    assert breakdown.est_variable == 80
    assert breakdown.tool_calls == 2
    assert breakdown.per_agent_tokens == {"Supervisor": 270, "Mythic_Operator": 85}

    rows = phoenix_reader.span_rows(db_path, [10, 11])
    assert len(rows) == 7
    assert set(rows[0]) == {"agent", "trace_id", "name", "span_kind", "prompt", "completion", "status_code"}
    assert rows[0]["agent"] == "Supervisor"
    assert rows[0]["trace_id"] == "trace-supervisor"
    assert rows[1]["prompt"] == "100"
    assert rows[2]["span_kind"] == "TOOL"
    assert rows[-1]["agent"] == "Mythic_Operator"
    assert rows[-1]["status_code"] == "ERROR"


def test_tool_outputs_extracts_fixture_and_live_shapes_with_cap(tmp_path, monkeypatch):
    db_path = make_tool_output_db(tmp_path)

    output = phoenix_reader.tool_outputs(db_path, [20])

    assert output.splitlines() == [
        "simple output value",
        "live kwargs content from tool",
        "malformed fallback text",
    ]
    assert phoenix_reader.tool_outputs(db_path, []) == ""

    monkeypatch.setattr(phoenix_reader, "TOOL_OUTPUTS_CHAR_CAP", 12)
    capped = phoenix_reader.tool_outputs(db_path, [20])

    assert len(capped) <= 12


def test_extract_answer_with_fallback_uses_respond_span_then_last_ai_message(tmp_path):
    db_path = make_answer_fallback_db(tmp_path)

    assert phoenix_reader.extract_answer_with_fallback(db_path, [30]) == "respond_to_user answer wins"
    assert phoenix_reader.extract_answer_with_fallback(db_path, [31]) == "fallback final AI answer"
    assert phoenix_reader.extract_answer_with_fallback(db_path, [32]) == ""


def test_score_answer_truth_table():
    forbid = ["traceback", "i cannot", "i'm sorry, but"]
    all_case = {"id": "a", "prompt": "find both", "expect_all": ["one", "two"]}
    any_case = {"id": "b", "prompt": "find one", "expect_any": ["alpha", "beta"]}

    assert harness.score_answer("ONE and two", "", all_case, forbid, 0).passed
    assert not harness.score_answer("one only", "", all_case, forbid, 0).passed
    assert harness.score_answer("contains BETA", "", any_case, forbid, 0).passed
    assert not harness.score_answer("contains neither", "", any_case, forbid, 0).passed
    assert not harness.score_answer("one two traceback", "", all_case, forbid, 0).passed
    assert not harness.score_answer("one two", "", all_case, forbid, 1).passed
    assert not harness.score_answer("", "", any_case, forbid, 0).passed
    assert not harness.score_answer("find one", "", any_case, forbid, 0).passed
    assert harness.score_answer("terse summary", "retrieved one and two", all_case, forbid, 0).passed
    assert not harness.score_answer("terse summary", "retrieved one only", all_case, forbid, 0).passed
    assert not harness.score_answer("i cannot help", "retrieved one and two", all_case, forbid, 0).passed
    assert not harness.score_answer("terse summary", "retrieved one and two", all_case, forbid, 1).passed


def test_cases_yaml_loads_and_validates():
    data = harness.load_cases(Path(__file__).resolve().parents[1] / "evals" / "cases.yaml")

    assert "sage_cb" not in data
    # apollo_cb is a live callback id that churns as the foothold is re-established
    # (cb17→18→19→20…); assert it's a valid id, not a transient literal.
    assert isinstance(data["apollo_cb"], int) and data["apollo_cb"] > 0
    assert data["forbid"]
    assert len(data["cases"]) == 10
    assert 8 <= len(data["cases"]) <= 12
    ids = [case["id"] for case in data["cases"]]
    assert set(ids) == REQUIRED_CASE_IDS
    assert len(ids) == len(set(ids))
    for case in data["cases"]:
        assert case["id"]
        assert case["category"]
        assert case["prompt"]
        assert sum(1 for key in ("expect_all", "expect_any") if key in case) == 1

    with (Path(__file__).resolve().parents[1] / "evals" / "cases.yaml").open("r", encoding="utf-8") as handle:
        assert len(yaml.safe_load(handle)["cases"]) == 10


def test_cases_yaml_rescoped_readonly():
    with (Path(__file__).resolve().parents[1] / "evals" / "cases.yaml").open("r", encoding="utf-8") as handle:
        cases = {case["id"]: case for case in yaml.safe_load(handle)["cases"]}

    for case_id in ("shortest_path_da", "gpo_abuse", "adcs_templates"):
        prompt = cases[case_id]["prompt"]
        lower = prompt.lower()
        assert "read-only" in lower
        assert "do not execute" in lower
        assert "REPORT" in prompt

    assert cases["shortest_path_da"]["expect_any"] == ["GenericWrite", "GPO", "STARKWALLPAPER", "Domain Admins"]
    assert cases["gpo_abuse"]["expect_any"] == ["STARKWALLPAPER", "GPO", "GenericWrite"]
    assert cases["adcs_templates"]["expect_any"] == ["ESC", "template", "ADCS", "certificate", "CA"]


def _install_fake_mythic(monkeypatch, get_all_tasks):
    package = ModuleType("mythic")
    module = ModuleType("mythic.mythic")
    module.get_all_tasks = get_all_tasks
    package.mythic = module
    monkeypatch.setitem(sys.modules, "mythic", package)
    monkeypatch.setitem(sys.modules, "mythic.mythic", module)
    monkeypatch.setattr("mythic.mythic.get_all_tasks", get_all_tasks)


def test_wait_for_task_complete_returns_on_terminal(monkeypatch):
    calls = {"count": 0}

    async def fake_get_all_tasks(mythic, custom_return_attributes):
        assert custom_return_attributes == "id display_id status completed"
        calls["count"] += 1
        if calls["count"] <= 2:
            return [{"id": 1, "display_id": 99, "status": "processing", "completed": False}]
        return [{"id": 1, "display_id": 99, "status": "completed", "completed": True}]

    _install_fake_mythic(monkeypatch, fake_get_all_tasks)

    status = asyncio.run(
        harness.wait_for_task_complete(SimpleNamespace(), 99, timeout_seconds=5, poll_interval_seconds=0.01)
    )

    assert "completed" in status.lower()


def test_wait_for_task_complete_times_out(monkeypatch):
    async def fake_get_all_tasks(mythic, custom_return_attributes):
        return [{"id": 1, "display_id": 99, "status": "processing", "completed": False}]

    _install_fake_mythic(monkeypatch, fake_get_all_tasks)

    with pytest.raises(TimeoutError):
        asyncio.run(
            harness.wait_for_task_complete(SimpleNamespace(), 99, timeout_seconds=0.05, poll_interval_seconds=0.01)
        )


def test_reporter_writes_json_markdown_and_compare_runs(tmp_path, capsys):
    case_records = [
        {
            "id": "list_callbacks",
            "category": "sage",
            "prompt": "prompt",
            "passed": True,
            "score": 1.0,
            "tokens": 100,
            "model_calls": 2,
            "max_prompt": 60,
            "wall_seconds": 1.2,
            "errors": [],
            "recursion_deaths": 0,
            "answer_snippet": "samwell.tarly CASTELBLACK",
            "trace_ids": ["abc"],
            "command_histogram": {"list": 1},
        }
    ]
    report = harness.build_report("2026-06-02T00:00:00Z", "2026-06-02T00:00:01Z", 15, case_records)
    json_path, md_path = harness.write_reports(report, tmp_path)

    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert set(loaded) == {"started", "finished", "sage_cb", "pass_rate", "total_tokens", "avg_tokens", "cases"}
    assert set(loaded["cases"][0]) == {
        "id",
        "category",
        "prompt",
        "passed",
        "score",
        "tokens",
        "model_calls",
        "max_prompt",
        "wall_seconds",
        "errors",
        "recursion_deaths",
        "answer_snippet",
        "trace_ids",
        "command_histogram",
    }
    assert "| Case | Category | Result |" in md_path.read_text(encoding="utf-8")

    newer = harness.build_report(
        "2026-06-02T00:00:02Z",
        "2026-06-02T00:00:03Z",
        15,
        [{**case_records[0], "passed": False, "tokens": 130}],
    )
    new_json, _ = harness.write_reports(newer, tmp_path / "new")
    harness.print_compare(json_path, new_json)
    output = capsys.readouterr().out
    assert "list_callbacks | PASS->FAIL | +30" in output
    assert "Aggregate pass-rate delta:" in output
    assert "Aggregate total-token delta: +30" in output


def test_multi_seed_case_aggregation_uses_population_std():
    case = {"id": "fake", "category": "sage", "prompt": "Find samwell"}
    aggregate = harness.aggregate_case_runs(
        case,
        [
            {"seed": 0, "passed": True, "total_tokens": 100, "wall_seconds": 2.0},
            {"seed": 1, "passed": False, "total_tokens": 120, "wall_seconds": 4.0},
        ],
    )

    assert set(aggregate) == {
        "id",
        "category",
        "prompt",
        "pass_fraction",
        "seeds",
        "incomplete_count",
        "skipped_count",
        "tokens_mean",
        "tokens_std",
        "wall_mean",
    }
    assert aggregate["pass_fraction"] == 0.5
    assert aggregate["tokens_mean"] == 110
    assert aggregate["tokens_std"] == 10
    assert aggregate["wall_mean"] == 3


def test_run_case_status_pass_fail_incomplete(tmp_path, monkeypatch):
    client = SimpleNamespace(client=True)
    case = {"id": "fake", "category": "sage", "prompt": "Find samwell", "expect_all": ["samwell"]}

    async def fake_issue(client, prompt, sage_cb):
        assert prompt == "Find samwell"
        assert sage_cb == 15
        return {"display_id": 99}

    monkeypatch.setattr(harness, "issue_chat_task", fake_issue)
    monkeypatch.setattr(harness.phoenix_reader, "max_trace_rowid", lambda db_path: 1)
    monkeypatch.setattr(
        harness.phoenix_reader,
        "aggregate_metrics",
        lambda db_path, rowids: phoenix_reader.Metrics(42, 1, 30, 0, [], 0),
    )
    monkeypatch.setattr(
        harness.phoenix_reader,
        "token_breakdown",
        lambda db_path, rowids: phoenix_reader.TokenBreakdown(30, 12, 30, 0, 1, 1, {"Supervisor": 42}),
    )
    monkeypatch.setattr(harness.phoenix_reader, "command_histogram", lambda db_path, rowids: {})
    monkeypatch.setattr(harness.phoenix_reader, "span_rows", lambda db_path, rowids: [])

    async def fake_completed(client, display_id, timeout_seconds, poll_interval_seconds=10.0):
        assert display_id == 99
        return "completed"

    def run_once(answer, wait_func=fake_completed, tool_output="tool samwell"):
        settle_calls = []

        async def fake_settle(db_path, pre_rowid, timeout_seconds, poll_interval_seconds):
            settle_calls.append(pre_rowid)
            return [phoenix_reader.TraceSummary(rowid=2, trace_id="t", spans=1, last_span="now")]

        monkeypatch.setattr(harness, "wait_for_task_complete", wait_func)
        monkeypatch.setattr(harness, "wait_for_settled_traces", fake_settle)
        monkeypatch.setattr(harness.phoenix_reader, "extract_answer_with_fallback", lambda db_path, rowids: answer)
        monkeypatch.setattr(harness.phoenix_reader, "tool_outputs", lambda db_path, rowids: tool_output)
        record = asyncio.run(
            harness.run_case(
                client,
                case,
                seed=0,
                db_path=tmp_path / "x.db",
                sage_cb=15,
                timeout_seconds=1,
                poll_interval_seconds=0.01,
                forbid=["traceback"],
            )
        )
        return record, settle_calls

    pass_record, pass_settle_calls = run_once("samwell found")
    assert pass_record["status"] == "pass"
    assert pass_record["passed"]
    assert pass_settle_calls == [1]

    fail_record, fail_settle_calls = run_once("nothing here", tool_output="tool nothing")
    assert fail_record["status"] == "fail"
    assert not fail_record["passed"]
    assert fail_settle_calls == [1]

    async def fake_timeout(client, display_id, timeout_seconds, poll_interval_seconds=10.0):
        raise TimeoutError("still running")

    incomplete_record, incomplete_settle_calls = run_once("samwell found", fake_timeout)
    assert incomplete_settle_calls == []
    assert incomplete_record["status"] == "incomplete"
    assert not incomplete_record["passed"]


def test_aggregate_incomplete_status():
    case = {"id": "x", "category": "c", "prompt": "p"}
    seeds = [
        {"status": "pass", "passed": True, "total_tokens": 100, "wall_seconds": 1.0},
        {"status": "fail", "passed": False, "total_tokens": 120, "wall_seconds": 1.0},
        {"status": "incomplete", "passed": False, "total_tokens": 0, "wall_seconds": 1.0},
    ]

    aggregate = harness.aggregate_case_runs(case, seeds)

    assert aggregate["pass_fraction"] == 0.5
    assert aggregate["incomplete_count"] == 1
    assert "incomplete_count" in aggregate

    all_incomplete = harness.aggregate_case_runs(
        case,
        [
            {"status": "incomplete", "passed": False, "total_tokens": 0, "wall_seconds": 1.0},
            {"status": "incomplete", "passed": False, "total_tokens": 0, "wall_seconds": 1.0},
        ],
    )
    assert all_incomplete["pass_fraction"] is None
    assert all_incomplete["incomplete_count"] == 2


def test_compare_tolerates_null_pass_fraction(tmp_path):
    report = harness.build_report_v2(
        "a",
        "b",
        2,
        [
            {
                "id": "x",
                "category": "c",
                "prompt": "p",
                "pass_fraction": None,
                "incomplete_count": 2,
                "seeds": [],
                "tokens_mean": 0.0,
                "tokens_std": 0.0,
                "wall_mean": 0.0,
            }
        ],
    )
    p1 = tmp_path / "one.json"
    p2 = tmp_path / "two.json"
    p1.write_text(json.dumps(report), encoding="utf-8")
    p2.write_text(json.dumps(report), encoding="utf-8")

    output = harness.compare_reports(p1, p2)

    assert isinstance(output, str)
    assert "x" in output
    assert report["pass_rate"] is None or isinstance(report["pass_rate"], (int, float))


def test_variance_aware_compare_reports_noise_significance_and_v1_compat(tmp_path):
    base_case = {"id": "list_callbacks", "category": "sage", "prompt": "prompt"}
    within_a = harness.aggregate_case_runs(
        base_case,
        [
            {"seed": 0, "passed": True, "total_tokens": 100, "wall_seconds": 1.0},
            {"seed": 1, "passed": True, "total_tokens": 140, "wall_seconds": 1.0},
        ],
    )
    within_b = harness.aggregate_case_runs(
        base_case,
        [
            {"seed": 0, "passed": True, "total_tokens": 110, "wall_seconds": 1.0},
            {"seed": 1, "passed": True, "total_tokens": 150, "wall_seconds": 1.0},
        ],
    )
    significant_b = harness.aggregate_case_runs(
        base_case,
        [
            {"seed": 0, "passed": True, "total_tokens": 180, "wall_seconds": 1.0},
            {"seed": 1, "passed": True, "total_tokens": 180, "wall_seconds": 1.0},
        ],
    )
    within_path = tmp_path / "within-a.json"
    within_new_path = tmp_path / "within-b.json"
    significant_path = tmp_path / "significant.json"
    v1_path = tmp_path / "v1.json"
    within_path.write_text(
        json.dumps(harness.build_report_v2("a", "b", 2, [within_a])),
        encoding="utf-8",
    )
    within_new_path.write_text(
        json.dumps(harness.build_report_v2("a", "b", 2, [within_b])),
        encoding="utf-8",
    )
    significant_path.write_text(
        json.dumps(harness.build_report_v2("a", "b", 2, [significant_b])),
        encoding="utf-8",
    )
    v1_path.write_text(
        json.dumps(
            harness.build_report(
                "a",
                "b",
                15,
                [
                    {
                        "id": "list_callbacks",
                        "category": "sage",
                        "prompt": "prompt",
                        "passed": True,
                        "score": 1.0,
                        "tokens": 100,
                        "model_calls": 1,
                        "max_prompt": 80,
                        "wall_seconds": 1.0,
                        "errors": [],
                        "recursion_deaths": 0,
                        "answer_snippet": "ok",
                        "trace_ids": ["trace"],
                        "command_histogram": {},
                    }
                ],
            )
        ),
        encoding="utf-8",
    )

    assert "within noise" in harness.compare_reports(within_path, within_new_path)
    assert "SIGNIFICANT" in harness.compare_reports(within_path, significant_path)
    mixed = harness.compare_reports(v1_path, within_new_path)
    assert "list_callbacks" in mixed
    assert "Aggregate pass-rate delta:" in mixed


def test_runner_offline_with_mocked_mythic_and_phoenix(tmp_path, monkeypatch):
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        yaml.safe_dump(
            {
                "sage_cb": 15,
                "apollo_cb": 17,
                "default_timeout": 1,
                "forbid": ["traceback"],
                "cases": [
                    {
                        "id": "fake",
                        "category": "sage",
                        "prompt": "Find samwell",
                        "expect_all": ["samwell"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    async def fake_login(password):
        assert password == "from-env"
        return SimpleNamespace(client=True)

    issue_calls = []
    settle_calls = []

    async def fake_issue(client, prompt, sage_cb):
        assert client.client
        assert prompt == "Find samwell"
        assert sage_cb == 15
        issue_calls.append(prompt)
        return {"display_id": 99}

    async def fake_settle(db_path, pre_rowid, timeout_seconds, poll_interval_seconds):
        assert poll_interval_seconds == 0.01
        settle_calls.append(pre_rowid)
        return [phoenix_reader.TraceSummary(rowid=2, trace_id="trace-fake", spans=2, last_span="now")]

    monkeypatch.setenv("MYTHIC_ADMIN_PASSWORD", "from-env")
    monkeypatch.setattr(harness, "login_to_mythic", fake_login)
    monkeypatch.setattr(harness, "issue_chat_task", fake_issue)
    monkeypatch.setattr(harness, "wait_for_task_complete", lambda *args, **kwargs: asyncio.sleep(0, result="completed"))
    monkeypatch.setattr(harness, "wait_for_settled_traces", fake_settle)
    monkeypatch.setattr(harness.phoenix_reader, "max_trace_rowid", lambda db_path: 1)
    monkeypatch.setattr(
        harness.phoenix_reader,
        "aggregate_metrics",
        lambda db_path, rowids: phoenix_reader.Metrics(42, 1, 30, 0, [], 0),
    )
    monkeypatch.setattr(
        harness.phoenix_reader,
        "token_breakdown",
        lambda db_path, rowids: phoenix_reader.TokenBreakdown(30, 12, 30, 0, 1, 1, {"Supervisor": 42}),
    )
    tool_text = "tool retrieved samwell host"
    monkeypatch.setattr(harness.phoenix_reader, "extract_answer_with_fallback", lambda db_path, rowids: "samwell found")
    monkeypatch.setattr(harness.phoenix_reader, "tool_outputs", lambda db_path, rowids: tool_text)
    monkeypatch.setattr(harness.phoenix_reader, "command_histogram", lambda db_path, rowids: {"whoami": 1})
    monkeypatch.setattr(
        harness.phoenix_reader,
        "span_rows",
        lambda db_path, rowids: [
            {
                "agent": "Supervisor",
                "trace_id": "trace-fake",
                "name": "Supervisor.llm",
                "span_kind": "",
                "prompt": "30",
                "completion": "12",
                "status_code": "",
            }
        ],
    )

    report = asyncio.run(
        harness.run_eval(
            cases_path=cases_path,
            db_path=tmp_path / "unused.db",
            out_dir=tmp_path / "out",
            poll_interval_seconds=0.01,
            seeds=2,
        )
    )

    record = report["cases"][0]
    assert report["schema_version"] == 2
    assert report["seeds"] == 2
    assert set(record) == {
        "id",
        "category",
        "prompt",
        "pass_fraction",
        "seeds",
        "incomplete_count",
        "skipped_count",
        "tokens_mean",
        "tokens_std",
        "wall_mean",
    }
    assert record["pass_fraction"] == 1.0
    assert record["tokens_mean"] == 42
    assert record["tokens_std"] == 0.0
    assert len(record["seeds"]) == 2
    assert len(issue_calls) == 2
    assert len(settle_calls) == 2
    for seed_record in record["seeds"]:
        assert set(seed_record) == {
            "seed",
            "status",
            "passed",
            "score",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "est_fixed_floor",
            "est_variable",
            "model_calls",
            "tool_calls",
            "per_agent_tokens",
            "command_histogram",
            "recursion_deaths",
            "errors",
            "wall_seconds",
            "answer_full",
            "answer_snippet",
            "tool_outputs_chars",
            "tool_outputs_snippet",
                "trace_ids",
                "spans",
                "chat_channel_id",
                "chat_request_id",
            }
        assert seed_record["passed"]
        assert seed_record["total_tokens"] == 42
        assert seed_record["answer_full"] == "samwell found"
        assert seed_record["tool_outputs_chars"] == len(tool_text)
        assert seed_record["tool_outputs_snippet"] == tool_text[:500]
        assert "tool_outputs" not in seed_record
        assert seed_record["est_fixed_floor"] == 30
        assert seed_record["est_variable"] == 0
        assert seed_record["per_agent_tokens"] == {"Supervisor": 42}
        assert seed_record["tool_calls"] == 1
        assert seed_record["spans"][0]["agent"] == "Supervisor"
    assert list((tmp_path / "out").glob("eval-*.json"))
    assert list((tmp_path / "out").glob("eval-*.md"))
