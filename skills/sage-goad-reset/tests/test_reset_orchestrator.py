from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SAGE_PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"
sys.path.insert(0, str(SAGE_PACKAGE_ROOT))
from ai.hillclimb.scenarios import structural_scenario_manifests


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reset_orchestrator.py"
SPEC = importlib.util.spec_from_file_location("reset_orchestrator", SCRIPT)
reset = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(reset)
READINESS_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "readiness_contract.py"
READINESS_SPEC = importlib.util.spec_from_file_location("reset_readiness_contract", READINESS_SCRIPT)
readiness = importlib.util.module_from_spec(READINESS_SPEC)
assert READINESS_SPEC and READINESS_SPEC.loader
READINESS_SPEC.loader.exec_module(readiness)


def _ready_sections(lane: str) -> dict:
    return {
        name: {"ready": True, "blockers": []}
        for name in readiness.READINESS_LANE_SECTIONS[lane]
    }


def _canonical_ready_output(lane: str) -> dict:
    decision = readiness.evaluate_readiness(
        lane=lane,
        sections=_ready_sections(lane),
        observation_id=f"{lane}-test-observation",
    )
    if lane == "range_boot":
        return decision
    return {
        "schema": readiness.READINESS_CONTRACT_SCHEMA,
        "ready": True,
        "observation_id": decision["observation_id"],
        "readiness": decision,
        **_ready_sections(lane),
    }


def _config() -> dict:
    return {
        "snapshot": "exact-clean-snapshot",
        "range_profile_id": "goad-trust-walker",
        "ludus_range_id": "GOAD",
        "ludus_mcp_server": "ludus_goad",
        "bootstrap_mode": "fresh-apollo",
        "retained_callback_config": "",
        "foothold_payload_type": "apollo",
        "foothold_host": "CASTELBLACK",
        "foothold_user_match": "samwell.tarly",
        "callback_host": "http://100.64.0.1",
        "download_dir": "/tmp/sage_payloads",
        "prepare_chat": False,
        "restart_env": {
            "SAGE_ENGAGEMENT_GATE": "1",
            "SAGE_BLOODHOUND_MCP_DIR": "/workspace/bloodhound_mcp",
        },
        "range_ready_timeout": 60,
        "range_poll_interval": 1.0,
        "mythic_server": "127.0.0.1",
        "mythic_user": "mythic_admin",
        "mythic_env_path": "/opt/mythic-install/.env",
    }


def _external_goad_manifest(tmp_path: Path) -> tuple[dict, Path]:
    external = tmp_path / "configured-goad"
    external.mkdir()
    item = external / "source.yml"
    item.write_text("exact synthetic source\n", encoding="utf-8")
    digest = hashlib.sha256()
    digest.update(item.relative_to(external).as_posix().encode())
    digest.update(b"\0")
    digest.update(hashlib.sha256(item.read_bytes()).digest())
    manifest = next(
        value.as_mapping() for value in structural_scenario_manifests()
        if value.profile_id == "goad-trust-walker"
    )
    manifest["source_sha256"] = digest.hexdigest()
    return manifest, external


def _successful_phase(
    name: str, command: list[str], _config: dict
) -> dict:
    assert command
    if name == "wait-range-ips":
        return _canonical_ready_output("range_boot")
    if name == "final-readiness":
        return _canonical_ready_output("full")
    return {"ok": True, "phase": name}


def _task_observation(
    *,
    count: int = 0,
    max_task_id: int | None = None,
    operation_id: int = 7,
    operator_id: int = 11,
    operator_username: str = "mythic_admin",
    observed_at: str = "2026-07-23T00:00:02+00:00",
) -> dict:
    return {
        "scope": "mythic-operation",
        "operation_id": operation_id,
        "operator_id": operator_id,
        "operator_username": operator_username,
        "count": count,
        "max_task_id": max_task_id,
        "observed_at": observed_at,
    }


def _zero_tasks(_config: dict) -> dict:
    return _task_observation()


def _complete_phase(phase: dict) -> None:
    phase["status"] = "completed"
    phase["started_at"] = "2026-07-23T00:00:00+00:00"
    phase["finished_at"] = "2026-07-23T00:00:01+00:00"
    if phase["name"] == "wait-range-ips":
        output = _canonical_ready_output("range_boot")
    elif phase["name"] == "final-readiness":
        output = _canonical_ready_output("full")
    else:
        output = {"ok": True}
    phase["result_summary"] = reset._completed_result(
        phase["name"], output
    )
    phase["error"] = None


def _running_phase(phase: dict) -> None:
    phase["status"] = "running"
    phase["started_at"] = "2026-07-23T00:00:00+00:00"
    phase["finished_at"] = None
    phase["result_summary"] = None
    phase["error"] = None


def _failed_phase(phase: dict, error: str) -> None:
    phase["status"] = "failed"
    phase["started_at"] = "2026-07-23T00:00:00+00:00"
    phase["finished_at"] = "2026-07-23T00:00:01+00:00"
    phase["result_summary"] = None
    phase["error"] = error


def _reset_observation() -> dict:
    return {
        **_task_observation(),
        "phase": "reset-mythic",
        "delta_from_baseline": 0,
    }


def _assert_resume_rejected_without_runner(
    tmp_path: Path, checkpoint: dict, *, match: str
) -> None:
    path = tmp_path / "reset.json"
    reset._atomic_write(path, checkpoint)
    calls = []
    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=lambda *args: calls.append(args),
        task_observer=_zero_tasks,
    )
    with pytest.raises(reset.ResetError, match=match):
        orchestrator.resume(operator_foothold_launched=False)
    assert calls == []


def test_plan_has_canonical_order_and_never_deploys_payload():
    commands = reset._phase_commands(_config())

    assert tuple(commands) == reset.PHASE_NAMES
    serialized = json.dumps(commands)
    assert "deploy_payload_via_ludus" not in serialized
    assert commands["bootstrap-chat-and-foothold"][-1] != "--prepare-chat"
    assert "--no-prepare-chat" in commands["bootstrap-chat-and-foothold"]
    assert "--no-require-prepared-channel" in commands["final-readiness"]
    assert commands["rollback-range"][2:6] == [
        "--mcp-server", "ludus_goad", "--range-id", "GOAD"
    ]


