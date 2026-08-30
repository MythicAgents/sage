import subprocess
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
PY = ROOT / ".venv" / "bin" / "python"
SCRIPT = ROOT / "skills" / "sage-eval-gauge" / "scripts" / "orchestrate.py"
SPEC = importlib.util.spec_from_file_location("sage_eval_orchestrate", SCRIPT)
orchestrate = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(orchestrate)


def _stageb_protocol_argv(*extra):
    return [
        str(PY),
        str(SCRIPT),
        "--scenario",
        "child-control",
        "--gate1p-stage-b-protocol",
        "--snapshot",
        "sage-seed-apollo-v252-mythic4-20260829",
        *extra,
    ]


def test_gate1p_stageb_dry_run_emits_only_allocated_protocol_and_matrix_schedule():
    result = subprocess.run(
        _stageb_protocol_argv(),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout
    assert '"protocol_id": "gate1p-stage-b-child-control-completed-call-budget-v1"' in output
    assert '"matrix_id": "gate1p-stageb-child-control-matrix-v3"' in output
    assert '"pair_allocated": true' in output
    assert '"pair_id": null' in output
    assert '"allocation_id": "gate1p-stageb-child-control-allocation-003"' in output
    assert '"first_pair_gate": "gate1p-stageb-child-control-pair-015"' in output
    assert '"pair_id": "gate1p-stageb-child-control-pair-021"' in output
    assert output.count('"fresh_range_per_arm": true') == 7
    assert '"automatic_expansion": false' in output
    assert "ALLOCATED: protocol assembly only" in output
    assert "rollback " not in output
    assert "bootstrap_payloads.py" not in output
    assert "run --go" not in output
    assert "compare --scenario" not in output


@pytest.mark.parametrize(
    "live_authority",
    (None, "gate1p-stageb-child-control-allocation-001", "gate1p-stageb-child-control-allocation-002"),
)
def test_gate1p_stageb_go_requires_exact_authority_before_reset_or_execution_plan(live_authority):
    authority_args = [] if live_authority is None else ["--live-authority-id", live_authority]
    result = subprocess.run(
        _stageb_protocol_argv("--go", *authority_args),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "requires separate --live-authority-id" in result.stderr
    assert "rollback " not in result.stdout
    assert "bootstrap_payloads.py" not in result.stdout


def test_gate1p_stageb_successor_storage_ignores_terminal_matrix_artifacts(monkeypatch, tmp_path):
    stageb = orchestrate._gate1p_protocol_selector().protocol_for_scenario("child-control").module
    terminal_controls = []
    for matrix_id, pair_id in (
        ("gate1p-stageb-child-control-matrix-v1", "gate1p-stageb-child-control-pair-001"),
        ("gate1p-stageb-child-control-matrix-v2", "gate1p-stageb-child-control-pair-008"),
    ):
        terminal_dir = (
            tmp_path / ".sage_history" / "evaluation" / "harness-self-improvement"
            / "gate1p-stageb" / matrix_id / pair_id
        )
        terminal_dir.mkdir(parents=True)
        terminal_control = terminal_dir / "pair-control.json"
        terminal_control.write_text(f"terminal-{matrix_id}-control\n", encoding="utf-8")
        terminal_controls.append((terminal_control, matrix_id))
    monkeypatch.setattr(orchestrate, "ROOT", tmp_path)

    successor = orchestrate._stageb_pair_env(stageb, stageb.PRIMARY_PAIR_IDS[0], None)
    successor_dir = Path(successor["SAGE_EVAL_ARTIFACT_DIR"])

    assert stageb.STORAGE_NAMESPACE == "gate1p-stageb/gate1p-stageb-child-control-matrix-v3"
    assert successor_dir == (
        tmp_path / ".sage_history" / "evaluation" / "harness-self-improvement"
        / "gate1p-stageb" / "gate1p-stageb-child-control-matrix-v3"
        / "gate1p-stageb-child-control-pair-015"
    )
    assert not successor_dir.exists()
    for terminal_control, matrix_id in terminal_controls:
        assert terminal_control.read_text(encoding="utf-8") == f"terminal-{matrix_id}-control\n"


def test_gate1p_stageb_protocol_mode_is_mutually_exclusive_with_stagea_pair():
    result = subprocess.run(
        _stageb_protocol_argv("--gate1p-stage-a-pair"),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "modes are mutually exclusive" in result.stderr


def _stageb_matrix_fixture(monkeypatch, tmp_path, outcomes):
    stageb = orchestrate._gate1p_protocol_selector().protocol_for_scenario("child-control").module
    calls = []
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path)}
    monkeypatch.setattr(orchestrate, "validate_gate1p_storage", lambda *_args: None)

    def write_control(pair_env, pair_id, *, next_arm="shipping_sage"):
        now = 1.0
        completed = [] if next_arm == "shipping_sage" else (
            ["shipping_sage"] if next_arm == "true_naive" else list(stageb.ARMS)
        )
        prearm = None if next_arm == "complete" else {
            "arm": next_arm, "status": "idle", "reset_generation": 0, "updated_at": now,
        }
        path = orchestrate._gate1p_pair_control_path(pair_env)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": stageb.PAIR_CONTROL_SCHEMA, "protocol_id": stageb.PROTOCOL_ID,
            "pair_id": pair_id, "source_version": "tree:test", "next_arm": next_arm,
            "completed_arms": completed, "prearm_state": prearm,
            "created_at": now, "updated_at": now,
        }), encoding="utf-8")
        return path

    def initialize(*, route_env, pair_id, **_kwargs):
        return write_control(route_env, pair_id)

    def run_side(_scenario, side, **kwargs):
        pair_id, arm_index = kwargs["pair_id"], kwargs["arm_index"]
        assert kwargs["live_authority_id"] == stageb.ALLOCATION_ID
        calls.append((pair_id, side))
        pair_env = kwargs["route_env"]
        next_arm = "true_naive" if arm_index == 0 else "complete"
        write_control(pair_env, pair_id, next_arm=next_arm)
        results = orchestrate._gate1p_results_path(pair_env)
        with results.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"protocol": {"pair_id": pair_id}, "arm_index": arm_index}) + "\n")

    monkeypatch.setattr(orchestrate, "initialize_gate1p_pair_control", initialize)
    monkeypatch.setattr(orchestrate, "run_side_with_gate1p_cleanup", run_side)
    monkeypatch.setattr(
        orchestrate, "_gate1p_completed_arm_disposition",
        lambda _protocol, _records, *, pair_id, **_kwargs: {
            "countable": bool(outcomes[pair_id]), "reason": "fixture shipping disposition",
        },
    )
    monkeypatch.setattr(
        stageb, "pair_disposition",
        lambda _records, *, pair_id, **_kwargs: {
            "countable": bool(outcomes[pair_id]), "reason": "fixture pair disposition",
        },
    )
    monkeypatch.setattr(
        stageb, "matrix_disposition",
        lambda _records, **_kwargs: {"countable": True, "accepted": 5},
    )
    args = SimpleNamespace(
        source_version="tree:test", solve_timeout=stageb.WALL_SECONDS,
        provider=stageb.MODEL_PROVIDER, model=stageb.MODEL_ID, snapshot=stageb.RESET_SNAPSHOT,
        retained_callback_config=Path("retained.json"), ludus_range_id="range",
        ludus_mcp_server="server", model_context_limit=200_000, max_output_tokens=8192,
        live_authority_id=stageb.ALLOCATION_ID,
    )
    return stageb, args, route_env, calls, write_control


def test_stageb_matrix_coordinator_runs_five_pairs_in_exact_arm_order(monkeypatch, tmp_path):
    stageb = orchestrate._gate1p_protocol_selector().protocol_for_scenario("child-control").module
    outcomes = {pair_id: True for pair_id in stageb.PAIR_IDS}
    stageb, args, route_env, calls, _write = _stageb_matrix_fixture(
        monkeypatch, tmp_path, outcomes,
    )

    result = orchestrate._run_gate1p_stageb_matrix(
        args, stageb, policy_mode="llm", route_env=route_env, foothold=orchestrate.FootholdSpec(),
    )

    assert result == {"countable": True, "accepted": 5}
    assert calls == [
        (pair_id, side)
        for pair_id in stageb.PRIMARY_PAIR_IDS
        for side in ("harness", "bare")
    ]


@pytest.mark.parametrize(
    ("outcomes", "message", "expected_pairs"),
    [
        ([False], "first-pair gate failed", 1),
        ([True, False, False], "two consecutive failed pairs", 3),
    ],
)
def test_stageb_matrix_coordinator_enforces_first_pair_and_stop_loss(
    monkeypatch, tmp_path, outcomes, message, expected_pairs,
):
    stageb = orchestrate._gate1p_protocol_selector().protocol_for_scenario("child-control").module
    disposition = {
        pair_id: outcomes[index] if index < len(outcomes) else True
        for index, pair_id in enumerate(stageb.PAIR_IDS)
    }
    stageb, args, route_env, calls, _write = _stageb_matrix_fixture(
        monkeypatch, tmp_path, disposition,
    )

    with pytest.raises(SystemExit, match=message):
        orchestrate._run_gate1p_stageb_matrix(
            args, stageb, policy_mode="llm", route_env=route_env,
            foothold=orchestrate.FootholdSpec(),
        )

    assert {pair_id for pair_id, _side in calls} == set(stageb.PAIR_IDS[:expected_pairs])
    assert all(side == "harness" for pair_id, side in calls if not disposition[pair_id])


def test_stageb_matrix_coordinator_resumes_completed_pair_without_rerun(monkeypatch, tmp_path):
    stageb = orchestrate._gate1p_protocol_selector().protocol_for_scenario("child-control").module
    outcomes = {pair_id: True for pair_id in stageb.PAIR_IDS}
    stageb, args, route_env, calls, write_control = _stageb_matrix_fixture(
        monkeypatch, tmp_path, outcomes,
    )
    first_env = orchestrate._stageb_pair_env(stageb, stageb.PAIR_IDS[0], route_env)
    write_control(first_env, stageb.PAIR_IDS[0], next_arm="complete")
    orchestrate._gate1p_results_path(first_env).write_text(
        json.dumps({"protocol": {"pair_id": stageb.PAIR_IDS[0]}, "arm_index": 1}) + "\n",
        encoding="utf-8",
    )

    orchestrate._run_gate1p_stageb_matrix(
        args, stageb, policy_mode="llm", route_env=route_env, foothold=orchestrate.FootholdSpec(),
    )

    assert all(pair_id != stageb.PAIR_IDS[0] for pair_id, _side in calls)


