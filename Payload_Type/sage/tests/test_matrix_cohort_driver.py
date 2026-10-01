"""Offline producer/consumer checks for the operator-run matrix cohort driver."""

import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-eval-gauge/scripts/run_matrix_cohort.py"
SPEC = importlib.util.spec_from_file_location("matrix_cohort_driver", SCRIPT)
driver = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(driver)


def plan_file(tmp_path, *, attempts=1):
    directory = tmp_path / ".sage_history"
    directory.mkdir(exist_ok=True)
    for name in ("matrix.json", "protected.json", "smoke.json", "authority-1.json", "authority-2.json"):
        (directory / name).write_text("{}\n")
    plan = {
        "schema": driver.SCHEMA, "cohort_id": "synthetic-cohort", "route_id": "synthetic-route",
        "contract_file": "matrix.json",
        "slots": [{
            "slot_id": "001", "arm_order": "shipping-first", "case_descriptor": None,
            "attempts": [{
                "live_authority_file": f"authority-{index}.json",
                "protected_manifest": "protected.json", "route_smoke": "smoke.json",
            } for index in range(1, attempts + 1)],
        }],
    }
    path = directory / "cohort.json"
    path.write_text(json.dumps(plan))
    return path, directory / "run"


def write_row(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")


def test_refusal_policy_is_bound_and_cannot_change_on_resume(tmp_path):
    path, work = plan_file(tmp_path)
    legacy = driver.load_plan(path)
    assert "continue_after_refusal" not in legacy
    raw = json.loads(path.read_text())
    raw["continue_after_refusal"] = True
    path.write_text(json.dumps(raw))
    selected = driver.load_plan(path)
    slot = selected["slots"][0]
    command = driver._cli("run-pair", selected, slot, slot["attempts"][0], work / "pair")
    assert "--continue-after-refusal" in command
    with pytest.raises(driver.DriverError, match="refusal continuation policy changed"):
        driver._check_amendment(legacy, selected, work)
    raw["continue_after_refusal"] = "true"
    path.write_text(json.dumps(raw))
    with pytest.raises(driver.DriverError, match="must be a boolean"):
        driver.load_plan(path)


def test_raw_reasoning_effort_is_frozen_and_forwarded_only_when_selected(tmp_path):
    path, work = plan_file(tmp_path)
    legacy = driver.load_plan(path)
    slot = legacy["slots"][0]
    assert "--raw-reasoning-effort" not in driver._cli(
        "run-pair", legacy, slot, slot["attempts"][0], work / "pair"
    )
    raw = json.loads(path.read_text())
    raw["raw_reasoning_effort"] = "none"
    path.write_text(json.dumps(raw))
    selected = driver.load_plan(path)
    slot = selected["slots"][0]
    for phase in ("rehearse-setup", "continue-setup", "run-pair"):
        command = driver._cli(phase, selected, slot, slot["attempts"][0], work / "pair")
        assert command[-2:] == ["--raw-reasoning-effort", "none"]
    with pytest.raises(driver.DriverError, match="raw reasoning effort changed"):
        driver._check_amendment(legacy, selected, work)
    raw["raw_reasoning_effort"] = "NONE"
    path.write_text(json.dumps(raw))
    with pytest.raises(driver.DriverError, match="raw_reasoning_effort is unsupported"):
        driver.load_plan(path)


def test_matched_reasoning_effort_requires_both_values_and_freezes_resume(tmp_path):
    path, work = plan_file(tmp_path)
    legacy = driver.load_plan(path)
    raw = json.loads(path.read_text())
    raw["raw_reasoning_effort"] = raw["sage_reasoning_effort"] = "none"
    path.write_text(json.dumps(raw))
    matched = driver.load_plan(path)
    slot = matched["slots"][0]
    command = driver._cli("run-pair", matched, slot, slot["attempts"][0], work / "pair")
    assert command[-4:] == ["--raw-reasoning-effort", "none", "--reasoning-effort", "none"]
    with pytest.raises(driver.DriverError, match="Sage reasoning effort changed|raw reasoning effort changed"):
        driver._check_amendment(legacy, matched, work)
    raw["raw_reasoning_effort"] = "high"
    path.write_text(json.dumps(raw))
    with pytest.raises(driver.DriverError, match="same raw and Sage"):
        driver.load_plan(path)
    raw["raw_reasoning_effort"] = "none"
    raw["sage_reasoning_effort"] = "high"
    path.write_text(json.dumps(raw))
    with pytest.raises(driver.DriverError, match="same raw and Sage"):
        driver.load_plan(path)


def test_raw_reasoning_effort_override_rejected_outside_prepare(tmp_path):
    path, work = plan_file(tmp_path)
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work),
                        "--raw-reasoning-effort", "none"]) == 1
    assert not work.exists()


