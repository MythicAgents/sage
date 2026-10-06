"""Assembled offline native-chat checks for the development report projection."""

import asyncio
import importlib.util
import json
from pathlib import Path
import socket

from langchain_core.messages import AIMessage
import pytest


ROOT = Path(__file__).resolve().parents[3]
MATRIX = ROOT / "skills/sage-focused-capability-tests/scripts/run_operator_prompt_matrix.py"
CANDIDATE = ROOT / "skills/sage-focused-capability-tests/scripts/report_projection_candidate.py"
PROFILE = {"provider": "openai", "model": "synthetic-test",
           "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native_setup(tmp_path, monkeypatch):
    matrix = load(MATRIX, "projection_native_matrix")
    candidate = load(CANDIDATE, "projection_native_candidate")
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    limits = type("Limits", (), {"max_case_calls": 10, "max_calls": 20,
        "max_case_tokens": 10000, "max_tokens": 200000, "output_tokens": 512,
        "request_seconds": 5, "max_wall": 30})()
    return matrix, candidate, limits


def run_scripted(matrix, candidate_module, limits, monkeypatch, tmp_path,
                 case_id, prompt, messages, decision=None):
    fake = matrix.CASE.ScriptedModel(messages=iter(messages))
    monkeypatch.setattr(matrix.model_module, "init_chat_model", lambda *a, **kw: fake)
    adapter = candidate_module.ReportProjectionCandidate()
    with candidate_module.install(adapter, matrix_module=matrix):
        result = asyncio.run(matrix.run_case(case_id, prompt, decision, PROFILE,
            tmp_path, limits, None, {"127.0.0.1"}))
    return result, adapter.audit()


def complete_text(emissions):
    return "\n".join(str(row.get("content") or "") for row in emissions
                     if row.get("complete_request"))


@pytest.mark.parametrize("supervisor_mode", ["reuse", "synthesis", "plain"])
def test_task811_positive_survives_worker_and_native_final(tmp_path, monkeypatch, supervisor_mode):
    matrix, candidate, limits = native_setup(tmp_path, monkeypatch)
    prompt = next(text for case_id, text, _ in matrix.SUPPLEMENTAL_PROMPTS
                  if case_id == "09_direct_evidence")
    worker = '{"proposals":[]}'
    if supervisor_mode == "reuse":
        supervisor = matrix.CASE.call("respond_to_user", {"reuse_latest_worker_answer": True}, 3)
    elif supervisor_mode == "synthesis":
        supervisor = matrix.CASE.call("respond_to_user", {"final_response": worker}, 3)
    else:
        supervisor = AIMessage(content=worker)
    messages = [
        matrix.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "inspect task 811", "handoff_instruction": prompt}, 1),
        matrix.CASE.call("get_all_task_output_by_task_id", {"task_id": 811}, 2),
        AIMessage(content=worker), supervisor,
    ]
    result, audit = run_scripted(matrix, candidate, limits, monkeypatch, tmp_path,
                                  "09_direct_evidence", prompt, messages)
    assert result["status"] == "observed", result.get("error")
    assert result["initial_cards"] == result["effects"] == []
    assert {row["tool"] for row in audit["observations"]} == {"get_all_task_output_by_task_id"}
    assert audit["observations"][0]["arguments"] == {"task_id": 811}
    visible = json.dumps(result["turns"][0]["emissions"], ensure_ascii=False)
    final = complete_text(result["turns"][0]["emissions"])
    for text in ("PartOfDomain: True", "Domain: example.test", "Trust inventory: not collected"):
        assert text in visible
        assert text in final
    assert "No next action proposed" in final
    assert any(event["surface"].startswith("stream:") and
               "PartOfDomain: True" in event["displayed"] for event in audit["events"])
    surfaces = {event["surface"] for event in audit["events"]}
    assert "worker_agent_text" in surfaces
    assert "service_final" in surfaces
    assert any(surface.startswith("delegation_") for surface in surfaces)
    assert not audit["candidate_format_failures"]


def test_stored_observations_and_conditional_followup_complete(tmp_path, monkeypatch):
    matrix, candidate, limits = native_setup(tmp_path, monkeypatch)
    prompt = next(text for case_id, text, _ in matrix.SUPPLEMENTAL_PROMPTS
                  if case_id == "09_stored_enumeration")
    task_reads = AIMessage(content="", tool_calls=[
        {"name": "get_all_task_output_by_task_id", "args": {"task_id": task_id},
         "id": f"task_{task_id}", "type": "tool_call"}
        for task_id in (701, 702, 703)])
    proposal = json.dumps({"proposals": [{"verify": ["domain membership is established"],
        "if_verified": "inspect an authorized directory collection path"}]})
    messages = [
        matrix.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "Known domain administrator account", "handoff_instruction": prompt}, 1),
        matrix.CASE.call("list_callbacks", {}, 2),
        matrix.CASE.call("get_task_history_for_callback", {"callback_display_id": 7}, 3),
        task_reads,
        AIMessage(content='{"proposals":[]}'),
        matrix.CASE.call("respond_to_user", {"reuse_latest_worker_answer": True}, 4),
        AIMessage(content=proposal),
    ]
    result, audit = run_scripted(matrix, candidate, limits, monkeypatch, tmp_path,
                                  "09_stored_enumeration", prompt, messages)
    assert result["status"] == "observed", result.get("error")
    followup = json.loads((tmp_path / "case_10_stored_recommend.json").read_text())
    assert followup["status"] == "observed", followup.get("error")
    assert result["initial_cards"] == result["effects"] == followup["effects"] == []
    first_visible = json.dumps(result["turns"][0]["emissions"], ensure_ascii=False)
    assert "Known domain administrator account" not in first_visible
    assert "Review with Mythic_Operator" in first_visible
    assert any(row["surface"] == "delegation_title" and
               row["raw"] == "Known domain administrator account" and
               row["displayed"] == "Review with Mythic_Operator"
               for row in audit["events"])
    assert {row["arguments"]["task_id"] for row in audit["observations"]
            if row["tool"] == "get_all_task_output_by_task_id"} == {701, 702, 703}
    final = complete_text(followup["emissions"])
    for text in ("LAB\\\\analyst", "explorer.exe", "powershell.exe", "User accounts for",
                 "collected_request=", "task_id"):
        assert text in final
    assert "Before acting, verify domain membership is established" in final
    assert "If verified, inspect an authorized directory collection path" in final
    assert not audit["candidate_format_failures"]


