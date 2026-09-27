"""Offline checks for bounded Supervisor task-evidence replay."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from langchain_core.load import dumpd
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/replay_supervisor_evidence.py"
SPEC = importlib.util.spec_from_file_location("replay_supervisor_evidence", SCRIPT)
assert SPEC and SPEC.loader
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)

REQUEST = "chat:1:request:2"
CASE = {"name": "stored-output", "operator_request": "Read stored task 7 output.",
        "worker_answer": "The worker reports elevated access; confirmation is missing.",
        "task_output": [{"response_text": "Observed only a status line."}], "task_id": 7}


def fixture_messages():
    answer = "Old specialist report."
    return [
        SystemMessage(content="Old Supervisor prompt." + replay.AUTHORITY_START + "\n[turn-authority] frozen"),
        SystemMessage(content="Sage system context", additional_kwargs={"_seq": 0}),
        HumanMessage(content="Old request", additional_kwargs={"_operator_input": True,
                      "_request_id": REQUEST, "_seq": 1}),
        AIMessage(content="[Mythic_Operator completed task]", name="Mythic_Operator",
                  additional_kwargs={"_is_completion_header": True, "_request_id": REQUEST, "_seq": 7}),
        AIMessage(content=answer, name="Mythic_Operator", additional_kwargs={
            "_worker_final_answer": {"request_id": REQUEST, "worker": "Mythic_Operator",
                                     "source_seq": 6, "sha256": hashlib.sha256(answer.encode()).hexdigest()},
            "_seq": 8}),
        AIMessage(content=replay.REFERENCE_START + "task 3 (historical_or_external)" + replay.REFERENCE_END,
                  name="Mythic_Operator", additional_kwargs={"_worker_task_references": True,
                                                               "_request_id": REQUEST, "_seq": 9}),
        HumanMessage(content=replay.shared.RECORDED_CONTINUATION,
                     additional_kwargs={"_synthetic_nudge": "provider_requires_user_turn",
                                        "_hide_from_stream": True}),
    ]


def fixture_files(tmp_path):
    evidence = tmp_path / "evidence.json"
    source = {"messages": [[dumpd(message) for message in fixture_messages()]]}
    evidence.write_text(json.dumps({"trace_id": "test-trace", "llm_calls": [
        {"input": {"value": "{}"}} for _ in range(3)] + [{"input": {"value": json.dumps(source)}}]}))
    case = tmp_path / "case.json"
    case.write_text(json.dumps(CASE))
    profile = tmp_path / "profile"
    profile.write_text("provider=openai\nmodel=example\nAPI_ENDPOINT=https://example.test/v1\nAPI_KEY=dummy\n")
    return evidence, case, profile


def test_recorded_case_arms_preserve_authority_and_only_add_production_evidence(tmp_path):
    evidence, case_path, _ = fixture_files(tmp_path)
    source, _, raw = replay.recorded_messages(evidence)
    case, case_hash = replay.read_case(case_path)
    baseline = replay.build_messages(source, case, "baseline")
    candidate = replay.build_messages(source, case, "candidate")
    assert case_hash == hashlib.sha256(case_path.read_bytes()).hexdigest()
    assert replay.shared.digest(raw) == replay.shared.digest(json.loads(json.loads(
        evidence.read_text())["llm_calls"][3]["input"]["value"]))
    assert [dumpd(m) for m in baseline] == [dumpd(m) for m in candidate[:6] + candidate[7:]]
    assert [dumpd(m) for m in source] != [dumpd(m) for m in baseline]
    assert baseline[0].content == replay.load_prompt("supervisor") + replay.AUTHORITY_START + "\n[turn-authority] frozen"
    assert [dumpd(m) for m in baseline[1:2] + baseline[3:4]] == [dumpd(m) for m in source[1:2] + source[3:4]]
    assert baseline[2].additional_kwargs == source[2].additional_kwargs
    assert baseline[4].additional_kwargs["_worker_final_answer"]["sha256"] == hashlib.sha256(
        CASE["worker_answer"].encode()).hexdigest()
    assert "task 7 (historical_or_external)" in baseline[5].content
    assert baseline[-1].content == replay.shared.model_module._provider_continuation_text("Supervisor")
    assert candidate[6].additional_kwargs["_worker_task_evidence"] is True
    assert "Observed only a status line." in candidate[6].content
    assert "task_id\": 7" in candidate[6].content


def test_empty_observation_produces_no_candidate_evidence():
    case = {**CASE, "task_output": []}
    baseline = replay.build_messages(fixture_messages(), case, "baseline")
    candidate = replay.build_messages(fixture_messages(), case, "candidate")
    assert [dumpd(message) for message in baseline] == [dumpd(message) for message in candidate]


@pytest.mark.parametrize("change", [
    {"task_id": -1}, {"task_id": True}, {"task_output": {"response_text": "x"}},
    {"task_output": [{"wrong": "x"}]}, {"worker_answer": ""}, {"extra": 1},
])
def test_rejects_malformed_cases(tmp_path, change):
    path = tmp_path / "case.json"
    path.write_text(json.dumps({**CASE, **change}))
    with pytest.raises(ValueError):
        replay.read_case(path)


def test_rejects_recorded_provenance_and_prompt_boundary(tmp_path):
    evidence, _, _ = fixture_files(tmp_path)
    data = json.loads(evidence.read_text())
    source = json.loads(data["llm_calls"][3]["input"]["value"])
    source["messages"][0][4]["kwargs"]["content"] = "tampered answer"
    data["llm_calls"][3]["input"]["value"] = json.dumps(source)
    evidence.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="provenance"):
        replay.recorded_messages(evidence)
    with pytest.raises(ValueError, match="boundary"):
        replay.current_prompt(SystemMessage(content="No authority marker"))


def test_local_completion_selection_uses_runtime_tool_and_never_dispatches_other_tools():
    messages = replay.build_messages(fixture_messages(), CASE, "baseline")
    response = {"status": "completed", "tool_calls": [{"name": "respond_to_user",
                "args": {"reuse_latest_worker_answer": True}}], "invalid_tool_calls": []}
    assert replay.resolve_response(response, messages) == {
        "selection": "completed", "final_answer": CASE["worker_answer"]}
    response["tool_calls"] = [{"name": "transfer_to_Mythic_Operator", "args": {}}]
    assert replay.resolve_response(response, messages)["selection"] == "tool_diversion"
    response["tool_calls"] = []
    assert replay.resolve_response(response, messages)["selection"] == "no_tool_call"
    response["tool_calls"] = [{"name": "respond_to_user", "args": {"final_response": "Ready."}}]
    assert replay.resolve_response(response, messages)["final_answer"] == "Ready."
    response["tool_calls"].append(response["tool_calls"][0])
    assert replay.resolve_response(response, messages)["selection"] == "multiple_tool_calls"
    response["invalid_tool_calls"] = [{"name": "bad"}]
    assert replay.resolve_response(response, messages)["selection"] == "invalid_tool_calls"


@pytest.mark.parametrize("args", [
    {"reuse_latest_worker_answer": "false"},
    {"reuse_latest_worker_answer": 1},
    {"final_response": ["not a string"]},
    {"final_response": None},
])
def test_local_completion_rejects_wrong_argument_types(args):
    messages = replay.build_messages(fixture_messages(), CASE, "baseline")
    response = {"status": "completed", "tool_calls": [{"name": "respond_to_user", "args": args}],
                "invalid_tool_calls": []}
    assert replay.resolve_response(response, messages) == {
        "selection": "invalid_completion", "final_answer": None}


@pytest.mark.parametrize("answer", ["The observed result contradicts the worker's claim.", "Standing by."])
def test_direct_answer_is_terminal_text_without_certifying_its_quality(answer):
    response = {"status": "completed", "content": answer, "tool_calls": [], "invalid_tool_calls": []}
    assert replay.resolve_response(response, []) == {
        "selection": "direct_answer", "final_answer": answer}


@pytest.mark.parametrize("content", ["", "  ", None])
def test_absent_direct_answer_remains_distinct_from_terminal_text(content):
    response = {"status": "completed", "content": content, "tool_calls": [], "invalid_tool_calls": []}
    assert replay.resolve_response(response, []) == {
        "selection": "no_tool_call", "final_answer": None}


def test_existing_output_prevents_provider_call(tmp_path, monkeypatch):
    evidence, case, profile = fixture_files(tmp_path)
    output = tmp_path / "existing.json"
    output.write_text("keep")

    async def forbidden(*args, **kwargs):
        raise AssertionError("provider request must not run")

    monkeypatch.setattr(replay.shared, "run_once", forbidden)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--evidence", str(evidence),
                                     "--case-file", str(case), "--profile", str(profile),
                                     "--arm", "candidate", "--run", "--output", str(output)])
    with pytest.raises(FileExistsError):
        replay.main()
    assert output.read_text() == "keep"