def test_matched_reasoning_effort_override_rejected_outside_prepare(tmp_path):
    path, work = plan_file(tmp_path)
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work),
                        "--reasoning-effort", "none"]) == 1
    assert not work.exists()


def test_refusal_override_rejected_outside_prepare(tmp_path):
    path, work = plan_file(tmp_path)
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work),
                        "--continue-after-refusal"]) == 1
    assert not work.exists()


def fake_producer(calls, *, stop_at=None):
    def invoke(argv, _lock_fd, *, progress=None):
        phase = argv[2]
        calls.append(phase)
        if phase == "matrix-preflight":
            return 0, {"ready": True}
        artifact = Path(argv[argv.index("--artifact-dir") + 1])
        if phase in {"rehearse-setup", "continue-setup"}:
            write_row(artifact / "setup-rehearsals.jsonl", {
                "record_type": "setup_rehearsal_result", "complete": True,
            })
            return 0, {"complete": True}
        if phase == "run-pair":
            ledger = artifact / "pilot.jsonl"
            write_row(ledger, {"record_type": "attempted_start", "arm": "shipping-sage"})
            if stop_at == "pair":
                write_row(ledger, {"record_type": "arm_started", "arm": "shipping-sage"})
                return 1, {"ready": False}
            write_row(ledger, {"record_type": "comparison", "run_complete": True})
            return 0, {"run_complete": True}
        if phase == "replay-pair":
            return 0, {"reproduced": True, "comparison": {"run_complete": True,
                                                          "comparison_ready": True, "protocol_valid": True}}
        raise AssertionError(phase)
    return invoke


def test_run_and_resume_skip_completed_effectful_phases(tmp_path, monkeypatch):
    path, work = plan_file(tmp_path)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls))

    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 0
    assert calls == ["matrix-preflight", "rehearse-setup", "run-pair", "replay-pair"]
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work)]) == 0
    assert calls.count("run-pair") == 1
    assert calls.count("rehearse-setup") == 1
    assert driver.main(["status", "--plan", str(path), "--work-dir", str(work)]) == 0
    assert driver._phase_state(work / "slot-001-attempt-001") == "complete"
    assert "attempt_complete" in (work / "driver.log").read_text()


def test_once_per_cohort_reuses_retained_first_rehearsal_across_three_pairs(tmp_path, monkeypatch):
    path, work = plan_file(tmp_path)
    plan = json.loads(path.read_text())
    plan["rehearsal_policy"] = "once-per-cohort"
    for number in (2, 3):
        slot = json.loads(json.dumps(plan["slots"][0]))
        slot["slot_id"] = f"{number:03d}"
        slot["arm_order"] = "raw-first" if number == 2 else "shipping-first"
        plan["slots"].append(slot)
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls))

    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 0
    assert calls.count("rehearse-setup") == 1
    assert calls.count("run-pair") == 3
    receipt = work / driver.REHEARSAL_RECEIPT_NAME
    assert receipt.is_file()
    assert "rehearsal_reused" in (work / "driver.log").read_text()
    assert driver._status(driver.load_plan(path), work)["rehearsal_policy"] == "once-per-cohort"
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work)]) == 0
    assert calls.count("rehearse-setup") == 1
    first_ledger = work / "slot-001-attempt-001/setup/setup-rehearsals.jsonl"
    write_row(first_ledger, {"record_type": "tamper"})
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work)]) == 1


def test_once_per_cohort_resume_requires_first_rehearsal_receipt(tmp_path, monkeypatch):
    path, work = plan_file(tmp_path)
    plan = json.loads(path.read_text())
    plan["rehearsal_policy"] = "once-per-cohort"
    plan["same_arm_recovery"] = True
    slot = json.loads(json.dumps(plan["slots"][0]))
    slot["slot_id"] = "002"
    plan["slots"].append(slot)
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls, stop_at="pair"))
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 1
    assert calls.count("rehearse-setup") == 1
    assert "run-pair" in calls
    receipt = work / driver.REHEARSAL_RECEIPT_NAME
    assert receipt.is_file()
    receipt.unlink()
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls))
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work)]) == 1
    assert calls.count("rehearse-setup") == 1
    assert calls.count("run-pair") == 1
    assert not (work / "slot-002-attempt-001").exists()