def test_unsupported_old_prose_blocked_on_all_views_with_format_failure(tmp_path, monkeypatch):
    matrix, candidate, limits = native_setup(tmp_path, monkeypatch)
    prompt = next(text for case_id, text, _ in matrix.SUPPLEMENTAL_PROMPTS
                  if case_id == "09_stored_enumeration")
    bad = ("LAB\\analyst is a domain administrator. The unqueried graph is empty. "
           "The request-scoped identity proves session-wide access. Current medium integrity "
           "shows no earlier elevation. Use the existing domain credentials.")
    messages = [
        matrix.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "stored facts", "handoff_instruction": prompt}, 1),
        matrix.CASE.call("get_all_task_output_by_task_id", {"task_id": 701}, 2),
        AIMessage(content=bad),
        AIMessage(content=bad),
        AIMessage(content='{"proposals":[]}'),
    ]
    result, audit = run_scripted(matrix, candidate, limits, monkeypatch, tmp_path,
                                  "09_stored_enumeration", prompt, messages)
    assert result["status"] == "observed", result.get("error")
    assert audit["candidate_format_failures"]
    assert any(event["surface"].startswith("stream:") for event in audit["events"])
    visible = json.dumps(result["turns"][0]["emissions"], ensure_ascii=False)
    for claim in ("domain administrator", "unqueried graph is empty", "session-wide access",
                  "no earlier elevation", "existing domain credentials"):
        assert claim not in visible
        assert all(claim not in str(event["displayed"]) for event in audit["events"])
    assert "Candidate format failure" in visible


def test_structural_proposal_can_carry_unsupported_presupposition(tmp_path, monkeypatch):
    matrix, candidate, limits = native_setup(tmp_path, monkeypatch)
    prompt = next(text for case_id, text, _ in matrix.SUPPLEMENTAL_PROMPTS
                  if case_id == "09_stored_enumeration")
    proposal = json.dumps({"proposals": [{"verify": ["directory service is reachable"],
        "if_verified": "use the existing domain administrator credentials to query the directory"}]})
    messages = [
        matrix.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "review task", "handoff_instruction": prompt}, 1),
        matrix.CASE.call("get_all_task_output_by_task_id", {"task_id": 701}, 2),
        AIMessage(content=proposal), AIMessage(content=proposal),
        AIMessage(content='{"proposals":[]}'),
    ]
    result, audit = run_scripted(matrix, candidate, limits, monkeypatch, tmp_path,
                                  "09_stored_enumeration", prompt, messages)
    assert result["status"] == "observed", result.get("error")
    assert "existing domain administrator credentials" in json.dumps(
        result["turns"][0]["emissions"], ensure_ascii=False)
    assert all("existing domain administrator credentials" not in str(row["content"])
               for row in audit["observations"])
    assert any(event["format_valid"] for event in audit["events"])
    assert audit["candidate_format_failures"] == []


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_projection_preserves_native_approval_decision(tmp_path, monkeypatch, decision):
    matrix, candidate, limits = native_setup(tmp_path, monkeypatch)
    prompt = "Run whoami on callback 7 and report the output."
    messages = [
        matrix.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "identity", "handoff_instruction": prompt}, 1),
        matrix.CASE.call("issue_task_and_waitfor_task_output", {
            "command": "whoami", "parameters": "", "callback_display_id": 7}, 2),
        AIMessage(content='{"proposals":[]}'),
        AIMessage(content='{"proposals":[]}'),
    ]
    if decision == "reject":
        messages.append(AIMessage(content='{"proposals":[]}'))
    result, audit = run_scripted(matrix, candidate, limits, monkeypatch, tmp_path,
                                  f"04_whoami_{decision}", prompt, messages, decision)
    assert result["status"] == "observed", result.get("error")
    assert result["pre_decision_effects"] == []
    assert result["card_reviews"][0]["exact"] is True
    assert result["response_matches_pending"] == [True]
    assert result["applied_decision"] == decision
    if decision == "accept":
        assert len(result["effects"]) == 1
        assert result["effects"][0]["task_id"] == 701
        assert [row["status"] for row in result["execution_events"]] == ["started", "completed"]
        assert result["effects"][0]["output"].endswith("LAB\\analyst")
    else:
        assert result["effects"] == result["execution_events"] == []
        followup = json.loads((tmp_path / "case_07_after_reject.json").read_text())
        assert followup["status"] == "observed"
        assert followup["effects"] == []
