import base64
import importlib.util
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import shutil
from types import SimpleNamespace

import pytest

from ai.hillclimb.trust_walker_outcome import classify_terminal_snapshot
from ai.hillclimb.trust_walker_pilot import summarize_pair


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-eval-gauge" / "scripts" / "run_trust_walker_pilot.py"
SPEC = importlib.util.spec_from_file_location("trust_walker_pilot_cli", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(cli)


def setup_start(**changes):
    value = {
        "snapshot": cli.SNAPSHOT,
        "range_profile": cli.RANGE_PROFILE,
        "inventory": list(cli.CANONICAL_INVENTORY),
        "clock_ready": True,
        "mythic_clean": True,
        "bloodhound_clean": True,
        "foothold": {
            "count": 1,
            "principal": r"NORTH\samwell.tarly",
            "host": "CASTELBLACK",
            "payload_type": "apollo",
        },
        "route": {
            "provider": cli.PROVIDER,
            "model": cli.MODEL,
            "endpoint_identity": "route-a",
        },
        "objective_sha256": hashlib.sha256(cli.OBJECTIVE.encode()).hexdigest(),
        "source_identity": "tree-a",
        "tool_contract": "tools-a",
        "dependency_identity": "deps-a",
    }
    value.update(changes)
    return value


def standing_authority_document(*contracts, status="active", endpoint_identity="e" * 64):
    return {
        "schema": cli.STANDING_LIVE_AUTHORITY_SCHEMA,
        "authority_id": "user-sage-goad-trust-walker-standing-20260917",
        "status": status,
        "revoked_at": None,
        "granted_at": "2026-09-17T00:00:00Z",
        "scope_id": cli.STANDING_LIVE_AUTHORITY_SCOPE,
        "allowed_commands": sorted(cli.STANDING_LIVE_COMMANDS),
        "allowed_operation_classes": sorted(cli.STANDING_LIVE_OPERATION_CLASSES),
        "stop_and_reauthorize_on": sorted(cli.STANDING_LIVE_EXCLUSIONS),
        "environment_binding": {
            "range_profile": cli.RANGE_PROFILE,
            "snapshot": cli.SNAPSHOT,
            "target_domain": "essos.local",
            "target_io": "Mythic payload tasks only",
        },
        "contract_bindings": {
            contract.to_record()["schema"]: cli._live_contract_binding(contract)
            for contract in contracts
        },
        "successor_policy": {
            "source_identity_changes_allowed": True,
            "requires_in_scope_evaluator_or_staging_repair": True,
            "requires_distinct_source_roots": True,
            "requires_preledger_dependency_gate": True,
            "requires_unchanged_contract_binding": True,
        },
        "persistence": {
            "consumed_by_completed_run": False,
            "requires_new_conversation_authorization_for_in_scope_successor": False,
        },
        "endpoint_identity": endpoint_identity,
    }


def write_standing_authority(path: Path, *contracts, **changes) -> dict:
    document = standing_authority_document(*contracts)
    document.update(changes)
    path.write_text(json.dumps(document), encoding="utf-8")
    return document


def test_standing_authority_accepts_successor_identities_and_returns_safe_receipt(tmp_path):
    authority_path = tmp_path / "authority.json"
    frozen = cli.ImprovementContract("a" * 64, "b" * 64)
    write_standing_authority(authority_path, frozen)
    successor = cli.ImprovementContract("c" * 64, "d" * 64)

    receipt = cli._load_standing_live_authority(
        authority_path,
        command="run-improvement-pair",
        contract=successor,
    )

    assert receipt["mode"] == "standing"
    assert receipt["authority_id"] == "user-sage-goad-trust-walker-standing-20260917"
    assert receipt["expected_endpoint_identity"] == "e" * 64
    assert receipt["authority_sha256"] == hashlib.sha256(authority_path.read_bytes()).hexdigest()
    assert str(authority_path) not in json.dumps(receipt)


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        ({"status": "revoked", "revoked_at": "2026-09-18T00:00:00Z"}, "revoked"),
        ({"scope_id": "another-lab"}, "environment scope"),
        ({"allowed_commands": ["run-improvement-pair"]}, "live command"),
        ({"stop_and_reauthorize_on": []}, "stop boundaries"),
        ({"persistence": {"consumed_by_completed_run": True}}, "persistence policy"),
        ({"endpoint_identity": "bad"}, "endpoint identity"),
    ),
)
def test_standing_authority_fails_closed_on_scope_or_revocation_drift(tmp_path, change, reason):
    authority_path = tmp_path / "authority.json"
    contract = cli.ImprovementContract("a" * 64, "b" * 64)
    write_standing_authority(authority_path, contract, **change)

    with pytest.raises(cli.PilotError, match=reason):
        cli._load_standing_live_authority(
            authority_path,
            command="run-improvement-pair",
            contract=contract,
        )


def test_standing_authority_rejects_contract_or_preledger_route_drift(tmp_path):
    authority_path = tmp_path / "authority.json"
    improvement = cli.ImprovementContract("a" * 64, "b" * 64)
    write_standing_authority(authority_path, improvement)

    with pytest.raises(cli.PilotError, match="exact frozen live contract"):
        cli._load_standing_live_authority(
            authority_path,
            command="run-pair",
            contract=cli.PilotContract(),
        )

    receipt = cli._load_standing_live_authority(
        authority_path,
        command="run-improvement-pair",
        contract=improvement,
    )
    with pytest.raises(cli.PilotError, match="prepared route"):
        cli._bind_authority_to_preledger(receipt, {
            "ready": True,
            "ledger_allocated": False,
            "attempted_start": False,
            "route_identity": {
                "provider": cli.PROVIDER,
                "model": cli.MODEL,
                "endpoint_identity": "f" * 64,
            },
        })


def test_standing_authority_runtime_receipt_binds_route_and_snapshot(tmp_path):
    authority_path = tmp_path / "authority.json"
    contract = cli.PilotContract()
    write_standing_authority(authority_path, contract)
    receipt = cli._load_standing_live_authority(
        authority_path, command="run-pair", contract=contract,
    )
    preflight = {
        "route": {
            "provider": cli.PROVIDER,
            "model": cli.MODEL,
            "endpoint_identity": "e" * 64,
        },
        "foothold_bootstrap": {"fixture": {"snapshot": cli.SNAPSHOT}},
    }

    bound = cli._authority_preflight_receipt(receipt, preflight)

    assert bound["runtime_binding_verified"] is True
    preflight["foothold_bootstrap"]["fixture"]["snapshot"] = "another-snapshot"
    with pytest.raises(cli.PilotError, match="runtime route or snapshot"):
        cli._authority_preflight_receipt(receipt, preflight)


def test_plan_has_no_network_and_exposes_no_single_arm_surface(monkeypatch, capsys):
    monkeypatch.setattr(socket.socket, "connect", lambda *_a, **_k: pytest.fail("network attempted"))
    assert cli.main(["plan"]) == 0
    output = capsys.readouterr().out
    assert '"single_arm_surface": false' in output
    assert '"setup_rehearsal_surface": "rehearse-setup"' in output
    assert '"setup_rehearsal_continuation_surface": "continue-setup"' in output
    assert '"raw-first"' in output
    assert cli.OBJECTIVE in output


def test_setup_rehearsal_runs_two_compatible_starts_without_model_arm(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda _path: tmp_path)

    class Runtime:
        current_reset_started = False

        def __init__(self):
            self.resets = []
            self.cleanups = []

        def preflight(self, _contract):
            return {"ready": True}

        def reset(self, rehearsal_id, _env):
            self.current_reset_started = True
            self.resets.append(rehearsal_id)
            return setup_start()

        def runtime_identity(self):
            return {"operation_id": 8, "callback_id": 4}

        def teardown(self, rehearsal_id, _start):
            self.current_reset_started = False
            self.cleanups.append(rehearsal_id)
            return {"complete": True}

    runtime = Runtime()
    result = cli.run_setup_rehearsals(runtime, cli.PilotContract(), tmp_path)

    assert result["complete"] is True
    assert result["attempts"] == 2
    assert result["consecutive_successes"] == 2
    assert result["model_inference_calls"] == 0
    assert result["measured_arms_started"] == 0
    assert runtime.resets == ["setup-rehearsal-1", "setup-rehearsal-2"]
    assert runtime.cleanups == runtime.resets


def test_setup_rehearsal_recovers_one_mutated_reset_failure_within_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda _path: tmp_path)

    class Runtime:
        current_reset_started = False
        attempt = 0

        def preflight(self, _contract):
            return {"ready": True}

        def reset(self, _rehearsal_id, _env):
            self.attempt += 1
            self.current_reset_started = True
            if self.attempt == 1:
                raise RuntimeError("transient reset adapter failure")
            return setup_start()

        def runtime_identity(self):
            return {"operation_id": 8, "callback_id": 4}

        def teardown(self, _rehearsal_id, _start):
            self.current_reset_started = False
            return {"complete": True}

    result = cli.run_setup_rehearsals(Runtime(), cli.PilotContract(), tmp_path)
    records = cli.read_records(tmp_path / "setup-rehearsals.jsonl")

    assert result["complete"] is True
    assert result["attempts"] == 3
    first = next(row for row in records if row.get("record_type") == "setup_attempt")
    assert first["success"] is False
    assert first["recoverable"] is True


def test_setup_rehearsal_records_and_recovers_orchestrator_system_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda _path: tmp_path)

    class Runtime:
        current_reset_started = False
        attempt = 0

        def preflight(self, _contract):
            return {"ready": True}

        def reset(self, _rehearsal_id, _env):
            self.attempt += 1
            self.current_reset_started = True
            if self.attempt == 1:
                raise SystemExit("ABORT: reset step failed")
            return setup_start()

        def runtime_identity(self):
            return {"operation_id": 8, "callback_id": 4}

        def teardown(self, _rehearsal_id, _start):
            self.current_reset_started = False
            return {"complete": True}

    result = cli.run_setup_rehearsals(Runtime(), cli.PilotContract(), tmp_path)
    records = cli.read_records(tmp_path / "setup-rehearsals.jsonl")

    assert result["complete"] is True
    assert result["attempts"] == 3
    first = next(row for row in records if row.get("record_type") == "setup_attempt")
    assert first["error_type"] == "SystemExit"
    assert first["reason"] == "ABORT: reset step failed"
    assert first["recoverable"] is True


