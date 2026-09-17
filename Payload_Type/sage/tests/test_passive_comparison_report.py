"""File-only reporting must retain missing/failing observations without provider access."""
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import socket

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-eval-gauge/scripts/summarize_comparison.py"
SPEC = importlib.util.spec_from_file_location("passive_comparison_report", SCRIPT)
reporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reporter)


def card(disposition):
    result = asdict(reporter.ScoreCard(
        scenario="fixture", verifier_hash="fixture", furthest_milestone="FOOTHOLD", capability=0,
        milestones_reached=0, milestones={}, substring_score=0, metric_capability_gap=0,
        tool_calls=2, model_calls=3, productive_action_ratio=0, turn_efficiency=0,
        status="timeout", recursion_deaths=0, error_count=0, total_tokens=1_000_010,
        wall_seconds=2700, objective_proven=disposition == "proven",
        objective_proof_disposition=disposition,
    ))
    result["cost_countable"] = {name: True for name in ("tool_calls", "model_calls", "total_tokens", "wall_seconds")}
    return result


def result(side, disposition):
    return {"record_type": "result", "side": side, "scenario": "fixture", "card": card(disposition)}


def test_real_file_entrypoint_reports_failure_cost_and_attempts_without_network(tmp_path, monkeypatch, capsys):
    def no_network(*args, **kwargs):
        pytest.fail("passive report attempted network access")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    records = [result("bare", "disproven"), result("harness", "proven"),
               {"record_type": "attempt", "scenario": "fixture", "error": "PRIVATE FAILURE"},
               {"record_type": "cleanup", "scenario": "fixture", "complete": False}]
    source = tmp_path / "results.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in records))
    before = source.read_bytes()
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([{"operation_id": 41, "display_id": 9, "completed": False,
                                 "original_params": "PRIVATE TASK"}]))
    assert reporter.main(["--results", str(source), "--scenario", "fixture",
                          "--task-states", str(tasks), "--operation-id", "41"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert "PRIVATE" not in output
    assert report["record_types"] == {"result": 2, "attempt": 1, "cleanup": 1}
    assert report["comparison"]["bare_outcomes"]["disproven"] == 1
    assert report["comparison"]["bare_cost_mean"]["total_tokens"] == 1_000_010
    assert report["comparison"]["resolved"] is False
    assert report["protocol_validity"] == "not_assessed"
    assert report["task_snapshot"]["task_quiescent"] is False
    assert source.read_bytes() == before


def test_old_card_without_disposition_is_not_relabelled_as_failure():
    row = result("bare", "disproven")
    del row["card"]["objective_proof_disposition"]
    report = reporter.summarize([row, result("harness", "unmeasured")], "fixture")
    assert report["comparison"]["bare_outcomes"]["unmeasured"] == 1
    assert report["comparison"]["bare_outcomes"]["disproven"] == 0
    assert "objective_proof_disposition" not in row["card"]


@pytest.mark.parametrize("disposition", ["proven", "disproven"])
def test_old_card_retained_terminal_proof_remains_usable(disposition):
    row = result("bare", disposition)
    del row["card"]["objective_proof_disposition"]
    row["card"]["milestones"] = {"FOOTHOLD": True, "OBJECTIVE": disposition == "proven"}
    row["card"]["proof_dispositions"] = {"FOOTHOLD": "proven", "OBJECTIVE": disposition}
    report = reporter.summarize([row], "fixture")
    assert report["comparison"]["bare_outcomes"][disposition] == 1
    assert "objective_proof_disposition" not in row["card"]


def test_bad_input_reports_location_without_echoing_content(tmp_path, capsys):
    source = tmp_path / "results.jsonl"
    source.write_text('SECRET INVALID JSON\n' + json.dumps(result("bare", "disproven")))
    assert reporter.main(["--results", str(source), "--scenario", "fixture"]) == 2
    output = capsys.readouterr().out
    assert "SECRET" not in output
    report = json.loads(output)
    assert report["invalid_record_lines"] == [1]
    assert report["comparison"]["bare_outcomes"]["disproven"] == 1
    assert report["comparison"]["resolved"] is False


@pytest.mark.parametrize("mutation", [
    {"side": []}, {"card": None}, {"card": {}}, {"side": "unexpected"},
])
def test_malformed_result_cannot_disappear_into_a_resolved_summary(mutation):
    row = {**result("bare", "disproven"), **mutation}
    report = reporter.summarize([row], "fixture")
    assert report["invalid_record_lines"] == [1]
    assert report["comparison"]["verdict"] == "incomplete-result-records"


def test_missing_file_is_sanitized(tmp_path, capsys):
    assert reporter.main(["--results", str(tmp_path / "missing"), "--scenario", "fixture"]) == 2
    assert json.loads(capsys.readouterr().out) == {"error": "input_file_unreadable"}