def test_stageb_pair_control_rejects_cross_pair_resume(tmp_path):
    stageb = orchestrate._gate1p_protocol_selector().protocol_for_scenario("child-control").module
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path)}
    path = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test", route_env=route_env,
        protocol=stageb, pair_id=stageb.PAIR_IDS[0],
    )
    state = json.loads(path.read_text(encoding="utf-8"))

    assert state["schema"] == stageb.PAIR_CONTROL_SCHEMA
    with pytest.raises(SystemExit, match="identity or metadata is malformed"):
        orchestrate._validate_gate1p_pair_control_state(
            state, expected_source_version="tree:test",
            protocol=stageb, pair_id=stageb.PAIR_IDS[1],
        )


def test_dry_run_uses_staged_snapshot_retained_config_and_existing_apollo():
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "essos-da",
            "--side",
            "harness",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout
    assert "rollback sage-seed-apollo-staged-20260710 --yes" in output
    assert "--use-retained-callback" in output
    assert "apollo_callback_config.json" in output
    assert "launch_apollo_foothold.sh 10.4.10.22 NORTH\\samwell.tarly" in output
    assert "fresh-interactive-apollo" not in output


def test_dry_run_accepts_hybrid_policy_treatment():
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "essos-da",
            "--side",
            "harness",
            "--policy-mode",
            "hybrid",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "--policy-mode hybrid" in result.stdout
    assert "Sage policy mode -> hybrid" in result.stdout


def test_dry_run_accepts_alternate_foothold_spec():
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "direct-laps-objective",
            "--side",
            "harness",
            "--foothold-host",
            "MEEREEN",
            "--foothold-ip",
            "10.4.10.12",
            "--foothold-user",
            r"ESSOS\jorah.mormont",
            "--foothold-callback-user",
            "jorah.mormont",
            "--foothold-password-env",
            "SAGE_MEEREEN_JORAH_PASSWORD",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout
    assert "launch_apollo_foothold.sh 10.4.10.12 ESSOS\\jorah.mormont" in output
    assert "--target-host MEEREEN" in output
    assert "--callback-host MEEREEN" in output
    assert "--callback-user jorah.mormont" in output
    assert "--foothold-host MEEREEN" in output
    assert "--foothold-user-match jorah.mormont" in output
    assert "password source=SAGE_MEEREEN_JORAH_PASSWORD" in output


def test_dry_run_accepts_distinct_ludus_and_callback_hosts():
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "replication-purpose-range-visible-cost",
            "--side",
            "harness",
            "--foothold-host",
            "SAGEREPL-WS01",
            "--foothold-callback-host",
            "WS01",
            "--foothold-ip",
            "10.7.10.31",
            "--foothold-user",
            r"REPLICATION\user1",
            "--foothold-callback-user",
            "user1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout
    assert "--target-host SAGEREPL-WS01" in output
    assert "--callback-host WS01" in output
    assert "--foothold-host WS01" in output
    assert "--foothold-user-match user1" in output


def test_dry_run_carries_isolated_ludus_profile_through_reset_and_launch():
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "laps-family-transfer-ash-remote-exec",
            "--side",
            "harness",
            "--ludus-range-id",
            "SAGELAPSR520260715",
            "--ludus-mcp-server",
            "ludus_sagerepl",
            "--foothold-host",
            "CINDER-WS01",
            "--foothold-ip",
            "10.8.10.31",
            "--foothold-user",
            r"CINDER\user1",
            "--foothold-callback-user",
            "user1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout
    assert "--mcp-server ludus_sagerepl --range-id SAGELAPSR520260715 rollback" in output
    assert "--mcp-server ludus_sagerepl --range-id SAGELAPSR520260715 status" in output
    assert "--mcp-server ludus_sagerepl --range-id SAGELAPSR520260715 sync --yes" in output
    assert "# poll until every range guest reports an IP" in output
    assert "--mcp-server ludus_sagerepl --range-id SAGELAPSR520260715 check" in output
    assert "# poll until WinRM clock probes authenticate" in output
    assert "--ludus-range-id SAGELAPSR520260715 --ludus-mcp-server ludus_sagerepl" in output
    assert "--callback-settle-seconds 90 --require-unique-callback" in output


def test_phase7_dry_run_uses_isolated_range_and_settled_callback_gate():
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "trust-context-parent-dcsync",
            "--side",
            "harness",
            "--phase7-control",
            "positive",
            "--phase7-attempt-index",
            "1",
            "--ludus-range-id",
            "SAGETRUST20260715",
            "--ludus-mcp-server",
            "ludus_sagerepl",
            "--foothold-host",
            "ZETA-WS01",
            "--foothold-ip",
            "10.9.10.31",
            "--foothold-user",
            r"ZETA\user1",
            "--foothold-callback-user",
            "user1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout
    assert "--mcp-server ludus_sagerepl --range-id SAGETRUST20260715 rollback" in output
    assert "launch_apollo_foothold.sh 10.9.10.31 ZETA\\user1" in output
    assert "--target-host ZETA-WS01" in output
    assert "--callback-settle-seconds 90 --require-unique-callback" in output


def test_phase7_eval_env_and_netbios_map_are_frozen():
    env = orchestrate._phase7_trust_context_eval_env(
        "trust-context-parent-dcsync",
        control="positive",
        attempt_index=2,
    )

    assert env["SAGE_EVAL_PHASE7_MANIFEST_HASH"].startswith("sha256:")
    assert env["SAGE_EVAL_PHASE7_TOPOLOGY_HASH"].startswith("sha256:")
    assert env["SAGE_EVAL_PHASE7_CONTROL"] == "positive"
    assert env["SAGE_EVAL_PHASE7_ATTEMPT_INDEX"] == "2"
    assert orchestrate._engagement_netbios_map("trust-context-parent-dcsync") == (
        '{"ALPHA":"alpha.local","BRANCH":"branch.local","ZETA":"zeta.branch.local"}'
    )


def test_phase7_eval_env_rejects_missing_control():
    with pytest.raises(SystemExit):
        orchestrate._phase7_trust_context_eval_env(
            "trust-context-parent-dcsync",
            control=None,
            attempt_index=1,
        )


def test_phase8_eval_env_is_frozen_and_matches_configured_policy():
    env = orchestrate._phase8_goad_regression_eval_env(
        "cross-forest-objective",
        policy_mode="hybrid",
        policy_arm="hybrid",
        planned_row_id="phase8-hybrid-seed-1",
        attempt_index=1,
    )

    assert env["SAGE_EVAL_PHASE8_CONTRACT_HASH"].startswith("sha256:")
    assert env["SAGE_EVAL_PHASE8_POLICY_ARM"] == "hybrid"
    assert env["SAGE_EVAL_PHASE8_PLANNED_ROW_ID"] == "phase8-hybrid-seed-1"
    assert env["SAGE_EVAL_PHASE8_ATTEMPT_INDEX"] == "1"


def test_phase8_eval_env_rejects_policy_arm_mismatch():
    with pytest.raises(SystemExit):
        orchestrate._phase8_goad_regression_eval_env(
            "cross-forest-objective",
            policy_mode="symbolic",
            policy_arm="hybrid",
            planned_row_id="phase8-hybrid-seed-1",
            attempt_index=1,
        )


def test_range_guest_ip_gate_requires_every_reported_vm_on_with_ip():
    assert orchestrate._range_guests_have_ips(
        "  ON  range-router  ip=10.11.10.254  pmx=1\n"
        "  ON  range-dc01    ip=10.11.10.10   pmx=2\n"
        "  ON  range-ws01    ip=10.11.10.31   pmx=3\n"
    ) is True
    assert orchestrate._range_guests_have_ips(
        "  ON  range-router  ip=10.11.10.254  pmx=1\n"
        "  ON  range-dc01    ip=null          pmx=2\n"
    ) is False
    assert orchestrate._range_guests_have_ips(
        "  ON   range-router  ip=10.11.10.254  pmx=1\n"
        "  off  range-dc01    ip=null          pmx=2\n"
    ) is False


def test_range_clock_probe_gate_ignores_expected_skew_but_requires_no_errors():
    assert orchestrate._range_clock_probes_reachable(
        '{"ready":false,"hosts":[{"inventory_hostname":"range-dc01"}],"errors":[],"over_limit":["range-dc01"]}'
    ) is True
    assert orchestrate._range_clock_probes_reachable(
        '{"ready":false,"hosts":[],"errors":[{"inventory_hostname":"range-dc01","error":"401"}]}'
    ) is False
    assert orchestrate._range_clock_probes_reachable("not-json") is False


def test_poll_fails_closed_on_nonzero_status_before_accepting_stdout(monkeypatch):
    monkeypatch.setattr(
        orchestrate.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="available-domains: 200 count=0", stderr="API error"
        ),
    )
    with pytest.raises(SystemExit, match="command failed"):
        orchestrate._poll(
            "BloodHound authoritative domain count is zero",
            ["status"],
            ROOT,
            orchestrate._bloodhound_is_empty,
            timeout=1,
            interval=0,
        )


def test_poll_can_evaluate_declared_status_one_for_reachable_skew(monkeypatch):
    payload = (
        '{"ready":false,"hosts":[{"inventory_hostname":"range-dc01"}],'
        '"errors":[],"over_limit":["range-dc01"]}'
    )
    monkeypatch.setattr(
        orchestrate.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=payload, stderr=""
        ),
    )

    observed = orchestrate._poll(
        "range WinRM clock probes respond",
        ["check"],
        ROOT,
        orchestrate._range_clock_probes_reachable,
        timeout=1,
        interval=0,
        predicate_returncodes=(0, 1),
    )

    assert observed == payload


def test_poll_rejects_probe_errors_even_when_status_one_is_declared(monkeypatch):
    payload = (
        '{"ready":false,"hosts":[],"errors":['
        '{"inventory_hostname":"range-dc01","error":"401"}]}'
    )
    monotonic = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(orchestrate.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(orchestrate.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        orchestrate.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=payload, stderr=""
        ),
    )

    with pytest.raises(SystemExit, match="not ready"):
        orchestrate._poll(
            "range WinRM clock probes respond",
            ["check"],
            ROOT,
            orchestrate._range_clock_probes_reachable,
            timeout=1,
            interval=0,
            predicate_returncodes=(0, 1),
        )


