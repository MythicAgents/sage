"""Offline resume checks at the pair producer's durable effect boundaries."""
from dataclasses import replace
import subprocess
import sys

import pytest

from ai.hillclimb import trust_walker_pilot as pilot
from ai.hillclimb.trust_walker_outcome import PROOF_CONTRACT, SNAPSHOT_SCHEMA


@pytest.fixture(autouse=True)
def isolate_rejected_kernel_module(monkeypatch):
    # Other suites intentionally import the rejected kernel to test its quarantine.
    # Exercise recovery with the same clean-process boundary as the existing pilot tests.
    for name in tuple(sys.modules):
        if name == "development_row_kernel" or name.endswith(".development_row_kernel"):
            monkeypatch.delitem(sys.modules, name)


def _start():
    return {
        "snapshot": pilot.SNAPSHOT, "range_profile": pilot.RANGE_PROFILE,
        "inventory": list(pilot.CANONICAL_INVENTORY), "clock_ready": True,
        "mythic_clean": True, "bloodhound_clean": True,
        "foothold": {"count": 1, "principal": r"NORTH\samwell.tarly", "host": "CASTELBLACK", "payload_type": "apollo"},
        "route": {"provider": pilot.PROVIDER, "model": pilot.MODEL, "endpoint_identity": "route-a"},
        "objective_sha256": pilot.PilotContract().to_record()["objective_sha256"],
        "source_identity": "source-a", "tool_contract": "tools-a", "dependency_identity": "deps-a",
    }


def _snapshot():
    return {
        "schema": SNAPSHOT_SCHEMA, "operation_id": 8, "target": "essos.local",
        "proof_contract": PROOF_CONTRACT,
        "arm_started_at": "2026-09-13T12:00:00+00:00",
        "arm_deadline_at": "2026-09-13T12:45:00+00:00",
        "observed_at": "2026-09-13T12:46:00+00:00",
        "observer_complete": {"tasks": True, "credentials": True, "callbacks": True, "bloodhound": True},
        "tasks": [], "task_accounting": {"limit": 100, "reservations": 0, "confirmed_task_ids": []},
        "proof_candidates": [],
    }


def _deps(events, *, crash_at=None):
    crashed = set()

    def mark(stage, arm):
        events.append((stage, arm))
        if crash_at == (stage, arm) and stage not in crashed:
            crashed.add(stage)
            raise KeyboardInterrupt("simulated process loss")

    def preflight(_contract):
        events.append(("preflight", None))
        return {"ready": True}

    def reset(arm, _environment):
        mark("reset", arm)
        return _start()

    def run_arm(arm, _start_value, progress):
        mark("run", arm)
        progress({"phase": "model"})
        return {"termination": "done", "protocol_error": False,
                "resource_complete": True, "cost": {"total_tokens": 10}}

    def resume_arm(arm, _start_value, progress, records):
        assert any(row["record_type"] == "producer_intent" for row in records)
        mark("resume", arm)
        return run_arm(arm, _start_value, progress)

    def settle(arm, _start_value, execution):
        mark("settle", arm)
        return {"complete": True, "execution": execution}

    def observe(arm, _start_value, _execution, _settlement):
        mark("observe", arm)
        return _snapshot()

    def teardown(arm, _start_value):
        mark("cleanup", arm)
        return {"complete": True}

    def restore(arm, _start_value, runtime, records):
        assert runtime == {"operation_id": 8, "callback_id": 4}
        assert any(row["record_type"] == "reset_completed" for row in records)
        events.append(("restore", arm))

    return pilot.PilotDependencies(
        preflight=preflight, reset=reset,
        runtime_identity=lambda: {"operation_id": 8, "callback_id": 4},
        run_arm=run_arm, stop_and_settle=settle, observe=observe, teardown=teardown,
        attest_preserved_state=lambda *_args: {"complete": True},
        restore_runtime=restore, resume_arm=resume_arm,
    )