def test_unqualified_profile_can_be_planned_but_not_started(capsys):
    common = [
        "--snapshot", "snapshot-v1",
        "--range-profile", "sage-structural-b",
        "--callback-host", "http://example.invalid",
        "--mythic-env-path", "/tmp/mythic.env",
    ]

    assert reset.main(["plan", *common]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["inputs"]["range_profile_id"] == "sage-structural-b"
    assert planned["live_activity_performed"] is False

    assert reset.main(["start", *common, "--yes"]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert "not live-qualified" in blocked["error"]


def test_reset_plan_propagates_explicit_external_source_to_manifest_validation(tmp_path, capsys):
    manifest, external = _external_goad_manifest(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    common = [
        "--snapshot", manifest["snapshot_id"],
        "--range-profile", manifest["profile_id"],
        "--scenario-manifest", str(manifest_path),
        "--callback-host", "http://example.invalid",
        "--mythic-env-path", "/tmp/mythic.env",
    ]

    assert reset.main(["plan", *common]) == 2
    assert "external_source_unverified" in json.loads(capsys.readouterr().out)["error"]

    assert reset.main(["plan", *common, "--external-source-dir", str(external)]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["scenario_manifest_binding"]["source_sha256"] == manifest["source_sha256"]
    assert planned["live_activity_performed"] is False


def test_reset_binding_validation_propagates_explicit_external_source(tmp_path):
    manifest, external = _external_goad_manifest(tmp_path)
    invalid_checkpoint = {"schema": "deliberately-invalid"}
    kwargs = {
        "expected_run_id": "reset-test",
        "not_before": "2026-01-01T00:00:00+00:00",
    }

    with pytest.raises(reset.ResetError, match="external_source_unverified"):
        reset.validate_scenario_reset_binding(
            manifest, invalid_checkpoint, tmp_path / "checkpoint.json", **kwargs,
        )
    with pytest.raises(reset.ResetError, match="unsupported reset checkpoint"):
        reset.validate_scenario_reset_binding(
            manifest, invalid_checkpoint, tmp_path / "checkpoint.json",
            external_source_dir=external, **kwargs,
        )


def test_structural_scenario_reset_binding_rejects_missing_wrong_or_simulated_evidence(tmp_path):
    manifest = next(
        item.as_mapping()
        for item in structural_scenario_manifests()
        if item.profile_id == "sage-structural-a"
    )
    config = _config()
    config["range_profile_id"] = manifest["profile_id"]
    config["snapshot"] = manifest["snapshot_id"]
    checkpoint = reset._new_checkpoint(config, "reset-test")
    binding = {"expected_run_id": "reset-test", "not_before": "2026-01-01T00:00:00+00:00"}

    with pytest.raises(reset.ResetError, match="reset evidence is missing"):
        reset.validate_scenario_reset_binding(manifest, None, tmp_path / "missing.json", **binding)
    with pytest.raises(reset.ResetError, match="checkpoint profile does not match"):
        wrong_profile_config = {**config, "range_profile_id": "goad-trust-walker"}
        wrong_profile = reset._new_checkpoint(wrong_profile_config, "reset-test")
        reset.validate_scenario_reset_binding(manifest, wrong_profile, tmp_path / "wrong-profile.json", **binding)
    with pytest.raises(reset.ResetError, match="checkpoint snapshot does not match"):
        wrong_snapshot_config = {**config, "snapshot": "invented-snapshot"}
        wrong_snapshot = reset._new_checkpoint(wrong_snapshot_config, "reset-test")
        reset.validate_scenario_reset_binding(manifest, wrong_snapshot, tmp_path / "wrong-snapshot.json", **binding)
    with pytest.raises(reset.ResetError, match="task_observations do not match"):
        wrong_task = copy.deepcopy(checkpoint)
        wrong_task["task_observations"] = [_reset_observation()]
        reset.validate_scenario_reset_binding(manifest, wrong_task, tmp_path / "wrong-task.json", **binding)
    simulated_path = tmp_path / "simulated-valid-shape.json"
    orchestrator = reset.ResetOrchestrator(
        simulated_path, phase_runner=_successful_phase, task_observer=_zero_tasks
    )
    assert orchestrator.start(_config(), run_id="reset-test")[0] == 0
    _, simulated = orchestrator.resume(operator_foothold_launched=True)
    simulated["inputs"] = reset._redact(config)
    for phase in simulated["phases"]:
        phase["command"] = reset._redact(reset._phase_commands(config)[phase["name"]])
    with pytest.raises(reset.ResetError, match="does not bind scenario manifest source"):
        reset.validate_scenario_reset_binding(manifest, simulated, simulated_path, **binding)
    with pytest.raises(reset.ResetError, match="run ID does not match"):
        reset.validate_scenario_reset_binding(
            manifest, simulated, simulated_path,
            expected_run_id="other-run", not_before=binding["not_before"],
        )
    with pytest.raises(reset.ResetError, match="predates prospective not-before"):
        reset.validate_scenario_reset_binding(
            manifest, simulated, simulated_path,
            expected_run_id=binding["expected_run_id"], not_before="2999-01-01T00:00:00+00:00",
        )
    simulated["scenario_manifest_binding"] = reset._scenario_manifest_binding(manifest)
    with pytest.raises(reset.ResetError, match="profile_not_live_qualified"):
        reset.validate_scenario_reset_binding(manifest, simulated, simulated_path, **binding)


def test_scenario_manifest_validation_cli_runs_local_positive_and_negative_fixture(tmp_path, capsys):
    profile_module = reset._load_module("range_profile_cli_fixture", reset.RANGE_PROFILE)
    selected = profile_module.get_profile("sage-replication-range")
    source_path = selected["source"]["path"]
    manifest = {
        "schema": profile_module.SCENARIO_RESET_MANIFEST_SCHEMA,
        "scenario_id": "replication-purpose-range-visible-cost",
        "profile_id": selected["id"],
        "source_path": source_path,
        "source_sha256": profile_module.source_tree_sha256(source_path),
        "available_capabilities": sorted(profile_module.available_capability_set(selected)),
        "required_capabilities": sorted(profile_module.required_capability_set(selected)),
        "required_routes": [sorted(route) for route in profile_module.declared_required_routes(selected)],
        "required_route_coverage": profile_module.required_route_coverage(selected),
        "oracle_dependencies": ["mythic-task-output-or-artifact"],
        "snapshot_id": profile_module.require_scenario(
            selected, "replication-purpose-range-visible-cost"
        )["snapshot"],
        "reset_checkpoint_schema": reset.SCHEMA,
    }
    base_path = tmp_path / "base.json"
    orchestrator = reset.ResetOrchestrator(
        base_path, phase_runner=_successful_phase, task_observer=_zero_tasks
    )
    assert orchestrator.start(_config(), run_id="cli-reset-test")[0] == 0
    _, checkpoint = orchestrator.resume(operator_foothold_launched=True)
    config = {**_config(), "range_profile_id": selected["id"], "snapshot": manifest["snapshot_id"]}
    checkpoint["inputs"] = reset._redact(config)
    for phase in checkpoint["phases"]:
        phase["command"] = reset._redact(reset._phase_commands(config)[phase["name"]])
    checkpoint["scenario_manifest_binding"] = reset._scenario_manifest_binding(manifest)
    manifest_path = tmp_path / "manifest.json"
    checkpoint_path = tmp_path / "checkpoint.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    common = [
        "validate-scenario-manifest", "--manifest", str(manifest_path),
        "--checkpoint", str(checkpoint_path), "--expected-run-id", "cli-reset-test",
        "--not-before", "2026-01-01T00:00:00+00:00",
    ]
    assert reset.main([
        *common,
        "--expected-range-id", "GOAD",
        "--expected-ludus-mcp-server", "ludus_goad",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["live_activity_performed"] is False

    # Omitting optional selectors preserves the historical binding contract.
    assert reset.main(common) == 0
    assert json.loads(capsys.readouterr().out)["live_activity_performed"] is False

    for selector, value, error in (
        ("--expected-range-id", "other-range", "range ID does not match"),
        ("--expected-ludus-mcp-server", "other-server", "MCP server does not match"),
        ("--expected-range-id", "", "range ID must be a non-empty string"),
        ("--expected-ludus-mcp-server", "", "MCP server must be a non-empty string"),
    ):
        assert reset.main([*common, selector, value]) == 2
        assert error in json.loads(capsys.readouterr().out)["error"]

    assert reset.main([*common, "--expected-run-id", "wrong-run"]) == 2
    assert "run ID does not match" in json.loads(capsys.readouterr().out)["error"]


def _fixture_manifest(profile_module, profile_id="sage-replication-range", snapshot=None):
    selected = profile_module.get_profile(profile_id)
    source_path = selected["source"]["path"]
    scenario = selected["bindings"][0]["scenario"]
    return {
        "schema": profile_module.SCENARIO_RESET_MANIFEST_SCHEMA,
        "scenario_id": scenario,
        "profile_id": selected["id"],
        "source_path": source_path,
        "source_sha256": profile_module.source_tree_sha256(source_path),
        "available_capabilities": sorted(profile_module.available_capability_set(selected)),
        "required_capabilities": sorted(profile_module.required_capability_set(selected)),
        "required_routes": [sorted(route) for route in profile_module.declared_required_routes(selected)],
        "required_route_coverage": profile_module.required_route_coverage(selected),
        "oracle_dependencies": ["mythic-task-output-or-artifact"],
        "snapshot_id": snapshot or profile_module.require_scenario(selected, scenario)["snapshot"],
        "reset_checkpoint_schema": reset.RANGE_FIXTURE_QUALIFICATION_SCHEMA,
    }


def _fixture_action_bindings(config):
    return {
        name: {
            "mechanism": "internal-observer" if name in {
                "capture-task-and-callback-baseline", "fresh-callback-selection",
            } else "direct-function",
            "adapter": "tests.fixture_observer" if name in {
                "capture-task-and-callback-baseline", "fresh-callback-selection",
            } else "tests.fixture_phase",
            "range_profile_id": config["range_profile_id"],
            "ludus_range_id": config["ludus_range_id"],
            "ludus_mcp_server": config["ludus_mcp_server"],
        }
        for name in reset.RANGE_FIXTURE_QUALIFICATION_PHASES
    }


def _fixture_phase(name, action_binding, config, *, callback_id=2):
    result_binding = {
        "mechanism": action_binding["mechanism"],
        "adapter": action_binding["adapter"],
    }
    if name == "wait-range-ips":
        return {
            **result_binding,
            "schema": "sage-range-fixture-inventory-v1",
            "observation_id": "fixture-inventory",
            "live_ready": False,
            "inventory": {
                "ready": True,
                "live_ready": False,
                "qualification_observation": True,
                "range_profile": {"id": config["range_profile_id"]},
            },
        }
    if name == "await-operator-foothold-launch":
        return {
            **result_binding,
            "operator_acknowledged": True, "payload_tasks_issued": 0,
        }
    if name == "final-start-readiness":
        return {
            **result_binding,
            "ok": True, "endpoint_observed": True, "callback_id": callback_id,
        }
    return {
        **result_binding,
        "ok": True, "exit_code": 0, "observer_state": {"observed": name},
    }


def _fixture_callback(callback_id, *, observed_at=None, active=True):
    return {
        "display_id": callback_id,
        "host": "srv02",
        "domain": "fixture.local",
        "user": "user1",
        "payload_type": "apollo",
        "payload_uuid": "fixture-payload-uuid",
        "operation_id": 7,
        "active": active,
        "integrity_level": 2,
        "observed_at": observed_at or reset._now(),
    }


def _fixture_expectation():
    return {
        "host": "srv02", "domain": "fixture.local", "user": "user1",
        "payload_type": "apollo", "payload_uuid": "fixture-payload-uuid",
        "operation_id": 7, "active": True, "integrity": "medium",
    }


def test_range_fixture_qualification_captures_real_scope_without_v2_admission(tmp_path, capsys):
    profile_module = reset._load_module("range_fixture_profile", reset.RANGE_PROFILE)
    manifest = _fixture_manifest(profile_module)
    config = {**_config(), "range_profile_id": manifest["profile_id"], "snapshot": manifest["snapshot_id"]}
    checkpoint_path = tmp_path / "fixture.json"
    callbacks = iter([
        [_fixture_callback(1, observed_at="2026-01-01T00:00:00+00:00", active=False)],
        [
            _fixture_callback(1, observed_at="2026-01-01T00:00:00+00:00", active=False),
            _fixture_callback(2, observed_at="2999-01-01T00:00:00+00:00"),
        ],
    ])

    result = reset.run_range_fixture_qualification(
        config, manifest=manifest, run_id="fixture-reset", checkpoint_path=checkpoint_path,
        callback_expectation=_fixture_expectation(),
        action_bindings=_fixture_action_bindings(config),
        phase_runner=_fixture_phase,
        task_observer=lambda _config: _task_observation(count=3, max_task_id=9),
        callback_observer=lambda _config: next(callbacks),
    )
    checkpoint = result["checkpoint"]
    assert result["live_ready"] is False
    assert result["observation_only"] is True
    assert [phase["name"] for phase in checkpoint["phases"]] == list(reset.RANGE_FIXTURE_QUALIFICATION_PHASES)
    assert checkpoint["task_baseline"]["count"] == 3
    assert checkpoint["fresh_callback"]["display_id"] == 2
    assert checkpoint["fresh_callback"]["active"] is True
    assert all("command" not in phase for phase in checkpoint["phases"])
    assert all(
        phase["action_binding"]["mechanism"] in {
            "internal-observer", "direct-function",
        }
        for phase in checkpoint["phases"]
    )
    assert checkpoint["phases"][1]["action_binding"]["adapter"] == "tests.fixture_phase"
    assert checkpoint["terminal"]["state"] == "complete"
    assert "reset-mythic" not in {phase["name"] for phase in checkpoint["phases"]}

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert reset.main([
        "validate-range-fixture-qualification", "--manifest", str(manifest_path),
        "--checkpoint", str(checkpoint_path), "--expected-run-id", "fixture-reset",
        "--not-before", "2026-01-01T00:00:00+00:00",
        "--expected-range-id", "GOAD", "--expected-ludus-mcp-server", "ludus_goad",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["live_ready"] is False
    assert report["observation_only"] is True

    altered = copy.deepcopy(checkpoint)
    altered["fresh_callback"]["payload_uuid"] = "other-payload"
    with pytest.raises(reset.ResetError, match="does not match expectation"):
        reset.validate_range_fixture_qualification(
            manifest, altered, checkpoint_path,
            expected_run_id="fixture-reset",
            not_before="2026-01-01T00:00:00+00:00",
            expected_range_id="GOAD",
            expected_ludus_mcp_server="ludus_goad",
        )

    altered = copy.deepcopy(checkpoint)
    altered["phases"][1]["action_binding"]["adapter"] = "other.adapter"
    with pytest.raises(reset.ResetError, match="mechanism does not match"):
        reset.validate_range_fixture_qualification(
            manifest, altered, checkpoint_path,
            expected_run_id="fixture-reset",
            not_before="2026-01-01T00:00:00+00:00",
            expected_range_id="GOAD",
            expected_ludus_mcp_server="ludus_goad",
        )

    assert reset.main([
        "validate-range-fixture-qualification", "--manifest", str(manifest_path),
        "--checkpoint", str(checkpoint_path), "--expected-run-id", "fixture-reset",
        "--not-before", "2026-01-01T00:00:00+00:00",
        "--expected-range-id", "other", "--expected-ludus-mcp-server", "ludus_goad",
    ]) == 2
    assert "selector does not match" in json.loads(capsys.readouterr().out)["error"]


def test_range_fixture_qualification_allows_private_allocated_snapshot_for_defined_only_profile(tmp_path):
    profile_module = reset._load_module("range_fixture_bsl_profile", reset.RANGE_PROFILE)
    manifest = next(item.as_mapping() for item in structural_scenario_manifests() if item.profile_id == "bsl-ad-elastic")
    manifest["snapshot_id"] = "private-allocation-snapshot"
    assert manifest["reset_checkpoint_schema"] == reset.RANGE_FIXTURE_QUALIFICATION_SCHEMA
    config = {**_config(), "range_profile_id": "bsl-ad-elastic", "snapshot": manifest["snapshot_id"], "ludus_range_id": "fixture-range", "ludus_mcp_server": "fixture-server"}
    callbacks = iter([
        [_fixture_callback(10, observed_at="2026-01-01T00:00:00+00:00")],
        [
            _fixture_callback(10, observed_at="2026-01-01T00:00:00+00:00"),
            _fixture_callback(11, observed_at="2999-01-01T00:00:00+00:00"),
        ],
    ])
    result = reset.run_range_fixture_qualification(
        config, manifest=manifest, run_id="defined-only-fixture", checkpoint_path=tmp_path / "defined-only.json",
        callback_expectation=_fixture_expectation(),
        action_bindings=_fixture_action_bindings(config),
        phase_runner=lambda *args: _fixture_phase(*args, callback_id=11),
        task_observer=lambda _config: _task_observation(count=2, max_task_id=7),
        callback_observer=lambda _config: next(callbacks),
    )
    assert result["live_ready"] is False
    assert profile_module.get_profile("bsl-ad-elastic")["qualification"]["state"] == "defined_only"

    old_schema = {**manifest, "reset_checkpoint_schema": reset.SCHEMA}
    with pytest.raises(reset.ResetError, match="checkpoint schema does not match"):
        reset.run_range_fixture_qualification(
            config, manifest=old_schema, run_id="old-schema-fixture",
            checkpoint_path=tmp_path / "old-schema.json",
            callback_expectation=_fixture_expectation(),
            action_bindings=_fixture_action_bindings(config),
            phase_runner=_fixture_phase,
            task_observer=lambda _config: _task_observation(count=2, max_task_id=7),
            callback_observer=lambda _config: [],
        )


def test_range_fixture_qualification_fails_changed_task_baseline_and_multiple_callbacks(tmp_path):
    profile_module = reset._load_module("range_fixture_negative_profile", reset.RANGE_PROFILE)
    manifest = _fixture_manifest(profile_module)
    config = {**_config(), "range_profile_id": manifest["profile_id"], "snapshot": manifest["snapshot_id"]}
    observations = iter([_task_observation(count=1, max_task_id=2), _task_observation(count=2, max_task_id=3)])
    callbacks = iter([[_fixture_callback(1)], [_fixture_callback(1), _fixture_callback(2), _fixture_callback(3)]])
    with pytest.raises(reset.ResetError, match="task baseline changed"):
        reset.run_range_fixture_qualification(
            config, manifest=manifest, run_id="negative-fixture", checkpoint_path=tmp_path / "negative.json",
            callback_expectation=_fixture_expectation(),
            action_bindings=_fixture_action_bindings(config),
            phase_runner=_fixture_phase, task_observer=lambda _config: next(observations),
            callback_observer=lambda _config: next(callbacks),
        )
    persisted = json.loads((tmp_path / "negative.json").read_text())
    assert persisted["terminal"]["state"] == "blocked"
    assert persisted["phases"][-2]["status"] == "failed"

    callbacks = iter([
        [_fixture_callback(1, observed_at="2026-01-01T00:00:00+00:00")],
        [
            _fixture_callback(1, observed_at="2026-01-01T00:00:00+00:00"),
            _fixture_callback(2, observed_at="2999-01-01T00:00:00+00:00"),
        ],
    ])

    def failed_poweron(name, action_binding, config):
        if name == "poweron-range":
            return {
                "mechanism": action_binding["mechanism"],
                "adapter": action_binding["adapter"],
                "ok": False, "exit_code": 1, "observer_state": "off",
            }
        return _fixture_phase(name, action_binding, config)

    with pytest.raises(reset.ResetError, match="result field ok must be true"):
        reset.run_range_fixture_qualification(
            config, manifest=manifest, run_id="failed-action-fixture",
            checkpoint_path=tmp_path / "failed-action.json",
            callback_expectation=_fixture_expectation(),
            action_bindings=_fixture_action_bindings(config),
            phase_runner=failed_poweron,
            task_observer=lambda _config: _task_observation(count=1, max_task_id=2),
            callback_observer=lambda _config: next(callbacks),
        )
    failed = json.loads((tmp_path / "failed-action.json").read_text())
    assert failed["terminal"]["state"] == "blocked"
    assert failed["phases"][2]["name"] == "poweron-range"
    assert failed["phases"][2]["status"] == "failed"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (AssertionError(), "AssertionError"),
        (RuntimeError("adapter failure"), "adapter failure"),
    ],
)
def test_range_fixture_qualification_persists_nonempty_adapter_failure(tmp_path, failure, expected):
    profile_module = reset._load_module("range_fixture_adapter_failure", reset.RANGE_PROFILE)
    manifest = _fixture_manifest(profile_module)
    config = {**_config(), "range_profile_id": manifest["profile_id"], "snapshot": manifest["snapshot_id"]}

    def failing_rollback(name, action_binding, runner_config):
        if name == "rollback-range":
            raise failure
        return _fixture_phase(name, action_binding, runner_config)

    with pytest.raises(reset.ResetError, match=expected):
        reset.run_range_fixture_qualification(
            config, manifest=manifest, run_id="adapter-failure-fixture",
            checkpoint_path=tmp_path / "adapter-failure.json",
            callback_expectation=_fixture_expectation(),
            action_bindings=_fixture_action_bindings(config),
            phase_runner=failing_rollback,
            task_observer=lambda _config: _task_observation(count=1, max_task_id=2),
            callback_observer=lambda _config: [_fixture_callback(1)],
        )

    persisted = json.loads((tmp_path / "adapter-failure.json").read_text())
    reset._validate_range_fixture_checkpoint(
        persisted, tmp_path / "adapter-failure.json",
    )
    assert persisted["terminal"] == {"state": "blocked", "reason": expected}
    assert persisted["phases"][0]["status"] == "completed"
    assert persisted["phases"][1]["name"] == "rollback-range"
    assert persisted["phases"][1]["status"] == "failed"
    assert persisted["phases"][1]["error"] == expected


def test_explicit_manifest_capture_is_persisted_before_synthetic_reset_steps(tmp_path):
    profile_module = reset._load_module("range_profile_capture_fixture", reset.RANGE_PROFILE)
    selected = profile_module.get_profile("sage-replication-range")
    source_path = selected["source"]["path"]
    manifest = {
        "schema": profile_module.SCENARIO_RESET_MANIFEST_SCHEMA,
        "scenario_id": "replication-purpose-range-visible-cost",
        "profile_id": selected["id"],
        "source_path": source_path,
        "source_sha256": profile_module.source_tree_sha256(source_path),
        "available_capabilities": sorted(profile_module.available_capability_set(selected)),
        "required_capabilities": sorted(profile_module.required_capability_set(selected)),
        "required_routes": [sorted(route) for route in profile_module.declared_required_routes(selected)],
        "required_route_coverage": profile_module.required_route_coverage(selected),
        "oracle_dependencies": ["mythic-task-output-or-artifact"],
        "snapshot_id": profile_module.require_scenario(
            selected, "replication-purpose-range-visible-cost"
        )["snapshot"],
        "reset_checkpoint_schema": reset.SCHEMA,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    args = reset.build_parser().parse_args([
        "plan", "--snapshot", manifest["snapshot_id"], "--range-profile", selected["id"],
        "--callback-host", "http://example.invalid", "--mythic-env-path", "/tmp/mythic.env",
        "--scenario-manifest", str(manifest_path),
    ])
    config = reset._config_from_args(args)
    capture = reset._scenario_manifest_capture(args, config)
    checkpoint_path = tmp_path / "checkpoint.json"

    def runner(*args):
        persisted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert persisted["scenario_manifest_binding"] == capture
        return _successful_phase(*args)

    orchestrator = reset.ResetOrchestrator(
        checkpoint_path, phase_runner=runner, task_observer=_zero_tasks
    )
    assert orchestrator.start(
        config, run_id="captured-reset", scenario_manifest_binding=capture
    )[0] == 0
    _, checkpoint = orchestrator.resume(operator_foothold_launched=True)
    assert reset.validate_scenario_reset_binding(
        manifest, checkpoint, checkpoint_path,
        expected_run_id="captured-reset", not_before="2026-01-01T00:00:00+00:00",
    )["run_id"] == "captured-reset"
    assert "scenario_manifest_binding" not in reset._new_checkpoint(_config(), "legacy-reset")

    mismatched = reset.build_parser().parse_args([
        "plan", "--snapshot", "wrong-snapshot", "--range-profile", selected["id"],
        "--callback-host", "http://example.invalid", "--mythic-env-path", "/tmp/mythic.env",
        "--scenario-manifest", str(manifest_path),
    ])
    with pytest.raises(reset.ResetError, match="snapshot does not match"):
        reset._scenario_manifest_capture(mismatched, reset._config_from_args(mismatched))


def test_wait_for_range_delegates_to_canonical_fresh_poller(monkeypatch):
    calls = []
    probe_calls = []

    class _Contract:
        LUDUS_PROBE_TIMEOUT_SECONDS = 30.0
        DEFAULT_TIMEOUT_GRACE_SECONDS = 5.0

        @staticmethod
        def range_boot_observation(*_args):
            probe_calls.append(_args)
            return {}

        @staticmethod
        def poll_readiness(probe, **kwargs):
            calls.append((probe, kwargs))
            return _canonical_ready_output("range_boot")

    monkeypatch.setattr(reset, "_load_module", lambda *_args: _Contract)

    result = reset._wait_for_range(_config(), sleep=lambda _seconds: None)

    assert result["ready"] is True
    assert len(calls) == 1
    calls[0][0]()
    assert probe_calls == [("GOAD", "ludus_goad", "goad-trust-walker")]
    assert calls[0][1]["lane"] == "range_boot"
    assert calls[0][1]["outer_timeout_seconds"] == 60


def test_phase_runner_exports_persisted_profile_and_range_route(monkeypatch):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(reset.subprocess, "run", fake_run)

    reset.run_phase_command("final-readiness", ["readiness"], _config())

    assert observed["env"]["SAGE_RANGE_PROFILE_ID"] == "goad-trust-walker"
    assert observed["env"]["SAGE_LUDUS_RANGE_ID"] == "GOAD"
    assert observed["env"]["SAGE_LUDUS_MCP_SERVER"] == "ludus_goad"


def test_cli_rejects_outer_timeout_not_greater_than_child_plus_grace():
    args = reset.build_parser().parse_args(
        [
            "plan",
            "--snapshot",
            "exact-clean-snapshot",
            "--callback-host",
            "http://100.64.0.1",
            "--range-ready-timeout",
            "35",
        ]
    )

    with pytest.raises(reset.ResetError, match="strictly greater"):
        reset._config_from_args(args)


def test_start_pauses_at_operator_boundary_with_zero_task_delta(tmp_path):
    path = tmp_path / "reset.json"
    calls = []

    def runner(name, command, config):
        calls.append(name)
        return _successful_phase(name, command, config)

    orchestrator = reset.ResetOrchestrator(
        path, phase_runner=runner, task_observer=_zero_tasks
    )
    code, checkpoint = orchestrator.start(_config(), run_id="reset-test")

    assert code == 0
    assert checkpoint["terminal"]["state"] == "awaiting_operator"
    assert checkpoint["task_baseline"]["count"] == 0
    assert [row["delta_from_baseline"] for row in checkpoint["task_observations"]] == [
        0,
        0,
    ]
    assert calls[-1] == "bootstrap-chat-and-foothold"
    assert "post-callback-preflight" not in calls


def test_resume_after_operator_ack_completes_without_payload_tasks(tmp_path):
    path = tmp_path / "reset.json"
    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=_successful_phase,
        task_observer=_zero_tasks,
    )
    code, _checkpoint = orchestrator.start(
        _config(), run_id="reset-test"
    )
    assert code == 0

    code, checkpoint = orchestrator.resume(
        operator_foothold_launched=True
    )

    assert code == 0
    assert checkpoint["terminal"]["state"] == "complete"
    assert all(
        phase["status"] == "completed"
        for phase in checkpoint["phases"]
    )
    assert len(checkpoint["task_observations"]) == 5
    assert all(
        observation["delta_from_baseline"] == 0
        for observation in checkpoint["task_observations"]
    )
    assert {
        (
            observation["scope"],
            observation["operation_id"],
            observation["operator_id"],
            observation["operator_username"],
        )
        for observation in checkpoint["task_observations"]
    } == {("mythic-operation", 7, 11, "mythic_admin")}
    assert all(
        reset._aware_time(observation["observed_at"]) is not None
        for observation in checkpoint["task_observations"]
    )


@pytest.mark.parametrize(
    "ready_value",
    ["true", 1, 1.0, {"value": True}, None],
)
def test_final_readiness_requires_literal_json_boolean_true(
    tmp_path, ready_value
):
    path = tmp_path / "reset.json"

    def runner(name, command, config):
        if name == "final-readiness":
            return {"ready": ready_value, "blockers": []}
        return _successful_phase(name, command, config)

    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=runner,
        task_observer=_zero_tasks,
    )
    code, _checkpoint = orchestrator.start(
        _config(), run_id="reset-test"
    )
    assert code == 0

    code, checkpoint = orchestrator.resume(
        operator_foothold_launched=True
    )

    assert code == 1
    assert checkpoint["terminal"]["state"] == "blocked"
    final = checkpoint["phases"][-1]
    assert final["status"] == "failed"
    assert "canonical full readiness" in final["error"]
    assert [
        row["phase"] for row in checkpoint["task_observations"]
    ] == [
        "reset-mythic",
        "bootstrap-chat-and-foothold",
        "operator-foothold-launch",
        "post-callback-preflight",
    ]


def test_literal_boolean_true_final_readiness_completes(tmp_path):
    path = tmp_path / "reset.json"
    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=_successful_phase,
        task_observer=_zero_tasks,
    )
    code, _checkpoint = orchestrator.start(
        _config(), run_id="reset-test"
    )
    assert code == 0

    code, checkpoint = orchestrator.resume(
        operator_foothold_launched=True
    )

    assert code == 0
    assert checkpoint["terminal"]["state"] == "complete"
    assert checkpoint["phases"][-1]["result_summary"]["phase"] == "final-readiness"
    assert checkpoint["phases"][-1]["result_summary"]["succeeded"] is True
    assert checkpoint["phases"][-1]["result_summary"]["output"]["ready"] is True


def test_nonzero_task_delta_blocks_before_operator_boundary(tmp_path):
    path = tmp_path / "reset.json"
    observations = iter(
        [
            _task_observation(),
            _task_observation(count=1, max_task_id=16),
        ]
    )
    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=_successful_phase,
        task_observer=lambda _config: next(observations),
    )

    code, checkpoint = orchestrator.start(
        _config(), run_id="reset-test"
    )

    assert code == 1
    assert checkpoint["terminal"]["state"] == "blocked"
    assert "task delta became 1" in checkpoint["terminal"]["reason"]
    failed = next(
        phase
        for phase in checkpoint["phases"]
        if phase["status"] == "failed"
    )
    assert failed["name"] == "bootstrap-chat-and-foothold"


@pytest.mark.parametrize(
    ("changed_field", "changed_value", "match"),
    [
        ("operation_id", 8, "operation_id changed"),
        ("operator_id", 12, "operator_id changed"),
        ("operator_username", "another-user", "operator_username changed"),
        ("scope", "visible-tasks", "scope must be mythic-operation"),
        (
            "observed_at",
            "2026-07-22T23:59:59+00:00",
            "time reversed",
        ),
    ],
)
def test_live_task_scope_drift_blocks_before_next_phase(
    tmp_path, changed_field, changed_value, match
):
    path = tmp_path / "reset.json"
    changed = _task_observation()
    changed[changed_field] = changed_value
    observations = iter([_task_observation(), changed])
    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=_successful_phase,
        task_observer=lambda _config: next(observations),
    )

    code, checkpoint = orchestrator.start(
        _config(), run_id="reset-test"
    )

    assert code == 1
    assert checkpoint["terminal"]["state"] == "blocked"
    assert match in checkpoint["terminal"]["reason"]
    assert [
        row["phase"] for row in checkpoint["task_observations"]
    ] == ["reset-mythic"]


def test_retryable_failed_phase_is_normalized_before_rerun(tmp_path):
    path = tmp_path / "reset.json"
    failed_once = {"value": False}

    def runner(name, command, config):
        if name == "wait-range-ips" and not failed_once["value"]:
            failed_once["value"] = True
            raise reset.ResetError("range still booting")
        return _successful_phase(name, command, config)

    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=runner,
        task_observer=_zero_tasks,
    )
    code, checkpoint = orchestrator.start(_config(), run_id="reset-test")
    assert code == 1
    assert checkpoint["terminal"]["state"] == "blocked"
    assert checkpoint["phases"][5]["name"] == "wait-range-ips"
    assert checkpoint["phases"][5]["status"] == "failed"

    code, checkpoint = orchestrator.resume(
        operator_foothold_launched=False
    )

    assert code == 0
    assert checkpoint["terminal"]["state"] == "awaiting_operator"
    assert checkpoint["phases"][5]["status"] == "completed"