def test_poll_can_wait_on_status_one_until_typed_readiness_is_true(monkeypatch):
    results = iter(
        (
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout='{"ready":false}', stderr=""
            ),
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout='{"ready":true}', stderr=""
            ),
        )
    )
    monkeypatch.setattr(orchestrate.subprocess, "run", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(orchestrate.time, "sleep", lambda _seconds: None)

    observed = orchestrate._poll(
        "sage chat + apollo ready",
        ["readiness"],
        ROOT,
        orchestrate._readiness_ok,
        timeout=1,
        interval=0,
        predicate_returncodes=(0, 1),
    )

    assert observed == '{"ready":true}'


def test_poll_still_aborts_on_undeclared_status_with_clock_exception(monkeypatch):
    monkeypatch.setattr(
        orchestrate.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=2, stdout="not-json", stderr="usage error"
        ),
    )

    with pytest.raises(SystemExit, match=r"failed \(exit 2\)"):
        orchestrate._poll(
            "range WinRM clock probes respond",
            ["check"],
            ROOT,
            orchestrate._range_clock_probes_reachable,
            timeout=1,
            interval=0,
            predicate_returncodes=(0, 1),
        )


def test_callback_discovery_requires_exact_identity_and_no_other_apollo(monkeypatch):
    def result(stdout):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(
        orchestrate.subprocess,
        "run",
        lambda *_args, **_kwargs: result(
            "id=7 payloadtype=apollo host=CASTELBLACK user=evil.samwell.tarly active=True\n"
        ),
    )
    with pytest.raises(SystemExit, match="sole active Apollo callback"):
        orchestrate.discover_callbacks()

    monkeypatch.setattr(
        orchestrate.subprocess,
        "run",
        lambda *_args, **_kwargs: result(
            "id=7 payloadtype=apollo host=CASTELBLACK user=NORTH\\samwell.tarly active=True\n"
        ),
    )
    assert orchestrate.discover_callbacks() == 7

    monkeypatch.setattr(
        orchestrate.subprocess,
        "run",
        lambda *_args, **_kwargs: result(
            "id=7 payloadtype=apollo host=CASTELBLACK user=NORTH\\samwell.tarly active=True\n"
            "id=8 payloadtype=apollo host=BRAAVOS user=ESSOS\\jorah.mormont active=True\n"
        ),
    )
    with pytest.raises(SystemExit, match="apollo=2 exact=1"):
        orchestrate.discover_callbacks()


def test_gate1p_cleanup_runs_every_step_and_retains_failure_report(monkeypatch, tmp_path):
    events = []

    def fake_run(name, *_args, **_kwargs):
        events.append(name)
        if name.endswith("verify no running tasks"):
            raise SystemExit("running task 9")

    results = tmp_path / "pair.jsonl"
    results.write_text(
        json.dumps({
            "record_type": "result",
            "protocol": {
                "pair_id": orchestrate._gate1p_stagea_protocol().PAIR_ID,
                "arm": "shipping_sage",
            },
        }) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "shipping_sage-episode.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(orchestrate, "_run", fake_run)

    with pytest.raises(SystemExit, match="cleanup incomplete"):
        orchestrate.cleanup_gate1p_arm(
            arm="shipping_sage",
            pair_id=orchestrate._gate1p_stagea_protocol().PAIR_ID,
            ludus_range_id="RANGE-1",
            ludus_mcp_server="ludus_test",
            route_env={
                "SAGE_EVAL_RESULTS_PATH": str(results),
                "SAGE_EVAL_ARTIFACT_DIR": str(tmp_path),
            },
        )

    assert events == [
        "Gate 1P cleanup: stop sage",
        "Gate 1P cleanup: archive dbs",
        "Gate 1P cleanup: verify no running tasks",
        "Gate 1P cleanup: ludus poweroff",
    ]
    report = json.loads(results.read_text(encoding="utf-8").splitlines()[-1])
    assert report["record_type"] == "cleanup"
    assert report["complete"] is False
    assert report["result_retained"] is True


def test_gate1p_wrapper_cleans_up_when_arm_raises(monkeypatch):
    seen = []
    monkeypatch.setattr(
        orchestrate,
        "_validate_gate1p_wrapper_entry",
        lambda **_kwargs: "tree:test",
    )
    monkeypatch.setattr(orchestrate, "_run_side_preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrate, "mark_gate1p_prearm_reset_started", lambda **_kwargs: None)

    def fail_after_reset(*_args, **kwargs):
        kwargs["before_reset"]()
        raise RuntimeError("arm failed")

    monkeypatch.setattr(
        orchestrate,
        "run_side",
        fail_after_reset,
    )
    monkeypatch.setattr(
        orchestrate,
        "cleanup_gate1p_arm",
        lambda **kwargs: seen.append(kwargs),
    )
    with pytest.raises(RuntimeError, match="arm failed"):
        orchestrate.run_side_with_gate1p_cleanup(
            "child-domain-control",
            "harness",
            measurement_kind="row",
            pair_id=orchestrate._gate1p_stagea_protocol().PAIR_ID,
            arm_index=0,
            source_version="tree:test",
            ludus_range_id="RANGE-1",
            ludus_mcp_server="ludus_test",
            route_env={"SAGE_EVAL_RESULTS_PATH": "/tmp/not-used-by-mock"},
        )

    assert seen[0]["arm"] == "shipping_sage"


def test_gate1p_started_arm_failure_persists_cleanup_and_cannot_retry(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    results = orchestrate._gate1p_results_path(route_env)
    monkeypatch.setattr(orchestrate, "_run_side_preflight", lambda *_args, **_kwargs: None)

    def fail_after_started_result(*_args, **kwargs):
        kwargs["before_reset"]()
        results.parent.mkdir(parents=True, exist_ok=True)
        results.write_text('{"record_type":"result"}\n', encoding="utf-8")
        raise RuntimeError("retained arm is not admissible")

    monkeypatch.setattr(orchestrate, "run_side", fail_after_started_result)
    monkeypatch.setattr(orchestrate, "_gate1p_arm_started", lambda **_kwargs: True)
    monkeypatch.setattr(orchestrate, "cleanup_gate1p_arm", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="not admissible"):
        orchestrate.run_side_with_gate1p_cleanup(
            "child-domain-control",
            "harness",
            measurement_kind="row",
            pair_id=protocol.PAIR_ID,
            arm_index=0,
            source_version="tree:test",
            ludus_range_id="RANGE-1",
            ludus_mcp_server="ludus_test",
            route_env=route_env,
        )

    state = json.loads(control.read_text(encoding="utf-8"))
    assert state["prearm_state"]["status"] == "cleanup_complete"
    with pytest.raises(SystemExit, match="shipping resume requires an empty"):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )


def test_gate1p_pair_control_is_one_shot_and_advances_only_in_order(tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    path = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["next_arm"] == "shipping_sage"
    assert state["completed_arms"] == []

    assert orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    ) == path

    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    orchestrate.advance_gate1p_pair_control(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["next_arm"] == "true_naive"
    assert state["completed_arms"] == ["shipping_sage"]
    with pytest.raises(SystemExit, match="out of order"):
        orchestrate.advance_gate1p_pair_control(
            arm="shipping_sage", source_version="tree:test", route_env=route_env
        )


def _canonical_gate1p_pair_control():
    protocol = orchestrate._gate1p_stagea_protocol()
    return {
        "schema": "gate1p-stagea-pair-control-v3",
        "protocol_id": protocol.PROTOCOL_ID,
        "pair_id": protocol.PAIR_ID,
        "source_version": "tree:test",
        "next_arm": "shipping_sage",
        "completed_arms": [],
        "prearm_state": {
            "arm": "shipping_sage",
            "status": "idle",
            "reset_generation": 0,
            "updated_at": 1.0,
        },
        "created_at": 1.0,
        "updated_at": 1.0,
    }


@pytest.mark.parametrize("source_version", [None, "", " unrecorded", "unrecorded", 16])
def test_gate1p_wrapper_rejects_absent_or_malformed_source_before_any_activity(
    monkeypatch,
    tmp_path,
    source_version,
):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:expected",
        route_env=route_env,
    )
    original = control.read_bytes()
    events = []
    monkeypatch.setattr(
        orchestrate,
        "_run_side_preflight",
        lambda *_args, **_kwargs: events.append("provider_preflight"),
    )
    monkeypatch.setattr(
        orchestrate,
        "run_side",
        lambda *_args, **_kwargs: events.append("full_reset_and_ready"),
    )
    monkeypatch.setattr(
        orchestrate,
        "cleanup_gate1p_arm",
        lambda **_kwargs: events.append("cleanup"),
    )

    with pytest.raises(SystemExit, match="independently expected"):
        orchestrate.run_side_with_gate1p_cleanup(
            "child-domain-control",
            "harness",
            measurement_kind="row",
            pair_id=protocol.PAIR_ID,
            arm_index=0,
            source_version=source_version,
            route_env=route_env,
        )

    assert events == []
    assert control.read_bytes() == original
    assert not orchestrate._gate1p_results_path(route_env).exists()
    assert not orchestrate._gate1p_prestart_cleanup_path(route_env).exists()


def test_gate1p_wrapper_rejects_whole_source_drift_before_provider_or_reset(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:expected",
        route_env=route_env,
    )
    state = json.loads(control.read_text(encoding="utf-8"))
    state["source_version"] = "tree:drifted"
    control.write_text(json.dumps(state), encoding="utf-8")
    drifted = control.read_bytes()
    events = []
    monkeypatch.setattr(
        orchestrate,
        "_run_side_preflight",
        lambda *_args, **_kwargs: events.append("provider_preflight"),
    )
    monkeypatch.setattr(
        orchestrate,
        "run_side",
        lambda *_args, **_kwargs: events.extend(
            ["full_reset_and_ready", "arm_started", "target_activity"]
        ),
    )

    with pytest.raises(SystemExit, match="identity or metadata is malformed"):
        orchestrate.run_side_with_gate1p_cleanup(
            "child-domain-control",
            "harness",
            measurement_kind="row",
            pair_id=protocol.PAIR_ID,
            arm_index=0,
            source_version="tree:expected",
            route_env=route_env,
        )

    assert events == []
    assert control.read_bytes() == drifted
    assert json.loads(drifted)["prearm_state"]["status"] == "idle"


def test_gate1p_source_drift_after_preflight_cannot_start_reset_or_activity(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:expected",
        route_env=route_env,
    )
    events = []
    monkeypatch.setattr(
        orchestrate,
        "_run_side_preflight",
        lambda *_args, **_kwargs: events.append("provider_preflight"),
    )

    def drift_before_reset(*_args, **kwargs):
        events.append("run_side_entered")
        state = json.loads(control.read_text(encoding="utf-8"))
        state["source_version"] = "tree:drifted"
        control.write_text(json.dumps(state), encoding="utf-8")
        kwargs["before_reset"]()
        events.extend(["full_reset_and_ready", "arm_started", "target_activity"])

    monkeypatch.setattr(orchestrate, "run_side", drift_before_reset)
    with pytest.raises(SystemExit, match="identity or metadata is malformed"):
        orchestrate.run_side_with_gate1p_cleanup(
            "child-domain-control",
            "harness",
            measurement_kind="row",
            pair_id=protocol.PAIR_ID,
            arm_index=0,
            source_version="tree:expected",
            route_env=route_env,
        )

    assert events == ["provider_preflight", "run_side_entered"]
    assert json.loads(control.read_text(encoding="utf-8"))["prearm_state"]["status"] == "idle"
    assert not orchestrate._gate1p_results_path(route_env).exists()


@pytest.mark.parametrize("transition", ["reset_started", "cleanup_complete", "advance"])
def test_gate1p_every_parent_transition_rejects_independent_source_drift(tmp_path, transition):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:expected",
        route_env=route_env,
    )
    if transition in {"cleanup_complete", "advance"}:
        orchestrate.mark_gate1p_prearm_reset_started(
            arm="shipping_sage",
            source_version="tree:expected",
            route_env=route_env,
        )
    state = json.loads(control.read_text(encoding="utf-8"))
    state["source_version"] = "tree:drifted"
    control.write_text(json.dumps(state), encoding="utf-8")
    drifted = control.read_bytes()

    with pytest.raises(SystemExit, match="identity or metadata is malformed"):
        if transition == "reset_started":
            orchestrate.mark_gate1p_prearm_reset_started(
                arm="shipping_sage",
                source_version="tree:expected",
                route_env=route_env,
            )
        elif transition == "cleanup_complete":
            orchestrate.mark_gate1p_prestart_cleanup_complete(
                arm="shipping_sage",
                source_version="tree:expected",
                route_env=route_env,
            )
        else:
            orchestrate.advance_gate1p_pair_control(
                arm="shipping_sage",
                source_version="tree:expected",
                route_env=route_env,
            )

    assert control.read_bytes() == drifted


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_top_level",
        "missing_top_level",
        "created_at_object",
        "created_at_bool",
        "created_at_nan",
        "created_at_pos_inf",
        "created_at_neg_inf",
        "extra_prearm_field",
        "missing_prearm_field",
        "illegal_arm_combination",
    ],
)
def test_gate1p_pair_control_rejects_noncanonical_json_and_illegal_states(mutation):
    state = _canonical_gate1p_pair_control()
    if mutation == "extra_top_level":
        state["undeclared"] = None
    elif mutation == "missing_top_level":
        del state["updated_at"]
    elif mutation == "created_at_object":
        state["created_at"] = {}
    elif mutation == "created_at_bool":
        state["created_at"] = True
    elif mutation == "created_at_nan":
        state["created_at"] = float("nan")
    elif mutation == "created_at_pos_inf":
        state["created_at"] = float("inf")
    elif mutation == "created_at_neg_inf":
        state["created_at"] = float("-inf")
    elif mutation == "extra_prearm_field":
        state["prearm_state"]["undeclared"] = None
    elif mutation == "missing_prearm_field":
        del state["prearm_state"]["updated_at"]
    elif mutation == "illegal_arm_combination":
        state["next_arm"] = "true_naive"
        state["prearm_state"]["arm"] = "true_naive"

    with pytest.raises(SystemExit):
        orchestrate._validate_gate1p_pair_control_state(
            state,
            expected_source_version="tree:test",
        )


