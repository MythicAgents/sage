"""Offline tests for the additive paired model decision benchmark."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import decision_benchmark as db  # noqa: E402


def _answer(case: db.DecisionCase, **overrides):
    answer = {
        "failure_label": case.expected.failure_label,
        "repair_kind": case.expected.repair_kind,
        "next_action": case.expected.next_action,
        "should_recollect": case.expected.should_recollect,
        "rationale": "bounded repair follows the current evidence",
    }
    answer.update(overrides)
    return answer


def test_default_cases_load_and_cover_retry_vs_recollect():
    cases = db.load_cases()

    assert len(cases) >= 10
    assert any(case.expected.should_recollect for case in cases)
    assert any(not case.expected.should_recollect for case in cases)
    assert {case.category for case in cases} >= {"failure_repair", "control_decision"}


def test_fixture_rejects_secret_like_text():
    raw = {
        "id": "secret_fixture",
        "category": "failure_repair",
        "objective": "Do not persist password=Hunter2 in eval data.",
        "state": "Current state is redacted.",
        "observation": "Operation failed.",
        "available_actions": ["Stop."],
        "candidate_failure_labels": ["repeated_no_progress"],
        "candidate_repairs": ["stop_replanning_and_surface_blocker"],
        "candidate_next_actions": ["stop_and_surface_blocker"],
        "expected": {
            "failure_label": "repeated_no_progress",
            "repair_kind": "stop_replanning_and_surface_blocker",
            "next_action": "stop_and_surface_blocker",
            "should_recollect": False,
        },
    }

    with pytest.raises(ValueError, match="unredacted secret-like material"):
        db.DecisionCase.from_dict(raw)


def test_score_response_accepts_fenced_json_and_redacts_persisted_answer():
    case = db.load_cases()[0]
    raw = "```json\n" + json.dumps(_answer(case, rationale="password=Hunter2")) + "\n```"

    score = db.score_response(case, raw)

    assert score.parse_ok is True
    assert score.schema_ok is True
    assert score.fully_correct is True
    assert "<password:redacted>" in score.raw_response
    assert score.parsed_answer["rationale"] == "password=<password:redacted>"


def test_score_response_rejects_extra_schema_keys():
    case = db.load_cases()[0]
    raw = json.dumps(_answer(case, extra="not allowed"))

    score = db.score_response(case, raw)

    assert score.parse_ok is True
    assert score.schema_ok is False
    assert score.fully_correct is False


def test_dry_run_benchmark_is_perfect_and_round_trips(tmp_path):
    cases = db.load_cases()
    spec = db.ModelSpec(name="dry", provider="openai", model="dry-model", api_key="never-persist")

    run = db.run_benchmark(cases, [spec], invoker_factory=db.make_dry_run_invoker)
    out = tmp_path / "decision.jsonl"
    db.append_run(out, run)
    loaded = db.load_runs(out)[0]

    assert run.model_results[0].summary.full_accuracy == 1.0
    assert loaded.model_results[0].summary.schema_success_rate == 1.0
    assert loaded.model_results[0].spec["name"] == "dry"
    assert "api_key" not in loaded.model_results[0].spec


def test_comparison_report_surfaces_differing_case_and_expected_answer():
    cases = db.load_cases()
    specs = [
        db.ModelSpec(name="strong", provider="openai", model="strong"),
        db.ModelSpec(name="weak", provider="openai", model="weak"),
    ]

    def invoker_factory(spec):
        def invoke(case, _system, _prompt):
            answer = _answer(case)
            if spec.name == "weak" and case.id == "objective_scope_expansion_collect":
                answer["next_action"] = "stop_and_surface_blocker"
            return json.dumps(answer)

        return invoke

    run = db.run_benchmark(cases, specs, invoker_factory=invoker_factory)
    pair = db.comparison_report(run)["pairwise"][0]
    diff = next(item for item in pair["differing_cases"] if item["case_id"] == "objective_scope_expansion_collect")

    assert pair["full_accuracy_delta"] > 0
    assert diff["left_full"] is True
    assert diff["right_full"] is False
    assert diff["expected"]["next_action"] == "recollect_graph"


def test_comparison_report_ignores_rationale_only_differences():
    case = db.load_cases()[0]
    specs = [
        db.ModelSpec(name="left", provider="openai", model="left"),
        db.ModelSpec(name="right", provider="openai", model="right"),
    ]

    def invoker_factory(spec):
        def invoke(_case, _system, _prompt):
            return json.dumps(_answer(case, rationale=f"{spec.name} rationale"))

        return invoke

    run = db.run_benchmark([case], specs, invoker_factory=invoker_factory)

    assert db.comparison_report(run)["pairwise"][0]["differing_cases"] == []


def test_combine_runs_supports_manual_one_model_at_a_time_workflow():
    cases = db.load_cases()
    left = db.run_benchmark(
        cases,
        [db.ModelSpec(name="early", provider="openai", model="early")],
        invoker_factory=db.make_dry_run_invoker,
    )
    right = db.run_benchmark(
        cases,
        [db.ModelSpec(name="prior", provider="openai", model="prior")],
        invoker_factory=db.make_dry_run_invoker,
    )

    combined = db.combine_runs([left, right])

    assert combined.cases_hash == left.cases_hash == right.cases_hash
    assert [result.spec["name"] for result in combined.model_results] == ["early", "prior"]


def test_combine_runs_rejects_case_hash_mismatch():
    cases = db.load_cases()
    full = db.run_benchmark(
        cases,
        [db.ModelSpec(name="full", provider="openai", model="full")],
        invoker_factory=db.make_dry_run_invoker,
    )
    subset = db.run_benchmark(
        cases[:1],
        [db.ModelSpec(name="subset", provider="openai", model="subset")],
        invoker_factory=db.make_dry_run_invoker,
    )

    with pytest.raises(ValueError, match="different case hashes"):
        db.combine_runs([full, subset])