def test_failed_operator_boundary_is_not_retried(tmp_path):
    path = tmp_path / "reset.json"
    observations = iter(
        [
            _task_observation(),
            _task_observation(),
            _task_observation(count=1, max_task_id=16),
        ]
    )
    calls = []

    def runner(name, command, config):
        calls.append(name)
        return _successful_phase(name, command, config)

    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=runner,
        task_observer=lambda _config: next(observations),
    )
    code, _checkpoint = orchestrator.start(_config(), run_id="reset-test")
    assert code == 0

    code, checkpoint = orchestrator.resume(
        operator_foothold_launched=True
    )
    assert code == 1
    assert checkpoint["phases"][10]["status"] == "failed"
    assert checkpoint["terminal"]["state"] == "blocked"
    call_count = len(calls)

    code, checkpoint = orchestrator.resume(
        operator_foothold_launched=True
    )

    assert code == 1
    assert len(calls) == call_count
    assert "ambiguous failed state" in checkpoint["terminal"]["reason"]


def test_ambiguous_interrupted_mythic_reset_fails_closed(tmp_path):
    path = tmp_path / "reset.json"
    checkpoint = reset._new_checkpoint(_config(), "reset-test")
    for phase in checkpoint["phases"][:2]:
        _complete_phase(phase)
    phase = next(
        item
        for item in checkpoint["phases"]
        if item["name"] == "reset-mythic"
    )
    _running_phase(phase)
    reset._atomic_write(path, checkpoint)
    calls = []
    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=lambda *args: calls.append(args),
        task_observer=_zero_tasks,
    )

    code, checkpoint = orchestrator.resume(
        operator_foothold_launched=False
    )

    assert code == 1
    assert calls == []
    assert checkpoint["terminal"]["state"] == "blocked"
    assert phase["name"] == "reset-mythic"