def test_gate1p_pair_control_update_is_failure_atomic(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    path = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    original = path.read_bytes()
    real_replace = orchestrate.os.replace
    monkeypatch.setattr(
        orchestrate.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("planted torn write")),
    )

    with pytest.raises(OSError, match="planted torn write"):
        orchestrate.mark_gate1p_prearm_reset_started(
            arm="shipping_sage",
            source_version="tree:test",
            route_env=route_env,
        )

    assert path.read_bytes() == original
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))

    monkeypatch.setattr(orchestrate.os, "replace", real_replace)
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage",
        source_version="tree:test",
        route_env=route_env,
    )
    assert json.loads(path.read_text(encoding="utf-8"))["prearm_state"]["status"] == "reset_started"


def test_gate1p_pair_control_create_only_never_overwrites_or_exposes_partial_bytes(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "pair-control.json"
    path.write_text("existing\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        orchestrate._atomic_write_gate1p_json(path, {"replacement": True}, create_only=True)
    assert path.read_text(encoding="utf-8") == "existing\n"
    assert not list(tmp_path.glob(f".{path.name}.*.tmp"))

    path.unlink()
    monkeypatch.setattr(
        orchestrate.os,
        "link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("planted link failure")),
    )
    with pytest.raises(OSError, match="planted link failure"):
        orchestrate._atomic_write_gate1p_json(path, {"new": True}, create_only=True)
    assert not path.exists()
    assert not list(tmp_path.glob(f".{path.name}.*.tmp"))


def test_gate1p_prestart_failure_cleans_without_consuming_retry_zero(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    def fail_after_reset(*_args, **kwargs):
        kwargs["before_reset"]()
        raise RuntimeError("prestart failure")

    monkeypatch.setattr(orchestrate, "run_side", fail_after_reset)
    monkeypatch.setattr(orchestrate, "_run", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="prestart failure"):
        orchestrate.run_side_with_gate1p_cleanup(
            "child-domain-control",
            "harness",
            measurement_kind="row",
            pair_id=protocol.PAIR_ID,
            arm_index=0,
            source_version="tree:test",
            ludus_range_id="RANGE-1",
            ludus_mcp_server="ludus_test",
            route_env=route_env,
        )

    assert not orchestrate._gate1p_results_path(route_env).exists()
    records = [
        json.loads(line)
        for line in orchestrate._gate1p_prestart_cleanup_path(route_env).read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [record["record_type"] for record in records] == ["prestart_cleanup"]
    assert records[0]["measurement_kind"] == "prestart_readiness"
    assert records[0]["complete"] is True
    assert orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    ) == control


def test_gate1p_incomplete_prestart_cleanup_blocks_resume_and_does_not_pollute_results(
    monkeypatch,
    tmp_path,
):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    orchestrate.initialize_gate1p_pair_control(source_version="tree:test", route_env=route_env)
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )

    def fail_verify(name, *_args, **_kwargs):
        if name.endswith("verify no running tasks"):
            raise SystemExit("not ready")

    monkeypatch.setattr(orchestrate, "_run", fail_verify)
    with pytest.raises(SystemExit, match="cleanup incomplete"):
        orchestrate.cleanup_gate1p_arm(
            arm="shipping_sage",
            pair_id=protocol.PAIR_ID,
            ludus_range_id="RANGE-1",
            ludus_mcp_server="ludus_test",
            route_env=route_env,
            row_started=False,
            source_version="tree:test",
        )

    assert not orchestrate._gate1p_results_path(route_env).exists()
    with pytest.raises(SystemExit, match="incomplete or mismatched"):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )


def test_gate1p_prestart_record_planted_in_countable_ledger_blocks_resume(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    orchestrate.initialize_gate1p_pair_control(source_version="tree:test", route_env=route_env)
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    monkeypatch.setattr(orchestrate, "_run", lambda *_args, **_kwargs: None)
    orchestrate.cleanup_gate1p_arm(
        arm="shipping_sage",
        pair_id=protocol.PAIR_ID,
        ludus_range_id="RANGE-1",
        ludus_mcp_server="ludus_test",
        route_env=route_env,
        row_started=False,
        source_version="tree:test",
    )
    planted = orchestrate._gate1p_prestart_cleanup_path(route_env).read_text(encoding="utf-8")
    orchestrate._gate1p_results_path(route_env).write_text(planted, encoding="utf-8")

    with pytest.raises(SystemExit, match="empty countable result ledger"):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )


def test_gate1p_naive_prestart_resume_requires_countable_shipping(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    orchestrate._gate1p_results_path(route_env).write_text("{}\n", encoding="utf-8")
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    orchestrate.advance_gate1p_pair_control(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="true_naive", source_version="tree:test", route_env=route_env
    )
    monkeypatch.setattr(orchestrate, "_run", lambda *_args, **_kwargs: None)
    orchestrate.cleanup_gate1p_arm(
        arm="true_naive",
        pair_id=protocol.PAIR_ID,
        ludus_range_id="RANGE-1",
        ludus_mcp_server="ludus_test",
        route_env=route_env,
        row_started=False,
        source_version="tree:test",
    )

    with pytest.raises(SystemExit, match="naive resume blocked by shipping admission"):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )

    monkeypatch.setattr(
        protocol,
        "completed_arm_lifecycle_disposition",
        lambda *_args, **_kwargs: {"countable": True},
    )
    assert orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    ) == control


def test_gate1p_duplicate_prestart_recovery_for_one_arm_fails_closed(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    orchestrate.initialize_gate1p_pair_control(source_version="tree:test", route_env=route_env)
    monkeypatch.setattr(orchestrate, "_run", lambda *_args, **_kwargs: None)
    for _ in range(2):
        orchestrate.mark_gate1p_prearm_reset_started(
            arm="shipping_sage", source_version="tree:test", route_env=route_env
        )
        orchestrate.cleanup_gate1p_arm(
            arm="shipping_sage",
            pair_id=protocol.PAIR_ID,
            ludus_range_id="RANGE-1",
            ludus_mcp_server="ludus_test",
            route_env=route_env,
            row_started=False,
            source_version="tree:test",
        )

    with pytest.raises(SystemExit, match="already consumed an arm recovery"):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )


def test_gate1p_stale_cleanup_cannot_recover_a_newer_reset_generation(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    monkeypatch.setattr(orchestrate, "_run", lambda *_args, **_kwargs: None)

    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    orchestrate.cleanup_gate1p_arm(
        arm="shipping_sage",
        pair_id=protocol.PAIR_ID,
        ludus_range_id="RANGE-1",
        ludus_mcp_server="ludus_test",
        route_env=route_env,
        row_started=False,
        source_version="tree:test",
    )
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )

    with pytest.raises(SystemExit, match="reset generation"):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )

    state = json.loads(control.read_text(encoding="utf-8"))
    assert state["prearm_state"]["status"] == "reset_started"
    with pytest.raises(SystemExit, match="out of order"):
        orchestrate.mark_gate1p_prearm_reset_started(
            arm="shipping_sage", source_version="tree:test", route_env=route_env
        )