def test_resume_same_arm_after_producer_interruption(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    path = tmp_path / "pair"
    events = []
    contract = pilot.PilotContract()
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(contract, path, _deps(events, crash_at=("run", contract.arm_order[0])), resumable=True)
    records = pilot.read_records(path / "pilot.jsonl")
    assert [row["record_type"] for row in records if row.get("arm") == contract.arm_order[0]][:4] == [
        "attempted_start", "reset_intent", "reset_completed", "arm_started",
    ]
    assert not any(row["record_type"] == "comparison" for row in records)
    result = pilot.run_pair(contract, path, _deps(events), resume=True)
    assert result["run_complete"] is True
    assert events.count(("reset", contract.arm_order[0])) == 1
    assert events.count(("resume", contract.arm_order[0])) == 1
    assert events.count(("run", contract.arm_order[1])) == 1
    assert len([row for row in pilot.read_records(path / "pilot.jsonl") if row["record_type"] == "arm_result"]) == 2


def test_resume_reuses_cleaned_first_arm(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    path = tmp_path / "pair"
    contract = pilot.PilotContract()
    events = []
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(contract, path, _deps(events, crash_at=("reset", contract.arm_order[1])), resumable=True)
    before = list(events)
    with pytest.raises(pilot.PilotError, match="reset outcome is uncertain"):
        pilot.run_pair(contract, path, _deps(events), resume=True)
    assert events[len(before):] == [("preflight", None)]
    assert len([row for row in pilot.read_records(path / "pilot.jsonl") if row["record_type"] == "arm_result"]) == 1


def test_resume_second_arm_without_rerunning_cleaned_first(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    path = tmp_path / "pair"
    contract = pilot.PilotContract()
    events = []
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(contract, path, _deps(events, crash_at=("run", contract.arm_order[1])), resumable=True)
    assert events.count(("cleanup", contract.arm_order[0])) == 1
    result = pilot.run_pair(contract, path, _deps(events), resume=True)
    assert result["run_complete"] is True
    assert events.count(("reset", contract.arm_order[0])) == 1
    assert events.count(("run", contract.arm_order[0])) == 1
    assert events.count(("cleanup", contract.arm_order[0])) == 1
    assert events.count(("resume", contract.arm_order[1])) == 1


def test_execution_checkpoint_prevents_second_producer_call(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    path = tmp_path / "pair"
    events = []
    original = pilot.append_record
    interrupted = False

    def crash_after_execution(ledger, row):
        nonlocal interrupted
        original(ledger, row)
        if row.get("record_type") == "execution_returned" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("after durable execution return")

    monkeypatch.setattr(pilot, "append_record", crash_after_execution)
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(pilot.PilotContract(), path, _deps(events), resumable=True)
    result = pilot.run_pair(pilot.PilotContract(), path, _deps(events), resume=True)
    assert result["run_complete"] is True
    assert events.count(("run", pilot.ARM_ORDER[0])) == 1
    assert events.count(("resume", pilot.ARM_ORDER[0])) == 0


def test_reset_checkpoint_resumes_before_producer_start(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    path = tmp_path / "pair"
    events = []
    original = pilot.append_record
    interrupted = False

    def crash_after_reset(ledger, row):
        nonlocal interrupted
        original(ledger, row)
        if row.get("record_type") == "reset_completed" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("after durable reset")

    monkeypatch.setattr(pilot, "append_record", crash_after_reset)
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(pilot.PilotContract(), path, _deps(events), resumable=True)
    result = pilot.run_pair(pilot.PilotContract(), path, _deps(events), resume=True)
    assert result["run_complete"] is True
    assert events.count(("reset", pilot.ARM_ORDER[0])) == 1
    assert events.count(("run", pilot.ARM_ORDER[0])) == 1
    assert events.count(("resume", pilot.ARM_ORDER[0])) == 0


@pytest.mark.parametrize("seam", ["restore_runtime", "resume_arm"])
def test_recovery_seam_failure_pauses_without_terminal_records(tmp_path, monkeypatch, seam):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    path = tmp_path / seam
    events = []
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(pilot.PilotContract(), path,
                       _deps(events, crash_at=("run", pilot.ARM_ORDER[0])), resumable=True)
    deps = _deps(events)

    def blocked(*_args):
        raise RuntimeError("requires reconciliation")

    setattr(deps, seam, blocked)
    with pytest.raises(pilot.PairRecoveryBlocked, match="requires reconciliation"):
        pilot.run_pair(pilot.PilotContract(), path, deps, resume=True)
    kinds = [row["record_type"] for row in pilot.read_records(path / "pilot.jsonl")]
    assert "arm_error" not in kinds
    assert "settlement" not in kinds
    assert "cleanup" not in kinds
    assert "comparison" not in kinds


def test_missing_recovery_seams_stop_before_effects(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    path = tmp_path / "pair"
    events = []
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(pilot.PilotContract(), path,
                       _deps(events, crash_at=("run", pilot.ARM_ORDER[0])), resumable=True)
    deps = _deps(events)
    deps.restore_runtime = None
    deps.resume_arm = None
    with pytest.raises(pilot.PilotError, match="restore_runtime dependency"):
        pilot.run_pair(pilot.PilotContract(), path, deps, resume=True)
    assert events[-1] == ("preflight", None)
    deps.restore_runtime = lambda *_args: None
    with pytest.raises(pilot.PilotError, match="resume_arm dependency"):
        pilot.run_pair(pilot.PilotContract(), path, deps, resume=True)
    assert events[-1] == ("preflight", None)


def test_resume_lock_blocks_independent_process_before_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    path = tmp_path / "pair"
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(pilot.PilotContract(), path,
                       _deps([], crash_at=("run", pilot.ARM_ORDER[0])), resumable=True)
    lock_path = pilot._pair_lock_path(path.resolve())
    script = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "sys.stdin.buffer.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(lock_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == b"locked\n"
        events = []
        with pytest.raises(pilot.PairRecoveryBlocked, match="active producer or resume"):
            pilot.run_pair(pilot.PilotContract(), path, _deps(events), resume=True)
        assert events == []
    finally:
        assert child.stdin is not None
        child.stdin.write(b"\n")
        child.stdin.flush()
        child.communicate(timeout=5)


@pytest.mark.parametrize("stage", ["settle", "observe", "cleanup"])
def test_resume_refuses_uncertain_effect_without_repeating_it(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    path = tmp_path / stage
    arm = pilot.ARM_ORDER[0]
    events = []
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(pilot.PilotContract(), path, _deps(events, crash_at=(stage, arm)), resumable=True)
    before = list(events)
    with pytest.raises(pilot.PilotError, match="uncertain"):
        pilot.run_pair(pilot.PilotContract(), path, _deps(events), resume=True)
    assert events[len(before):] == [("preflight", None)]
    assert events.count((stage, arm)) == 1


def test_resume_requires_origin_and_exact_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    path = tmp_path / "legacy"
    pilot.run_pair(pilot.PilotContract(), path, _deps([]))
    with pytest.raises(pilot.PilotError, match="resumable checkpoints"):
        pilot.run_pair(pilot.PilotContract(), path, _deps([]), resume=True)
    path = tmp_path / "bound"
    events = []
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(pilot.PilotContract(), path, _deps(events, crash_at=("run", pilot.ARM_ORDER[0])), resumable=True)
    with pytest.raises(pilot.PilotError, match="contract differs"):
        pilot.run_pair(replace(pilot.PilotContract(), arm_order=tuple(reversed(pilot.ARM_ORDER))), path, _deps(events), resume=True)
    assert events[-1] == ("run", pilot.ARM_ORDER[0])