def test_rehearsal_policy_is_frozen_and_legacy_runs_per_pair(tmp_path, monkeypatch):
    path, work = plan_file(tmp_path)
    legacy = driver.load_plan(path)
    plan = json.loads(path.read_text())
    plan["rehearsal_policy"] = "once-per-cohort"
    path.write_text(json.dumps(plan))
    selected = driver.load_plan(path)
    with pytest.raises(driver.DriverError, match="rehearsal policy changed"):
        driver._check_amendment(legacy, selected, work)
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work),
                        "--rehearsal-policy", "per-pair"]) == 1
    plan.pop("rehearsal_policy")
    slot = json.loads(json.dumps(plan["slots"][0]))
    slot["slot_id"] = "002"
    plan["slots"].append(slot)
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls))
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 0
    assert calls.count("rehearse-setup") == 2


def test_interrupted_pair_needs_cleanup_and_explicit_new_attempt(tmp_path, monkeypatch, capsys):
    path, work = plan_file(tmp_path)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls, stop_at="pair"))

    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 1
    assert "phase=run-pair exit=1 elapsed=" in capsys.readouterr().err
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work)]) == 1
    assert calls.count("run-pair") == 1
    old = work / "slot-001-attempt-001/pair/pilot.jsonl"
    original = old.read_bytes()

    plan = json.loads(path.read_text())
    plan["slots"][0]["attempts"].append({
        "live_authority_file": "authority-2.json", "protected_manifest": "protected.json",
        "route_smoke": "smoke.json",
    })
    path.write_text(json.dumps(plan))
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work)]) == 1
    assert not (work / "slot-001-attempt-002").exists()

    write_row(old, {"record_type": "recovery_cleanup", "complete": True})
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls))
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work)]) == 0
    assert original in old.read_bytes()
    assert driver._phase_state(work / "slot-001-attempt-002") == "complete"


def test_failed_reset_stage_is_reported_without_child_text(tmp_path, monkeypatch, capsys):
    path, work = plan_file(tmp_path)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    secret = "PLANTED-RESET-CHILD-SECRET"
    ordinary = fake_producer([])

    def invoke(argv, lock_fd, *, progress=None):
        if argv[2] != "run-pair":
            return ordinary(argv, lock_fd, progress=progress)
        artifact = Path(argv[argv.index("--artifact-dir") + 1])
        write_row(artifact / "pilot.jsonl", {"record_type": "attempted_start", "arm": "shipping-sage"})
        write_row(artifact / "pilot.jsonl", {"record_type": "reset_intent", "arm": "shipping-sage"})
        write_row(artifact / "reset-stages.jsonl", {
            "record_type": "reset_stage", "arm": "shipping-sage", "stage": "archive-dbs",
            "status": "failed", "error_class": "SystemExit", "exit_code": -15,
            "stderr": secret,
        })
        return 1, None

    monkeypatch.setattr(driver, "_invoke", invoke)
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 1
    visible = capsys.readouterr().err
    assert "reset-stage=archive-dbs status=failed error_class=SystemExit exit_code=-15" in visible
    assert secret not in visible
    log = (work / "driver.log").read_text()
    assert "reset-stage=archive-dbs status=failed error_class=SystemExit exit_code=-15" in log
    assert secret not in log
    assert driver._phase_state(work / "slot-001-attempt-001") == "pair_interrupted"


def test_reset_stage_reader_rejects_untrusted_labels(tmp_path):
    artifact = tmp_path / "pair"
    secret = "PLANTED-RESET-CHILD-SECRET"
    write_row(artifact / "reset-stages.jsonl", {
        "record_type": "reset_stage", "stage": secret, "status": "failed",
        "error_class": secret, "exit_code": secret,
    })
    write_row(artifact / "reset-stages.jsonl", {
        "record_type": "reset_stage", "stage": [secret], "status": {"secret": secret},
    })
    assert driver._reset_stage_diagnostic(artifact) is None


def test_interrupted_setup_uses_only_existing_continuation_surface(tmp_path, monkeypatch):
    path, work = plan_file(tmp_path)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    setup = work / "slot-001-attempt-001/setup"
    write_row(setup / "setup-rehearsals.jsonl", {"record_type": "setup_preflight", "ready": True})
    write_row(setup / "setup-rehearsals.jsonl", {"record_type": "setup_attempted", "attempt": 1})
    (setup / "attempt-001-abort-report.json").write_text("{}")
    driver._write_json_atomic(work / driver.STATE_NAME, driver.load_plan(path))
    calls = []
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls))

    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 0
    assert "continue-setup" in calls
    assert "rehearse-setup" not in calls