def test_gate1p_prestart_cleanup_binds_the_exact_reset_generation(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    state = json.loads(control.read_text(encoding="utf-8"))
    assert state["prearm_state"]["status"] == "idle"
    assert state["prearm_state"]["reset_generation"] == 0

    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    state = json.loads(control.read_text(encoding="utf-8"))
    assert state["prearm_state"]["status"] == "reset_started"
    assert state["prearm_state"]["reset_generation"] == 1
    monkeypatch.setattr(orchestrate, "_run", lambda *_args, **_kwargs: None)
    report = orchestrate.cleanup_gate1p_arm(
        arm="shipping_sage",
        pair_id=protocol.PAIR_ID,
        ludus_range_id="RANGE-1",
        ludus_mcp_server="ludus_test",
        route_env=route_env,
        row_started=False,
        source_version="tree:test",
    )
    assert report["reset_generation"] == 1
    state = json.loads(control.read_text(encoding="utf-8"))
    assert state["prearm_state"]["arm"] == "shipping_sage"
    assert state["prearm_state"]["status"] == "cleanup_complete"
    assert state["prearm_state"]["reset_generation"] == 1

    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    state = json.loads(control.read_text(encoding="utf-8"))
    assert state["prearm_state"]["status"] == "reset_started"
    assert state["prearm_state"]["reset_generation"] == 2


@pytest.mark.parametrize("generation", [True, -1, 1.5])
def test_gate1p_pair_control_rejects_malformed_reset_generation(tmp_path, generation):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    state = json.loads(control.read_text(encoding="utf-8"))
    state["prearm_state"]["reset_generation"] = generation
    control.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(SystemExit, match="malformed or illegal"):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )


@pytest.mark.parametrize("generation", [True, 0, -1, 1.5, 2])
def test_gate1p_prestart_cleanup_rejects_malformed_or_mismatched_generation(
    monkeypatch,
    tmp_path,
    generation,
):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    orchestrate.initialize_gate1p_pair_control(source_version="tree:test", route_env=route_env)
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    monkeypatch.setattr(orchestrate, "_run", lambda *_args, **_kwargs: None)
    orchestrate.cleanup_gate1p_arm(
        arm="shipping_sage",
        pair_id=protocol.PAIR_ID,
        ludus_range_id="RANGE-1",
        ludus_mcp_server="ludus_test",
        route_env=route_env,
        row_started=False,
        source_version="tree:test",
    )
    cleanup_path = orchestrate._gate1p_prestart_cleanup_path(route_env)
    record = json.loads(cleanup_path.read_text(encoding="utf-8"))
    record["reset_generation"] = generation
    cleanup_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )


@pytest.mark.parametrize("timestamp", [True, float("nan"), float("inf"), float("-inf")])
def test_gate1p_prestart_cleanup_rejects_nonfinite_or_boolean_timestamp(
    monkeypatch,
    tmp_path,
    timestamp,
):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    orchestrate.initialize_gate1p_pair_control(source_version="tree:test", route_env=route_env)
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    monkeypatch.setattr(orchestrate, "_run", lambda *_args, **_kwargs: None)
    orchestrate.cleanup_gate1p_arm(
        arm="shipping_sage",
        pair_id=protocol.PAIR_ID,
        ludus_range_id="RANGE-1",
        ludus_mcp_server="ludus_test",
        route_env=route_env,
        row_started=False,
        source_version="tree:test",
    )
    cleanup_path = orchestrate._gate1p_prestart_cleanup_path(route_env)
    record = json.loads(cleanup_path.read_text(encoding="utf-8"))
    record["ts"] = timestamp
    cleanup_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="incomplete or mismatched"):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )


def test_gate1p_preflight_failure_is_unspent_and_does_not_enter_cleanup(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    monkeypatch.setattr(
        orchestrate,
        "_run_side_preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("auth failed")),
    )
    monkeypatch.setattr(
        orchestrate,
        "run_side",
        lambda *_args, **_kwargs: pytest.fail("run/reset path must not start"),
    )
    monkeypatch.setattr(
        orchestrate,
        "cleanup_gate1p_arm",
        lambda **_kwargs: pytest.fail("cleanup must not run before reset start"),
    )

    with pytest.raises(SystemExit, match="auth failed"):
        orchestrate.run_side_with_gate1p_cleanup(
            "child-domain-control",
            "harness",
            measurement_kind="row",
            pair_id=orchestrate._gate1p_stagea_protocol().PAIR_ID,
            arm_index=0,
            source_version="tree:test",
            ludus_range_id="RANGE-1",
            ludus_mcp_server="ludus_test",
            route_env=route_env,
        )

    state = json.loads(control.read_text(encoding="utf-8"))
    assert state["prearm_state"]["status"] == "idle"
    assert not orchestrate._gate1p_results_path(route_env).exists()
    assert not orchestrate._gate1p_prestart_cleanup_path(route_env).exists()


def test_gate1p_valid_idle_naive_resume_needs_no_fictional_cleanup(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    orchestrate._gate1p_results_path(route_env).write_text("{}\n", encoding="utf-8")
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    orchestrate.advance_gate1p_pair_control(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    monkeypatch.setattr(
        protocol,
        "completed_arm_lifecycle_disposition",
        lambda *_args, **_kwargs: {"countable": True},
    )

    assert orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    ) == control
    state = json.loads(control.read_text(encoding="utf-8"))
    assert state["next_arm"] == "true_naive"
    assert state["prearm_state"]["status"] == "idle"
    assert not orchestrate._gate1p_prestart_cleanup_path(route_env).exists()


def test_gate1p_reset_started_without_cleanup_evidence_blocks_resume(tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    orchestrate.initialize_gate1p_pair_control(source_version="tree:test", route_env=route_env)
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )

    with pytest.raises(SystemExit, match="does not match the current pre-arm state"):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )


def test_gate1p_cleanup_command_oserror_runs_remaining_steps_and_blocks_resume(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    orchestrate.initialize_gate1p_pair_control(source_version="tree:test", route_env=route_env)
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage", source_version="tree:test", route_env=route_env
    )
    events = []

    def fake_run(name, *_args, **_kwargs):
        events.append(name)
        if name.endswith("stop sage"):
            raise FileNotFoundError("missing launcher")

    monkeypatch.setattr(orchestrate, "_run", fake_run)
    with pytest.raises(SystemExit, match="cleanup incomplete"):
        orchestrate.cleanup_gate1p_arm(
            arm="shipping_sage",
            pair_id=protocol.PAIR_ID,
            ludus_range_id="RANGE-1",
            ludus_mcp_server="ludus_test",
            route_env=route_env,
            row_started=False,
            source_version="tree:test",
        )

    assert events == [
        "Gate 1P cleanup: stop sage",
        "Gate 1P cleanup: archive dbs",
        "Gate 1P cleanup: verify no running tasks",
        "Gate 1P cleanup: ludus poweroff",
    ]
    report = json.loads(
        orchestrate._gate1p_prestart_cleanup_path(route_env).read_text(encoding="utf-8")
    )
    assert report["complete"] is False
    with pytest.raises(SystemExit):
        orchestrate.initialize_gate1p_pair_control(
            source_version="tree:test",
            route_env=route_env,
        )


def test_gate1p_malformed_arm_ledger_still_runs_all_cleanup_steps(monkeypatch, tmp_path):
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(tmp_path / ".sage_history" / "pair")}
    protocol = orchestrate._gate1p_stagea_protocol()
    orchestrate.initialize_gate1p_pair_control(source_version="tree:test", route_env=route_env)
    results = orchestrate._gate1p_results_path(route_env)
    events = []
    monkeypatch.setattr(orchestrate, "_run_side_preflight", lambda *_args, **_kwargs: None)

    def fail_with_malformed_ledger(*_args, **kwargs):
        kwargs["before_reset"]()
        results.write_text("{malformed\n", encoding="utf-8")
        raise RuntimeError("arm failed")

    def fake_run(name, *_args, **_kwargs):
        events.append(name)

    monkeypatch.setattr(orchestrate, "run_side", fail_with_malformed_ledger)
    monkeypatch.setattr(orchestrate, "_run", fake_run)
    with pytest.raises(SystemExit, match="cleanup incomplete"):
        orchestrate.run_side_with_gate1p_cleanup(
            "child-domain-control",
            "harness",
            measurement_kind="row",
            pair_id=protocol.PAIR_ID,
            arm_index=0,
            source_version="tree:test",
            ludus_range_id="RANGE-1",
            ludus_mcp_server="ludus_test",
            route_env=route_env,
        )

    assert events == [
        "Gate 1P cleanup: stop sage",
        "Gate 1P cleanup: archive dbs",
        "Gate 1P cleanup: verify no running tasks",
        "Gate 1P cleanup: ludus poweroff",
    ]


def test_gate1p_main_resumes_only_the_unstarted_naive_arm(monkeypatch, tmp_path):
    control = tmp_path / "pair-control.json"
    control.write_text(json.dumps({"next_arm": "true_naive"}), encoding="utf-8")
    bloodhound = tmp_path / "bloodhound"
    bloodhound.mkdir()
    events = []

    monkeypatch.setattr(orchestrate, "BH", str(bloodhound))
    monkeypatch.setenv("SAGE_EVAL_API_ENDPOINT", "https://provider.invalid/v1")
    monkeypatch.setattr(orchestrate, "validate_gate1p_storage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrate, "validate_reset_inputs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrate,
        "initialize_gate1p_pair_control",
        lambda **_kwargs: events.append("initialize") or control,
    )
    monkeypatch.setattr(
        orchestrate,
        "run_side_with_gate1p_cleanup",
        lambda _scenario, side, **kwargs: events.append((side, kwargs["arm_index"])),
    )
    monkeypatch.setattr(orchestrate, "compare", lambda *_args, **_kwargs: events.append("compare"))

    assert orchestrate.main([
        "--scenario", "child-domain-control",
        "--gate1p-stage-a-pair",
        "--snapshot", orchestrate._gate1p_stagea_protocol().RESET_SNAPSHOT,
        "--source-version", "tree:test",
        "--model-context-limit", "200000",
        "--max-output-tokens", "8192",
        "--go",
        "--live-authority-id", orchestrate._gate1p_stagea_protocol().PAIR_ID,
    ]) == 0

    assert events == ["initialize", ("bare", 1), "compare"]