def test_failed_ambiguous_bootstrap_is_not_retried(tmp_path):
    path = tmp_path / "reset.json"
    checkpoint = reset._new_checkpoint(_config(), "reset-test")
    for phase in checkpoint["phases"]:
        if phase["name"] == "bootstrap-chat-and-foothold":
            _failed_phase(phase, "connection dropped")
            break
        _complete_phase(phase)
    checkpoint["task_baseline"] = _task_observation()
    checkpoint["task_observations"] = [_reset_observation()]
    checkpoint["terminal"] = {
        "state": "blocked",
        "reason": "connection dropped",
    }
    reset._atomic_write(path, checkpoint)
    calls = []
    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=lambda *args: calls.append(args),
        task_observer=_zero_tasks,
    )

    code, checkpoint = orchestrator.resume(
        operator_foothold_launched=False
    )

    assert code == 1
    assert calls == []
    assert "ambiguous failed state" in checkpoint["terminal"]["reason"]


def test_plan_command_is_read_only_and_redacts_no_secret_values(
    tmp_path, capsys
):
    rc = reset.main(
        [
            "plan",
            "--snapshot",
            "exact-clean-snapshot",
            "--callback-host",
            "http://100.64.0.1",
        ]
    )

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["live_activity_performed"] is False
    assert not list(tmp_path.iterdir())


