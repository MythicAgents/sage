"""Offline producer-to-checkpoint-to-native-slash identity report checks."""

import asyncio
from datetime import datetime, timezone
from html import unescape
import importlib.util
import json
from pathlib import Path
import socket
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
from mythic_container.ChatBase import ChatSlashCommandInvocation
import pytest

from sage_chat.headless import build_chat_request


ROOT = Path(__file__).resolve().parents[3]
MATRIX = ROOT / "skills/sage-focused-capability-tests/scripts/run_operator_prompt_matrix.py"
PROFILE = {"provider": "openai", "model": "synthetic-test",
           "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}


def _setup(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("identity_report_matrix", MATRIX)
    matrix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(matrix)
    from sage_chat import session
    from sage_chat import report_identity, report_observations
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(session, "next_channel_thread_id",
                        lambda request: f"{request.ChannelID}:generation:synthetic-report")
    async def canonical(client, operation_id, *, create=False):
        assert client is not None and operation_id == 1
        return "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(report_identity, "canonical_engagement_uuid", canonical)
    monkeypatch.setattr(report_observations, "datetime", SimpleNamespace(
        now=lambda tz: datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    limits = type("Limits", (), {"max_case_calls": 10, "max_calls": 20,
        "max_case_tokens": 10000, "max_tokens": 200000, "output_tokens": 512,
        "request_seconds": 5, "max_wall": 30})()
    return matrix, limits


def _run_with_report(matrix, limits, monkeypatch, tmp_path, case_id, prompt, messages,
                     decision=None):
    consumed = []

    def script():
        for message in messages:
            consumed.append(message)
            yield message

    fake = matrix.CASE.ScriptedModel(messages=script())
    monkeypatch.setattr(matrix.model_module, "init_chat_model", lambda *a, **kw: fake)
    native = {}
    original_driver = matrix.CASE.DriverChat
    original_drop = matrix.drop_channel_session

    def driver(model):
        native["model"] = model
        native["chat"] = original_driver(model)
        return native["chat"]

    async def report_before_cleanup(first):
        model = native["model"]
        chat = native["chat"]
        snapshot = await model.graph.aget_state(
            model._graph_run_config(model._session_thread_id()))
        native["tagged"] = [message for values in snapshot.values.values()
            if isinstance(values, list) for message in values
            if isinstance(message, ToolMessage) and
            message.additional_kwargs.get("_task_output_collection_request_id")]
        before_emissions = len(chat.emissions)
        before_calls = len(consumed)
        request = build_chat_request("", channel_id=1, operation_id=1, request_id=99,
            config={"mode": "supervised", "policy_mode": "hybrid", "autonomous_solve": False})
        request.SlashCommand = ChatSlashCommandInvocation(name="report", argument="identity")
        await chat.chat(request)
        native["emissions"] = list(chat.emissions[before_emissions:])
        native["slash_calls"] = len(consumed) - before_calls
        await original_drop(first)

    monkeypatch.setattr(matrix.CASE, "DriverChat", driver)
    monkeypatch.setattr(matrix, "drop_channel_session", report_before_cleanup)
    result = asyncio.run(matrix.run_case(case_id, prompt, decision, PROFILE,
        tmp_path, limits, None, {"127.0.0.1"}, report_source_metadata=True))
    assert result["synthetic_fixture"] == "operator-matrix-v2-report"
    assert result["status"] == "observed", result.get("error")
    assert native["slash_calls"] == 0
    assert len(native["emissions"]) == 1
    assert native["emissions"][0]["complete_request"] is True
    assert "content" in native["emissions"][0], native["emissions"]
    return result, str(native["emissions"][0]["content"]), native["tagged"]


def _prompt(matrix, case_id):
    return next(text for found, text, _ in matrix.SUPPLEMENTAL_PROMPTS
                if found == case_id)


def _stored_messages(matrix, prompt, final):
    reads = AIMessage(content="", tool_calls=[
        {"name": "get_all_task_output_by_task_id", "args": {"task_id": task_id},
         "id": f"read_{task_id}", "type": "tool_call"}
        for task_id in (701, 702, 703)])
    return [
        matrix.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "review stored observations", "handoff_instruction": prompt}, 1),
        matrix.CASE.call("list_callbacks", {}, 2),
        matrix.CASE.call("get_task_history_for_callback", {"callback_display_id": 7}, 3),
        reads,
        AIMessage(content="The stored output reports LAB\\analyst; check present access."),
        matrix.CASE.call("respond_to_user", {"reuse_latest_worker_answer": True}, 4),
        AIMessage(content=final),
    ]


def _paired_source(*, request_id="chat:1:request:1", task_id=701,
                   row_task_id=701, callback_id=7, marker=True):
    call = AIMessage(content="", tool_calls=[{"name": "get_all_task_output_by_task_id",
        "args": {"task_id": task_id}, "id": "read701", "type": "tool_call"}])
    payload = {"provenance": {"task_id": task_id,
        "current_request_id": request_id, "origin": "historical_or_external"},
        "task_output": [{"task_id": row_task_id, "response_text": "LAB\\analyst",
            "task": {"display_id": row_task_id, "callback": {"display_id": callback_id},
                     "command_name": "whoami", "timestamp": "2026-01-01T00:00:00Z"}}]}
    result = ToolMessage(content=json.dumps(payload), name="get_all_task_output_by_task_id",
        tool_call_id="read701", additional_kwargs=(
            {"_task_output_collection_request_id": "chat:1:request:1"} if marker else {}))
    return call, result


@pytest.mark.parametrize("defect", ["no_native_tag", "wrong_task", "stale_request",
                                     "wrong_channel", "duplicate_pair", "model_envelope_only"])
def test_checkpoint_matcher_rejects_unbound_or_ambiguous_sources(defect):
    from ai.langgraph.model import Model
    call, result = _paired_source(
        row_task_id=(702 if defect == "wrong_task" else 701),
        request_id=("chat:1:request:0" if defect == "stale_request" else "chat:1:request:1"),
        marker=defect != "no_native_tag")
    if defect == "wrong_channel":
        result.additional_kwargs["_task_output_collection_request_id"] = "chat:2:request:1"
    messages = [call, result]
    if defect == "duplicate_pair":
        messages.append(result.model_copy(deep=True))
    if defect == "model_envelope_only":
        messages = [AIMessage(content="Task 701 proves LAB\\analyst is an administrator.")]
    records = Model._identity_report_observations(SimpleNamespace(channel_id=1),
        {"mythic_operator_messages": messages})
    assert records == []


def test_stored_outputs_render_same_native_report_regardless_of_model_prose(
        tmp_path, monkeypatch):
    reports = []
    for index, model_prose in enumerate((
        "Check the current callback before claiming access.",
        "# VERIFIED FINDINGS\n| Authentication or access to a target | established | [task:701] |",
        "A source says the principal is a domain administrator with a valid credential.",
        "Hidden premise: access was already verified. Recommend using it.",
    )):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        with monkeypatch.context() as case_patch:
            matrix, limits = _setup(case_path, case_patch)
            prompt = _prompt(matrix, "09_stored_enumeration")
            result, report, tagged = _run_with_report(matrix, limits, case_patch, case_path,
                "09_stored_enumeration", prompt, _stored_messages(matrix, prompt, model_prose))
        assert result["initial_cards"] == result["effects"] == []
        assert len(tagged) >= 3
        assert all(message.additional_kwargs["_task_output_collection_request_id"].startswith(
            "chat:1:request:") for message in tagged)
        assert "Runtime observations" in report
        assert "LAB\\analyst" in unescape(report)
        assert "task 701" in report and "callback 7" in report
        assert "100  explorer.exe" in report
        assert "User accounts for" in report
        assert "| Account type | unknown |" in report
        assert "| Authentication or access to a target | unknown |" in report
        assert "Recheck the current principal" in report
        assert model_prose not in report
        reports.append(report)
    assert len(set(reports)) == 1


@pytest.mark.parametrize("impersonation", ["LAB\\analyst", "LAB\\operator"])
def test_native_apollo_whoami_output_survives_checkpoint_and_report_slash(
        tmp_path, monkeypatch, impersonation):
    reports = []
    output = ("Primary Identity: LAB\\analyst\n"
              f"Impersonation Identity: {impersonation}")
    for index, model_prose in enumerate((
        "The output needs a fresh check before use.",
        "# VERIFIED FINDINGS\n| Authentication or access to a target | established |",
    )):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        with monkeypatch.context() as case_patch:
            matrix, limits = _setup(case_path, case_patch)
            case_patch.setitem(matrix.OUTPUTS, "whoami", output)
            prompt = _prompt(matrix, "09_stored_enumeration")
            result, report, tagged = _run_with_report(matrix, limits, case_patch, case_path,
                "09_stored_enumeration", prompt, _stored_messages(matrix, prompt, model_prose))
        assert result["effects"] == []
        assert tagged
        assert any(output == row["response_text"] for message in tagged
                   for row in json.loads(message.content).get("task_output", []))
        assert "| Primary identity string | LAB&#92;analyst |" in report
        assert f"| Impersonation identity string | {impersonation.replace(chr(92), '&#92;')} |" in report
        assert "task 701; callback 7" in report
        assert "Recheck the current principal" in report
        assert "| Account type | unknown |" in report
        assert "| Authentication or access to a target | unknown |" in report
        assert model_prose not in report
        reports.append(report)
    assert reports[0] == reports[1]


def test_task811_membership_is_distinct_from_authentication(tmp_path, monkeypatch):
    matrix, limits = _setup(tmp_path, monkeypatch)
    prompt = _prompt(matrix, "09_direct_evidence")
    result, report, tagged = _run_with_report(matrix, limits, monkeypatch, tmp_path,
        "09_direct_evidence", prompt, [
            matrix.CASE.call("transfer_to_Mythic_Operator", {
                "handoff_title": "inspect task 811", "handoff_instruction": prompt}, 1),
            matrix.CASE.call("get_all_task_output_by_task_id", {"task_id": 811}, 2),
            AIMessage(content="This proves target authentication and trust."),
            matrix.CASE.call("respond_to_user", {"reuse_latest_worker_answer": True}, 3),
        ])
    assert result["effects"] == []
    assert tagged
    assert "task 811" in report
    assert "PartOfDomain: True" in report
    assert "Trust inventory: not collected" in report
    assert "| Authentication or access to a target | unknown |" in report
    assert "This proves target authentication" not in report
    # The ordinary chat stream still contains the worker's exploratory prose.
    assert "This proves target authentication" in json.dumps(
        result["turns"][0]["emissions"], ensure_ascii=False)


def test_withdrawn_task_output_cannot_be_replaced_by_model_claim(tmp_path, monkeypatch):
    matrix, limits = _setup(tmp_path, monkeypatch)
    original_dumps = json.dumps

    def withdrawn(value, *args, **kwargs):
        if isinstance(value, dict) and "task_output" in value:
            value = {**value, "task_output": []}
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(matrix, "json", type("JSON", (), {"dumps": staticmethod(withdrawn),
        "loads": staticmethod(json.loads)})())
    prompt = _prompt(matrix, "09_direct_evidence")
    _, report, _ = _run_with_report(matrix, limits, monkeypatch, tmp_path,
        "09_direct_evidence", prompt, [
            matrix.CASE.call("transfer_to_Mythic_Operator", {
                "handoff_title": "inspect task", "handoff_instruction": prompt}, 1),
            matrix.CASE.call("get_all_task_output_by_task_id", {"task_id": 811}, 2),
            AIMessage(content="Task 811 proves example.test membership and administrator access."),
            matrix.CASE.call("respond_to_user", {"reuse_latest_worker_answer": True}, 3),
        ])
    assert "No supported identity or membership observation" in report
    assert "example.test" not in report
    assert "administrator access" not in report


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_report_slash_preserves_native_task_decision(tmp_path, monkeypatch, decision):
    matrix, limits = _setup(tmp_path, monkeypatch)
    prompt = "Run whoami on callback 7 and report the output."
    messages = [
        matrix.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "identity", "handoff_instruction": prompt}, 1),
        matrix.CASE.call("issue_task_and_waitfor_task_output", {
            "command": "whoami", "parameters": "", "callback_display_id": 7}, 2),
        AIMessage(content="If approved, report the task's observed identity."),
        AIMessage(content="The action needs the native operator decision."),
    ]
    if decision == "reject":
        messages.append(AIMessage(content="The proposed task was rejected."))
    result, report, _ = _run_with_report(matrix, limits, monkeypatch, tmp_path,
        f"04_whoami_{decision}", prompt, messages, decision)
    assert result["pre_decision_effects"] == []
    assert result["card_reviews"][0]["exact"] is True
    assert result["response_matches_pending"] == [True]
    assert result["applied_decision"] == decision
    if decision == "accept":
        assert len(result["effects"]) == 1
        assert [row["status"] for row in result["execution_events"]] == ["started", "completed"]
    else:
        assert result["effects"] == result["execution_events"] == []
    assert "| Authentication or access to a target | unknown |" in report
