import importlib.util
import json
from pathlib import Path
import sqlite3
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "supervised_cost.py"
SPEC = importlib.util.spec_from_file_location("supervised_cost", SCRIPT)
cost = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cost
SPEC.loader.exec_module(cost)


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _fixture(tmp_path, *, issued=False, duplicate_snapshot=False):
    db = tmp_path / "phoenix.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY,trace_id TEXT)")
        con.execute("CREATE TABLE spans (id INTEGER PRIMARY KEY,trace_rowid INTEGER,span_id TEXT,parent_id TEXT,name TEXT,span_kind TEXT,start_time TEXT,end_time TEXT,llm_token_count_prompt INTEGER,llm_token_count_completion INTEGER,attributes TEXT)")
        con.execute("INSERT INTO traces VALUES (1,'trace-1')")
        con.execute("INSERT INTO spans VALUES (1,1,'root',NULL,'Mythic Operator','CHAIN','2026-10-06 21:00:00','2026-10-06 21:01:00',NULL,NULL,'{}')")
        attrs = {"metadata": {"lc_agent_name": "Worker", "thread_id": "3:generation:fixture"},
                 "llm": {"model_name": "fixture", "provider": "fixture", "input_messages": [
            {"message": {"role": "system", "content": "private fixture secret"}}, {"message": {"role": "user", "content": "b"}}],
            "tools": [{"tool": {"json_schema": "private fixture schema"}}], "output_messages": [{"message": {"role": "assistant", "tool_calls": [{"tool_call": {"function": {"name": "issue_task_and_waitfor_task_output", "arguments": "private fixture args"}}}]}}]}}
        for sid, start, end in [("llm-a", "21:00:05", "21:00:30"), ("llm-b", "21:00:20", "21:00:40")]:
            con.execute("INSERT INTO spans(trace_rowid,span_id,parent_id,name,span_kind,start_time,end_time,llm_token_count_prompt,llm_token_count_completion,attributes) VALUES (1,?,?,?,?,?,?,?,?,?)",
                        (sid, "root", "ChatModel", "LLM", f"2026-10-06 {start}", f"2026-10-06 {end}", 10, 2, json.dumps(attrs)))
    user = {"id": 1, "channel_id": 3, "chat_request_id": None, "created_at": "2026-10-06T21:00:00", "updated_at": "2026-10-06T21:00:00", "metadata": {}}
    data = {"tool_name": "issue_task_and_waitfor_task_output"}
    card = {"id": 2, "channel_id": 3, "chat_request_id": 7, "created_at": "2026-10-06T21:00:10", "updated_at": "2026-10-06T21:00:12", "metadata": {"input_requested": {"data": data}}}
    messages = [user, card]
    if issued:
        messages.append({"id": 3, "channel_id": 3, "chat_request_id": 7, "created_at": "2026-10-06T21:00:52", "updated_at": "2026-10-06T21:00:55", "metadata": {"tool_use": {"tool_name": "issue_task_and_waitfor_task_output", "result_preview": "[Mythic task issuance identity] task_display_id=9 callback_display_id=2"}}})
    request = {"id": 7, "channel_id": 3, "request_message_id": 1, "status": "complete", "updated_at": "2026-10-06T21:01:00"}
    native = _write(tmp_path / "native.json", {"request": request, "messages": messages})
    native_paths = [native]
    if duplicate_snapshot:
        older = dict(card, updated_at="2026-10-06T21:00:11", metadata={"input_requested": {"data": {"tool_name": "stale"}}})
        native_paths.insert(0, _write(tmp_path / "older.json", {"request": request, "messages": [user, older]}))
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"event": "native-card-decision", "request_id": 7, "message_id": 2,
                                  "card_digest": cost.digest(data), "decision": "accept", "at": "2026-10-06T21:00:50Z"}) + "\n")
    tasks = _write(tmp_path / "tasks.json", {"observed_at": "2026-10-06T21:02:00Z", "tasks": ([{"display_id": 9, "timestamp": "2026-10-06T21:00:53", "completed": True, "status": "success"}] if issued else [])})
    meter = tmp_path / "meter.jsonl"
    meter.write_text("\n".join(map(json.dumps, [
        {"type": "reservation", "reservation_id": "r1", "amounts": {"provider_tokens": 100}},
        {"type": "provider_settlement", "reservation_id": "r1", "actual_tokens": 24},
        {"type": "reservation", "reservation_id": "r2", "amounts": {"provider_tokens": 100}}])) + "\n")
    return db, native_paths, events, tasks, meter


def _report(args):
    db, native, events, tasks, meter = args
    return cost.build_report(db, "2026-10-06T21:00:00Z", "2026-10-06T21:02:00Z", native, [events], tasks, [meter])