def test_secret_restart_env_is_refused():
    try:
        reset._parse_restart_env(["SAGE_API_KEY=secret"])
    except reset.ResetError as exc:
        assert "secret-bearing" in str(exc)
    else:
        raise AssertionError("secret-bearing restart env must be refused")


def _scope_bootstrap(
    *,
    client_operation_id: object = 7,
    observed_operation_id: object = 7,
    operator_id: object = 11,
    operator_username: object = "mythic_admin",
):
    calls = []

    async def login(_namespace):
        return SimpleNamespace(
            current_operation_id=client_operation_id
        )

    async def execute_custom_query(
        _client, query, variables=None
    ):
        calls.append((query, variables))
        return {
            "whoami": {
                "status": "success",
                "user_id": operator_id,
                "username": operator_username,
                "current_operation_id": observed_operation_id,
            },
            "task_aggregate": {"aggregate": {"count": 0}},
            "task": [],
        }

    return SimpleNamespace(
        login=login,
        mythic=SimpleNamespace(
            execute_custom_query=execute_custom_query
        ),
        calls=calls,
    )


def test_task_observer_filters_and_binds_exact_mythic_scope(monkeypatch):
    bootstrap = _scope_bootstrap()
    monkeypatch.setattr(
        reset,
        "_load_module",
        lambda _name, _path: bootstrap,
    )

    observed = asyncio.run(
        reset._observe_mythic_tasks_async(_config())
    )

    assert len(bootstrap.calls) == 1
    query, variables = bootstrap.calls[0]
    assert "task_aggregate(where: {operation_id: {_eq: $operationId}})" in query
    assert "where: {operation_id: {_eq: $operationId}}" in query
    assert variables == {"operationId": 7}
    assert observed["scope"] == "mythic-operation"
    assert observed["operation_id"] == 7
    assert observed["operator_id"] == 11
    assert observed["operator_username"] == "mythic_admin"
    assert observed["count"] == 0
    assert observed["max_task_id"] is None
    assert reset._aware_time(observed["observed_at"]) is not None


