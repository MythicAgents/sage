"""File-only reporting must retain missing/failing observations without provider access."""
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import socket

import pytest
from ai.hillclimb import experiment_contracts

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


def _label(side):
    return {
        "protocol_id": "fixture-protocol", "pair_id": "fixture-pair", "logical_seed": 0,
        "policy_arm": side, "model_provider": "fixture-provider", "model_id": "fixture-model",
        "model_version": "fixture-version", "backend_route": "fixture-route",
        "generation_config": {"temperature": 0}, "wrapper_id": "fixture-wrapper",
        "wrapper_revision": "fixture-revision", "authority_mode": "auto",
        "execution_path": "true_naive_raw_tools" if side == "bare" else "policy_selected_controller",
        "source_version": "fixture-source",
    }


def _typed_result(attempt_id, side, disposition, *, evidence_state="admitted", supersedes="", amendment_reason=""):
    attempt = experiment_contracts.AttemptContract(
        attempt_id=attempt_id, experiment_id="fixture", campaign_id="fixture", treatment_id=side,
        evidence_state=evidence_state, supersedes_attempt_id=supersedes, amendment_reason=amendment_reason,
        source={"ordinary_result": True}, cost=experiment_contracts.CostMeasurement(),
    )
    row = {**attempt.to_dict(), **result(side, disposition)}
    row["record_hash"] = experiment_contracts.content_hash(row)
    return row


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
                          "--task-states", str(tasks), "--operation-id", "41"]) == 2
    output = capsys.readouterr().out
    report = json.loads(output)
    assert "PRIVATE" not in output
    assert report["record_types"] == {"result": 2, "attempt": 1, "cleanup": 1}
    assert report["comparison"] == {"available": False, "reason": "measurement_labels_unavailable"}
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


def test_labeled_cli_may_render_the_explicitly_descriptive_comparison(tmp_path, capsys):
    rows = [result("bare", "disproven"), result("harness", "proven")]
    for row in rows:
        label = _label(row["side"])
        row["measurement_identity"] = label
        row["card"]["measurement_identity"] = label
    source = tmp_path / "labeled-results.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert reporter.main(["--results", str(source), "--scenario", "fixture"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["measurement"]["performance_assertion_allowed"] is True
    assert "bare_outcomes" in report["comparison"]


def test_summary_selects_only_admitted_amendment_heads_and_excludes_burned_rows():
    rows = [
        _typed_result("burned", "bare", "disproven", evidence_state="burned"),
        _typed_result("bare-old", "bare", "disproven"),
        _typed_result("bare-amended", "bare", "proven", supersedes="bare-old", amendment_reason="corrected evidence"),
        _typed_result("harness", "harness", "proven"),
        {"record_type": "arm_started", "scenario": "fixture", "event": "informational"},
    ]
    report = reporter.summarize(rows, "fixture")
    assert report["comparison"]["bare_outcomes"]["proven"] == 1
    assert report["comparison"]["bare_outcomes"]["disproven"] == 0
    assert report["comparison"]["harness_outcomes"]["proven"] == 1


def test_orphan_typed_lifecycle_is_refused_by_summary_and_cli(tmp_path, capsys):
    rows = [_typed_result("orphan", "bare", "proven", supersedes="missing", amendment_reason="incorrect source")]
    with pytest.raises(ValueError, match="ambiguous or invalid"):
        reporter.summarize(rows, "fixture")
    source = tmp_path / "orphan.jsonl"
    source.write_text(json.dumps(rows[0]), encoding="utf-8")
    assert reporter.main(["--results", str(source), "--scenario", "fixture"]) == 2
    assert json.loads(capsys.readouterr().out)["comparison"] == {
        "available": False, "reason": "comparison_lifecycle_unavailable",
    }


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
    assert report["comparison"] == {"available": False, "reason": "measurement_labels_unavailable"}


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