def test_plan_drift_and_invalid_replay_receipt_fail_closed(tmp_path, monkeypatch):
    path, work = plan_file(tmp_path)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    monkeypatch.setattr(driver, "_invoke", fake_producer([]))
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 0

    plan = json.loads(path.read_text())
    plan["slots"][0]["arm_order"] = "raw-first"
    path.write_text(json.dumps(plan))
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work)]) == 1
    plan["slots"][0]["arm_order"] = "shipping-first"
    path.write_text(json.dumps(plan))
    ledger = work / "slot-001-attempt-001/pair/pilot.jsonl"
    write_row(ledger, {"record_type": "progress", "event": "tampered"})
    assert driver.main(["status", "--plan", str(path), "--work-dir", str(work)]) == 1


def test_catalog_or_incomplete_attempt_input_drift_stops_before_next_command(tmp_path, monkeypatch):
    path, work = plan_file(tmp_path)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls, stop_at="pair"))
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 1
    count = len(calls)
    (path.parent / "smoke.json").write_text('{"changed": true}\n')
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work)]) == 1
    assert len(calls) == count + 1  # local matrix check is safe; no pair is repeated
    (path.parent / "matrix.json").write_text('{"changed": true}\n')
    assert driver.main(["resume", "--plan", str(path), "--work-dir", str(work)]) == 1
    assert len(calls) == count + 1


def test_replacement_requires_each_prior_arm_cleaned(tmp_path, monkeypatch):
    path, work = plan_file(tmp_path, attempts=2)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    pair = work / "slot-001-attempt-001/pair/pilot.jsonl"
    write_row(pair, {"record_type": "attempted_start", "arm": "shipping-sage"})
    write_row(pair, {"record_type": "attempted_start", "arm": "raw-mythic-bloodhound"})
    write_row(pair, {"record_type": "recovery_cleanup", "complete": True})
    assert driver._safe_to_replace(work / "slot-001-attempt-001") is False
    write_row(pair, {"record_type": "cleanup", "arm": "shipping-sage", "complete": True})
    assert driver._safe_to_replace(work / "slot-001-attempt-001") is True
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 1


@pytest.mark.parametrize("bad", ["outside", "duplicate-slot", "secret-key"])
def test_invalid_plan_or_public_output_is_rejected(tmp_path, monkeypatch, bad):
    path, work = plan_file(tmp_path)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    plan = json.loads(path.read_text())
    if bad == "outside":
        assert driver.main(["run", "--plan", str(path), "--work-dir", str(tmp_path / "public")]) == 1
        return
    if bad == "duplicate-slot":
        plan["slots"].append(plan["slots"][0])
    else:
        plan["slots"][0]["attempts"][0]["api_key"] = "must-not-be-retained"
    path.write_text(json.dumps(plan))
    assert driver.main(["plan", "--plan", str(path)]) == 1
    assert not work.exists()


def test_subprocess_output_is_bounded_and_lock_is_inherited(tmp_path):
    fd = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        code, result = driver._invoke([
            sys.executable, "-c",
            f"import os,json; os.fstat({fd}); print(json.dumps({{'ready': True}}))",
        ], fd)
        assert (code, result) == (0, {"ready": True})
        code, result = driver._invoke([
            sys.executable, "-c",
            f"import sys; sys.stderr.write('x' * 2000000); print('x' * {driver.MAX_RESULT_BYTES + 1})",
        ], fd)
        assert (code, result) == (0, None)
    finally:
        os.close(fd)


def test_subprocess_wait_emits_heartbeat_without_subprocess_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(driver, "PROGRESS_INTERVAL_SECONDS", 0.05)
    fd = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        code, result = driver._invoke([
            sys.executable, "-c",
            "import sys,time; sys.stderr.write('secret-stderr'); "
            "time.sleep(0.18); print('{\"ready\": true}')",
        ], fd, progress=lambda elapsed: driver._terminal_progress(
            "matrix-preflight", None, None, None, elapsed))
    finally:
        os.close(fd)
    captured = capsys.readouterr()
    assert (code, result) == (0, {"ready": True})
    assert captured.out == ""
    assert "[cohort] waiting slot=- attempt=- phase=matrix-preflight" in captured.err
    assert "stage=checking-route" in captured.err
    assert "secret-stderr" not in captured.err


def test_subprocess_wait_keeps_heartbeat_after_stdout_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "PROGRESS_INTERVAL_SECONDS", 0.05)
    fd = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    ticks = []
    try:
        code, result = driver._invoke([
            sys.executable, "-c",
            "import os,time; os.close(1); time.sleep(0.18)",
        ], fd, progress=ticks.append)
    finally:
        os.close(fd)
    assert (code, result) == (0, None)
    assert ticks and ticks[0] >= 0.05