@pytest.mark.parametrize(
    ("bootstrap", "match"),
    [
        (
            _scope_bootstrap(client_operation_id=0),
            "no positive current operation",
        ),
        (
            _scope_bootstrap(observed_operation_id=8),
            "operation changed",
        ),
        (
            _scope_bootstrap(operator_id=True),
            "incomplete operator identity",
        ),
        (
            _scope_bootstrap(operator_username="another-user"),
            "does not match configured user",
        ),
    ],
    ids=(
        "missing-operation",
        "operation-drift",
        "boolean-operator-id",
        "operator-drift",
    ),
)
def test_task_observer_rejects_ambiguous_scope(
    monkeypatch, bootstrap, match
):
    monkeypatch.setattr(
        reset,
        "_load_module",
        lambda _name, _path: bootstrap,
    )

    with pytest.raises(reset.ResetError, match=match):
        asyncio.run(reset._observe_mythic_tasks_async(_config()))


@pytest.mark.parametrize(
    "mutator",
    [
        lambda checkpoint: checkpoint["phases"][0].__setitem__(
            "status", "unknown"
        ),
        lambda checkpoint: checkpoint["phases"].pop(3),
        lambda checkpoint: checkpoint["phases"].insert(
            3, copy.deepcopy(checkpoint["phases"][3])
        ),
        lambda checkpoint: checkpoint["phases"].__setitem__(
            slice(1, 3),
            [checkpoint["phases"][2], checkpoint["phases"][1]],
        ),
        lambda checkpoint: checkpoint["phases"].append(
            copy.deepcopy(checkpoint["phases"][-1])
        ),
    ],
    ids=(
        "unknown-status",
        "missing-phase",
        "duplicate-phase",
        "reordered-phases",
        "extra-phase",
    ),
)
def test_malformed_phase_layouts_fail_closed_before_runner(
    tmp_path, mutator
):
    checkpoint = reset._new_checkpoint(_config(), "reset-test")
    mutator(checkpoint)
    _assert_resume_rejected_without_runner(
        tmp_path,
        checkpoint,
        match="invalid reset checkpoint",
    )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda checkpoint: checkpoint.__setitem__(
            "terminal", {"state": "unknown", "reason": None}
        ),
        lambda checkpoint: checkpoint.__setitem__(
            "terminal", {"state": "complete", "reason": None}
        ),
        lambda checkpoint: checkpoint.__setitem__(
            "terminal", {"state": "blocked", "reason": None}
        ),
        lambda checkpoint: checkpoint.__setitem__(
            "interactive_boundary",
            {
                "required": True,
                "kind": "operator-launched-foothold-payload",
                "status": "unknown",
            },
        ),
        lambda checkpoint: checkpoint.__setitem__(
            "interactive_boundary",
            {
                "required": True,
                "kind": "operator-launched-foothold-payload",
                "status": "completed",
            },
        ),
    ],
    ids=(
        "unknown-terminal",
        "false-complete-terminal",
        "blocked-without-reason",
        "unknown-interactive-status",
        "interactive-progress-mismatch",
    ),
)
def test_terminal_and_interactive_shape_drift_fails_closed(
    tmp_path, mutator
):
    checkpoint = reset._new_checkpoint(_config(), "reset-test")
    mutator(checkpoint)
    _assert_resume_rejected_without_runner(
        tmp_path,
        checkpoint,
        match="invalid reset checkpoint",
    )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda phase: phase["result_summary"].__setitem__(
            "succeeded", 1
        ),
        lambda phase: phase["result_summary"].__setitem__(
            "phase", "another-phase"
        ),
        lambda phase: phase["result_summary"].__setitem__(
            "output", []
        ),
        lambda phase: phase["result_summary"].__setitem__(
            "output", {"ok": False}
        ),
        lambda phase: phase["result_summary"].__setitem__(
            "output", {"status": "error"}
        ),
        lambda phase: phase["result_summary"].__setitem__(
            "output", {"status": "FAILED"}
        ),
        lambda phase: phase["result_summary"].__setitem__(
            "output", {"error": "phase failed"}
        ),
        lambda phase: phase["result_summary"].__setitem__(
            "unexpected", True
        ),
    ],
    ids=(
        "boolean-like-success",
        "phase-mismatch",
        "nonobject-output",
        "negative-output",
        "error-status",
        "casefolded-failed-status",
        "truthy-error",
        "extra-envelope-field",
    ),
)
def test_completed_result_semantics_fail_closed_before_runner(
    tmp_path, mutator
):
    path = tmp_path / "valid.json"
    orchestrator = reset.ResetOrchestrator(
        path,
        phase_runner=_successful_phase,
        task_observer=_zero_tasks,
    )
    code, checkpoint = orchestrator.start(
        _config(), run_id="reset-test"
    )
    assert code == 0
    mutator(checkpoint["phases"][0])

    case_dir = tmp_path / "case"
    case_dir.mkdir()
    _assert_resume_rejected_without_runner(
        case_dir,
        checkpoint,
        match="invalid reset checkpoint",
    )