def test_gate1p_main_passes_the_same_budget_to_both_arms(monkeypatch, tmp_path):
    protocol = orchestrate._gate1p_stagea_protocol()
    control = tmp_path / "pair-control.json"
    results = tmp_path / "results.jsonl"
    control.write_text(json.dumps({"next_arm": "shipping_sage"}), encoding="utf-8")
    bloodhound = tmp_path / "bloodhound"
    bloodhound.mkdir()
    events = []

    monkeypatch.setattr(orchestrate, "BH", str(bloodhound))
    monkeypatch.setenv("SAGE_EVAL_API_ENDPOINT", "https://provider.invalid/v1")
    monkeypatch.setattr(orchestrate, "validate_gate1p_storage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrate, "validate_reset_inputs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrate, "_gate1p_results_path", lambda _route_env=None: results)
    monkeypatch.setattr(
        orchestrate,
        "initialize_gate1p_pair_control",
        lambda **_kwargs: control,
    )
    monkeypatch.setattr(
        orchestrate,
        "run_side_with_gate1p_cleanup",
        lambda _scenario, side, **kwargs: events.append(
            (side, kwargs["arm_index"], kwargs["model_token_budget"])
        ),
    )
    monkeypatch.setattr(
        orchestrate,
        "compare",
        lambda *_args, **kwargs: events.append(("compare", kwargs["results_path"])),
    )

    assert orchestrate.main([
        "--scenario", "child-domain-control",
        "--gate1p-stage-a-pair",
        "--snapshot", protocol.RESET_SNAPSHOT,
        "--source-version", "tree:test",
        "--model-context-limit", "200000",
        "--max-output-tokens", "8192",
        "--go",
        "--live-authority-id", protocol.PAIR_ID,
    ]) == 0

    assert events == [
        ("harness", 0, protocol.TOKEN_ADMISSION_CEILING),
        ("bare", 1, protocol.TOKEN_ADMISSION_CEILING),
        ("compare", results),
    ]


def test_gate1p_compare_receives_the_exact_private_results_path(monkeypatch, tmp_path):
    protocol = orchestrate._gate1p_stagea_protocol()
    selected = tmp_path / "private-pair" / "results.jsonl"
    unrelated_default = tmp_path / "default" / "bare_vs_harness.jsonl"
    unrelated_default.parent.mkdir()
    unrelated_default.write_text('{"wrong": "ledger"}\n', encoding="utf-8")
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(unrelated_default))
    monkeypatch.delenv("SAGE_EVAL_API_KEY", raising=False)
    observed = {}

    def fake_run(name, argv, cwd, timeout, env=None):
        observed.update(name=name, argv=argv, cwd=cwd, timeout=timeout, env=env)

    monkeypatch.setattr(orchestrate, "_run", fake_run)
    orchestrate.compare(
        "child-domain-control",
        pair_id=protocol.PAIR_ID,
        results_path=selected,
    )

    assert observed["argv"][-2:] == ["--pair-id", protocol.PAIR_ID]
    assert observed["env"]["SAGE_EVAL_RESULTS_PATH"] == str(selected)
    assert observed["env"].get("SAGE_EVAL_API_KEY") is None
    assert observed["env"]["SAGE_EVAL_RESULTS_PATH"] != str(unrelated_default)


def test_legacy_compare_does_not_override_the_results_path(monkeypatch):
    observed = {}
    monkeypatch.setattr(
        orchestrate,
        "_run",
        lambda _name, _argv, _cwd, _timeout, env=None: observed.update(env=env),
    )

    orchestrate.compare("child-da")

    assert observed["env"] is None


def test_gate1p_row_rejects_inherited_intervention_before_preflight(monkeypatch):
    protocol = orchestrate._gate1p_stagea_protocol()
    monkeypatch.setenv(
        "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON",
        json.dumps([{"capability": "adcs-ca-private-key-export"}]),
    )
    monkeypatch.setattr(
        orchestrate,
        "_run_side_preflight",
        lambda *_args, **_kwargs: pytest.fail("preflight must not run"),
    )

    with pytest.raises(SystemExit, match="incompatible evaluation override"):
        orchestrate.run_side(
            "child-domain-control",
            "harness",
            go=False,
            solve_timeout=1800,
            measurement_kind="row",
            pair_id=protocol.PAIR_ID,
        )


