"""Offline checks for the assembled Payload handoff measurement contract."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest


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
    assert row["max_real_calls"] == 4
    assert row["max_observed_tokens"] == 98304
    assert row["target_effects"] == 0
    assert row["fixture_sha256"] == case.fixture_digest()


def test_run_requires_identity_and_shared_ledger_before_dns(monkeypatch, capsys, tmp_path):
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
    monkeypatch.setattr(case.resource_budget, "configured_ledger", lambda: None)
    assert case.main(common) == 2
    assert "SAGE_RESOURCE_BUDGET" in capsys.readouterr().out


@pytest.mark.parametrize("change", [
    {"file_uuid": "00000000-0000-0000-0000-000000000001"},
    {"callback_display_id": 8},
    {"parameters": {"remote_path": "C:\\Temp\\wrong.txt"}},
])
def test_wrong_upload_identity_or_target_is_not_supported(change):
    args = {"command": "upload", "parameters": {"remote_path": case.DESTINATION,
                                             "file": case.FILE_UUID},
            "file_uuid": case.FILE_UUID, "callback_display_id": 7, "token_id": None}
    args.update(change)
    result = case.proposal_classification([{"tool_name": "upload_file_by_file_uuid",
                                            "arguments": args}])
    assert result["supported"] is False


def test_exact_pending_upload_can_be_supported():
    args = {"command": "upload", "parameters": {"remote_path": case.DESTINATION,
                                             "file": case.FILE_UUID},
            "file_uuid": case.FILE_UUID, "callback_display_id": 7, "token_id": None}
    assert case.proposal_classification([{"tool_name": "upload_file_by_file_uuid",
                                           "arguments": args}])["supported"]
    assert not case.proposal_classification([{"tool_name": "upload_file_by_file_uuid",
        "arguments": args}, {"tool_name": "delete_payload", "arguments": {}}])["supported"]


def test_registered_name_needs_production_preparation_binding():
    args = {"command": "upload", "parameters": {"remote_path": case.DESTINATION,
            "file": case.FILENAME}, "callback_display_id": 7}
    action = {"tool_name": "issue_task_and_waitfor_task_output", "arguments": args}
    assert not case.proposal_classification([action])["supported"]
    args["visibility_context"] = {"_sage_prepared_binding": {
        "file": {"agent_file_id": case.FILE_UUID}}}
    assert case.proposal_classification([action])["supported"]
    args["visibility_context"]["_sage_prepared_binding"]["file"]["agent_file_id"] = "wrong"
    assert not case.proposal_classification([action])["supported"]


def test_offline_scripted_continuation_recovers_missing_uuid_then_proposes_upload():
    def response(messages):
        request = httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions",
            json={"model": "fixture-model", "messages": messages, "stream": False,
                "tools": [{"function": {"name": name}} for name in
                    ("get_all_uploaded_files", "upload_file_by_file_uuid")]})
        return case.offline_scripted_continuation(request, "fixture-model", 99).json()[
            "choices"][0]["message"]["tool_calls"][0]["function"]["name"]
    assert response([{"role": "user", "content": case.PROMPT}]) == "get_all_uploaded_files"
    assert response([{"role": "tool", "content": json.dumps(case.BUILD_RESULT)}]) == \
        "upload_file_by_file_uuid"


@pytest.mark.parametrize("proposal_tool,proposal_args", [
    ("upload_file_by_file_uuid", {"command": "upload", "parameters": {
        "remote_path": case.DESTINATION, "file": case.FILE_UUID},
        "file_uuid": case.FILE_UUID, "callback_display_id": 7}),
    ("issue_task_and_waitfor_task_output", {"command": "upload", "parameters": {
        "remote_path": case.DESTINATION, "file": case.FILENAME}, "callback_display_id": 7}),
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
    assert result["status"] == "observed", result.get("error")
    assert result["accepted_local_builds"] and result["classification"]["supported"]
    assert result["effects"] == []
    assert result["provider_requests"] == []
    if proposal_tool == "upload_file_by_file_uuid":
        assert result["fixture_reads"] == []  # Current source carries the build result.


def test_run_cli_reaches_assembled_case_with_inert_route_and_ledger(
    monkeypatch, tmp_path, capsys,
):
    profile = {"provider": "openai", "model": "fixture-model",
               "API_ENDPOINT": "http://127.0.0.1:9/v1", "API_KEY": "inert"}

    class Ledger:
        def status(self):
            return {"unknown_provider_usage": False, "provider_overrun": False}

    original = case.SeededTransport

    class OfflineTransport(original):
        async def handle_async_request(self, request):
            if self.seed_index < len(case.SEEDS):
                return await super().handle_async_request(request)
            return case.offline_scripted_continuation(request, self.model, 99)

    monkeypatch.setattr(case, "SeededTransport", OfflineTransport)
    monkeypatch.setattr(case.matrix.REAL, "load_profile", lambda *_: profile)
    monkeypatch.setattr(case.matrix.REAL, "resolved_endpoint_ips", lambda *_: {"127.0.0.1"})
    monkeypatch.setattr(case.matrix.REAL, "sage_tls_context", lambda: None)
    monkeypatch.setattr(case.resource_budget, "configured_ledger", Ledger)
    output = tmp_path / "case"
    args = ["--run", "--config", str(tmp_path / "config"), "--output-dir", str(output),
        "--expected-model", profile["model"],
        "--expected-endpoint-sha256", case.digest_bytes(profile["API_ENDPOINT"].encode()),
        "--expected-source-sha256", case.digest_bytes(case.IMPORTED_MODEL_PATH.read_bytes())]
    assert case.main(args) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "observed"
    result = json.loads((output / "case.json").read_text())
    assert result["objective_success"] is True
    assert result["effects"] == []


@pytest.mark.parametrize("reservation,expected_success", [(65536, True), (32768, False)])
def test_posthandoff_uses_bounded_metered_provider_transport(
    monkeypatch, tmp_path, reservation, expected_success,
):
    monkeypatch.setenv("SAGE_RESOURCE_PROVIDER_TOKEN_RESERVATION", str(reservation))
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

    class Provider(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return case.offline_scripted_continuation(request, profile["model"], 99,
                usage={"prompt_tokens": 100, "completion_tokens": 23, "total_tokens": 123})

    class MeteredLocalTransport(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.inner.transport = Provider()

    monkeypatch.setattr(case, "SeededTransport", MeteredLocalTransport)
    result = asyncio.run(asyncio.wait_for(case.run_case(
        profile, tmp_path, shared, None, {"127.0.0.1"}), timeout=10))
    if not expected_success:
        assert result["status"] == "error"
        assert result["transport_rejections"][-1]["reason"] == "per_call_reservation_headroom"
        assert shared.reservations == [] and result["provider_requests"] == []
        return
    assert result["status"] == "observed", result.get("error")
    assert result["objective_success"] and result["effects"] == []
    assert len(shared.reservations) == len(shared.settlements) == len(result["provider_requests"]) == 1
    assert shared.settlements[0][1] == 123
    assert result["posthandoff_input_bytes"][0] > 0
    assert result["provider_requests"][0]["request"]["messages"]
    assert result["model_calls"][-1]["usage"][0]["total_tokens"] == 123
