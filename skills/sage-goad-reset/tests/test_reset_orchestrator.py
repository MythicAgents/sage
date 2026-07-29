from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reset_orchestrator.py"
SPEC = importlib.util.spec_from_file_location("reset_orchestrator", SCRIPT)
reset = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(reset)


def _config() -> dict:
    return {
        "snapshot": "exact-clean-snapshot",
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


def _successful_phase(
    name: str, command: list[str], _config: dict
) -> dict:
    assert command
    if name in reset.READY_RESULT_PHASES:
        return {"ready": True, "blockers": []}
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
    output = (
        {"ready": True, "blockers": []}
        if phase["name"] in reset.READY_RESULT_PHASES
        else {"ok": True}
    )
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
    assert "ready=true as a JSON boolean" in final["error"]
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
    assert checkpoint["phases"][-1]["result_summary"] == {
        "phase": "final-readiness",
        "succeeded": True,
        "output": {"ready": True, "blockers": []},
    }


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
