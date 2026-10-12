"""Offline checks for the assembled Payload handoff measurement contract."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool
from unittest.mock import AsyncMock


SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-focused-capability-tests/scripts/run_payload_handoff_case.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("run_payload_handoff_case", SCRIPT)
case = importlib.util.module_from_spec(spec)
spec.loader.exec_module(case)


def test_default_inspection_is_offline_and_records_exact_scope(monkeypatch, capsys):
    monkeypatch.setattr(case.matrix.REAL, "load_profile", lambda *_: pytest.fail("read config"))
    monkeypatch.setattr(case.matrix.REAL, "resolved_endpoint_ips", lambda *_: pytest.fail("DNS"))
    assert case.main([]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["status"] == "offline_inspection"
    assert row["config_path"] == str(case.DEFAULT_CONFIG.resolve())
    assert row["max_real_calls"] is None
    assert row["max_observed_tokens"] is None
    assert row["max_wall_seconds"] == 300
    assert row["target_effects"] == 0
    assert row["fixture_sha256"] == case.fixture_digest()


def test_run_requires_identity_before_dns(monkeypatch, capsys, tmp_path):
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}
    monkeypatch.setattr(case.matrix.REAL, "load_profile", lambda *_: profile)
    monkeypatch.setattr(case.matrix.REAL, "resolved_endpoint_ips", lambda *_: pytest.fail("DNS"))
    common = ["--run", "--config", str(tmp_path / "config"), "--output-dir",
              str(tmp_path / "output"), "--expected-model", profile["model"],
              "--expected-endpoint-sha256", case.digest_bytes(profile["API_ENDPOINT"].encode()),
              "--expected-source-sha256", case.digest_bytes(
                  (case.SAGE / "ai/langgraph/model.py").read_bytes())]
    for flag in ("--expected-model", "--expected-endpoint-sha256",
                 "--expected-source-sha256"):
        assert case.main([*common, flag, "wrong"]) == 2
        assert "identity mismatch" in capsys.readouterr().out


@pytest.mark.parametrize("change", [
    {"file_uuid": "00000000-0000-0000-0000-000000000001"},
    {"callback_display_id": 8},
    {"parameters": {"remote_path": "C:\\Temp\\wrong.txt"}},
    {"parameters": {"File": "a9c43269-2cb5-4aa3-b893-219294b88b17",
                    "Path": case.DESTINATION}},
    {"parameters": {"File": case.FILE_UUID, "Path": "C:\\Temp\\wrong.txt"}},
    {"parameters": {"file": case.FILE_UUID, "remote_path": case.DESTINATION,
                    "Path": case.DESTINATION}},
    {"parameters": {"file": case.FILE_UUID, "File": case.FILE_UUID,
                    "remote_path": case.DESTINATION}},
    {"parameters": {"file": case.FILE_UUID, "File": "wrong",
                    "remote_path": case.DESTINATION}},
])
def test_wrong_upload_identity_or_target_is_not_supported(change):
    args = {"command": "upload", "parameters": {"remote_path": case.DESTINATION,
                                             "file": case.FILE_UUID},
            "file_uuid": case.FILE_UUID, "callback_display_id": 7, "token_id": None}
    args.update(change)
    result = case.proposal_classification([{"tool_name": "upload_file_by_file_uuid",
                                            "arguments": args}])
    assert result["supported"] is False


@pytest.mark.parametrize("parameters", [
    {"remote_path": case.DESTINATION, "file": case.FILE_UUID},
    {"Path": case.DESTINATION, "File": case.FILE_UUID},
    {"remote_path": case.DESTINATION, "File": case.FILE_UUID},
    {"Path": case.DESTINATION, "file": case.FILE_UUID},
])
def test_exact_pending_upload_can_be_supported(parameters):
    args = {"command": "upload", "parameters": parameters,
            "file_uuid": case.FILE_UUID, "callback_display_id": 7, "token_id": None}
    assert case.proposal_classification([{"tool_name": "upload_file_by_file_uuid",
                                           "arguments": args}])["supported"]
    assert not case.proposal_classification([{"tool_name": "upload_file_by_file_uuid",
        "arguments": args}, {"tool_name": "delete_payload", "arguments": {}}])["supported"]


def test_native_payload_uses_apollo_file_transport_not_registered_file_selector():
    args = {"command": "upload", "parameters": {"remote_path": case.DESTINATION,
            "file": case.FILENAME}, "callback_display_id": 7}
    action = {"tool_name": "issue_task_and_waitfor_task_output", "arguments": args}
    assert not case.proposal_classification([action])["supported"]
    args["parameters"] = {"File": case.FILE_UUID, "Path": case.DESTINATION}
    assert not case.proposal_classification([action])["supported"]
    args["visibility_context"] = {"_sage_prepared_action": True,
        "_sage_prepared_binding": {"file": None, "group": None}}
    assert case.proposal_classification([action])["supported"]
    args["parameters"]["File"] = "a9c43269-2cb5-4aa3-b893-219294b88b17"
    assert not case.proposal_classification([action])["supported"]
    assert case.proposal_classification([{"tool_name": "upload_file_by_file_uuid",
        "arguments": {"command": "upload", "parameters": {
            "remote_path": case.DESTINATION, "file": case.FILE_UUID},
            "file_uuid": case.FILE_UUID, "callback_display_id": 7}}])["supported"]


def test_offline_scripted_continuation_delegates_missing_payload_identity():
    def response(messages):
        request = httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions",
            json={"model": "fixture-model", "messages": messages, "stream": False,
                "tools": [{"function": {"name": name}} for name in
                    ("transfer_to_Mythic_Payload", "upload_file_by_file_uuid")]})
        return case.offline_scripted_continuation(request, "fixture-model", 99).json()[
            "choices"][0]["message"]["tool_calls"][0]["function"]["name"]
    assert response([{"role": "user", "content": case.PROMPT}]) == "transfer_to_Mythic_Payload"
    assert response([{"role": "tool", "content": json.dumps(case.BUILD_RESULT)}]) == \
        "upload_file_by_file_uuid"


def test_mocked_payload_metadata_matches_reuse_contract():
    assert case.FILENAME.endswith(".exe")
    assert case.BUILD_ARGS["c2_profiles"] == [{"c2_profile": "http",
        "c2_profile_parameters": {"callback_host": case.C2_HOST, "callback_port": "443"}}]
    assert case.RECOVERY_ROW["os"] == "Windows"
    assert case.RECOVERY_ROW["payloadtype"]["name"] == "apollo"
    assert case.CALLBACK_C2_CONFIG["c2_parameters"] == case.PAYLOAD_C2_CONFIG["c2_parameters"]
    assert case.DOWNLOAD_RESULT["agent_file_id"] == case.FILE_UUID
    assert case.UPLOADED_FILES == []
    assert [row["parameter_group_name"] for row in case.UPLOAD_SCHEMA] == [
        "Default", "ExistingFile", "Default", "ExistingFile"]
    assert [(row["cli_name"], row["type"]) for row in case.UPLOAD_SCHEMA] == [
        ("Path", "String"), ("Path", "String"), ("File", "File"),
        ("fileName", "ChooseOne")]
    assert case.UPLOAD_SCHEMA[-1]["choices"] == []


def test_scripted_payload_reuse_sequence_reaches_download_before_summary():
    messages = [{"role": "user", "content": "Inspect callback 7 Apollo payload."}]
    tools = ("get_all_payloads", "get_callback_c2_config", "get_payload_c2_config",
             "download_payload")
    for expected in tools:
        request = httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions",
            json={"model": "fixture-model", "messages": messages, "stream": False,
                "tools": [{"function": {"name": name}} for name in tools]})
        call = case.offline_scripted_continuation(request, "fixture-model", len(messages)).json()[
            "choices"][0]["message"]["tool_calls"][0]
        assert call["function"]["name"] == expected
        messages.extend([{"role": "assistant", "tool_calls": [call]},
                         {"role": "tool", "tool_call_id": call["id"], "content": "{}"}])
    request = httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions",
        json={"model": "fixture-model", "messages": messages, "stream": False,
            "tools": [{"function": {"name": name}} for name in tools]})
    final = case.offline_scripted_continuation(request, "fixture-model", 99).json()[
        "choices"][0]["message"]
    assert final.get("tool_calls") is None
    assert case.FILE_UUID in final["content"]


@pytest.mark.parametrize("proposal_tool,proposal_args", [
    ("upload_file_by_file_uuid", {"command": "upload", "parameters": {
        "remote_path": case.DESTINATION, "file": case.FILE_UUID},
        "file_uuid": case.FILE_UUID, "callback_display_id": 7}),
    ("issue_task_and_waitfor_task_output", {"command": "upload", "parameters": {
        "remote_path": case.DESTINATION, "file": case.FILENAME}, "callback_display_id": 7}),
    ("issue_task_and_waitfor_task_output", {"command": "upload", "parameters": {
        "remote_path": case.DESTINATION, "file": case.FILE_UUID}, "callback_display_id": 7}),
    ("issue_task_and_waitfor_task_output", {"command": "upload", "parameters": {
        "remote_path": case.DESTINATION, "file": "a9c43269-2cb5-4aa3-b893-219294b88b17"},
        "callback_display_id": 7}),
])
def test_assembled_seed_approval_and_pending_upload_have_no_effect(
    monkeypatch, tmp_path, proposal_tool, proposal_args,
):
    original = case.SeededTransport

    class OfflineTransport(original):
        async def handle_async_request(self, request):
            if self.seed_index < len(case.SEEDS):
                return await super().handle_async_request(request)
            body = json.loads(request.content)
            assert case.matrix.REAL._role_hint(body).startswith("Mythic_Operator")
            if proposal_tool == "upload_file_by_file_uuid":
                return case.offline_scripted_continuation(request, self.model, 99)
            return case._seed_response(request, self.model, 99, proposal_tool, proposal_args)

    monkeypatch.setattr(case, "SeededTransport", OfflineTransport)
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}
    result = asyncio.run(asyncio.wait_for(
        case.run_case(profile, tmp_path, object(), None, {"127.0.0.1"}), timeout=10))
    assert result["accepted_local_builds"]
    assert result["effects"] == []
    assert result["provider_requests"] == []
    if proposal_tool == "upload_file_by_file_uuid":
        assert result["status"] == "observed", result.get("error")
        assert result["classification"]["supported"]
        assert result["fixture_reads"] == []  # Current source carries the build result.
    elif proposal_args["parameters"]["file"] == case.FILENAME:
        # Apollo's Default file parameter is typed File, so preparation may
        # produce a card, but a filename is not the payload's file UUID.
        assert result["status"] == "observed"
        assert not result["classification"]["supported"]
        assert result["proposals"][0]["arguments"]["parameters"]["File"] == case.FILENAME
    else:
        assert result["status"] == "observed", result
        assert result["classification"]["supported"] is (
            proposal_args["parameters"]["file"] == case.FILE_UUID)
        assert result["proposals"][0]["arguments"]["parameters"] == {
            "File": proposal_args["parameters"]["file"], "Path": case.DESTINATION}


def test_apollo_upload_preparation_reaches_inert_issue_boundary(monkeypatch):
    from tests.test_supervised_action_preparation import _claim

    tools = case.MythicTools(agent_task_id="offline-upload")
    tools.client = object()
    monkeypatch.setattr(tools, "_authenticate_live_command", AsyncMock(return_value={
        "status": "available", "command": "upload"}))
    monkeypatch.setattr(tools, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    monkeypatch.setattr(tools, "_fetch_command_schema", AsyncMock(return_value=case.UPLOAD_SCHEMA))
    monkeypatch.setattr(tools, "_callback_tasking_liveness_blocker", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_ensure_registered_file_available", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_engagement_issue_hook", AsyncMock(return_value=None))
    sink = AsyncMock(side_effect=RuntimeError("inert issue boundary"))
    monkeypatch.setattr(tools, "_issue_mythic_task", sink)

    async def probe():
        prepared = await tools._prepare_supervised_issue_action({
            "command": "upload", "parameters": {"file": case.FILE_UUID,
                "remote_path": case.DESTINATION}, "callback_display_id": 7})
        _claim(tools, {"name": "issue_task_and_waitfor_task_output", "args": prepared})
        return prepared, await tools.issue_task_and_waitfor_task_output(**prepared)

    prepared, result = asyncio.run(probe())
    assert prepared["parameters"] == {"File": case.FILE_UUID, "Path": case.DESTINATION}
    assert prepared["visibility_context"]["_sage_prepared_binding"]["group"] is None
    assert "inert issue boundary" in result
    sink.assert_awaited_once()
    assert sink.await_args.args == ("upload", prepared["parameters"], 7)
    assert sink.await_args.kwargs["parameter_group_name"] is None


@pytest.mark.parametrize("args", [
    {"payload": "wrong", "command": "upload"},
    {"payload": "apollo", "command": "wrong"},
])
def test_upload_schema_discovery_rejects_wrong_identity(monkeypatch, tmp_path, args):
    original = case.SeededTransport

    class OfflineTransport(original):
        async def handle_async_request(self, request):
            if self.seed_index < len(case.SEEDS):
                return await super().handle_async_request(request)
            return case._seed_response(request, self.model, 99,
                "get_all_command_args_for_payloadtype", args)

    monkeypatch.setattr(case, "SeededTransport", OfflineTransport)
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}
    result = asyncio.run(asyncio.wait_for(
        case.run_case(profile, tmp_path, object(), None, {"127.0.0.1"}), timeout=10))
    assert result["status"] == "harness_gap"
    assert "unknown fixture command args" in result["error"]
    assert result["effects"] == []


@pytest.mark.parametrize("explicit_config", [False, True])
def test_run_cli_reaches_assembled_case_with_inert_route(
    monkeypatch, tmp_path, capsys, explicit_config,
):
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}

    original = case.SeededTransport

    class OfflineTransport(original):
        async def handle_async_request(self, request):
            if self.seed_index < len(case.SEEDS):
                return await super().handle_async_request(request)
            return case.offline_scripted_continuation(request, self.model, 99)

    monkeypatch.setattr(case, "SeededTransport", OfflineTransport)
    selected = []
    def load_profile(path):
        selected.append(path)
        return profile
    monkeypatch.setattr(case.matrix.REAL, "load_profile", load_profile)
    monkeypatch.setattr(case.matrix.REAL, "resolved_endpoint_ips", lambda *_: {"127.0.0.1"})
    monkeypatch.setattr(case.matrix.REAL, "sage_tls_context", lambda: None)
    output = tmp_path / "case"
    config = tmp_path / "explicit.env" if explicit_config else case.DEFAULT_CONFIG
    args = ["--run", "--output-dir", str(output),
        "--expected-model", profile["model"],
        "--expected-endpoint-sha256", case.digest_bytes(profile["API_ENDPOINT"].encode()),
        "--expected-source-sha256", case.digest_bytes(case.IMPORTED_MODEL_PATH.read_bytes())]
    if explicit_config:
        args += ["--config", str(config)]
    assert case.main(args) == 0
    assert selected == [config]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "observed"
    result = json.loads((output / "case.json").read_text())
    assert result["objective_success"] is True
    assert result["effects"] == []
    assert result["config_path"] == str(config.resolve())


def test_missing_default_config_fails_before_dns(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(case, "DEFAULT_CONFIG", tmp_path / "absent.env")
    monkeypatch.setattr(case.matrix.REAL, "resolved_endpoint_ips", lambda *_: pytest.fail("DNS"))
    args = ["--run", "--output-dir", str(tmp_path / "case"),
        "--expected-model", "fixture-model", "--expected-endpoint-sha256", "0" * 64,
        "--expected-source-sha256", case.digest_bytes(case.IMPORTED_MODEL_PATH.read_bytes())]
    assert case.main(args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "missing_config_file"
    assert not (tmp_path / "case").exists()


def test_posthandoff_accounts_provider_without_reservation_headroom(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION", "1")
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}

    original = case.SeededTransport

    class Provider(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return case.offline_scripted_continuation(request, profile["model"], 99,
                usage={"prompt_tokens": 100, "completion_tokens": 23, "total_tokens": 123})

    class MeteredLocalTransport(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.inner = Provider()

    monkeypatch.setattr(case, "SeededTransport", MeteredLocalTransport)
    result = asyncio.run(asyncio.wait_for(case.run_case(
        profile, tmp_path, None, None, {"127.0.0.1"}), timeout=10))
    assert result["status"] == "observed", result.get("error")
    assert result["objective_success"] and result["effects"] == []
    assert len(result["provider_requests"]) == 1
    assert result["provider_accounting"]["total_tokens"] == 123
    assert result["provider_accounting"]["usage_complete"]
    assert result["posthandoff_input_bytes"][0] > 0
    messages = result["provider_requests"][0]["request"]["messages"]
    completion = next(index for index, message in enumerate(messages)
                      if message.get("content") == "[Mythic_Payload completed task]")
    # Exercise the real request producer metadata and complete middleware chain:
    # the completed artifact follows both copies of the unchanged instruction.
    assert [message["content"] for message in messages[:completion]
            if message["role"] == "user"] == [case.PROMPT, case.PROMPT]
    assert not any(message.get("content") == case.PROMPT for message in messages[completion:])
    assert messages[-1] == {"role": "user", "content":
        "Internal conversation continuation. The operator's request and authorization are unchanged."}
    assert "Sage-generated provider continuation" in messages[0]["content"]
    assert result["model_calls"][-1]["usage"][0]["total_tokens"] == 123
    offered = {tool["function"]["name"]: tool["function"]
               for tool in result["provider_requests"][0]["request"]["tools"]}
    effect_names = ["issue_task_and_waitfor_task_output", "upload_file_by_file_uuid"]
    production = case.MythicTools(agent_task_id="schema-control", operation_id=1, channel_id=1)
    for tool in production.get_tools(effect_names):
        expected = convert_to_openai_tool(tool)["function"]
        assert offered[tool.name]["parameters"] == expected["parameters"]
        assert offered[tool.name]["description"] == expected["description"]
    assert set(offered["issue_task_and_waitfor_task_output"]["parameters"]["required"]) == {
        "command", "parameters", "callback_display_id"}
    assert set(offered["upload_file_by_file_uuid"]["parameters"]["required"]) == {
        "command", "parameters", "file_uuid", "callback_display_id"}


@pytest.mark.parametrize("reads,expected", [
    ([ ("get_payload_names", {}), ("get_callback_c2_config", {"display_id": 7}) ],
     [["apollo"], case.CALLBACK_C2_CONFIG]),
    ([ ("get_all_payloads", {}), ("get_payload_c2_config", {"payload_uuid": case.PAYLOAD_UUID}) ],
     [[case.RECOVERY_ROW], case.PAYLOAD_C2_CONFIG]),
    ([ ("get_all_payloads", {}), ("get_callback_c2_config", {"display_id": 7}),
       ("get_payload_c2_config", {"payload_uuid": case.PAYLOAD_UUID}),
       ("download_payload", {"payload_uuid": case.PAYLOAD_UUID}) ],
     [[case.RECOVERY_ROW], case.CALLBACK_C2_CONFIG, case.PAYLOAD_C2_CONFIG,
      case.DOWNLOAD_RESULT]),
    ([ ("get_c2_profiles_for_payload", {"payload": "apollo"}) ],
     [case.APOLLO_C2_PROFILES]),
])
def test_redelegated_payload_metadata_reads_and_repeated_build_pause(
    monkeypatch, tmp_path, reads, expected,
):
    """A baseline-style second Payload visit must reach another guarded proposal."""
    monkeypatch.setenv("SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION", "65536")
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}

    class Ledger:
        def __init__(self):
            self.reservations = []
            self.settlements = []

        def reserve(self, amounts, operation):
            self.reservations.append((amounts, operation))
            return f"r-{len(self.reservations)}"

        def settle_provider(self, reservation_id, actual_tokens):
            self.settlements.append((reservation_id, actual_tokens))

    shared = Ledger()
    original = case.SeededTransport
    sequence = [("transfer_to_Mythic_Payload", {"handoff_title": "Inspect synthetic build",
        "handoff_instruction": "Check Payload metadata, then propose the text build."}),
        *reads, ("create_payload", case.BUILD_ARGS)]

    class Provider(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = []

        async def handle_async_request(self, request):
            body = json.loads(request.content)
            tool, args = sequence[len(self.calls)]
            offered = {item.get("function", {}).get("name") for item in body.get("tools", [])}
            assert tool in offered
            self.calls.append(tool)
            return case._seed_response(request, profile["model"], len(self.calls) + 10,
                                       tool, args, usage={"prompt_tokens": 100,
                                       "completion_tokens": 23, "total_tokens": 123})

    provider = Provider()

    class MeteredLocalTransport(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.inner = provider

    monkeypatch.setattr(case, "SeededTransport", MeteredLocalTransport)
    result = asyncio.run(asyncio.wait_for(case.run_case(
        profile, tmp_path, shared, None, {"127.0.0.1"}), timeout=10))
    assert result["status"] == "observed", result.get("error")
    assert provider.calls == [tool for tool, _ in sequence]
    assert [read["tool"] for read in result["fixture_reads"]] == [name for name, _ in reads]
    assert [read["result"] if isinstance(read["result"], list) else json.loads(read["result"])
            for read in result["fixture_reads"]] == expected
    assert result["provider_accounting"]["requests"] == len(sequence)
    assert result["provider_accounting"]["usage_complete"]
    assert len(result["accepted_local_builds"]) == 1
    assert result["proposals"][0]["tool_name"] == "create_payload"
    assert result["classification"]["supported"] is False
    assert result["objective_success"] is False
    assert result["effects"] == []


@pytest.mark.parametrize("download_uuid", [case.PAYLOAD_UUID, "00000000-0000-0000-0000-000000000001"])
def test_assembled_reuse_download_and_wrong_selector(monkeypatch, tmp_path, download_uuid):
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}
    sequence = [
        ("Mythic_Operator", "transfer_to_Mythic_Payload", {
            "handoff_title": "Reuse Apollo payload",
            "handoff_instruction": "Inspect the Apollo payload and callback 7 C2, download the match, and report its file UUID."}),
        ("Mythic_Payload", "get_all_payloads", {}),
        ("Mythic_Payload", "get_callback_c2_config", {"display_id": 7}),
        ("Mythic_Payload", "get_payload_c2_config", {"payload_uuid": case.PAYLOAD_UUID}),
        ("Mythic_Payload", "download_payload", {"payload_uuid": download_uuid}),
        ("Mythic_Payload", None, {"content": f"Reused {case.FILENAME}; agent_file_id={case.FILE_UUID}."}),
        ("Supervisor", "transfer_to_Mythic_Operator", {
            "handoff_title": "Propose exact upload",
            "handoff_instruction": f"Propose callback 7 upload of file {case.FILE_UUID} to {case.DESTINATION}."}),
        ("Mythic_Operator", "upload_file_by_file_uuid", {
            "command": "upload", "parameters": {"file": case.FILE_UUID,
                "remote_path": case.DESTINATION}, "file_uuid": case.FILE_UUID,
            "callback_display_id": 7}),
    ]
    original = case.SeededTransport

    class Provider(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = []

        async def handle_async_request(self, request):
            role, tool, args = sequence[len(self.calls)]
            body = json.loads(request.content)
            offered = {item.get("function", {}).get("name") for item in body.get("tools", [])}
            observed_role = ("Mythic_Payload" if any(item.get("function", {}).get("name")
                == "download_payload" for item in body.get("tools", []))
                else case.matrix.REAL._role_hint(body))
            assert observed_role.startswith(role), (observed_role, role)
            assert tool is None or tool in offered
            self.calls.append(tool)
            return case._seed_response(request, profile["model"], len(self.calls) + 100,
                tool, args, usage={"prompt_tokens": 100, "completion_tokens": 23,
                                   "total_tokens": 123})

    provider = Provider()

    class Local(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.inner = provider

    monkeypatch.setattr(case, "SeededTransport", Local)
    result = asyncio.run(asyncio.wait_for(case.run_case(
        profile, tmp_path, None, None, {"127.0.0.1"}), timeout=20))
    if download_uuid != case.PAYLOAD_UUID:
        assert result["status"] == "harness_gap", result.get("error")
        assert "unknown fixture payload download" in result["error"]
        assert [read["tool"] for read in result["fixture_reads"]] == [
            "get_all_payloads", "get_callback_c2_config", "get_payload_c2_config"]
        assert result["effects"] == []
        return
    # The actual Supervisor gate terminates after the second Payload return.
    # This is a measured system outcome, not a missing fixture response.
    assert result["status"] == "no_pending_upload", result.get("error")
    assert provider.calls == [tool for _, tool, _ in sequence[:6]]
    assert [read["tool"] for read in result["fixture_reads"]] == [
        "get_all_payloads", "get_callback_c2_config", "get_payload_c2_config", "download_payload"]
    assert result["provider_accounting"]["requests"] == 6
    assert result["provider_accounting"]["usage_complete"]
    assert result["classification"]["supported"] is False
    assert result["objective_success"] is False and result["effects"] == []


def test_more_than_four_calls_and_old_token_ceiling_reach_pending_upload(monkeypatch, tmp_path):
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}
    sequence = [("get_all_uploaded_files", {})] * 5 + [
        ("upload_file_by_file_uuid", {"command": "upload", "parameters": {
            "file": case.FILE_UUID, "remote_path": case.DESTINATION},
            "file_uuid": case.FILE_UUID, "callback_display_id": 7})]
    original = case.SeededTransport

    class Provider(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0

        async def handle_async_request(self, request):
            name, args = sequence[self.calls]
            self.calls += 1
            return case._seed_response(request, profile["model"], self.calls + 100,
                name, args, usage={"prompt_tokens": 20000,
                "completion_tokens": 23, "total_tokens": 20023})

    provider = Provider()

    class Local(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.inner = provider

    monkeypatch.setattr(case, "SeededTransport", Local)
    result = asyncio.run(asyncio.wait_for(case.run_case(
        profile, tmp_path, None, None, {"127.0.0.1"}), timeout=20))
    assert result["status"] == "observed", result.get("error")
    assert provider.calls == 6
    assert result["provider_accounting"]["requests"] == 6
    assert result["provider_accounting"]["total_tokens"] == 6 * 20023 > 98304
    assert result["provider_accounting"]["usage_complete"]
    assert result["classification"]["supported"] and result["effects"] == []


@pytest.mark.parametrize("first_tool", ["get_all_uploaded_files", "upload_file_by_file_uuid"])
def test_missing_provider_usage_is_uncertain_even_with_known_seed_usage(
    monkeypatch, tmp_path, first_tool,
):
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}
    upload = {"command": "upload", "parameters": {"file": case.FILE_UUID,
        "remote_path": case.DESTINATION}, "file_uuid": case.FILE_UUID,
        "callback_display_id": 7}
    original = case.SeededTransport

    class Provider(httpx.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            response = case._seed_response(request, profile["model"], self.calls + 100,
                first_tool, {} if first_tool == "get_all_uploaded_files" else upload,
                usage={"prompt_tokens": 100, "completion_tokens": 23,
                       "total_tokens": 123})
            if json.loads(request.content).get("stream"):
                chunks = [chunk for chunk in response.content.decode().split("\n\n")
                          if '"usage"' not in chunk]
                return httpx.Response(200, request=request,
                    content="\n\n".join(chunks).encode(),
                    headers={"content-type": "text/event-stream"})
            body = response.json()
            del body["usage"]
            return httpx.Response(200, request=request, json=body)

    provider = Provider()

    class Local(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.inner = provider

    monkeypatch.setattr(case, "SeededTransport", Local)
    result = asyncio.run(asyncio.wait_for(case.run_case(
        profile, tmp_path, None, None, {"127.0.0.1"}), timeout=20))
    assert provider.calls == 1
    assert result["status"] == "unknown_provider_usage"
    assert result["usage_uncertain"] is True
    assert result["provider_accounting"]["unknown_request_indices"] == [1]
    assert result["objective_success"] is False
    assert result["effects"] == []
    if first_tool == "get_all_uploaded_files":
        assert any(row["reason"] == "unknown_observed_usage"
                   for row in result["transport_rejections"])


def test_wall_limit_persists_in_flight_uncertainty(monkeypatch, tmp_path):
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}
    original = case.SeededTransport

    class WaitingProvider(httpx.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            await asyncio.Event().wait()

    provider = WaitingProvider()

    class Local(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.inner = provider

    monkeypatch.setattr(case, "SeededTransport", Local)
    result = asyncio.run(case.run_case(profile, tmp_path, None, None, {"127.0.0.1"},
        limits=case.SimpleNamespace(max_wall=1)))
    assert result["status"] == "time_limit"
    assert result["stop_reason"] == "wall_time"
    assert result["protocol_valid"] and not result["objective_success"]
    assert provider.calls == 1
    assert result["provider_accounting"]["unknown_request_indices"] == [1]
    assert result["usage_uncertain"] is True
    assert json.loads((tmp_path / "case.json").read_text())["status"] == "time_limit"
    assert result["effects"] == []


def test_external_cancellation_is_not_labeled_wall_time(monkeypatch, tmp_path):
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}
    started = asyncio.Event()
    original = case.SeededTransport

    class WaitingProvider(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            started.set()
            await asyncio.Event().wait()

    class Local(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.inner = WaitingProvider()

    monkeypatch.setattr(case, "SeededTransport", Local)

    async def execute():
        running = asyncio.create_task(case.run_case(profile, tmp_path, None, None,
            {"127.0.0.1"}, limits=case.SimpleNamespace(max_wall=5)))
        await asyncio.wait_for(started.wait(), timeout=4)
        running.cancel()
        return await running

    result = asyncio.run(execute())
    assert result["status"] == "cancelled"
    assert result["stop_reason"] == "CancelledError"
    assert result["usage_uncertain"] is True
    assert result["effects"] == []


def test_transport_deadline_first_is_reported_as_time_limit(monkeypatch, tmp_path):
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}
    original = case.SeededTransport

    class WaitingProvider(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.Event().wait()

    class EarlierTransport(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.utc_deadline = datetime.now(timezone.utc) + timedelta(seconds=1.2)
            self.inner = WaitingProvider()

        async def handle_async_request(self, request):
            try:
                return await super().handle_async_request(request)
            finally:
                # A response timeout has no explicit pre-dispatch rejection.
                self.rejections[:] = [row for row in self.rejections
                    if row.get("reason") != "utc_deadline_expired"]

    monkeypatch.setattr(case, "SeededTransport", EarlierTransport)
    result = asyncio.run(case.run_case(profile, tmp_path, None, None, {"127.0.0.1"},
        limits=case.SimpleNamespace(max_wall=3)))
    assert result["status"] == "time_limit"
    assert result["stop_reason"] == "wall_time"
    assert result["usage_uncertain"] is True


def test_inherited_runtime_resource_ledger_rejected_before_dns(monkeypatch, tmp_path, capsys):
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", str(tmp_path / "old-budget.json"))
    monkeypatch.setattr(case.matrix.REAL, "load_profile", lambda *_: profile)
    monkeypatch.setattr(case.matrix.REAL, "resolved_endpoint_ips", lambda *_: pytest.fail("DNS"))
    assert case.main(["--run", "--output-dir", str(tmp_path / "case"),
        "--expected-model", profile["model"],
        "--expected-endpoint-sha256", case.digest_bytes(profile["API_ENDPOINT"].encode()),
        "--expected-source-sha256", case.digest_bytes(case.IMPORTED_MODEL_PATH.read_bytes())]) == 2
    assert "requires SAGE_RESOURCE_BUDGET_CONFIG" in capsys.readouterr().out


def test_payload_metadata_identity_and_digest(monkeypatch):
    original = case.CALLBACK_C2_CONFIG
    digest = case.fixture_digest()
    monkeypatch.setattr(case, "CALLBACK_C2_CONFIG", {**original,
        "callback": {**original["callback"], "display_id": 8}})
    assert case.fixture_digest() != digest


@pytest.mark.parametrize("tool,args", [
    ("get_callback_c2_config", {"display_id": 8}),
    ("get_payload_c2_config", {"payload_uuid": "unknown-payload"}),
    ("get_c2_profiles_for_payload", {"payload": "merlin"}),
])
def test_unknown_payload_metadata_fails_closed_in_assembled_graph(
    monkeypatch, tmp_path, tool, args,
):
    monkeypatch.setenv("SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION", "65536")
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}

    class Ledger:
        def reserve(self, *_args):
            return "local-reservation"

        def settle_provider(self, *_args):
            return None

    sequence = [
        ("transfer_to_Mythic_Payload", {"handoff_title": "Inspect synthetic build",
            "handoff_instruction": "Read synthetic Payload metadata."}),
        (tool, args),
    ]

    class Provider(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = []

        async def handle_async_request(self, request):
            name, arguments = sequence[len(self.calls)]
            offered = {item.get("function", {}).get("name") for item in
                       json.loads(request.content).get("tools", [])}
            assert name in offered
            self.calls.append(name)
            return case._seed_response(request, profile["model"], len(self.calls) + 20,
                name, arguments, usage={"prompt_tokens": 100,
                "completion_tokens": 23, "total_tokens": 123})

    provider = Provider()
    original = case.SeededTransport

    class MeteredLocalTransport(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.inner = provider

    monkeypatch.setattr(case, "SeededTransport", MeteredLocalTransport)
    result = asyncio.run(asyncio.wait_for(case.run_case(
        profile, tmp_path, Ledger(), None, {"127.0.0.1"}), timeout=10))
    assert result["status"] == "harness_gap", result.get("error")
    assert provider.calls == [name for name, _ in sequence]
    assert len(result["accepted_local_builds"]) == 1
    assert result["effects"] == []
