"""Offline guards for the recorded specialist answer replay."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock

from langchain_core.load import dumpd
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/replay_specialist_grounding.py"
SPEC = importlib.util.spec_from_file_location("replay_specialist_grounding", SCRIPT)
assert SPEC and SPEC.loader
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


def _evidence(tmp_path: Path) -> Path:
    system = SystemMessage(content="prefix\n" + replay.START + " Old instruction.\n" + replay.END + "\ntail")
    operator = HumanMessage(content="read the stored output", additional_kwargs={"_operator_input": True})
    tool_call = AIMessage(content="", name="Mythic_Operator", tool_calls=[
        {"name": "get_all_task_output_by_task_id", "args": {"task_id": 3}, "id": "call-1"}])
    tool_result = ToolMessage(content='{"task_output":"sample"}',
                              name="get_all_task_output_by_task_id", tool_call_id="call-1")
    inputs = [[], [system, operator], [system, operator, tool_call, tool_result]]
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({"trace_id": "offline", "llm_calls": [
        {"input": {"value": json.dumps({"messages": [[dumpd(m) for m in messages]]})}}
        for messages in inputs]}))
    return path


def test_stored_case_preserves_all_recorded_messages(tmp_path: Path):
    messages, evidence, case = replay.evidence_messages(_evidence(tmp_path))
    recorded = replay._recorded_messages(evidence, 2)
    assert [dumpd(m) for m in messages] == [dumpd(m) for m in recorded]
    assert case == {"name": "recorded_task_output", "call_index": 2, "case_sha256": None}


def test_candidate_changes_only_evidence_scope_paragraph(tmp_path: Path):
    messages, _, _ = replay.evidence_messages(_evidence(tmp_path))
    prompt = tmp_path / "operator.md"
    prompt.write_text("other source\n" + replay.START + " New instruction.\n" + replay.END)
    revised = replay.candidate_messages(messages, prompt)
    assert revised[0].content == messages[0].content.replace("Old instruction.", "New instruction.")
    assert [dumpd(m) for m in revised[1:]] == [dumpd(m) for m in messages[1:]]
    assert messages[0].content.endswith("tail")


@pytest.mark.parametrize("candidate", [
    "without start" + replay.END,
    replay.START + " changed without end",
    replay.START + " changed" + replay.START + replay.END,
    replay.END + replay.START,
    replay.START + " Old instruction.\n" + replay.END,
])
def test_candidate_rejects_missing_ambiguous_or_unchanged_scope(tmp_path: Path, candidate: str):
    messages, _, _ = replay.evidence_messages(_evidence(tmp_path))
    prompt = tmp_path / "operator.md"
    prompt.write_text(candidate)
    with pytest.raises(ValueError):
        replay.candidate_messages(messages, prompt)


def test_synthetic_fixture_replaces_only_operator_question(tmp_path: Path):
    evidence_path = _evidence(tmp_path)
    case_file = tmp_path / "case.json"
    case_file.write_text(json.dumps({"name": "one-token-fixture",
                                     "operator_request": "Interpret this named primary token only."}))
    messages, evidence, case = replay.evidence_messages(evidence_path, case_file)
    original = replay._recorded_messages(evidence, 1)
    assert len(messages) == 2 and case["call_index"] == 1 and case["case_sha256"]
    assert dumpd(messages[0]) == dumpd(original[0])
    assert messages[1].content == "Interpret this named primary token only."
    assert messages[1].additional_kwargs == original[1].additional_kwargs
    assert original[1].content == "read the stored output"


def test_synthetic_fixture_rejects_extra_or_empty_fields(tmp_path: Path):
    evidence_path = _evidence(tmp_path)
    case_file = tmp_path / "case.json"
    case_file.write_text(json.dumps({"name": "case", "operator_request": " ", "extra": "x"}))
    with pytest.raises(ValueError, match="case file requires"):
        replay.evidence_messages(evidence_path, case_file)


def test_production_tool_capture_has_expected_tool_and_slimmed_descriptions():
    tools = replay.specialist_tools()
    assert "get_all_task_output_by_task_id" in {tool.name for tool in tools}
    assert all(tool.description == replay.shared.model_module._slim_tool_description(tool.description)
               for tool in tools)
    assert len(tools) == len(replay.get_prompt_tools("mythic_operator"))


def test_existing_output_path_prevents_provider_call(tmp_path: Path, monkeypatch):
    evidence = _evidence(tmp_path)
    profile = tmp_path / "profile"
    profile.write_text("provider=openai\nmodel=example\nAPI_ENDPOINT=http://127.0.0.1:9/v1\nAPI_KEY=dummy\n")
    output = tmp_path / "existing.json"
    output.write_text("preserve me")
    forbidden = AsyncMock(side_effect=AssertionError("provider called"))
    monkeypatch.setattr(replay.shared, "run_once", forbidden)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--evidence", str(evidence),
                                  "--arm", "baseline", "--profile", str(profile),
                                  "--run", "--output", str(output)])
    with pytest.raises(FileExistsError):
        replay.main()
    forbidden.assert_not_awaited()
    assert output.read_text() == "preserve me"