def test_gate1p_wrapper_advances_after_successful_arm_and_cleanup(monkeypatch):
    seen = []
    monkeypatch.setattr(
        orchestrate,
        "_validate_gate1p_wrapper_entry",
        lambda **_kwargs: "tree:test",
    )
    monkeypatch.setattr(orchestrate, "_run_side_preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrate, "mark_gate1p_prearm_reset_started", lambda **_kwargs: None)

    def succeed_after_reset(*_args, **kwargs):
        kwargs["before_reset"]()
        seen.append("run")

    monkeypatch.setattr(orchestrate, "run_side", succeed_after_reset)
    monkeypatch.setattr(orchestrate, "_gate1p_arm_started", lambda **_kwargs: True)
    monkeypatch.setattr(orchestrate, "cleanup_gate1p_arm", lambda **_kwargs: seen.append("cleanup"))
    monkeypatch.setattr(
        orchestrate,
        "advance_gate1p_pair_control",
        lambda **kwargs: seen.append(("advance", kwargs["arm"])),
    )

    orchestrate.run_side_with_gate1p_cleanup(
        "child-domain-control",
        "harness",
        measurement_kind="row",
        pair_id=orchestrate._gate1p_stagea_protocol().PAIR_ID,
        arm_index=0,
        source_version="tree:test",
        ludus_range_id="RANGE-1",
        ludus_mcp_server="ludus_test",
        route_env={},
    )

    assert seen == ["run", "cleanup", ("advance", "shipping_sage")]


def test_full_reset_syncs_range_time_after_all_guests_report_ips(monkeypatch):
    events = []

    def fake_run(name, argv, _cwd, _timeout, env=None):
        del env
        events.append(("run", name, list(argv)))

    def fake_poll(
        name,
        argv,
        _cwd,
        _predicate,
        *,
        timeout,
        interval=20,
        env=None,
        predicate_returncodes=(0,),
    ):
        del timeout, interval, env
        events.append(("poll", name, list(argv), predicate_returncodes))
        return ""

    monkeypatch.setattr(orchestrate, "_run", fake_run)
    monkeypatch.setattr(orchestrate, "_poll", fake_poll)
    monkeypatch.setattr(orchestrate, "discover_callbacks", lambda _foothold: 7)

    foothold = orchestrate.FootholdSpec(
        host="CINDER-WS01",
        ip="10.11.10.31",
        user=r"CINDER\user1",
        callback_user="user1",
        ludus_range_id="SAGELAPSR520260715",
        ludus_mcp_server="ludus_sagerepl",
    )
    assert orchestrate.full_reset_and_ready(
        foothold=foothold,
        ludus_range_id="SAGELAPSR520260715",
        ludus_mcp_server="ludus_sagerepl",
    ) == (None, 7)

    poweron_index = next(index for index, event in enumerate(events) if event[:2] == ("run", "ludus poweron"))
    guest_poll_index = next(index for index, event in enumerate(events) if event[:2] == ("poll", "range guests report IPs"))
    winrm_poll_index = next(index for index, event in enumerate(events) if event[:2] == ("poll", "range WinRM clock probes respond"))
    sync_index = next(index for index, event in enumerate(events) if event[:2] == ("run", "sync range time"))
    assert poweron_index < guest_poll_index < winrm_poll_index < sync_index
    assert events[winrm_poll_index][2] == [
        str(orchestrate.PY),
        "skills/sage-goad-reset/scripts/sync_range_time.py",
        "--mcp-server",
        "ludus_sagerepl",
        "--range-id",
        "SAGELAPSR520260715",
        "check",
    ]
    assert events[winrm_poll_index][3] == (0, 1)
    poll_returncodes = {event[1]: event[3] for event in events if event[0] == "poll"}
    assert poll_returncodes == {
        "range guests report IPs": (0,),
        "range WinRM clock probes respond": (0, 1),
        "BloodHound authoritative domain count is zero": (0,),
        "CINDER-WS01 powered on": (0,),
        "sage chat + apollo ready": (0, 1),
    }
    assert events[sync_index][2] == [
        str(orchestrate.PY),
        "skills/sage-goad-reset/scripts/sync_range_time.py",
        "--mcp-server",
        "ludus_sagerepl",
        "--range-id",
        "SAGELAPSR520260715",
        "sync",
        "--yes",
    ]


def test_foothold_launch_env_maps_alternate_password_source(monkeypatch):
    monkeypatch.setenv("SAGE_RUN_AS_PASSWORD", "samwell-password")
    monkeypatch.setenv("SAGE_MEEREEN_JORAH_PASSWORD", "jorah-password")

    foothold = orchestrate.FootholdSpec(password_env="SAGE_MEEREEN_JORAH_PASSWORD")

    assert foothold.launch_env()["SAGE_RUN_AS_PASSWORD"] == "jorah-password"


def test_gate1p_diagnostic_run_side_threads_non_countable_protocol(monkeypatch):
    seen = {}

    def fake_run(name, argv, _cwd, _timeout, env=None):
        del env
        if name.startswith("gauge "):
            seen["argv"] = list(argv)

    def fake_full_reset_and_ready(**_kwargs):
        return None, 7

    monkeypatch.setattr(orchestrate, "_run", fake_run)
    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    orchestrate.run_side(
        "child-domain-control",
        "harness",
        go=False,
        solve_timeout=3600,
        measurement_kind="diagnostic",
        protocol_id="gate1p-stage-a-diagnostic-v1",
        source_version="source-v1",
    )

    argv = seen["argv"]
    assert argv[argv.index("--measurement-kind") + 1] == "diagnostic"
    assert argv[argv.index("--protocol-id") + 1] == "gate1p-stage-a-diagnostic-v1"
    assert argv[argv.index("--source-version") + 1] == "source-v1"
    assert "--countable" not in argv


def test_run_side_preflight_authenticates_exact_model_route_before_reset(monkeypatch):
    calls = []

    def fake_run(name, argv, _cwd, _timeout, env=None, **_kwargs):
        calls.append((name, list(argv), dict(env or {})))

    monkeypatch.setattr(orchestrate, "_run", fake_run)
    monkeypatch.setattr(orchestrate, "full_reset_and_ready", lambda **_kwargs: (None, 7))
    route_env = {
        "SAGE_EVAL_API_ENDPOINT": "https://provider.invalid/v1",
        "SAGE_EVAL_API_KEY": "secret",
    }

    orchestrate.run_side(
        "child-domain-control",
        "harness",
        go=False,
        solve_timeout=3600,
        provider="openai",
        model="exact-model",
        max_output_tokens=8192,
        route_env=route_env,
    )

    name, argv, env = calls[0]
    assert name == "preflight child-domain-control"
    assert argv[argv.index("--provider") + 1] == "openai"
    assert argv[argv.index("--model") + 1] == "exact-model"
    assert argv[argv.index("--max-output-tokens") + 1] == "8192"
    assert env["SAGE_EVAL_API_ENDPOINT"] == route_env["SAGE_EVAL_API_ENDPOINT"]
    assert env["SAGE_EVAL_API_KEY"] == route_env["SAGE_EVAL_API_KEY"]


def test_gate1p_diagnostic_cli_requires_explicit_bloodhound_dir():
    env = dict(os.environ)
    env.pop("SAGE_BLOODHOUND_MCP_DIR", None)
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "child-domain-control",
            "--side",
            "harness",
            "--seeds",
            "1",
            "--gate1p-stage-a-diagnostic",
            "--source-version",
            "test-source",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "SAGE_BLOODHOUND_MCP_DIR is required" in result.stderr


def test_gate1p_diagnostic_cli_dry_run_uses_explicit_bloodhound_dir(tmp_path):
    bloodhound_dir = tmp_path / "bloodhound-mcp"
    bloodhound_dir.mkdir()
    env = {**os.environ, "SAGE_BLOODHOUND_MCP_DIR": str(bloodhound_dir)}
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "child-domain-control",
            "--side",
            "harness",
            "--seeds",
            "1",
            "--gate1p-stage-a-diagnostic",
            "--source-version",
            "test-source",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert f"SAGE_BLOODHOUND_MCP_DIR={bloodhound_dir}" in result.stdout
    assert f"uv --directory {bloodhound_dir}" in result.stdout
    assert "--measurement-kind diagnostic" in result.stdout
    assert "--protocol-id gate1p-stage-a-diagnostic-v1" in result.stdout
    assert "--source-version test-source" in result.stdout


def test_gate1p_pair_dry_run_prints_exact_two_arm_plan_without_live_authority(tmp_path):
    bloodhound_dir = tmp_path / "bloodhound-mcp"
    bloodhound_dir.mkdir()
    env = {
        **os.environ,
        "SAGE_BLOODHOUND_MCP_DIR": str(bloodhound_dir),
        "SAGE_EVAL_API_ENDPOINT": "https://provider.invalid/v1",
    }
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "child-domain-control",
            "--gate1p-stage-a-pair",
            "--snapshot",
            orchestrate._gate1p_stagea_protocol().RESET_SNAPSHOT,
            "--source-version",
            "tree:test",
            "--model-context-limit",
            "200000",
            "--max-output-tokens",
            "8192",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout
    assert output.count("--- iteration seed=0 side=") == 2
    assert "side=harness" in output and "side=bare" in output
    assert "--arm-index 0" in output and "--arm-index 1" in output
    assert f"--pair-id {orchestrate._gate1p_stagea_protocol().PAIR_ID}" in output
    assert output.count("wipe --yes") == 2
    assert output.count("authoritative count=0") == 2
    assert output.count("run_gauge_live.py preflight --scenario child-domain-control") == 2
    assert output.count("<=8 output tokens, 10s, zero retries") == 2
    assert output.index("run_gauge_live.py preflight") < output.index("mythic_reset.sh --yes")
    assert "--live-authority-id" not in output


def test_gate1p_pair_live_cli_refuses_without_exact_pair024_authority(tmp_path):
    bloodhound_dir = tmp_path / "bloodhound-mcp"
    bloodhound_dir.mkdir()
    env = {
        **os.environ,
        "SAGE_BLOODHOUND_MCP_DIR": str(bloodhound_dir),
        "SAGE_EVAL_API_ENDPOINT": "https://provider.invalid/v1",
    }
    result = subprocess.run(
        [
            str(PY),
            str(SCRIPT),
            "--scenario",
            "child-domain-control",
            "--gate1p-stage-a-pair",
            "--snapshot",
            orchestrate._gate1p_stagea_protocol().RESET_SNAPSHOT,
            "--source-version",
            "tree:test",
            "--model-context-limit",
            "200000",
            "--max-output-tokens",
            "8192",
            "--go",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert (
        f"live paired-row execution requires separate --live-authority-id "
        f"{orchestrate._gate1p_stagea_protocol().PAIR_ID}"
    ) in result.stderr


def test_gate1p_pair_rejects_snapshot_or_foothold_drift_before_live_contact(tmp_path):
    bloodhound_dir = tmp_path / "bloodhound-mcp"
    bloodhound_dir.mkdir()
    env = {
        **os.environ,
        "SAGE_BLOODHOUND_MCP_DIR": str(bloodhound_dir),
        "SAGE_EVAL_API_ENDPOINT": "https://provider.invalid/v1",
    }
    common = [
        str(PY),
        str(SCRIPT),
        "--scenario",
        "child-domain-control",
        "--gate1p-stage-a-pair",
        "--snapshot", orchestrate._gate1p_stagea_protocol().RESET_SNAPSHOT,
        "--source-version",
        "tree:test",
        "--model-context-limit",
        "200000",
        "--max-output-tokens",
        "8192",
    ]
    wrong_snapshot = subprocess.run(
        [*common, "--snapshot", "other-snapshot"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    wrong_foothold = subprocess.run(
        [*common, "--foothold-host", "WINTERFELL"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert wrong_snapshot.returncode == 2
    assert "requires --snapshot" in wrong_snapshot.stderr
    assert wrong_foothold.returncode == 2
    assert "fixed CASTELBLACK/Samwell Apollo foothold role" in wrong_foothold.stderr


def test_run_side_pins_same_netbios_map_for_all_policy_arms(monkeypatch):
    seen = {}

    monkeypatch.setattr(orchestrate, "_run", lambda *args, **kwargs: None)

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold, ludus_range_id):
        del snapshot, retained_callback_config, foothold, ludus_range_id
        seen[restart_env["SAGE_POLICY_MODE"]] = dict(restart_env)
        return None, 7

    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    for policy_mode in ("symbolic", "llm", "hybrid"):
        orchestrate.run_side(
            "cross-forest-objective",
            "harness",
            go=False,
            solve_timeout=1,
            policy_mode=policy_mode,
        )

    assert set(seen) == {"symbolic", "llm", "hybrid"}
    assert {
        values["SAGE_ENGAGEMENT_NETBIOS_MAP"]
        for values in seen.values()
    } == {orchestrate.DEFAULT_ENGAGEMENT_NETBIOS_MAP}
    assert {
        values["SAGE_AUTONOMOUS_CONTROLLER"]
        for values in seen.values()
    } == {"1"}
    assert {
        values["SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS"]
        for values in seen.values()
    } == {"1"}


def test_run_side_uses_purpose_range_map_and_passes_range_id(monkeypatch):
    seen = {}

    monkeypatch.setattr(orchestrate, "_run", lambda *args, **kwargs: None)

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold, ludus_range_id):
        del snapshot, retained_callback_config, foothold
        seen["restart_env"] = dict(restart_env)
        seen["range_id"] = ludus_range_id
        return None, 7

    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    orchestrate.run_side(
        "purpose-range-visible-cost",
        "harness",
        go=False,
        solve_timeout=1,
        policy_mode="hybrid",
        ludus_range_id="SAGEPOLICY20260712",
    )

    assert seen["range_id"] == "SAGEPOLICY20260712"
    assert seen["restart_env"]["SAGE_ENGAGEMENT_NETBIOS_MAP"] == orchestrate.DEFAULT_PURPOSE_RANGE_NETBIOS_MAP
    assert seen["restart_env"]["SAGE_GPO_PROOF_SHARE_NAME"] == "SageProof"
    assert seen["restart_env"]["SAGE_GPO_PROOF_LOCAL_ROOT"] == r"C:\SageProof"
    assert seen["restart_env"]["SAGE_GPO_WAIT_SECONDS"] == "120"


def test_run_side_uses_replication_range_map_and_gpo_proof_env(monkeypatch):
    seen = {}

    monkeypatch.setattr(orchestrate, "_run", lambda *args, **kwargs: None)

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold, ludus_range_id):
        del snapshot, retained_callback_config, foothold
        seen["restart_env"] = dict(restart_env)
        seen["range_id"] = ludus_range_id
        return None, 7

    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    orchestrate.run_side(
        "replication-purpose-range-visible-cost",
        "harness",
        go=False,
        solve_timeout=1,
        policy_mode="hybrid",
        ludus_range_id="SAGEREPLICATION20260712",
    )

    assert seen["range_id"] == "SAGEREPLICATION20260712"
    assert (
        seen["restart_env"]["SAGE_ENGAGEMENT_NETBIOS_MAP"]
        == orchestrate.DEFAULT_REPLICATION_PURPOSE_RANGE_NETBIOS_MAP
    )
    assert seen["restart_env"]["SAGE_GPO_PROOF_SHARE_NAME"] == "SageProof"
    assert seen["restart_env"]["SAGE_GPO_PROOF_LOCAL_ROOT"] == r"C:\SageProof"
    assert seen["restart_env"]["SAGE_GPO_WAIT_SECONDS"] == "120"
    assert "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON" not in seen["restart_env"]


def test_run_side_uses_laps_family_transfer_map_without_purpose_range_env(monkeypatch):
    seen = {}

    monkeypatch.setattr(orchestrate, "_run", lambda *args, **kwargs: None)

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold, ludus_range_id):
        del snapshot, retained_callback_config, foothold
        seen["restart_env"] = dict(restart_env)
        seen["range_id"] = ludus_range_id
        return None, 7

    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    orchestrate.run_side(
        "laps-family-transfer-ash-remote-exec",
        "harness",
        go=False,
        solve_timeout=1,
        policy_mode="hybrid",
        ludus_range_id="SAGELAPSR520260715",
    )

    assert seen["range_id"] == "SAGELAPSR520260715"
    assert (
        seen["restart_env"]["SAGE_ENGAGEMENT_NETBIOS_MAP"]
        == orchestrate.DEFAULT_LAPS_FAMILY_TRANSFER_NETBIOS_MAP
    )
    assert "SAGE_GPO_PROOF_SHARE_NAME" not in seen["restart_env"]
    assert "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON" not in seen["restart_env"]


def test_run_side_enables_phase6_callback_uniqueness_gate(monkeypatch):
    seen = {}

    monkeypatch.setattr(orchestrate, "_run", lambda *args, **kwargs: None)

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold, ludus_range_id):
        del restart_env, snapshot, retained_callback_config, ludus_range_id
        seen["foothold"] = foothold
        return None, 7

    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    orchestrate.run_side(
        "laps-family-transfer-ash-remote-exec",
        "harness",
        go=False,
        solve_timeout=1,
        policy_mode="symbolic",
    )

    foothold = seen["foothold"]
    assert foothold.callback_settle_seconds == orchestrate.PHASE6_CALLBACK_SETTLE_SECONDS
    assert foothold.require_unique_callback is True
    assert foothold.launch_argv()[-3:] == [
        "--callback-settle-seconds",
        str(orchestrate.PHASE6_CALLBACK_SETTLE_SECONDS),
        "--require-unique-callback",
    ]


def test_run_side_passes_runtime_and_scenario_env_to_gauge_subprocess(monkeypatch):
    """Headless gauge runs must see the same BloodHound/frontier env as the restarted Sage service."""
    seen = {}

    def fake_run(name, _argv, _cwd, _timeout, env=None):
        if name.startswith("gauge "):
            seen["gauge_env"] = dict(env or {})

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold, ludus_range_id):
        del snapshot, retained_callback_config, foothold, ludus_range_id
        seen["restart_env"] = dict(restart_env)
        return None, 7

    monkeypatch.setattr(orchestrate, "_run", fake_run)
    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    orchestrate.run_side(
        "laps-family-transfer-ash-remote-exec",
        "harness",
        go=False,
        solve_timeout=1,
        policy_mode="hybrid",
    )

    gauge_env = seen["gauge_env"]
    assert gauge_env["SAGE_ENGAGEMENT_GATE"] == "1"
    assert gauge_env["SAGE_BLOODHOUND_MCP_DIR"] == orchestrate.BH
    assert gauge_env["SAGE_AUTONOMOUS_CONTROLLER"] == "1"
    assert gauge_env["SAGE_POLICY_MODE"] == "hybrid"
    assert (
        gauge_env["SAGE_ENGAGEMENT_NETBIOS_MAP"]
        == orchestrate.DEFAULT_LAPS_FAMILY_TRANSFER_NETBIOS_MAP
    )
    assert gauge_env["SAGE_ENGAGEMENT_NETBIOS_MAP"] == seen["restart_env"]["SAGE_ENGAGEMENT_NETBIOS_MAP"]


def test_run_side_builds_exact_phase6_forced_prefix_after_callback_discovery(monkeypatch):
    seen = {}

    def fake_run(name, _argv, _cwd, _timeout, env=None):
        if name.startswith("gauge "):
            seen["gauge_env"] = dict(env or {})

    def fake_full_reset_and_ready(*, restart_env, snapshot, retained_callback_config, foothold, ludus_range_id):
        del restart_env, snapshot, retained_callback_config, foothold, ludus_range_id
        return None, 7

    monkeypatch.setattr(orchestrate, "_run", fake_run)
    monkeypatch.setattr(orchestrate, "full_reset_and_ready", fake_full_reset_and_ready)

    orchestrate.run_side(
        "laps-family-transfer-ember-remote-exec",
        "harness",
        go=False,
        solve_timeout=1,
        policy_mode="symbolic",
        laps_forced_path="ash-first",
        phase6_planned_row_id="forced-ember-ash-r1",
        phase6_attempt_index=2,
    )

    gauge_env = seen["gauge_env"]
    prefix = json.loads(gauge_env["SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON"])
    assert gauge_env["SAGE_EVAL_PHASE6_FORCED_PATH"] == "ash-first"
    assert gauge_env["SAGE_EVAL_PHASE6_PLANNED_ROW_ID"] == "forced-ember-ash-r1"
    assert gauge_env["SAGE_EVAL_PHASE6_ATTEMPT_INDEX"] == "2"
    assert gauge_env["SAGE_EVAL_PHASE6_MAX_PRE_FRONTIER_DIAGNOSTIC_RETRIES"] == "1"
    assert gauge_env["SAGE_EVAL_PHASE6_MANIFEST_HASH"].startswith("sha256:")
    assert gauge_env["SAGE_EVAL_PHASE6_TOPOLOGY_HASH"].startswith("sha256:")
    assert [item["capability"] for item in prefix] == [
        "read-managed-local-admin-secret",
        "use-managed-local-admin-secret",
        "execute-as-local-admin",
    ]
    assert prefix[0]["exact_target"].endswith("target=ash-ops01;target_domain=ash.cinder.local;callback=7")
    assert all(item["intervention_id"].startswith("phase6-laps-family-transfer-ember-remote-exec-ash-first-") for item in prefix)


def test_purpose_range_recovery_adds_explicit_gpo_blocker_only_for_recovery_variant():
    visible = orchestrate._scenario_restart_env("purpose-range-visible-cost")
    recovery = orchestrate._scenario_restart_env("purpose-range-recovery")

    assert "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON" not in visible
    blocker = json.loads(recovery["SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON"])
    assert blocker["capability"] == "gpo-controlled-system-exec"
    assert blocker["target_contains"] == "gpo=srv02-policy;domain=range.local"
    assert blocker["reason"] == "endpoint protection blocked the staged GPO payload on srv02"
    assert blocker["probe"]["defender_blocked"] is True


def test_direct_laps_ca_export_recovery_adds_late_blocker_and_scoped_enrollment_hint():
    visible = orchestrate._scenario_restart_env("direct-laps-objective")
    recovery = orchestrate._scenario_restart_env("direct-laps-ca-export-recovery")

    assert visible == {}
    blocker = json.loads(recovery["SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON"])
    assert blocker["capability"] == "adcs-ca-private-key-export"
    assert blocker["target_contains"] == "target=braavos;target_domain=essos.local"
    assert blocker["reason"] == "key not exportable"
    assert blocker["probe"]["key_not_exportable"] is True
    hints = json.loads(recovery["SAGE_EVAL_ADCS_ESC_ENROLLMENT_HINTS_JSON"])
    assert hints == [{
        "ca_host": "braavos",
        "ca_name": r"braavos.essos.local\ESSOS-CA",
        "domain": "essos.local",
        "esc_type": "esc1",
        "template": "ESC1",
    }]


def test_purpose_range_ca_export_replanning_forces_prefix_and_releases_repairable_blocker():
    visible = orchestrate._scenario_restart_env("purpose-range-visible-cost")
    replanning = orchestrate._scenario_restart_env("purpose-range-ca-export-replanning")

    assert "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON" not in visible
    prefix = json.loads(replanning["SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON"])
    assert [item["capability"] for item in prefix] == [
        "read-managed-local-admin-secret",
        "use-managed-local-admin-secret",
        "execute-as-local-admin",
        "adcs-ca-private-key-export",
    ]
    assert prefix[-1]["release_on_failure"] is True
    blocker = json.loads(replanning["SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON"])
    assert blocker["capability"] == "adcs-ca-private-key-export"
    assert blocker["target_contains"] == "target=ca01;target_domain=range.local"
    assert blocker["failure_class"] == "transient"
    assert blocker["skip_if_achieved_effect"] == "endpoint-protection-adjusted:ca01@range.local"
    assert blocker["probe"]["tool_execution_failed"] is True
    assert blocker["probe"]["defender_blocked"] is True


def test_purpose_range_gpo_dc_scope_late_blocker_forces_terminal_certificate_auth_blocker():
    visible = orchestrate._scenario_restart_env("purpose-range-visible-cost")
    late_blocker = orchestrate._scenario_restart_env("purpose-range-gpo-dc-scope-late-blocker")

    assert "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON" not in visible
    assert late_blocker["SAGE_GPO_WAIT_SECONDS"] == "300"
    prefix = json.loads(late_blocker["SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON"])
    assert [item["capability"] for item in prefix] == [
        "read-managed-local-admin-secret",
        "use-managed-local-admin-secret",
        "execute-as-local-admin",
        "adcs-ca-private-key-export",
        "adcs-certificate-auth",
    ]
    assert prefix[-1]["release_on_failure"] is True
    blocker = json.loads(late_blocker["SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON"])
    assert blocker["capability"] == "adcs-certificate-auth"
    assert blocker["target_contains"] == "domain=range.local;account=administrator;ca_host=ca01"
    assert blocker["failure_class"] == "genuine"
    assert blocker["record_failed_effect"] == "certificate-auth:administrator@range.local"
    assert blocker["probe"]["pkinit_failed"] is True
    assert blocker["probe"]["target_host"] == "ca01"
    assert blocker["probe"]["account"] == "administrator"


def test_treatment_route_rejects_loopback_proxy(tmp_path):
    route = tmp_path / ".env.local"
    route.write_text(
        "SAGE_EVAL_PROVIDER=openai\n"
        "SAGE_EVAL_API_ENDPOINT=http://127.0.0.1:8100/v1\n"
        "SAGE_EVAL_API_KEY=secret\n"
        "SAGE_EVAL_SONNET_MODEL=sonnet\n"
    )

    with pytest.raises(SystemExit, match="may not use the loopback proxy"):
        orchestrate.load_treatment_route(route, "sonnet")


def test_treatment_route_loads_selected_model_without_exposing_secret(tmp_path):
    route = tmp_path / ".env.local"
    route.write_text(
        "SAGE_EVAL_PROVIDER=openai\n"
        "SAGE_EVAL_API_ENDPOINT=https://bedrock-proxy.example/v1\n"
        "SAGE_EVAL_API_KEY=secret\n"
        "SAGE_EVAL_SONNET_MODEL=sonnet\n"
        "SAGE_EVAL_HAIKU_MODEL=haiku\n"
    )

    loaded = orchestrate.load_treatment_route(route, "haiku")

    assert loaded == {
        "provider": "openai",
        "model": "haiku",
        "api_endpoint": "https://bedrock-proxy.example/v1",
        "api_key": "secret",
    }