def test_task_observation_shape_drift_fails_closed_before_runner(tmp_path):
    base_path = tmp_path / "valid.json"
    orchestrator = reset.ResetOrchestrator(
        base_path,
        phase_runner=_successful_phase,
        task_observer=_zero_tasks,
    )
    code, checkpoint = orchestrator.start(_config(), run_id="reset-test")
    assert code == 0
    assert checkpoint["terminal"]["state"] == "awaiting_operator"

    variants = []
    missing_baseline = copy.deepcopy(checkpoint)
    missing_baseline["task_baseline"] = None
    variants.append(missing_baseline)
    bad_delta = copy.deepcopy(checkpoint)
    bad_delta["task_observations"][1]["delta_from_baseline"] = 1
    variants.append(bad_delta)
    duplicate_observation = copy.deepcopy(checkpoint)
    duplicate_observation["task_observations"].append(
        copy.deepcopy(duplicate_observation["task_observations"][-1])
    )
    variants.append(duplicate_observation)
    bool_count = copy.deepcopy(checkpoint)
    bool_count["task_observations"][0]["count"] = True
    variants.append(bool_count)
    polluted_baseline = copy.deepcopy(checkpoint)
    polluted_baseline["task_baseline"]["phase"] = "reset-mythic"
    variants.append(polluted_baseline)
    operation_drift = copy.deepcopy(checkpoint)
    operation_drift["task_observations"][1]["operation_id"] = 8
    variants.append(operation_drift)
    operator_drift = copy.deepcopy(checkpoint)
    operator_drift["task_observations"][1]["operator_id"] = 12
    variants.append(operator_drift)
    username_drift = copy.deepcopy(checkpoint)
    username_drift["task_observations"][1][
        "operator_username"
    ] = "another-user"
    variants.append(username_drift)
    scope_drift = copy.deepcopy(checkpoint)
    scope_drift["task_observations"][1]["scope"] = "visible-tasks"
    variants.append(scope_drift)
    naive_timestamp = copy.deepcopy(checkpoint)
    naive_timestamp["task_observations"][1][
        "observed_at"
    ] = "2026-07-23T00:00:03"
    variants.append(naive_timestamp)
    time_reversal = copy.deepcopy(checkpoint)
    time_reversal["task_observations"][1][
        "observed_at"
    ] = "2026-07-22T23:59:59+00:00"
    variants.append(time_reversal)

    for index, variant in enumerate(variants):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        _assert_resume_rejected_without_runner(
            case_dir,
            variant,
            match="invalid reset checkpoint",
        )