def test_overlap_is_partitioned_and_accept_without_issue_is_not_execution(tmp_path):
    report = _report(_fixture(tmp_path))
    assert report["approvals"][0]["execution_state"] == "accepted_no_issue_evidence"
    assert report["approvals"][0]["card_digest_match"] is True
    wall = report["requests"][0]["wall_partition"]
    assert wall == {"model_active_seconds": 35.0, "awaiting_native_decision_seconds": 10.0, "remainder_seconds": 15.0}
    assert report["meter"] == {"provider_reservations": 2, "provider_settlements": 1, "settled_tokens": 24, "unsettled_provider_reservations": 1}
    assert report["calls"][0]["exact_token_component_split"] is None
    assert report["calls"][0]["retained_tool_schema_bytes"] > 0
    assert report["calls"][0]["output_tool_names"] == ["issue_task_and_waitfor_task_output"]
    assert report["aggregates"]["workers"]["Worker"]["calls"] == 2
    assert report["aggregates"]["requests"]["7"]["calls"] == 2
    assert report["aggregates"]["exact_repeated_tool_call_fingerprints"] == 1
    assert report["global_wall"]["partition"]["model_active_seconds"] == 35.0
    assert report["usage_reconciliation"] == {
        "traced_llm_spans": 2, "usage_bearing_llm_spans": 2,
        "meter_provider_settlements": 1, "usage_bearing_count_equals_settlement_count": False,
        "count_equality_is_not_per_call_settlement_proof": True}
    assert "private fixture" not in json.dumps(report)


def test_duplicate_snapshot_keeps_latest_and_exact_task_receipt(tmp_path):
    report = _report(_fixture(tmp_path, issued=True, duplicate_snapshot=True))
    assert report["approvals"][0]["tool_name"] == "issue_task_and_waitfor_task_output"
    assert report["approvals"][0]["execution_state"] == "issued_temporal_match"
    assert report["approvals"][0]["task_ids"] == [9]
    assert report["tasks"][0]["response_timestamp"] is None
    assert report["tasks"][0]["observed_at"] == "2026-10-06T21:02:00Z"


def test_sparse_telemetry_and_no_arbitrary_number_as_task(tmp_path):
    args = _fixture(tmp_path, issued=False)
    native = json.loads(args[1][0].read_text())
    native["messages"].append({"id": 3, "channel_id": 3, "chat_request_id": 7, "created_at": "2026-10-06T21:00:52", "metadata": {"tool_use": {"tool_name": "issue_task_and_waitfor_task_output", "result_preview": "failure code 9"}}})
    native["runtime_telemetry"] = {}
    _write(args[1][0], native)
    report = _report(args)
    assert report["issue_cards"] == []
    assert report["approvals"][0]["execution_state"] == "accepted_no_issue_evidence"


def test_concurrent_channel_does_not_inflate_request_model_time(tmp_path):
    args = _fixture(tmp_path)
    second = {"request": {"id": 8, "channel_id": 4, "request_message_id": 10,
                          "status": "complete", "updated_at": "2026-10-06T21:01:00"},
              "messages": [{"id": 10, "channel_id": 4, "chat_request_id": None,
                            "created_at": "2026-10-06T21:00:00", "updated_at": "2026-10-06T21:00:00",
                            "metadata": {}}], "runtime_telemetry": {}}
    args[1].append(_write(tmp_path / "other_channel.json", second))
    report = _report(args)
    other = next(x for x in report["requests"] if x["channel_id"] == 4)
    assert other["wall_partition"]["model_active_seconds"] == 0.0
    assert other["wall_partition"]["remainder_seconds"] == 60.0


def test_accepted_control_action_cannot_claim_unbound_task(tmp_path):
    args = _fixture(tmp_path)
    native = json.loads(args[1][0].read_text())
    control = {"tool_name": "ensure_tool_uploaded"}
    native["messages"].append({"id": 4, "channel_id": 3, "chat_request_id": 7,
                               "created_at": "2026-10-06T21:00:51", "updated_at": "2026-10-06T21:00:51",
                               "metadata": {"input_requested": {"data": control}}})
    _write(args[1][0], native)
    with args[2].open("a") as handle:
        handle.write(json.dumps({"event": "native-card-decision", "request_id": 7, "message_id": 4,
                                 "card_digest": cost.digest(control), "decision": "accept",
                                 "at": "2026-10-06T21:00:55Z"}) + "\n")
    _write(args[3], {"observed_at": "2026-10-06T21:02:00Z", "tasks": [
        {"display_id": 9, "timestamp": "2026-10-06T21:00:56", "completed": True, "status": "success"}]})
    report = _report(args)
    assert [x["execution_state"] for x in report["approvals"]] == [
        "accepted_no_issue_evidence", "accepted_non_task_action"]
    assert report["join_gaps"]["task_ids_without_issue_card"] == [9]