def test_setup_rehearsal_continuation_counts_orphaned_attempt_and_allows_only_three_more(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda _path: tmp_path)
    authority_id = "authorized-continuation"
    cli.append_record(tmp_path / "setup-rehearsals.jsonl", {
        "record_type": "setup_preflight",
        "ready": True,
        "live_authority_id": authority_id,
        "setup_cleanup_elapsed_seconds": 7.0,
    })
    cli.append_record(tmp_path / "setup-rehearsals.jsonl", {
        "record_type": "setup_attempted",
        "attempt": 1,
        "rehearsal_id": "setup-rehearsal-1",
    })
    (tmp_path / "attempt-001-abort-report.json").write_text(json.dumps({
        "schema": "sage-trust-walker-setup-rehearsal-abort-v1",
        "authority_id": authority_id,
        "attempt": 1,
        "disposition": "stopped_before_reset_mutation",
        "failure": {"error_type": "SystemExit", "reason": "stop failed"},
        "resource_and_activity_counts": {
            "setup_attempts_consumed": 1,
            "model_provider_calls": 0,
            "measured_arms_started": 0,
            "readiness_payload_tasks": 0,
        },
        "continuation": {"performed": False},
    }), encoding="utf-8")

    class Runtime:
        current_reset_started = False
        live_authority_id = authority_id

        def __init__(self):
            self.resets = []

        def preflight(self, _contract):
            return {"ready": True, "live_authority_id": authority_id}

        def reset(self, rehearsal_id, _env):
            self.current_reset_started = True
            self.resets.append(rehearsal_id)
            return setup_start()

        def runtime_identity(self):
            return {"operation_id": 8, "callback_id": 4}

        def teardown(self, _rehearsal_id, _start):
            self.current_reset_started = False
            return {"complete": True}

    runtime = Runtime()
    result = cli.run_setup_rehearsals(
        runtime, cli.PilotContract(), tmp_path, resume=True,
    )
    records = cli.read_records(tmp_path / "setup-rehearsals.jsonl")

    assert result["complete"] is True
    assert result["attempts"] == 3
    assert result["continued_after_no_mutation_abort"] is True
    assert runtime.resets == ["setup-rehearsal-2", "setup-rehearsal-3"]
    recovered = [row for row in records if row.get("record_type") == "setup_attempt"]
    assert recovered[0]["attempt"] == 1
    assert recovered[0]["cleanup"]["state"] == "not_required_no_mutation"


def test_setup_rehearsal_continuation_rejects_missing_abort_report(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda _path: tmp_path)
    cli.append_record(tmp_path / "setup-rehearsals.jsonl", {
        "record_type": "setup_preflight",
        "ready": True,
        "live_authority_id": "authorized-continuation",
        "setup_cleanup_elapsed_seconds": 1.0,
    })
    cli.append_record(tmp_path / "setup-rehearsals.jsonl", {
        "record_type": "setup_attempted",
        "attempt": 1,
        "rehearsal_id": "setup-rehearsal-1",
    })

    runtime = SimpleNamespace(live_authority_id="authorized-continuation")
    with pytest.raises(cli.PilotError, match="abort report is missing or malformed"):
        cli.run_setup_rehearsals(
            runtime, cli.PilotContract(), tmp_path, resume=True,
        )


def test_setup_rehearsal_stops_on_invariant_failure_after_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda _path: tmp_path)

    class Runtime:
        current_reset_started = False
        cleanups = 0

        def preflight(self, _contract):
            return {"ready": True}

        def reset(self, _rehearsal_id, _env):
            self.current_reset_started = True
            raise cli.SetupInvariantError("wrong range profile")

        def runtime_identity(self):
            pytest.fail("invariant failure cannot produce a runtime identity")

        def teardown(self, _rehearsal_id, _start):
            self.current_reset_started = False
            self.cleanups += 1
            return {"complete": True}

    runtime = Runtime()
    result = cli.run_setup_rehearsals(runtime, cli.PilotContract(), tmp_path)

    assert result["complete"] is False
    assert result["attempts"] == 1
    assert result["hard_stop"]["stage"] == "reset"
    assert runtime.cleanups == 1


def test_setup_rehearsal_exhausts_four_attempts_without_silent_fifth(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda _path: tmp_path)

    class Runtime:
        current_reset_started = False
        attempts = 0

        def preflight(self, _contract):
            return {"ready": True}

        def reset(self, _rehearsal_id, _env):
            self.attempts += 1
            self.current_reset_started = True
            raise RuntimeError("recoverable reset failure")

        def runtime_identity(self):
            pytest.fail("failed reset cannot produce a runtime identity")

        def teardown(self, _rehearsal_id, _start):
            self.current_reset_started = False
            return {"complete": True}

    runtime = Runtime()
    result = cli.run_setup_rehearsals(runtime, cli.PilotContract(), tmp_path)

    assert result["complete"] is False
    assert result["attempts"] == 4
    assert result["attempt_limit"] == 4
    assert result["hard_stop"]["stage"] == "attempt_limit"
    assert runtime.attempts == 4


