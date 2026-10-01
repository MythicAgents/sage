"""Offline assembled checks for the matrix raw runner's durable recovery seams."""
import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.hillclimb import bare_mythic_tools
from ai.langgraph import controller_recovery
from ai.hillclimb.bare_runner import BareModelRunner, BareRunnerRecoveryError
from ai.hillclimb import trust_walker_pilot as pilot


SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-eval-gauge/scripts/run_trust_walker_pilot.py"
SPEC = importlib.util.spec_from_file_location("matrix_runtime_recovery_cli", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(cli)


class Crash(BaseException):
    pass


USAGE = {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
CALL = {"tool": "issue_command", "args": {
    "callback_display_id": 7, "command": "whoami", "parameters": "", "timeout": 10,
}}


def _runtime(tmp_path, monkeypatch, model, *, resume=False):
    obj = object.__new__(cli.RuntimePilot)
    obj.artifact_dir = tmp_path
    obj.source_root = tmp_path
    obj.current_client = SimpleNamespace(current_operation_id=8)
    obj.current_operation_id = 8
    obj.current_callback_id = 7
    obj.current_arm = "raw-mythic-bloodhound"
    obj.safe_route = {"provider": "openai", "model": "synthetic", "endpoint_identity": "route-1"}
    obj.live_authority_receipt = {"authority_id": "synthetic"}
    obj.recovery_enabled = True
    obj._resume_arm_active = resume
    monkeypatch.setattr(cli.RuntimePilot, "runtime_identity", lambda self: {
        "operation_id": 8, "callback_id": 7, "callback_uuid": "uuid-one",
        "arm": self.current_arm,
    })
    monkeypatch.setattr(cli, "_source_identity_at", lambda _: "source-one")
    monkeypatch.setattr(cli.bare_bloodhound, "load_bloodhound_mcp_surface", lambda: ([], {}, None))
    monkeypatch.setattr(cli.bare_bloodhound, "make_collection_ingestor", lambda _: None)
    monkeypatch.setattr(cli.bare_bloodhound, "make_bloodhound_dispatcher", lambda _: lambda __: "unused")
    monkeypatch.setattr(cli, "make_stdout_logger", lambda: None)
    monkeypatch.setattr(cli.RuntimePilot, "_deadline_model", lambda self, _: model)
    monkeypatch.setattr(cli.RuntimePilot, "_validate_one_raw_response", lambda self, _: None)
    return obj


def test_bounded_raw_runner_uses_original_arm_start_even_after_setup_delay(tmp_path, monkeypatch):
    obj = _runtime(tmp_path, monkeypatch, lambda *_: pytest.fail("late model call"))
    obj.recovery_enabled = False
    obj.matrix_wall_seconds = 3600
    obj.current_started_at = datetime.now(UTC) - timedelta(seconds=3601)
    result = obj._raw_runner(time.monotonic() - 1, lambda _: None).run("objective")
    assert result.termination["cause"] == "wall_time"
    assert result.model_attempts == 0


def test_raw_recovery_checkpoint_binds_explicit_reasoning_effort(tmp_path, monkeypatch):
    model = lambda *_: {"final": "STUCK", "_usage": USAGE}
    first = _runtime(tmp_path, monkeypatch, model)
    first.raw_reasoning_effort = "none"
    first._raw_runner(None, lambda _: None).run("objective")
    saved = json.loads((tmp_path / "raw-runner-checkpoint.json").read_text())
    assert saved["binding"]["caller"]["raw_reasoning_effort"] == "none"

    changed = _runtime(tmp_path, monkeypatch, model, resume=True)
    changed.raw_reasoning_effort = "high"
    with pytest.raises(BareRunnerRecoveryError, match="checkpoint binding differs"):
        changed._raw_runner(None, lambda _: None).run("objective")


def test_wrapped_adapter_deadline_stays_wall_time_and_early_error_does_not():
    def sdk_wraps_timeout():
        try:
            time.sleep(0.1)
        except TimeoutError as exc:
            raise RuntimeError("SDK wrapped the deadline signal") from exc

    result = BareModelRunner(
        lambda *_: cli._call_before_deadline(time.monotonic() + 0.02, sdk_wraps_timeout),
        lambda _: pytest.fail("no task after the deadline"), timeout=0.02,
        provider_timeout_is_wall=False,
    ).run("objective")
    assert result.termination == {"cause": "wall_time", "subtype": "wrapper_timeout"}
    assert result.model_calls == 0

    def early_provider_failure():
        raise RuntimeError("provider connection failed before the arm deadline")

    early = BareModelRunner(
        lambda *_: cli._call_before_deadline(time.monotonic() + 5, early_provider_failure),
        lambda _: pytest.fail("no task after provider failure"), timeout=5,
        provider_timeout_is_wall=False,
    ).run("objective")
    assert early.termination == {"cause": "other_declared_boundary", "subtype": "model_error"}


def test_raw_runner_cold_resume_uses_saved_task_without_duplicate_submission(tmp_path, monkeypatch):
    counts = {"issue": 0, "wait": 0, "query": 0}

    async def issue_task(_client, **kwargs):
        counts["issue"] += 1
        assert kwargs["callback_display_id"] == 7 and kwargs["command_name"] == "whoami"
        return {"display_id": 42}

    async def waitfor_for_task_output(_client, **kwargs):
        counts["wait"] += 1
        if counts["wait"] == 1:
            raise Crash()
        assert kwargs["task_display_id"] == 42
        return b"known output"

    async def execute_custom_query(_client, _query, *, variables):
        counts["query"] += 1
        assert variables == {"operation": 8, "task": 42}
        return {"task": [{"display_id": 42, "command_name": "whoami", "original_params": "",
                          "callback": {"display_id": 7}}]}

    sdk = SimpleNamespace(issue_task=issue_task, waitfor_for_task_output=waitfor_for_task_output,
                          execute_custom_query=execute_custom_query)
    monkeypatch.setattr(bare_mythic_tools, "_mythic_sdk", lambda: sdk)
    monkeypatch.setitem(sys.modules, "mythic", SimpleNamespace(mythic=sdk))
    first = _runtime(tmp_path, monkeypatch, lambda *_: {**CALL, "_usage": USAGE})
    with pytest.raises(Crash):
        first._raw_runner(None, lambda _: None).run("objective")
    saved = json.loads((tmp_path / "raw-runner-checkpoint.json").read_text())
    assert saved["phase"] == "tool_pending" and saved["pending_task_id"] == 42
    assert saved["pending_call"] == CALL
    assert counts == {"issue": 1, "wait": 1, "query": 0}

    def final_model(_system, _tools, history):
        assert history[0]["obs"] == "known output"
        return {"final": "STUCK", "_usage": USAGE}

    second = _runtime(tmp_path, monkeypatch, final_model, resume=True)
    result = second._raw_runner(None, lambda _: None).run("objective")
    assert result.stopped == "done" and result.steps == 1 and result.model_calls == 2
    assert result.total_tokens == 10
    assert counts == {"issue": 1, "wait": 2, "query": 1}


def test_dispatcher_persistence_failure_stops_before_wait(monkeypatch):
    counts = {"issue": 0, "wait": 0}

    async def issue_task(*_args, **_kwargs):
        counts["issue"] += 1
        return {"display_id": 42}

    async def waitfor_for_task_output(*_args, **_kwargs):
        counts["wait"] += 1
        return b"should not wait"

    sdk = SimpleNamespace(issue_task=issue_task, waitfor_for_task_output=waitfor_for_task_output)
    monkeypatch.setattr(bare_mythic_tools, "_mythic_sdk", lambda: sdk)

    def failed_persistence(task_id):
        assert task_id == 42
        raise BareRunnerRecoveryError("journal unavailable")

    dispatch = bare_mythic_tools.make_mythic_dispatcher(
        SimpleNamespace(), task_submitted_sink=failed_persistence,
    )
    with pytest.raises(BareRunnerRecoveryError, match="journal unavailable"):
        dispatch(CALL)
    assert counts == {"issue": 1, "wait": 0}


@pytest.mark.parametrize("changed", ["operation", "callback"])
def test_restore_runtime_rejects_changed_operation_or_callback(tmp_path, monkeypatch, changed):
    arm = "raw-mythic-bloodhound"
    obj = object.__new__(cli.RuntimePilot)
    obj.artifact_dir = tmp_path
    obj.source_root = tmp_path
    obj.native = SimpleNamespace(login=lambda: _login(9 if changed == "operation" else 8))
    obj.current_chat_channel_id = None
    obj.current_chat_request_id = None
    obj.recovery_enabled = True
    monkeypatch.setattr(cli.RuntimePilot, "_activate_source", lambda self, _: None)
    monkeypatch.setattr(cli, "_source_identity_at", lambda _: "source-one")
    monkeypatch.setattr(cli.RuntimePilot, "_engagement_id", lambda self, _: "engagement-one")
    monkeypatch.setattr(cli.RuntimePilot, "_resume_callback_identity", lambda self: {
        "agent_callback_id": "uuid-two" if changed == "callback" else "uuid-one",
        "init_callback": "created-one",
    })
    identity = {
        "operation_id": 8, "callback_id": 7, "engagement_id": "engagement-one",
        "source_root": str(tmp_path),
        "resume_callback": {"agent_callback_id": "uuid-one", "init_callback": "created-one"},
    }
    with pytest.raises(cli.PilotError, match="different Mythic operation|callback changed"):
        obj.restore_runtime(arm, {"source_identity": "source-one"}, identity, [])


async def _login(operation_id):
    return SimpleNamespace(current_operation_id=operation_id)


async def _ready():
    return True


def test_cold_recovery_rejects_changed_source_before_login(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda path: path)
    monkeypatch.setattr(cli, "read_records", lambda _: [{
        "record_type": "arm_started", "arm": "raw-mythic-bloodhound",
        "runtime_identity": {"operation_id": 8, "callback_id": 7, "source_root": str(tmp_path)},
        "start_identity": {"source_identity": "source-one"},
    }])
    monkeypatch.setattr(cli, "_source_identity_at", lambda _: "source-two")
    with pytest.raises(cli.PilotError, match="source root no longer matches"):
        cli.RuntimePilot.for_recovery(tmp_path)


def test_cleanup_only_source_drift_requires_original_callback_lineage(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda path: path)
    callback = {"operation_id": 8, "display_id": 7,
                "agent_callback_id": "uuid-one", "init_callback": "created-one"}
    rows = [{"record_type": "arm_started", "arm": "raw-mythic-bloodhound",
             "runtime_identity": {"operation_id": 8, "callback_id": 7,
                                  "source_root": str(tmp_path), "resume_callback": callback},
             "start_identity": {"source_identity": "source-one"}}]
    monkeypatch.setattr(cli, "read_records", lambda _: rows)
    monkeypatch.setattr(cli, "_source_identity_at", lambda _: "source-two")
    monkeypatch.setattr(cli, "_orchestrate", lambda _: SimpleNamespace())
    monkeypatch.setattr(cli, "_native_chat", lambda *args: SimpleNamespace(login=lambda: _login(8)))
    monkeypatch.setattr(cli.RuntimePilot, "_resume_callback_identity", lambda self: dict(callback))

    with pytest.raises(cli.PilotError, match="source root no longer matches"):
        cli.RuntimePilot.for_recovery(tmp_path)
    recovered, _start, _execution = cli.RuntimePilot.for_recovery(
        tmp_path, cleanup_source_drift_reason="operator interrupted; evaluator source changed")
    assert recovered.cleanup_source_binding == {
        "original_source_identity": "source-one", "cleanup_source_identity": "source-two",
        "reason": "operator interrupted; evaluator source changed",
        "callback_lineage_verified": True,
    }
    monkeypatch.setattr(cli.RuntimePilot, "_resume_callback_identity", lambda self: {
        **callback, "agent_callback_id": "different-uuid"})
    with pytest.raises(cli.PilotError, match="callback UUID lineage"):
        cli.RuntimePilot.for_recovery(
            tmp_path, cleanup_source_drift_reason="operator interrupted; evaluator source changed")


def test_restore_runtime_rejects_source_drift_before_login(tmp_path, monkeypatch):
    arm = "raw-mythic-bloodhound"
    for relative in (
        "Payload_Type/sage/main.py",
        "skills/sage-goad-reset/scripts/sage_restart.sh",
        "skills/sage-eval-gauge/scripts/orchestrate.py",
        ".venv/bin/python",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    obj = object.__new__(cli.RuntimePilot)
    obj.source_roots = {arm: tmp_path}
    obj.source_identities = {arm: "source-one"}
    obj.protected_manifest = tmp_path / "protected.json"
    obj.native = SimpleNamespace(login=lambda: pytest.fail("login after source drift"))
    monkeypatch.setattr(cli, "_source_identity_at", lambda _: "source-two")
    with pytest.raises(cli.PilotError, match="source identity mismatch before reset"):
        obj.restore_runtime(arm, {}, {"operation_id": 8}, [])


def test_recovery_cli_requires_matrix_route_and_explicit_authority_scope(tmp_path, monkeypatch, capsys):
    args = ["run-pair", "--artifact-dir", str(tmp_path),
            "--protected-manifest", str(tmp_path / "protected.json"),
            "--route-smoke", str(tmp_path / "smoke.json"), "--resumable"]
    assert cli.main(args) == 1
    assert "explicitly bound matrix pairs" in capsys.readouterr().out

    matrix = pilot.MatrixContract(schema=pilot.MATRIX_SCHEMA, routes=(
        pilot.MatrixRoute("synthetic", "openai", "synthetic-model", "https://example.invalid/api"),
    ))
    pair = pilot.MatrixPairContract.for_route(matrix, "synthetic", start_case="samwell-castelblack")
    protocol = tmp_path / "protocol.md"
    protocol.write_text("synthetic prospective protocol\n")
    record = {
        "schema": cli.MATRIX_LIVE_AUTHORITY_SCHEMA, "status": "active", "revoked_at": None,
        "authority_id": "synthetic", "authorization_source": "synthetic fixture",
        "matrix_identity": matrix.identity, "pair_contract_identity": pair.identity,
        "protocol_binding": {"file": protocol.name, "sha256": hashlib.sha256(protocol.read_bytes()).hexdigest()},
        "allowed_route_ids": ["synthetic"], "allowed_start_cases": ["samwell-castelblack"],
        "snapshot_bindings": {"samwell-castelblack": cli.SNAPSHOT},
        "no_numeric_arm_ceilings": True,
        "environment_binding": {"range_profile": cli.RANGE_PROFILE, "target_domain": "essos.local",
                                "target_io": "Mythic payload tasks only", "reset": "full reset before each arm"},
        "allowed_commands": ["run-pair"],
        "scope": {"pair_order": "shipping-first", "provider": pair.provider, "model": pair.model,
                  "api_endpoint": pair.route.api_endpoint, "objective": pair.objective,
                  "provider_request_timeout_seconds": 900, "automatic_replacement_arms": 0,
                  "response_to_refusal": "retain as observed outcome; do not change Sage or add a replacement arm",
                  "safety_stops": ["authority or target boundary violation", "wrong model or route",
                                   "corrupt start", "uncontrolled activity", "loss of interpretable evidence"],
                  "cleanup": "settle and archive each arm before the next reset; retain the final Mythic state after the batch"},
    }
    authority = tmp_path / "authority.json"
    authority.write_text(json.dumps(record))
    request = SimpleNamespace(command="run-pair", live_authority_file=str(authority),
                              live_authority_id=None, resumable=True, resume=False)
    with pytest.raises(cli.PilotError, match="same_arm_recovery"):
        cli._resolve_live_authority(request, pair)
    record["scope"]["same_arm_recovery"] = {
        "enabled": True, "ambiguous_submission": "stop", "model_request_without_response": "stop",
    }
    authority.write_text(json.dumps(record))
    assert cli._resolve_live_authority(request, pair)["contract_binding_sha256"] == pair.identity


def _shipping_resume_runtime(tmp_path, *, original_status="processing", ready=True):
    calls = {"snapshot": [], "wait": [], "create": [], "readiness": 0}

    async def fetch_request_snapshot(_client, request_id):
        calls["snapshot"].append(request_id)
        return {"request": {"id": request_id, "status": original_status}}

    async def wait_for_request(_client, request_id, **kwargs):
        calls["wait"].append((request_id, kwargs))
        return {"request": {"id": request_id, "status": "completed"}}

    async def create_message(_client, channel_id, body):
        calls["create"].append((channel_id, body))
        return {"chat_request_id": 20}

    async def inspect_readiness(_client):
        calls["readiness"] += 1
        return {"ready": ready}

    obj = object.__new__(cli.RuntimePilot)
    obj.current_operation_id = 8
    obj.current_chat_channel_id = 9
    obj.current_chat_request_id = 10
    obj.current_original_chat_request_id = 10
    obj.current_arm = "shipping-sage"
    obj.current_client = SimpleNamespace()
    obj.source_root = tmp_path
    obj._resume_records = []
    obj.native = SimpleNamespace(
        TERMINAL_STATUSES={"completed", "error"},
        fetch_request_snapshot=fetch_request_snapshot,
        wait_for_request=wait_for_request,
        create_message=create_message,
        inspect_readiness=inspect_readiness,
        request_snapshot_result=lambda snapshot: {"request_id": snapshot["request"]["id"]},
    )
    return obj, calls


def test_native_live_controller_lock_waits_original_without_new_message(tmp_path, monkeypatch):
    obj, calls = _shipping_resume_runtime(tmp_path)
    monkeypatch.setattr(controller_recovery.ControllerRecoveryStore, "inspect", lambda _: {
        "exists": True, "active": not bool(calls["wait"]),
        "phase": "terminal" if calls["wait"] else "effect_pending",
        "original_request_id": "chat:9:request:10",
    })
    result = asyncio.run(obj._resume_shipping_request(lambda _: None))
    assert result == {"request_id": 10}
    assert calls["wait"][0][0] == 10 and calls["create"] == []
    assert calls["readiness"] == 0


@pytest.mark.parametrize("prior_recovery_refused", [False, True])
def test_native_clean_inactive_creates_exact_restore_message_with_receipt(tmp_path, monkeypatch, prior_recovery_refused):
    obj, calls = _shipping_resume_runtime(tmp_path, original_status="completed" if prior_recovery_refused else "processing")
    if prior_recovery_refused:
        obj.current_chat_request_id = 11
        obj._resume_records = [
            {"record_type": "progress", "arm": "shipping-sage", "event": {"event": event}}
            for event in ("resume_request_intent", "resume_request_started")
        ]
    monkeypatch.setattr(cli.RuntimePilot, "_sage_resume_ready", lambda self: _ready())
    monkeypatch.setattr(controller_recovery.ControllerRecoveryStore, "inspect", lambda _: {
        "exists": True, "active": False,
        "phase": "terminal" if calls["wait"] else "clean",
        "original_request_id": "chat:9:request:10",
    })
    events = []
    result = asyncio.run(obj._resume_shipping_request(events.append))
    assert result == {"request_id": 20}
    assert calls["create"] == [(9, "SAGE_RESUME_CONTROLLER 10")]
    assert calls["wait"][0][0] == 20
    assert [event["event"] for event in events] == ["resume_request_intent", "resume_request_started"]
    assert events[1]["chat_request_id"] == 20 and obj.current_chat_request_id == 20


def test_native_inactive_pending_boundary_stops_without_message(tmp_path, monkeypatch):
    obj, calls = _shipping_resume_runtime(tmp_path)
    monkeypatch.setattr(controller_recovery.ControllerRecoveryStore, "inspect", lambda _: {
        "exists": True, "active": False, "phase": "effect_pending",
        "original_request_id": "chat:9:request:10",
    })
    with pytest.raises(pilot.PairRecoveryBlocked, match="effect_pending boundary"):
        asyncio.run(obj._resume_shipping_request(lambda _: None))
    assert calls["create"] == [] and calls["wait"] == []


def test_native_missing_resume_receipt_stops_without_second_message(tmp_path, monkeypatch):
    obj, calls = _shipping_resume_runtime(tmp_path)
    obj._resume_records = [{"record_type": "progress", "arm": "shipping-sage",
                            "event": {"event": "resume_request_intent"}}]
    monkeypatch.setattr(controller_recovery.ControllerRecoveryStore, "inspect", lambda _: {
        "exists": True, "active": False, "phase": "clean",
        "original_request_id": "chat:9:request:10",
    })
    with pytest.raises(pilot.PairRecoveryBlocked, match="no saved receipt"):
        asyncio.run(obj._resume_shipping_request(lambda _: None))
    assert calls["create"] == [] and calls["wait"] == []


def test_native_terminal_original_reuses_result_without_store_or_message(tmp_path, monkeypatch):
    obj, calls = _shipping_resume_runtime(tmp_path, original_status="completed")
    monkeypatch.setattr(controller_recovery.ControllerRecoveryStore, "inspect",
                        lambda _: pytest.fail("terminal request inspected controller store"))
    assert asyncio.run(obj._resume_shipping_request(lambda _: None)) == {"request_id": 10}
    assert calls["create"] == [] and calls["wait"] == [] and calls["readiness"] == 0


def test_resumed_shipping_arm_rejects_changed_channel_route_before_reattach(tmp_path, monkeypatch):
    obj = object.__new__(cli.RuntimePilot)
    obj.current_client = SimpleNamespace()
    obj.current_operation_id = 8
    obj.current_chat_channel_id = 9
    obj.current_chat_request_id = 10
    obj.current_arm = "shipping-sage"
    obj.current_started_at = datetime(2026, 9, 29, tzinfo=UTC)
    obj._resume_arm_active = True
    obj.selected_route = SimpleNamespace(route_id="synthetic")
    obj.route = {"provider": "openai", "model": "synthetic-model",
                 "api_endpoint": "https://example.invalid/api", "api_key": "synthetic-key"}
    obj.measurement_metadata = {}

    async def channel_identity(_client, channel_id):
        assert channel_id == 9
        return {"provider": "openai", "model": "other-model", "route": "route-one"}

    obj.native = SimpleNamespace(
        default_ai_metadata=lambda: {"config": {}},
        fetch_channel_runtime_identity=channel_identity,
        _chat_runtime_identity_from_metadata=lambda _: {
            "provider": "openai", "model": "synthetic-model", "route": "route-one",
        },
        RequestWaitTimeout=TimeoutError,
    )
    monkeypatch.setattr(cli.RuntimePilot, "_resume_shipping_request",
                        lambda *_: pytest.fail("route drift reached reattach"))
    with pytest.raises(cli.PilotError, match="channel route differs"):
        obj.run_arm("shipping-sage", {}, lambda _: None)


@pytest.mark.parametrize("reason,expected", [
    ("wall-clock budget exhausted", "time_limit"),
    ("token budget exhausted", "completed"),
])
def test_native_arm_records_controller_wall_stop_without_relabeling_other_stops(tmp_path, reason, expected):
    obj, _ = _shipping_resume_runtime(tmp_path)
    obj.selected_route = SimpleNamespace(route_id="synthetic")
    obj.matrix_wall_seconds = 3600
    obj.route = {"provider": "openai", "model": "synthetic-model",
                 "api_endpoint": "https://example.invalid/api", "api_key": "synthetic-key"}
    obj._channel_name = lambda _: "synthetic-channel"
    captured = {}

    async def turn(_client, _objective, **kwargs):
        captured.update(kwargs)
        return {"status": "completed", "runtime_telemetry": {
            "controller_status": "halted_budget", "controller_terminal_reason": reason}}

    obj.native.default_ai_metadata = lambda: {"config": {}}
    obj.native._chat_runtime_identity_from_metadata = lambda _: {"route": "synthetic"}
    obj.native.run_native_chat_turn = turn
    obj.native.RequestWaitTimeout = TimeoutError
    result = obj.run_arm("shipping-sage", {}, lambda _: None)
    assert result["termination"] == expected
    assert 3590 < captured["timeout_seconds"] <= 3600
    assert datetime.fromisoformat(result["arm_deadline_at"]) - datetime.fromisoformat(
        result["arm_started_at"]) == timedelta(seconds=3600)


def test_native_resume_identity_wait_cannot_extend_original_deadline(tmp_path, monkeypatch):
    obj, _ = _shipping_resume_runtime(tmp_path)
    obj.selected_route = SimpleNamespace(route_id="synthetic")
    obj.matrix_wall_seconds = 3600
    obj._resume_arm_active = True
    obj.current_started_at = datetime.now(UTC) - timedelta(seconds=3599.95)
    original_start = obj.current_started_at
    obj.route = {"provider": "openai", "model": "synthetic-model",
                 "api_endpoint": "https://example.invalid/api", "api_key": "synthetic-key"}
    obj.native.default_ai_metadata = lambda: {"config": {}}
    obj.native.RequestWaitTimeout = type("NativeWaitTimeout", (TimeoutError,), {})

    async def slow_identity(*_):
        await asyncio.sleep(10)
        pytest.fail("identity wait crossed original deadline")

    obj.native.fetch_channel_runtime_identity = slow_identity
    monkeypatch.setattr(cli.RuntimePilot, "_resume_shipping_request",
                        lambda *_: pytest.fail("expired arm submitted a resume"))
    result = obj.run_arm("shipping-sage", {}, lambda _: None)
    assert result["termination"] == "time_limit"
    assert result["arm_started_at"] == original_start.isoformat()