def test_terminal_reports_safe_ledger_stage_and_phase_completion(tmp_path, monkeypatch, capsys):
    path, work = plan_file(tmp_path)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls))
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 0
    captured = capsys.readouterr()
    assert '"complete": true' in captured.out
    assert "[cohort] phase_started slot=001 attempt=1 phase=run-pair" in captured.err
    assert "[cohort] phase_returned slot=001 attempt=1 phase=run-pair exit=0 elapsed=" in captured.err
    assert "[cohort] attempt_complete slot=001 attempt=1" in captured.err
    assert "[cohort] cohort_complete" in captured.err

    ledger = work / "slot-001-attempt-001/pair/pilot.jsonl"
    write_row(ledger, {"record_type": "arm_started", "arm": "raw-mythic-bloodhound",
                       "event": {"message": "secret-output"}})
    assert driver._ledger_stage("run-pair", ledger.parent) == "raw-measured-arm-running"
    write_row(ledger, {"record_type": "progress", "arm": []})
    with ledger.open("ab") as stream:
        stream.write(b'{"record_type": "progress", "arm": "partial')
    assert driver._ledger_stage("run-pair", ledger.parent) == "measured-arm-running"
    assert "secret-output" not in captured.err
    assert "objective result" in captured.err


@pytest.mark.parametrize("error,expected", [(FileNotFoundError(2, "secret detail"), 1), (KeyboardInterrupt(), 130)])
def test_launch_failure_and_interrupt_leave_safe_handoff(tmp_path, monkeypatch, error, expected):
    path, work = plan_file(tmp_path)
    monkeypatch.setattr(driver, "ROOT", tmp_path)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(driver, "_invoke", fail)
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == expected
    log = (work / driver.LOG_NAME).read_text()
    assert "secret detail" not in log
    assert "interrupted" in log or "failure" in log


def test_orphan_artifacts_are_not_adopted_into_new_plan(tmp_path, monkeypatch):
    path, work = plan_file(tmp_path)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    (work / "slot-001-attempt-001").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls))
    assert driver.main(["run", "--plan", str(path), "--work-dir", str(work)]) == 1
    assert not calls


def test_commands_parse_through_real_pair_cli(tmp_path):
    spec = importlib.util.spec_from_file_location("cohort_pair_cli", driver.PAIR_CLI)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    path, work = plan_file(tmp_path)
    plan = driver.load_plan(path)
    slot = plan["slots"][0]
    slot["case_descriptor"] = str(tmp_path / "case.json")
    attempt = slot["attempts"][0]
    attempt["measurement_metadata"] = str(tmp_path / "metadata.json")
    parser = cli.build_parser()
    for phase in ("matrix-preflight", "rehearse-setup", "continue-setup", "run-pair"):
        command = driver._cli(phase, plan, slot, attempt, work)
        parsed = parser.parse_args(command[2:])
        assert parsed.route_id == plan["route_id"]
        assert parsed.contract_file == Path(plan["contract_file"])
        if phase != "matrix-preflight":
            assert parsed.arm_order == slot["arm_order"]
            assert parsed.live_authority_file == attempt["live_authority_file"]


def test_opted_in_pair_resume_preserves_attempt_and_forwards_flag(tmp_path, monkeypatch):
    path, work = plan_file(tmp_path)
    value = json.loads(path.read_text())
    value["same_arm_recovery"] = True
    path.write_text(json.dumps(value))
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(driver, "_invoke", fake_producer(calls, stop_at="pair"))
    args = ["run", "--plan", str(path), "--work-dir", str(work)]
    assert driver.main(args) == 1
    old = work / "slot-001-attempt-001/pair/pilot.jsonl"
    original = old.read_bytes()
    normal = fake_producer(calls)

    def resume(argv, fd, *, progress=None):
        if argv[2] == "run-pair":
            assert "--resume" in argv and "--resumable" in argv
            write_row(old, {"record_type": "comparison", "run_complete": True})
            return 0, {"run_complete": True}
        return normal(argv, fd, progress=progress)

    monkeypatch.setattr(driver, "_invoke", resume)
    assert driver.main(args) == 0
    assert old.read_bytes().startswith(original)
    assert not (work / "slot-001-attempt-002").exists()
    value["same_arm_recovery"] = False
    path.write_text(json.dumps(value))
    assert driver.main(args) == 1