@pytest.fixture
def retained_callback_fixture(tmp_path, monkeypatch):
    """Exercise fixture validation without an operator's ignored callback export."""
    path = tmp_path / "callback.json"
    path.write_text(json.dumps({
        "config": {"payload": {"uuid": cli.STAGED_FOOTHOLD_PAYLOAD_UUID}},
    }), encoding="utf-8")
    monkeypatch.setattr(cli, "STAGED_FOOTHOLD_CONFIG", path)
    monkeypatch.setattr(cli, "STAGED_FOOTHOLD_CONFIG_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())


def test_staged_snapshot_uses_interactive_launch_existing_path(retained_callback_fixture):
    runtime = object.__new__(cli.RuntimePilot)
    runtime.orchestrate = cli._orchestrate()

    foothold = runtime._staged_interactive_foothold()
    argv = foothold.launch_argv()

    assert argv[:2] == ["/bin/bash", cli.STAGED_FOOTHOLD_LAUNCHER]
    assert "--callback-settle-seconds" in argv
    assert argv[argv.index("--callback-settle-seconds") + 1] == "90"
    assert "--require-unique-callback" in argv
    assert argv[argv.index("--expected-task-state") + 1] == "Ready"
    assert argv[argv.index("--expected-remote-path") + 1] == cli.STAGED_FOOTHOLD_REMOTE_PATH
    assert argv[argv.index("--expected-remote-sha256") + 1] == cli.STAGED_FOOTHOLD_REMOTE_SHA256
    assert "skills/sage-foothold-launch/scripts/foothold_launch.py" not in argv


def test_staged_fixture_binds_snapshot_config_uuid_and_executable(retained_callback_fixture):
    fixture = cli._staged_foothold_fixture()

    assert fixture == {
        "snapshot": "sage-seed-apollo-v252-mythic4-20260829",
        "retained_config_sha256": cli.STAGED_FOOTHOLD_CONFIG_SHA256,
        "payload_uuid": "aef50036-02ad-4c7a-8786-7ca778281ad2",
        "task_name": "SageApolloBootstrap",
        "task_prelaunch_state": "Ready",
        "remote_path": cli.STAGED_FOOTHOLD_REMOTE_PATH,
        "remote_sha256": cli.STAGED_FOOTHOLD_REMOTE_SHA256,
    }


def test_staged_fixture_rejects_config_hash_mismatch(monkeypatch, retained_callback_fixture):
    monkeypatch.setattr(cli, "STAGED_FOOTHOLD_CONFIG_SHA256", "0" * 64)

    with pytest.raises(cli.PilotError, match="config hash mismatch"):
        cli._staged_foothold_fixture()


def test_staged_fixture_rejects_payload_uuid_mismatch(monkeypatch, tmp_path):
    path = tmp_path / "callback.json"
    path.write_text(json.dumps({"config": {"payload": {"uuid": "wrong"}}}), encoding="utf-8")
    monkeypatch.setattr(cli, "STAGED_FOOTHOLD_CONFIG", path)
    monkeypatch.setattr(cli, "STAGED_FOOTHOLD_CONFIG_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())

    with pytest.raises(cli.PilotError, match="payload UUID mismatch"):
        cli._staged_foothold_fixture()


def test_engagement_state_identity_is_scoped_to_attempt_and_arm():
    first = object.__new__(cli.RuntimePilot)
    first.artifact_dir = cli.ROOT / ".sage_history" / "evaluation" / "pilot" / "attempt-006"
    second = object.__new__(cli.RuntimePilot)
    second.artifact_dir = cli.ROOT / ".sage_history" / "evaluation" / "pilot" / "attempt-007"

    shipping = first._engagement_id("shipping-sage")

    assert shipping != "trust-walker-pilot-shipping-sage"
    assert "attempt-006" in shipping
    assert shipping != first._engagement_id("raw-mythic-bloodhound")
    assert shipping != second._engagement_id("shipping-sage")


def test_engagement_state_preflight_rejects_attempt_scoped_stale_file(monkeypatch, tmp_path):
    runtime = object.__new__(cli.RuntimePilot)
    runtime.artifact_dir = cli.ROOT / ".sage_history" / "evaluation" / "pilot" / "attempt-007"
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    stale = runtime._engagement_state_path("shipping-sage")
    stale.write_text("{}", encoding="utf-8")

    with pytest.raises(cli.PilotError, match="attempt-scoped engagement state already exists"):
        runtime._fresh_engagement_state()


def test_engagement_state_preflight_ignores_legacy_cross_attempt_key(monkeypatch, tmp_path):
    runtime = object.__new__(cli.RuntimePilot)
    runtime.artifact_dir = cli.ROOT / ".sage_history" / "evaluation" / "pilot" / "attempt-007"
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    (tmp_path / "state_trust-walker-pilot-shipping-sage.json").write_text("{}", encoding="utf-8")

    state = runtime._fresh_engagement_state()

    assert all(value["state_file_absent"] is True for value in state.values())


def test_bloodhound_service_preflight_rejects_unreachable_api():
    class Readiness:
        @staticmethod
        def probe_bloodhound_api_status():
            raise ConnectionError("connection refused")

    runtime = object.__new__(cli.RuntimePilot)
    runtime.native = SimpleNamespace(_readiness_contract_module=lambda: Readiness)

    with pytest.raises(cli.PilotError, match="BloodHound API is unreachable before setup reset"):
        runtime._bloodhound_service_preflight()


def test_bloodhound_service_preflight_retains_only_readiness_metadata():
    class Readiness:
        @staticmethod
        def probe_bloodhound_api_status():
            return {"status_code": 200, "domain_count": 3, "domains": ["not-retained"]}

    runtime = object.__new__(cli.RuntimePilot)
    runtime.native = SimpleNamespace(_readiness_contract_module=lambda: Readiness)

    assert runtime._bloodhound_service_preflight() == {
        "reachable": True,
        "status_code": 200,
        "domain_count": 3,
    }


def test_reset_propagates_attempt_scoped_engagement_id_to_sage_restart(monkeypatch, tmp_path):
    captured = {}

    def full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold):
        captured.update(restart_env)
        return {}, 2

    class Client:
        current_operation_id = 1

    async def login():
        return Client()

    async def inspect_readiness(_client, *, runtime_dbs_archived):
        assert runtime_dbs_archived is True
        return {
            "ready": True,
            "ludus": {
                "range_profile": {
                    "id": cli.RANGE_PROFILE,
                    "qualification": {"state": "live_qualified"},
                    "scenarios": [cli.SCENARIO],
                    "topology_identity": "topology",
                },
                "vms": [{"canonical_name": name} for name in cli.CANONICAL_INVENTORY],
            },
            "clock": {"ready": True},
            "foothold": {"callbacks": [{"display_id": 2, "payloadtype": "apollo"}]},
        }

    runtime = object.__new__(cli.RuntimePilot)
    runtime.artifact_dir = cli.ROOT / ".sage_history" / "evaluation" / "pilot" / "attempt-007"
    runtime.protected_manifest = tmp_path / "protected.json"
    runtime.orchestrate = SimpleNamespace(full_reset_and_ready=full_reset_and_ready)
    runtime.native = SimpleNamespace(login=login, inspect_readiness=inspect_readiness)
    runtime._staged_interactive_foothold = lambda: object()
    runtime._operation_snapshot = lambda *_args: {
        "task": [],
        "credential": [],
        "callback": [{
            "display_id": 2,
            "active": True,
            "host": "CASTELBLACK",
            "domain": "NORTH",
            "user": "samwell.tarly",
            "payload": {"payloadtype": {"name": "apollo"}},
        }],
    }
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", str(tmp_path / "bloodhound"))
    monkeypatch.setattr(cli, "_validate_protected", lambda _path: {})
    monkeypatch.setattr(cli, "assert_rejected_kernel_absent", lambda: None)
    monkeypatch.setattr(cli, "_staged_foothold_fixture", lambda: {})
    monkeypatch.setattr(cli, "_validate_external_paths", lambda: {})
    monkeypatch.setattr(cli, "_source_identity", lambda: "source")
    monkeypatch.setattr(cli, "_tool_contract", lambda: "tools")
    monkeypatch.setattr(cli, "_route", lambda _orchestrate: {
        "provider": cli.PROVIDER,
        "model": cli.MODEL,
        "api_endpoint": "https://model.invalid:7443",
        "api_key": "not-retained",
    })
    monkeypatch.setattr(
        cli.live_seams,
        "bloodhound_empty_state_evidence",
        lambda: {"returncode": 0, "domain_count": 0},
    )

    start = runtime.reset("shipping-sage", {})
    expected = runtime._engagement_id("shipping-sage")

    assert captured["SAGE_ENGAGEMENT_ID"] == expected
    assert "attempt-007" in expected
    assert runtime.runtime_identity()["engagement_id"] == expected
    assert start["mythic_clean"] is True


def test_callback_census_accepts_one_active_plus_imported_inactive_metadata():
    selected = cli._select_unique_active_callback([
        {"display_id": 1, "active": False},
        {"display_id": 2, "active": True, "host": "CASTELBLACK"},
    ], 2)

    assert selected["display_id"] == 2


@pytest.mark.parametrize(
    "callbacks",
    [
        [{"display_id": 1, "active": False}],
        [{"display_id": 2, "active": True}, {"display_id": 3, "active": True}],
    ],
)
def test_callback_census_rejects_other_than_one_active(callbacks):
    with pytest.raises(cli.PilotError, match="exactly one active callback"):
        cli._select_unique_active_callback(callbacks, 2)


def test_callback_census_rejects_wrong_selected_active_callback():
    with pytest.raises(cli.PilotError, match="does not match the selected foothold"):
        cli._select_unique_active_callback([{"display_id": 3, "active": True}], 2)


def test_cleanup_explicitly_deactivates_active_callbacks(monkeypatch):
    from mythic import mythic

    client = object()
    censuses = iter([
        [{"display_id": 2, "active": True}],
        [],
    ])
    updates = []

    async def fake_get_all_active_callbacks(observed_client):
        assert observed_client is client
        return next(censuses)

    async def fake_update_callback(observed_client, *, callback_display_id, active):
        assert observed_client is client
        updates.append((callback_display_id, active))
        return {"status": "success", "error": ""}

    monkeypatch.setattr(mythic, "get_all_active_callbacks", fake_get_all_active_callbacks)
    monkeypatch.setattr(mythic, "update_callback", fake_update_callback)
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_client = client

    result = runtime._deactivate_active_callbacks()

    assert result == {
        "ok": True,
        "deactivated_display_ids": [2],
        "active_callback_count": 0,
    }
    assert updates == [(2, False)]


def test_cleanup_rejects_callback_deactivation_failure(monkeypatch):
    from mythic import mythic

    async def fake_get_all_active_callbacks(_client):
        return [{"display_id": 2, "active": True}]

    async def fake_update_callback(_client, *, callback_display_id, active):
        return {"status": "error", "error": "denied"}

    monkeypatch.setattr(mythic, "get_all_active_callbacks", fake_get_all_active_callbacks)
    monkeypatch.setattr(mythic, "update_callback", fake_update_callback)
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_client = object()

    with pytest.raises(cli.PilotError, match="could not deactivate callback 2"):
        runtime._deactivate_active_callbacks()


def test_staged_snapshot_rejects_download_and_deploy_headless_path(retained_callback_fixture):
    class WrongFoothold:
        direct_retained_launch = False
        callback_settle_seconds = 90
        require_unique_callback = True

        def launch_argv(self):
            return [
                cli.PYTHON,
                "skills/sage-foothold-launch/scripts/foothold_launch.py",
                "launch",
            ]

    runtime = object.__new__(cli.RuntimePilot)
    runtime.orchestrate = SimpleNamespace(FootholdSpec=lambda **_kwargs: WrongFoothold())

    with pytest.raises(cli.PilotError, match="canonical interactive launch-existing path"):
        runtime._staged_interactive_foothold()


def test_staged_snapshot_rejects_incomplete_interactive_vector(retained_callback_fixture):
    class IncompleteFoothold:
        direct_retained_launch = False
        callback_settle_seconds = 90
        require_unique_callback = True

        def launch_argv(self):
            return ["/bin/bash", cli.STAGED_FOOTHOLD_LAUNCHER, "--require-unique-callback"]

    runtime = object.__new__(cli.RuntimePilot)
    runtime.orchestrate = SimpleNamespace(FootholdSpec=lambda **_kwargs: IncompleteFoothold())

    with pytest.raises(cli.PilotError, match="canonical interactive launch-existing path"):
        runtime._staged_interactive_foothold()


def test_response_validation_accepts_shipping_and_raw_shapes():
    shipping = [{
        "effective_backend": cli.EXPECTED_BACKEND,
        "effective_model_provider": cli.PROVIDER,
        "effective_model_id": cli.MODEL,
        "token_usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    }]
    raw = [{
        "effective_backend": cli.EXPECTED_BACKEND,
        "observed_provider": cli.PROVIDER,
        "observed_model": cli.MODEL,
        "input_tokens": 7, "output_tokens": 3, "total_tokens": 10,
    }]
    assert cli._validate_response_records(shipping, model_calls=1)[0] == 10
    assert cli._validate_response_records(raw, model_calls=1)[0] == 10


def test_response_route_mismatch_fails_before_admission():
    with pytest.raises(cli.PilotError, match="route mismatch"):
        cli._validate_response_records([{
            "effective_backend": "openai:wrong",
            "observed_provider": cli.PROVIDER,
            "observed_model": "wrong",
            "input_tokens": 7, "output_tokens": 3, "total_tokens": 10,
        }], model_calls=1)


def test_proof_time_is_the_first_response_prefix_that_satisfies_the_predicate():
    encode = lambda value: base64.b64encode(value.encode()).decode()
    rows = [
        {"id": 12, "sequence_number": 2, "timestamp": "2026-09-13T12:44:00+00:00", "response_text": encode("omega")},
        {"id": 11, "sequence_number": 1, "timestamp": "2026-09-13T12:43:00+00:00", "response_text": encode("alpha")},
        {"id": 13, "sequence_number": 3, "timestamp": "2026-09-13T12:46:00+00:00", "response_text": encode("unrelated")},
    ]

    prefix = cli._first_response_prefix_satisfying(rows, lambda output: "alpha\nomega" in output)

    assert prefix == {
        "output": "alpha\nomega",
        "effect_at": "2026-09-13T12:44:00+00:00",
        "response_id": 12,
    }


def test_proof_response_without_timestamp_stays_fail_closed():
    encoded = base64.b64encode(b"proof").decode()

    prefix = cli._first_response_prefix_satisfying(
        [{"id": 11, "sequence_number": 1, "timestamp": None, "response_text": encoded}],
        lambda output: output == "proof",
    )

    assert prefix is not None
    assert prefix["effect_at"] is None


def test_task_observer_query_requests_mythic_lifecycle_and_response_timestamps():
    for field_name in (
        "status_timestamp_preprocessing",
        "status_timestamp_submitted",
        "status_timestamp_processing",
        "status_timestamp_processed",
        "responses { id sequence_number timestamp response_text }",
    ):
        assert field_name in cli.TASK_OUTPUT_QUERY


@pytest.mark.parametrize("control,proven", [
    (r"u:NORTH\samwell.tarly", True),
    (r"u:NORTH\Administrator", True),
    (r"u:ESSOS\Administrator", False),
    ("administrator@ESSOS.LOCAL.", False),
])
def test_runtime_observer_emits_evaluator_owned_certificate_admin_proof(monkeypatch, control, proven):
    source = "\n".join((
        "System.DirectoryServices.Protocols.SecurityDescriptorFlagControl",
        "[System.DirectoryServices.Protocols.SecurityMasks]::Sacl",
        "[System.DirectoryServices.Protocols.AuthType]::External",
        "$connection.ClientCertificates.Add($certificate)",
        "CERT_AUTH_WHOAMI=", "DA_SACL_STATUS=", "CONTROL_SACL_STATUS=",
        "CERT_AUTH_MEMBER_OF=", "CERT_AUTH_STATUS=",
    ))
    output = """\
CERT_AUTH_METHOD=schannel-ldap
CERT_AUTH_DOMAIN=essos.local
CERT_AUTH_ACCOUNT=administrator
CERT_AUTH_TRANSPORT=ldaps
CERT_AUTH_PORT=636
CERT_AUTH_LDAP_BIND=True
CERT_AUTH_WHOAMI=u:ESSOS\\Administrator
DA_ONLY_READ_OP=domain-sacl
DA_SACL_STATUS=sacl
DA_SACL_ACE_COUNT=8
CONTROL_BIND=True
CONTROL_WHOAMI=u:NORTH\\samwell.tarly
CONTROL_SACL_STATUS=no-sd
CONTROL_SACL_ACE_COUNT=-1
DA_ONLY_READ_PROVEN=True
CERT_AUTH_USER_DN=CN=Administrator,CN=Users,DC=essos,DC=local
CERT_AUTH_MEMBER_OF=CN=Domain Admins,CN=Users,DC=essos,DC=local
CERT_AUTH_DOMAIN_ADMIN=True
CERT_AUTH_STATUS=OK
"""
    output = output.replace(r"u:NORTH\samwell.tarly", control)
    started = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_client = object()
    runtime.current_operation_id = 8
    runtime.current_started_at = started
    runtime.current_deadline_at = started + timedelta(minutes=45)
    runtime._operation_snapshot = lambda *_args: {
        "task": [{
            "operation_id": 8,
            "display_id": 54,
            "command_name": "powerpick",
            "display_params": source,
            "original_params": source,
            "completed": True,
            "timestamp": (started + timedelta(minutes=22)).isoformat(),
            "status_timestamp_preprocessing": (started + timedelta(minutes=20)).isoformat(),
            "status_timestamp_submitted": (started + timedelta(minutes=20, seconds=5)).isoformat(),
            "status_timestamp_processing": (started + timedelta(minutes=20, seconds=10)).isoformat(),
            "status_timestamp_processed": (started + timedelta(minutes=21)).isoformat(),
            "callback": {"domain": "NORTH"},
            "responses": [{
                "id": 72,
                "sequence_number": 1,
                "timestamp": (started + timedelta(minutes=21, seconds=30)).isoformat(),
                "response_text": base64.b64encode(output.encode()).decode(),
            }],
        }],
        "credential": [],
        "callback": [],
    }
    monkeypatch.setattr(cli.live_seams, "bloodhound_domain_count", lambda **_kwargs: 3)

    observed = runtime.observe(
        "shipping-sage", {},
        {"task_budget": {"limit": 100, "reservations": 1, "confirmed_task_ids": [54]}},
        {},
    )

    outcome = classify_terminal_snapshot(observed).to_record()
    report = summarize_pair([{
        "record_type": "arm_result", "arm": "shipping-sage",
        "terminal_snapshot": observed, "outcome": outcome, "execution": {},
    }])
    assert report["arms"][0]["objective"]["disposition"] == ("proven" if proven else "disproven")
    if not proven:
        assert observed["proof_candidates"] == []
        assert report["arms"][0]["objective"]["rejected_proofs"] == [{
            "task_id": "54",
            "reason": "certificate principal bindings are missing, mismatched, or identical",
        }]
        return

    assert observed["intermediate_milestones"]["OBJECTIVE"] == {
        "disposition": "proven", "task_ids": [54],
    }
    assert len(observed["proof_candidates"]) == 1
    assert observed["proof_candidates"][0]["effect_at"] == (
        started + timedelta(minutes=21, seconds=30)
    ).isoformat()
    assert observed["tasks"][0]["created_at"] == (started + timedelta(minutes=20)).isoformat()
    assert observed["tasks"][0]["first_response_at"] == (
        started + timedelta(minutes=21)
    ).isoformat()
    proof = observed["proof_candidates"][0]["proof_record"]
    assert proof["disposition"] == "proven"
    assert proof["observation"]["proof_kind"] == "domain_admin_differential"


def test_runtime_observer_treats_pre_agent_task_creation_error_as_terminal_nonproof(monkeypatch):
    started = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_client = object()
    runtime.current_operation_id = 8
    runtime.current_started_at = started
    runtime.current_deadline_at = started + timedelta(minutes=45)
    runtime._operation_snapshot = lambda *_args: {
        "task": [{
            "operation_id": 8,
            "display_id": 19,
            "command_name": "execute_assembly",
            "display_params": "-Assembly Rubeus.exe",
            "original_params": "-Assembly Rubeus.exe",
            "completed": True,
            "status": "Error: creating task ",
            "timestamp": (started + timedelta(minutes=20, seconds=1)).isoformat(),
            "status_timestamp_preprocessing": (started + timedelta(minutes=20)).isoformat(),
            "status_timestamp_submitted": None,
            "status_timestamp_processing": None,
            "status_timestamp_processed": None,
            "callback": {"domain": "NORTH"},
            "responses": [{
                "id": 91,
                "sequence_number": None,
                "timestamp": (started + timedelta(minutes=20, seconds=1)).isoformat(),
                "response_text": base64.b64encode(b"Failed to find assembly").decode(),
            }],
        }],
        "credential": [],
        "callback": [],
    }
    monkeypatch.setattr(cli.live_seams, "bloodhound_domain_count", lambda **_kwargs: 0)

    observed = runtime.observe(
        "raw-mythic-bloodhound", {},
        {"task_budget": {"limit": 100, "reservations": 1, "confirmed_task_ids": [19]}},
        {},
    )

    assert observed["tasks"] == [{
        "operation_id": 8,
        "display_id": 19,
        "command_name": "execute_assembly",
        "completed": True,
        "status": "Error: creating task ",
        "created_at": (started + timedelta(minutes=20)).isoformat(),
        "submitted_at": None,
        "processing_at": None,
        "first_response_at": None,
        "last_update_at": (started + timedelta(minutes=20, seconds=1)).isoformat(),
    }]
    assert observed["proof_candidates"] == []
    assert classify_terminal_snapshot(observed).disposition == "disproven"


def test_incomplete_usage_is_rejected():
    with pytest.raises(cli.PilotError, match="usage is incomplete"):
        cli._validate_response_records([{
            "effective_backend": cli.EXPECTED_BACKEND,
            "observed_provider": cli.PROVIDER,
            "observed_model": cli.MODEL,
            "input_tokens": 7, "output_tokens": 3,
        }], model_calls=1)


def test_shipping_unanswered_attempt_preserves_completed_records_but_not_numeric_cost():
    telemetry = {
        "model_calls": 2,
        "token_usage_complete": False,
        "model_response_records": [{
            "effective_backend": cli.EXPECTED_BACKEND,
            "effective_model_provider": cli.PROVIDER,
            "effective_model_id": cli.MODEL,
            "token_usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        }],
    }

    attempts, completed, total, records, route_error, cost_error = cli._shipping_response_accounting(telemetry)

    assert attempts == 2
    assert completed == 1
    assert total is None
    assert records == [{
        "response_index": 1,
        "effective_backend": cli.EXPECTED_BACKEND,
        "total_tokens": 10,
    }]
    assert route_error is None
    assert cost_error == "provider request has no authoritative response usage"


def test_run_pair_exits_nonzero_when_protocol_is_invalid(monkeypatch, tmp_path, capsys):
    class FakeRuntime:
        preflight = reset = runtime_identity = run_arm = stop_and_settle = observe = teardown = lambda *args: {}
        attest_preserved_state = lambda *args: {}
        live_authority_id = ""

    monkeypatch.setattr(cli, "_runtime", lambda _args: FakeRuntime())
    monkeypatch.setattr(cli, "run_pair", lambda *_args: {"protocol_valid": False})

    code = cli.main([
        "run-pair",
        "--artifact-dir", str(tmp_path),
        "--protected-manifest", str(tmp_path / "protected.json"),
        "--route-smoke", str(tmp_path / "smoke.json"),
        "--live-authority-id", "authorized-test",
    ])

    assert code == 1
    assert '"protocol_valid": false' in capsys.readouterr().out


def test_run_pair_passes_prospectively_selected_raw_first_contract(monkeypatch, tmp_path):
    class FakeRuntime:
        preflight = reset = runtime_identity = run_arm = stop_and_settle = observe = teardown = lambda *args: {}
        attest_preserved_state = lambda *args: {}
        live_authority_id = ""

    captured = {}
    monkeypatch.setattr(cli, "_runtime", lambda _args: FakeRuntime())

    def fake_run_pair(contract, *_args):
        captured["arm_order"] = contract.arm_order
        return {"run_complete": True}

    monkeypatch.setattr(cli, "run_pair", fake_run_pair)
    code = cli.main([
        "run-pair",
        "--artifact-dir", str(tmp_path),
        "--protected-manifest", str(tmp_path / "protected.json"),
        "--route-smoke", str(tmp_path / "smoke.json"),
        "--live-authority-id", "authorized-test",
        "--arm-order", "raw-first",
    ])

    assert code == 0
    assert captured["arm_order"] == tuple(reversed(cli.ARM_ORDER))


def test_protected_manifest_requires_canonical_path_set(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    manifest = tmp_path / "protected.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(cli.PilotError, match="canonical seven"):
        cli._validate_protected(manifest)


def test_raw_provider_timeout_is_protocol_error_with_unknown_token_cost(tmp_path):
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_client = object()
    runtime.current_operation_id = 8
    runtime.current_budget = None
    runtime.current_execution = None
    runtime.artifact_dir = tmp_path
    result = SimpleNamespace(
        objective=cli.OBJECTIVE,
        transcript=[],
        final_text="",
        termination={"cause": "wall_time", "subtype": "wrapper_timeout"},
        model_response_records=[],
        recoverable_tool_errors=[],
        model_attempts=1,
        model_calls=0,
        total_tokens=0,
        wall_seconds=2700.0,
        token_budget_overshoot=False,
    )
    runtime._raw_runner = lambda _deadline, _progress: SimpleNamespace(run=lambda _objective: result)

    execution = runtime.run_arm("raw-mythic-bloodhound", {}, lambda _event: None)

    assert execution["termination"] == result.termination
    assert execution["protocol_error"] is False
    assert execution["cost_error"] == "provider request has no authoritative response usage"
    assert execution["resource_complete"] is False
    assert execution["in_flight_model_responses"] == 1
    assert execution["cost"]["model_attempts"] == 1
    assert execution["cost"]["model_calls"] == 0
    assert execution["cost"]["total_tokens"] is None


def test_raw_completed_response_with_incomplete_usage_retains_record_and_unknown_cost(tmp_path):
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_client = object()
    runtime.current_operation_id = 8
    runtime.current_budget = None
    runtime.current_execution = None
    runtime.artifact_dir = tmp_path
    result = SimpleNamespace(
        objective=cli.OBJECTIVE,
        transcript=[],
        final_text="",
        termination={"cause": "other_declared_boundary", "subtype": "measurement_error"},
        model_response_records=[{
            "effective_backend": cli.EXPECTED_BACKEND,
            "observed_provider": cli.PROVIDER,
            "observed_model": cli.MODEL,
            "input_tokens": 7,
            "output_tokens": None,
            "total_tokens": None,
        }],
        recoverable_tool_errors=[],
        model_attempts=1,
        model_calls=1,
        total_tokens=0,
        token_usage_complete=False,
        wall_seconds=1.0,
        token_budget_overshoot=False,
    )
    runtime._raw_runner = lambda _deadline, _progress: SimpleNamespace(run=lambda _objective: result)

    execution = runtime.run_arm("raw-mythic-bloodhound", {}, lambda _event: None)

    assert execution["protocol_error"] is False
    assert execution["cost_error"] == "completed provider response has incomplete authoritative usage"
    assert execution["resource_complete"] is False
    assert execution["response_records"] == [{
        "response_index": 1,
        "effective_backend": cli.EXPECTED_BACKEND,
        "total_tokens": None,
    }]
    assert execution["cost"]["total_tokens"] is None
    assert execution["cost"]["token_overshoot"] is None


def test_recovery_cleanup_retains_prior_and_failed_phase_elapsed(tmp_path, monkeypatch, capsys):
    clock = {"now": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda _path: tmp_path)

    class Runtime:
        prior_support_elapsed_seconds = 7_199.0
        current_arm = "shipping-sage"

        def stop_and_settle(self, _arm, _start, _execution):
            clock["now"] += 2.0
            raise RuntimeError("settlement failed")

        def teardown(self, _arm, _start):
            clock["now"] += 3.0
            return {"complete": True}

    runtime = Runtime()
    monkeypatch.setattr(
        cli.RuntimePilot,
        "for_recovery",
        classmethod(lambda _cls, _path: (runtime, {}, {})),
    )

    assert cli.main(["cleanup", "--artifact-dir", str(tmp_path)]) == 1
    records = cli.read_records(tmp_path / "pilot.jsonl")
    recovery = next(row for row in records if row["record_type"] == "recovery_cleanup")
    assert recovery["setup_cleanup_elapsed_seconds"] == 7_204.0
    assert recovery["support_budget_exhausted"] is True
    assert any(row["record_type"] == "support_budget_exhausted" for row in records)
    assert '"complete": false' in capsys.readouterr().out


def test_recovery_cleanup_refuses_a_second_mutating_attempt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "validate_artifact_dir", lambda _path: tmp_path)
    (tmp_path / "pilot.jsonl").write_text(
        json.dumps({"record_type": "recovery_cleanup"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli.RuntimePilot,
        "for_recovery",
        classmethod(lambda _cls, _path: pytest.fail("recovery should fail before runtime mutation")),
    )

    assert cli.main(["cleanup", "--artifact-dir", str(tmp_path)]) == 1
    assert "attempt limit is exhausted" in capsys.readouterr().out


def test_task_settlement_window_is_bounded_and_retains_outstanding_ids(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    closed = []
    timeouts = []
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_client = object()
    runtime.current_operation_id = 8
    runtime.current_budget = SimpleNamespace(close=lambda: closed.append(True))
    runtime.current_execution = None
    runtime._operation_snapshot = lambda _client, _operation_id, *, timeout: (
        timeouts.append(timeout) or {
            "task": [
                {"display_id": 12, "command_name": "run", "completed": True},
                {"display_id": 13, "command_name": "dcsync", "completed": False},
            ]
        }
    )

    result = runtime.stop_and_settle(
        "raw-mythic-bloodhound",
        {},
        {"termination": "done", "protocol_error": False},
    )

    assert closed == [True]
    assert result["complete"] is False
    assert result["producer_terminal"] is True
    assert result["task_quiescent"] is False
    assert result["objective_effect_quiescent"] is False
    assert result["window_seconds"] == 300
    assert result["window_relationship"] == "outside_measured_window_after_producer_stop_attempt"
    assert result["elapsed_seconds"] == 300
    assert result["poll_count"] == 60
    assert result["completed_task_ids"] == [12]
    assert result["outstanding_task_ids"] == [13]
    assert max(timeouts) <= 30


def test_objective_inert_ps_can_settle_for_teardown_but_remains_nonterminal(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_client = object()
    runtime.current_operation_id = 8
    runtime.current_budget = SimpleNamespace(close=lambda: None)
    runtime.current_execution = None
    runtime._operation_snapshot = lambda _client, _operation_id, *, timeout: {"task": [
        {"display_id": 12, "command_name": "run", "completed": True},
        {"display_id": 13, "command_name": "ps", "completed": False},
    ]}

    result = runtime.stop_and_settle(
        "raw-mythic-bloodhound", {}, {"termination": "done", "protocol_error": False},
    )

    assert result["complete"] is True
    assert result["producer_terminal"] is True
    assert result["task_quiescent"] is False
    assert result["objective_effect_quiescent"] is True
    assert result["outstanding_task_ids"] == [13]
    assert result["objective_inert_outstanding_task_ids"] == [13]
    assert result["elapsed_seconds"] == 300

    clock["now"] = 0.0
    runtime._operation_snapshot = lambda _client, _operation_id, *, timeout: {"task": [
        {"display_id": 13, "command_name": "ps", "completed": None},
    ]}
    unknown = runtime.stop_and_settle(
        "raw-mythic-bloodhound", {}, {"termination": "done", "protocol_error": False},
    )
    assert unknown["complete"] is False
    assert unknown["objective_effect_quiescent"] is False
    assert unknown["objective_inert_outstanding_task_ids"] == []


def test_preserved_state_attestation_is_read_only_and_captures_all_sections(monkeypatch):
    from mythic import mythic

    client = SimpleNamespace(current_operation_id=8)
    vms = [
        {"canonical_name": name, "name": name, "powered_on": True, "ip": f"10.4.10.{index}"}
        for index, name in enumerate(("dc01", "dc02", "dc03", "router", "srv02", "srv03"), 10)
    ]

    async def query(_client, query, variables=None):
        assert _client is client
        if query == cli.PRESERVED_STATE_QUERY:
            return {
                "task": [{
                    "operation_id": 8, "display_id": 5, "command_name": "ps",
                    "completed": False, "status": "agent processing",
                    "callback": {"display_id": 2, "active": True},
                }],
                "callback": [{
                    "operation_id": 8, "display_id": 2, "active": True,
                    "host": "CASTELBLACK", "user": "samwell.tarly",
                }],
                "consuming_container": [{
                    "id": 3, "name": "sage", "type": "chat",
                    "container_running": True, "deleted": False,
                }],
            }
        if query == cli.PRESERVED_REQUEST_QUERY:
            return {"chat_request": [{"id": 9, "channel_id": 7, "status": "cancelled"}]}
        pytest.fail("unexpected GraphQL operation")

    async def forbidden_mutation(*_args, **_kwargs):
        pytest.fail("mutation-capable Mythic command executed")

    monkeypatch.setattr(mythic, "execute_custom_query", query)
    monkeypatch.setattr(mythic, "issue_task", forbidden_mutation)
    monkeypatch.setattr(mythic, "update_callback", forbidden_mutation)
    monkeypatch.setattr(cli, "_safe_command", lambda *_a, **_k: pytest.fail("mutation-capable local command executed"))

    readiness = SimpleNamespace(
        runtime_db_status=lambda *_a, **_k: {
            "existing_required": ["Payload_Type/sage/sage.db", "Payload_Type/sage/.phoenix/phoenix.db"],
        },
        sage_deployment_status=lambda **_k: {
            "mode": "local", "container_running": False, "local_process_running": True,
        },
        probe_ludus_status=lambda **_k: {
            "range_number": 3, "range_state": "RUNNING", "vms": vms,
        },
        probe_bloodhound_api_status=lambda: {
            "status_code": 200, "domain_count": 3, "domains": ["one", "two", "three"],
        },
    )
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_client = client
    runtime.artifact_dir = Path("/private/attempt")
    runtime.current_operation_id = 8
    runtime.current_callback_id = 2
    runtime.current_chat_request_id = 9
    runtime.native = SimpleNamespace(_readiness_contract_module=lambda: readiness)

    result = runtime.attest_preserved_state(
        "shipping-sage", {}, {}, {"stage": "observer", "producer_started": True},
    )

    assert result["complete"] is True
    assert result["unknown_sections"] == []
    assert result["payload_tasks_issued_by_attestation"] == 0
    assert result["state_mutations_by_attestation"] == 0
    assert result["sections"]["mythic"]["tasks"][0]["display_id"] == 5
    assert result["sections"]["sage_chat"]["request"]["status"] == "cancelled"
    assert result["sections"]["runtime_databases"]["active_runtime_db_paths"]
    assert result["sections"]["bloodhound"]["domain_count"] == 3
    assert result["sections"]["ludus"]["vm_count"] == 6
    assert len(result["inspection_instructions"]) == 7
    assert "inspect-preserved" in result["inspection_instructions"][0]


def test_preserved_state_malformed_section_stays_unknown(monkeypatch):
    from mythic import mythic

    async def malformed(*_args, **_kwargs):
        return {"task": None, "callback": [], "consuming_container": []}

    monkeypatch.setattr(mythic, "execute_custom_query", malformed)
    vms = [
        {"canonical_name": name, "name": name, "powered_on": True, "ip": f"10.4.10.{index}"}
        for index, name in enumerate(("dc01", "dc02", "dc03", "router", "srv02", "srv03"), 10)
    ]
    readiness = SimpleNamespace(
        runtime_db_status=lambda *_a, **_k: {"existing_required": []},
        sage_deployment_status=lambda **_k: {
            "mode": "local", "container_running": False, "local_process_running": True,
        },
        probe_ludus_status=lambda **_k: {"range_number": 3, "range_state": "RUNNING", "vms": vms},
        probe_bloodhound_api_status=lambda: {"status_code": 200, "domain_count": 0, "domains": []},
    )
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_client = SimpleNamespace(current_operation_id=8)
    runtime.artifact_dir = Path("/private/attempt")
    runtime.current_operation_id = 8
    runtime.current_callback_id = 2
    runtime.current_chat_request_id = None
    runtime.native = SimpleNamespace(_readiness_contract_module=lambda: readiness)

    result = runtime.attest_preserved_state("raw-mythic-bloodhound", {}, {}, {})

    assert result["complete"] is False
    assert result["sections"]["mythic"]["state"] == "unknown"
    assert result["sections"]["sage_chat"]["state"] == "unknown"
    assert result["sections"]["bloodhound"]["state"] == "observed"
    assert result["unknown_sections"] == ["mythic", "sage_chat"]


def test_preserved_reinspection_uses_only_the_current_arms_request(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path.parent)
    history = tmp_path.parent / ".sage_history"
    history.mkdir()
    tmp_path = history / "attempt"
    tmp_path.mkdir()

    class Native:
        async def login(self):
            return SimpleNamespace(current_operation_id=8)

    monkeypatch.setattr(cli, "_native_chat", lambda: Native())
    rows = [
        {
            "record_type": "arm_started", "arm": "shipping-sage",
            "start_identity": {}, "runtime_identity": {"operation_id": 8, "callback_id": 2},
        },
        {
            "record_type": "progress", "arm": "shipping-sage",
            "event": {"event": "request_started", "chat_channel_id": 4, "chat_request_id": 5},
        },
        {
            "record_type": "arm_started", "arm": "raw-mythic-bloodhound",
            "start_identity": {}, "runtime_identity": {"operation_id": 8, "callback_id": 2},
        },
    ]
    (tmp_path / "pilot.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )

    runtime, _start, _execution = cli.RuntimePilot.for_preserved_inspection(tmp_path)

    assert runtime.current_arm == "raw-mythic-bloodhound"
    assert runtime.current_chat_channel_id is None
    assert runtime.current_chat_request_id is None


def _metric_task(display_id, command, params, output=""):
    responses = []
    if output:
        responses.append({
            "id": display_id * 10,
            "sequence_number": 1,
            "timestamp": f"2026-09-16T12:{display_id:02d}:00+00:00",
            "response_text": base64.b64encode(output.encode()).decode(),
        })
    return {
        "operation_id": 8,
        "display_id": display_id,
        "command_name": command,
        "display_params": params,
        "original_params": params,
        "completed": True,
        "responses": responses,
    }


def _certificate_transaction_rows(*, include_pkinit=True, extra_task=False):
    rows = [
        _metric_task(49, "shell", "klist"),
        _metric_task(50, "shell", r"dir \\MEEREEN.ESSOS.LOCAL\C$"),
        _metric_task(51, "upload", r"C:\Windows\Temp\sage_ca_signing_admin_2.pfx"),
        _metric_task(
            52, "execute_assembly",
            r"Certify.exe forge --output-path C:\Windows\Temp\sage_forged_cert_admin_2.pfx",
        ),
    ]
    next_id = 53
    if include_pkinit:
        rows.append(_metric_task(
            next_id,
            "execute_assembly",
            r"Rubeus.exe asktgt /certificate:C:\Windows\Temp\sage_forged_cert_admin_2.pfx",
            "[X] KRB-ERROR (16) : KDC_ERR_PADATA_TYPE_NOSUPP",
        ))
        next_id += 1
    if extra_task:
        rows.append(_metric_task(next_id, "shell", "echo unrelated"))
        next_id += 1
    rows.append(_metric_task(
        next_id,
        "powerpick",
        "certificate control proof",
        "CERT_AUTH_METHOD=schannel-ldap\nCERT_AUTH_STATUS=OK\n",
    ))
    return rows


def test_certificate_auth_primary_metric_reproduces_baseline_and_candidate_threshold():
    baseline = cli.certificate_auth_primary_metric(_certificate_transaction_rows())
    candidate = cli.certificate_auth_primary_metric(
        _certificate_transaction_rows(include_pkinit=False),
    )

    assert baseline["complete"] is True
    assert baseline["certificate_auth_transaction_tasks"] == 6
    assert baseline["avoidable_pkinit_compatibility_error_tasks"] == 1
    assert baseline["avoidable_pkinit_compatibility_error_task_ids"] == [53]
    assert candidate["complete"] is True
    assert candidate["certificate_auth_transaction_tasks"] == 5
    assert candidate["avoidable_pkinit_compatibility_error_tasks"] == 0


@pytest.mark.parametrize("include_pkinit", [True, False])
def test_certificate_auth_metric_accepts_native_run_task_names(include_pkinit):
    rows = _certificate_transaction_rows(include_pkinit=include_pkinit)
    for row in rows[:2]:
        row["command_name"] = "run"
    metric = cli.certificate_auth_primary_metric(rows)
    assert metric["complete"] is True
    assert metric["certificate_auth_transaction_tasks"] == (6 if include_pkinit else 5)
    assert metric["avoidable_pkinit_compatibility_error_tasks"] == int(include_pkinit)


@pytest.mark.parametrize("command,params", [
    ("runas", "klist"), ("powerpick", "klist"), ("run", "klisting"),
])
def test_certificate_auth_native_task_near_matches_do_not_supply_inventory(command, params):
    rows = _certificate_transaction_rows()
    rows[0] = _metric_task(49, command, params)
    metric = cli.certificate_auth_primary_metric(rows)
    assert metric["complete"] is False
    assert metric["anchor_task_ids"]["current_context_ticket_inventory"] is None


def test_certificate_auth_primary_metric_counts_offsetting_unclassified_task():
    metric = cli.certificate_auth_primary_metric(
        _certificate_transaction_rows(include_pkinit=False, extra_task=True),
    )

    assert metric["complete"] is True
    assert metric["certificate_auth_transaction_tasks"] == 6
    assert any(row["class"] == "unclassified" for row in metric["transaction_task_classes"])


def test_certificate_auth_primary_metric_fails_closed_on_near_match():
    rows = _certificate_transaction_rows(include_pkinit=False)
    rows[-1]["responses"][0]["response_text"] = base64.b64encode(
        b"CERT_AUTH_METHOD=schannel-ldap\nCERT_AUTH_STATUS=FAILED\n"
    ).decode()

    metric = cli.certificate_auth_primary_metric(rows)

    assert metric["complete"] is False
    assert metric["proof_task_count"] == 0


def test_improvement_source_identity_failure_precedes_reset_mutation(monkeypatch, tmp_path):
    runtime = object.__new__(cli.RuntimePilot)
    runtime.current_reset_started = False
    runtime.source_roots = {"baseline-sage": tmp_path / "baseline"}
    runtime.source_identities = {"baseline-sage": "a" * 64}
    monkeypatch.setattr(
        runtime,
        "_source_runtime_preflight",
        lambda *_args: (_ for _ in ()).throw(cli.PilotError("source identity mismatch before reset")),
    )

    with pytest.raises(cli.PilotError, match="before reset"):
        runtime.reset("baseline-sage", {})

    assert runtime.current_reset_started is False


def test_improvement_plan_is_shipping_only_and_has_no_single_arm_surface(capsys):
    code = cli.main([
        "improvement-plan",
        "--baseline-source-identity", "a" * 64,
        "--candidate-source-identity", "b" * 64,
    ])

    assert code == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["supported_pair_orders"] == {
        "baseline-first": ["baseline-sage", "candidate-sage"],
        "candidate-first": ["candidate-sage", "baseline-sage"],
    }
    assert plan["single_arm_surface"] is False
    assert "raw-mythic-bloodhound" not in json.dumps(plan)


def test_pair_two_gate_requires_comparison_ready_safely_cleaned_pair_one(tmp_path):
    contract = cli.ImprovementContract("a" * 64, "b" * 64)
    cli.append_record(tmp_path / "pilot.jsonl", {
        "record_type": "contract", "contract": contract.to_record(),
    })
    cli.append_record(tmp_path / "pilot.jsonl", {
        "record_type": "comparison",
        "comparison_ready": True,
        "lifecycle_complete": True,
        "isolation_complete": True,
        "resources_complete": True,
        "run_complete": True,
    })

    cli._require_first_pair_ready(tmp_path, contract)

    rows = cli.read_records(tmp_path / "pilot.jsonl")
    rows[-1]["isolation_complete"] = False
    (tmp_path / "pilot.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )
    with pytest.raises(cli.PilotError, match="pair 2 requires"):
        cli._require_first_pair_ready(tmp_path, contract)


def _write_retained_improvement_pair(path, contract, order, *, candidate_metric=True):
    selected = cli.ImprovementContract(
        contract.baseline_source_identity,
        contract.candidate_source_identity,
        arm_order=order,
    )
    cli.append_record(path / "pilot.jsonl", {
        "record_type": "contract", "contract": selected.to_record(),
    })
    cli.append_record(path / "pilot.jsonl", {
        "record_type": "comparison",
        "comparison_ready": True,
        "lifecycle_complete": True,
        "isolation_complete": True,
        "resources_complete": True,
        "run_complete": True,
        "arms": [
            {"arm": "baseline-sage", "objective_disposition": "proven"},
            {"arm": "candidate-sage", "objective_disposition": "proven"},
        ],
        "primary_metric": {
            "complete": True,
            "baseline_reproduced": True,
            "candidate_threshold_met": candidate_metric,
            "meaningful_effect_met": candidate_metric,
        },
    })


def test_improvement_summary_accepts_only_two_order_balanced_threshold_passing_pairs(tmp_path):
    contract = cli.ImprovementContract("a" * 64, "b" * 64)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_retained_improvement_pair(first, contract, cli.IMPROVEMENT_ARM_ORDER)
    _write_retained_improvement_pair(
        second, contract, tuple(reversed(cli.IMPROVEMENT_ARM_ORDER)),
    )

    result = cli.summarize_improvement_experiment(first, second, contract)

    assert result["disposition"] == "accept"
    assert result["meaningful_effect_met_in_both_pairs"] is True


def test_improvement_summary_direction_neutrally_rejects_flat_candidate(tmp_path):
    contract = cli.ImprovementContract("a" * 64, "b" * 64)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_retained_improvement_pair(first, contract, cli.IMPROVEMENT_ARM_ORDER)
    _write_retained_improvement_pair(
        second,
        contract,
        tuple(reversed(cli.IMPROVEMENT_ARM_ORDER)),
        candidate_metric=False,
    )

    result = cli.summarize_improvement_experiment(first, second, contract)

    assert result["disposition"] == "reject"
    assert "threshold" in result["reason"]


def test_improvement_cli_binds_external_roots_and_shipping_order(monkeypatch, tmp_path):
    class FakeRuntime:
        preflight = reset = runtime_identity = run_arm = stop_and_settle = observe = teardown = lambda *args: {}
        attest_preserved_state = lambda *args: {}
        live_authority_id = ""

    captured = {}

    def fake_prepare(args, contract):
        captured["prepared"] = True
        assert not (tmp_path / "artifacts" / "pilot.jsonl").exists()
        return {"ready": True}

    def fake_runtime(args, contract):
        captured["roots"] = (args.baseline_source_root, args.candidate_source_root)
        captured["identities"] = (
            contract.baseline_source_identity, contract.candidate_source_identity,
        )
        return FakeRuntime()

    def fake_run_pair(contract, *_args):
        captured["arm_order"] = contract.arm_order
        return {"run_complete": True}

    monkeypatch.setattr(cli, "_runtime", fake_runtime)
    monkeypatch.setattr(cli, "_prepared_improvement_dependencies", fake_prepare)
    monkeypatch.setattr(cli, "run_pair", fake_run_pair)
    code = cli.main([
        "run-improvement-pair",
        "--artifact-dir", str(tmp_path / "artifacts"),
        "--protected-manifest", str(tmp_path / "protected.json"),
        "--route-smoke", str(tmp_path / "smoke.json"),
        "--operator-config-root", str(tmp_path / "operator"),
        "--live-authority-id", "authorized-test",
        "--pair-order", "baseline-first",
        "--baseline-source-root", str(tmp_path / "baseline"),
        "--candidate-source-root", str(tmp_path / "candidate"),
        "--baseline-source-identity", "a" * 64,
        "--candidate-source-identity", "b" * 64,
    ])

    assert code == 0
    assert captured["prepared"] is True
    assert captured["arm_order"] == cli.IMPROVEMENT_ARM_ORDER
    assert captured["roots"] == (str(tmp_path / "baseline"), str(tmp_path / "candidate"))
    assert captured["identities"] == ("a" * 64, "b" * 64)


def test_improvement_cli_uses_standing_authority_without_one_use_id(monkeypatch, tmp_path):
    contract = cli.ImprovementContract("a" * 64, "b" * 64)
    authority_path = tmp_path / "standing-authority.json"
    write_standing_authority(authority_path, cli.PilotContract(), contract)

    class FakeRuntime:
        preflight = reset = runtime_identity = run_arm = stop_and_settle = observe = teardown = lambda *args: {}
        attest_preserved_state = lambda *args: {}
        live_authority_id = ""
        live_authority_receipt = {}

    runtime = FakeRuntime()
    prepared = {"called": False}

    def fake_prepare(_args, _contract):
        prepared["called"] = True
        return {
            "ready": True,
            "ledger_allocated": False,
            "attempted_start": False,
            "route_identity": {
                "provider": cli.PROVIDER,
                "model": cli.MODEL,
                "endpoint_identity": "e" * 64,
            },
        }

    monkeypatch.setattr(cli, "_prepared_improvement_dependencies", fake_prepare)
    monkeypatch.setattr(cli, "_runtime", lambda *_args: runtime)
    monkeypatch.setattr(cli, "run_pair", lambda *_args: {"run_complete": True})

    code = cli.main([
        "run-improvement-pair",
        "--artifact-dir", str(tmp_path / "artifacts"),
        "--protected-manifest", str(tmp_path / "protected.json"),
        "--route-smoke", str(tmp_path / "smoke.json"),
        "--operator-config-root", str(tmp_path / "operator"),
        "--live-authority-file", str(authority_path),
        "--pair-order", "baseline-first",
        "--baseline-source-root", str(tmp_path / "baseline"),
        "--candidate-source-root", str(tmp_path / "candidate"),
        "--baseline-source-identity", "a" * 64,
        "--candidate-source-identity", "b" * 64,
    ])

    assert code == 0
    assert prepared["called"] is True
    assert runtime.live_authority_id == "user-sage-goad-trust-walker-standing-20260917"
    assert runtime.live_authority_receipt["mode"] == "standing"
    assert runtime.live_authority_receipt["authority_sha256"] == hashlib.sha256(
        authority_path.read_bytes()
    ).hexdigest()


def test_invalid_standing_authority_fails_before_staging_runtime_or_ledger(monkeypatch, tmp_path):
    contract = cli.ImprovementContract("a" * 64, "b" * 64)
    authority_path = tmp_path / "standing-authority.json"
    write_standing_authority(authority_path, contract, status="revoked", revoked_at="now")
    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setattr(
        cli,
        "_prepared_improvement_dependencies",
        lambda *_args: pytest.fail("staging ran after standing authority rejection"),
    )
    monkeypatch.setattr(cli, "_runtime", lambda *_args: pytest.fail("runtime was constructed"))

    code = cli.main([
        "run-improvement-pair",
        "--artifact-dir", str(artifact_dir),
        "--protected-manifest", str(tmp_path / "protected.json"),
        "--route-smoke", str(tmp_path / "smoke.json"),
        "--operator-config-root", str(tmp_path / "operator"),
        "--live-authority-file", str(authority_path),
        "--pair-order", "baseline-first",
        "--baseline-source-root", str(tmp_path / "baseline"),
        "--candidate-source-root", str(tmp_path / "candidate"),
        "--baseline-source-identity", "a" * 64,
        "--candidate-source-identity", "b" * 64,
    ])

    assert code == 1
    assert not artifact_dir.exists()


def _operator_dependency_fixture(root: Path) -> None:
    for relative in cli.OPERATOR_LOCAL_LINKS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    (root / cli.OPERATOR_LOCAL_VENV).mkdir(parents=True)
    tools = root / cli.OPERATOR_RUNTIME_TOOLS_DIR
    tools.mkdir(parents=True)
    for name in ("SharpHound.exe", "Rubeus.exe"):
        (tools / name).write_bytes(b"MZfixture")


def _tls_probe_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    payload = root / "Payload_Type" / "sage"
    certs = payload / "certs"
    certs.mkdir(parents=True)
    shutil.copyfile(
        SCRIPT.parents[3] / cli.SAGE_TLS_BOOTSTRAP,
        root / cli.SAGE_TLS_BOOTSTRAP,
    )
    return root


def _runtime_tool_probe_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    payload = root / "Payload_Type" / "sage"
    langgraph = payload / "ai" / "langgraph"
    ttps = payload / "ttps"
    tools = payload / "tools"
    langgraph.mkdir(parents=True)
    ttps.mkdir()
    tools.mkdir()
    shutil.copyfile(
        SCRIPT.parents[3] / "Payload_Type/sage/ai/langgraph/ttp_library.py",
        langgraph / "ttp_library.py",
    )
    shutil.copyfile(
        SCRIPT.parents[3] / "Payload_Type/sage/ttps/sharphound.md",
        ttps / "sharphound.md",
    )
    return root


def test_tls_dependency_contract_tracks_the_production_consumer():
    producer = cli._load(
        "trust_walker_test_ssl_bootstrap",
        SCRIPT.parents[3] / cli.SAGE_TLS_BOOTSTRAP,
    )
    relative = producer.DEFAULT_SAGE_CUSTOM_CA_BUNDLE_PATH.relative_to(SCRIPT.parents[3])

    assert relative.as_posix() == cli.SAGE_TLS_CA_BUNDLE
    assert cli.SAGE_TLS_CA_BUNDLE in cli.OPERATOR_LOCAL_LINKS


def test_runtime_tool_dependency_contract_tracks_the_production_consumer():
    from ai.langgraph import mythic_tools

    producer = mythic_tools.ttp_library
    frontmatter, _ = producer.load_ttp("sharphound")
    relative = producer.TOOLS_DIR.relative_to(SCRIPT.parents[3])

    assert relative.as_posix() == cli.OPERATOR_RUNTIME_TOOLS_DIR
    assert frontmatter["binary_filename"] in cli.TRUST_WALKER_REQUIRED_RUNTIME_TOOLS


def test_staged_tls_probe_executes_root_relative_producer_without_network(tmp_path, monkeypatch):
    root = _tls_probe_root(tmp_path)
    system_ca = Path(ssl.get_default_verify_paths().openssl_cafile or "")
    if not system_ca.is_file():
        pytest.skip("platform CA bundle is unavailable")
    (root / cli.SAGE_TLS_CA_BUNDLE).symlink_to(system_ca)
    monkeypatch.setattr(socket.socket, "connect", lambda *_a, **_k: pytest.fail("network attempted"))

    assert cli._probe_staged_tls_trust(root, Path(sys.executable)) is True
    assert (root / "Payload_Type/sage/certs/combined-bundle.pem").is_file()


@pytest.mark.parametrize("contents", [None, "not a certificate\n"])
def test_staged_tls_probe_rejects_missing_or_malformed_bundle(tmp_path, contents):
    root = _tls_probe_root(tmp_path)
    if contents is not None:
        (root / cli.SAGE_TLS_CA_BUNDLE).write_text(contents, encoding="utf-8")

    assert cli._probe_staged_tls_trust(root, Path(sys.executable)) is False


def test_staged_runtime_tool_probe_executes_production_producer(tmp_path, monkeypatch):
    root = _runtime_tool_probe_root(tmp_path)
    (root / cli.OPERATOR_RUNTIME_TOOLS_DIR / "SharpHound.exe").write_bytes(b"MZfixture")
    monkeypatch.setattr(socket.socket, "connect", lambda *_a, **_k: pytest.fail("network attempted"))

    assert cli._probe_staged_runtime_tools(
        root, Path(sys.executable), cli.TRUST_WALKER_REQUIRED_RUNTIME_TOOLS,
    ) is True


@pytest.mark.parametrize("contents", [b"", b"not-a-pe"])
def test_staged_runtime_tool_probe_rejects_empty_or_malformed_required_tool(tmp_path, contents):
    root = _runtime_tool_probe_root(tmp_path)
    (root / cli.OPERATOR_RUNTIME_TOOLS_DIR / "SharpHound.exe").write_bytes(contents)

    assert cli._probe_staged_runtime_tools(
        root, Path(sys.executable), cli.TRUST_WALKER_REQUIRED_RUNTIME_TOOLS,
    ) is False


def test_operator_dependency_staging_reports_complete_missing_set(tmp_path):
    operator = tmp_path / "operator"
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    operator.mkdir()
    baseline.mkdir()
    candidate.mkdir()

    materialized, problems = cli._stage_operator_links(
        operator,
        {"baseline-sage": baseline, "candidate-sage": candidate},
    )

    assert materialized == []
    assert {row["dependency"] for row in problems} == {
        *(f"operator-config:{relative}" for relative in cli.OPERATOR_LOCAL_LINKS),
        f"operator-config:{cli.OPERATOR_LOCAL_VENV}",
        f"operator-config:{cli.OPERATOR_RUNTIME_TOOLS_DIR}",
        f"baseline-sage:{cli.OPERATOR_LOCAL_VENV}",
        f"candidate-sage:{cli.OPERATOR_LOCAL_VENV}",
    }
    assert not list(baseline.iterdir())
    assert not list(candidate.iterdir())


@pytest.mark.parametrize("missing", (*cli.OPERATOR_LOCAL_LINKS, cli.OPERATOR_LOCAL_VENV))
def test_each_operator_local_dependency_fails_as_one_preledger_class(tmp_path, missing):
    operator = tmp_path / "operator"
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _operator_dependency_fixture(operator)
    for root in (baseline, candidate):
        root.mkdir()
        (root / cli.OPERATOR_LOCAL_VENV).mkdir()
    selected = operator / missing
    if selected.is_dir():
        selected.rmdir()
    else:
        selected.unlink()

    materialized, problems = cli._stage_operator_links(
        operator,
        {"baseline-sage": baseline, "candidate-sage": candidate},
    )

    assert materialized == []
    assert f"operator-config:{missing}" in {row["dependency"] for row in problems}
    assert not (baseline / cli.OPERATOR_LOCAL_LINKS[0]).exists()
    assert not (candidate / cli.OPERATOR_LOCAL_LINKS[0]).exists()


def test_missing_required_runtime_tool_fails_before_any_staging_mutation(tmp_path):
    operator = tmp_path / "operator"
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _operator_dependency_fixture(operator)
    (operator / cli.OPERATOR_RUNTIME_TOOLS_DIR / "SharpHound.exe").unlink()
    for root in (baseline, candidate):
        root.mkdir()
        (root / cli.OPERATOR_LOCAL_VENV).mkdir()

    materialized, problems = cli._stage_operator_links(
        operator,
        {"baseline-sage": baseline, "candidate-sage": candidate},
    )

    assert materialized == []
    assert f"operator-config:{cli.OPERATOR_RUNTIME_TOOLS_DIR}/SharpHound.exe" in {
        row["dependency"] for row in problems
    }
    assert not (baseline / cli.OPERATOR_LOCAL_LINKS[0]).exists()
    assert not (candidate / cli.OPERATOR_RUNTIME_TOOLS_DIR / "Rubeus.exe").exists()


def test_stale_runtime_tool_fails_closed_without_overwriting_it(tmp_path):
    operator = tmp_path / "operator"
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _operator_dependency_fixture(operator)
    for root in (baseline, candidate):
        root.mkdir()
        (root / cli.OPERATOR_LOCAL_VENV).mkdir()
    stale = baseline / cli.OPERATOR_RUNTIME_TOOLS_DIR / "stale.bin"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")

    materialized, problems = cli._stage_operator_links(
        operator,
        {"baseline-sage": baseline, "candidate-sage": candidate},
    )

    assert materialized == []
    assert "baseline-sage:runtime-tool-drop-zone" in {row["dependency"] for row in problems}
    assert stale.read_bytes() == b"stale"
    assert not (candidate / cli.OPERATOR_LOCAL_LINKS[0]).exists()


def test_operator_dependency_staging_links_without_changing_canonical_identity(tmp_path):
    operator = tmp_path / "operator"
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _operator_dependency_fixture(operator)
    for root in (baseline, candidate):
        root.mkdir()
        (root / cli.OPERATOR_LOCAL_VENV).mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        ignored = "\n".join((
            *cli.OPERATOR_LOCAL_LINKS,
            f"{cli.OPERATOR_LOCAL_VENV}/",
            f"{cli.OPERATOR_RUNTIME_TOOLS_DIR}/*",
        )) + "\n"
        (root / ".gitignore").write_text(ignored, encoding="utf-8")
        (root / "tracked.txt").write_text("same\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    before = {root: cli._source_identity(root) for root in (baseline, candidate)}

    materialized, problems = cli._stage_operator_links(
        operator,
        {"baseline-sage": baseline, "candidate-sage": candidate},
    )

    assert problems == []
    assert len(materialized) == 2 * (len(cli.OPERATOR_LOCAL_LINKS) + 2)
    assert {root: cli._source_identity(root) for root in (baseline, candidate)} == before
    for root in (baseline, candidate):
        for relative in cli.OPERATOR_LOCAL_LINKS:
            assert (root / relative).is_symlink()
        for name in ("SharpHound.exe", "Rubeus.exe"):
            assert (root / cli.OPERATOR_RUNTIME_TOOLS_DIR / name).is_symlink()
        assert (root / cli.OPERATOR_LOCAL_VENV).is_dir()
        assert not (root / cli.OPERATOR_LOCAL_VENV).is_symlink()


def test_improvement_dependency_failure_precedes_runtime_and_ledger(monkeypatch, tmp_path, capsys):
    artifact_dir = tmp_path / "artifacts"

    def fail_prepare(_args, _contract):
        raise cli.PilotError(
            "operator dependency preflight failed:\n"
            "- baseline-sage:.mcp.json: create the required operator link\n"
            "- candidate-sage:retained-fixture: restore the retained fixture"
        )

    monkeypatch.setattr(cli, "_prepared_improvement_dependencies", fail_prepare)
    monkeypatch.setattr(cli, "_runtime", lambda *_a: pytest.fail("runtime constructed after failed gate"))
    code = cli.main([
        "run-improvement-pair",
        "--artifact-dir", str(artifact_dir),
        "--protected-manifest", str(tmp_path / "protected.json"),
        "--route-smoke", str(tmp_path / "smoke.json"),
        "--operator-config-root", str(tmp_path / "operator"),
        "--live-authority-id", "authorized-test",
        "--pair-order", "baseline-first",
        "--baseline-source-root", str(tmp_path / "baseline"),
        "--candidate-source-root", str(tmp_path / "candidate"),
        "--baseline-source-identity", "a" * 64,
        "--candidate-source-identity", "b" * 64,
    ])

    assert code == 1
    assert not artifact_dir.exists()
    output = json.loads(capsys.readouterr().out)
    assert "baseline-sage:.mcp.json" in output["reason"]
    assert "candidate-sage:retained-fixture" in output["reason"]


def test_preserved_inspection_command_loads_root_env_without_printing_secrets(monkeypatch):
    from mythic import mythic

    async def query(_client, _query, variables=None):
        return {"task": [], "callback": [], "consuming_container": []}

    monkeypatch.setattr(mythic, "execute_custom_query", query)
    vms = [{"canonical_name": str(index)} for index in range(6)]
    readiness = SimpleNamespace(
        runtime_db_status=lambda *_a, **_k: {"existing_required": []},
        sage_deployment_status=lambda **_k: {
            "mode": "local", "container_running": False, "local_process_running": True,
        },
        probe_ludus_status=lambda **_k: {"range_number": 1, "range_state": "RUNNING", "vms": vms},
        probe_bloodhound_api_status=lambda: {"status_code": 200, "domain_count": 0, "domains": []},
    )
    runtime = object.__new__(cli.RuntimePilot)
    runtime.artifact_dir = Path(".sage_history/evaluation/attempt")
    runtime.current_client = SimpleNamespace(current_operation_id=8)
    runtime.current_operation_id = 8
    runtime.current_callback_id = None
    runtime.current_chat_request_id = None
    runtime.native = SimpleNamespace(_readiness_contract_module=lambda: readiness)

    result = runtime.attest_preserved_state("raw-mythic-bloodhound", None, {}, {})
    command = result["inspection_instructions"][0]

    assert command.startswith(
        "set -a; . ./.env; . Payload_Type/sage/.env.local; set +a;"
    )
    assert "inspect-preserved --artifact-dir .sage_history/evaluation/attempt" in command
    assert "API_KEY" not in command
