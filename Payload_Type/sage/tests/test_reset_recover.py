"""Offline guards for the interrupted-reset recovery boundary."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ai.hillclimb import trust_walker_pilot as pilot
from ai.hillclimb.trust_walker_outcome import PROOF_CONTRACT, SNAPSHOT_SCHEMA


SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-eval-gauge/scripts/reset_recover.py"
SPEC = importlib.util.spec_from_file_location("reset_recover_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
recover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recover)


def fixture_rows():
    contract = SimpleNamespace(source_identity="source-a", alternate_start=None,
                               arm_order=("shipping-sage", "raw-mythic-bloodhound"))
    records = [
        {"record_type": "contract", "contract": {"schema": "matrix"}},
        {"record_type": "resumable_origin", "schema": "sage-pair-checkpoint-v1"},
        {"record_type": "arm_started", "arm": "shipping-sage", "start_identity": {}},
        {"record_type": "arm_result", "arm": "shipping-sage"},
        {"record_type": "cleanup", "arm": "shipping-sage", "complete": True},
        {"record_type": "attempted_start", "arm": "raw-mythic-bloodhound"},
        {"record_type": "reset_intent", "arm": "raw-mythic-bloodhound", "recorded_at": "2026-10-02T13:00:00+00:00"},
    ]
    stages = [{"arm": "raw-mythic-bloodhound", "stage": stage, "status": status}
              for stage in recover.PREFIX for status in ("started", "passed")]
    stages.extend([
        {"arm": "raw-mythic-bloodhound", "stage": "readiness", "status": "started"},
        {"arm": "raw-mythic-bloodhound", "stage": "readiness", "status": "failed",
         "error_class": "SystemExit", "recorded_at": "2026-10-02T13:20:00+00:00"},
    ])
    return contract, records, stages


def validate(contract, records, stages, tmp_path):
    def read_records(path):
        return stages if path.name == "reset-stages.jsonl" else records
    pair = SimpleNamespace(read_records=read_records,
                           _replay_contract=lambda _: contract,
                           _source_identity_at=lambda _: "source-a",
                           MatrixPairContract=type(contract))
    return recover._validate_ledger(pair, tmp_path, tmp_path, None)


def test_accepts_only_failed_final_readiness_after_full_mutation_prefix(tmp_path):
    contract, records, stages = fixture_rows()
    selected, _, _, failed = validate(contract, records, stages, tmp_path)
    assert selected is contract
    assert failed["stage"] == "readiness"
    for damaged in (stages[:-3] + stages[-2:], stages[:-1], stages + [{"arm": "raw-mythic-bloodhound", "stage": "callback-discovery", "status": "passed"}]):
        with pytest.raises(recover.RecoveryError, match="stage sequence"):
            validate(contract, records, damaged, tmp_path)


@pytest.mark.parametrize("kind", ["arm_started", "producer_intent", "reset_completed", "arm_error", "preserved_state"])
def test_rejects_existing_producer_checkpoint_or_terminal_state(tmp_path, kind):
    contract, records, stages = fixture_rows()
    records.append({"record_type": kind, "arm": "raw-mythic-bloodhound"})
    with pytest.raises(recover.RecoveryError):
        validate(contract, records, stages, tmp_path)


def test_rejects_second_recovery_and_wrong_source(tmp_path):
    contract, records, stages = fixture_rows()
    records.append({"record_type": "reset_recovery_intent", "arm": "raw-mythic-bloodhound"})
    with pytest.raises(recover.RecoveryError, match="already been recovered"):
        validate(contract, records, stages, tmp_path)
    records.pop()
    contract.source_identity = "other-source"
    with pytest.raises(recover.RecoveryError, match="measurement source"):
        validate(contract, records, stages, tmp_path)


def _kernel_resume_probe(tmp_path):
    """Exercise the real pair kernel in a fresh interpreter with stubbed effects."""
    pilot._history_root = lambda: tmp_path
    path = tmp_path / "pair"
    events = []
    crash = [True]
    start = {
        "snapshot": pilot.SNAPSHOT, "range_profile": pilot.RANGE_PROFILE,
        "inventory": list(pilot.CANONICAL_INVENTORY), "clock_ready": True,
        "mythic_clean": True, "bloodhound_clean": True,
        "foothold": {"count": 1, "principal": r"NORTH\samwell.tarly",
                    "host": "CASTELBLACK", "payload_type": "apollo"},
        "route": {"provider": pilot.PROVIDER, "model": pilot.MODEL, "endpoint_identity": "route-a"},
        "objective_sha256": pilot.PilotContract().to_record()["objective_sha256"],
        "source_identity": "source-a", "tool_contract": "tools-a", "dependency_identity": "deps-a",
    }
    def reset(arm, _environment):
        events.append(("reset", arm))
        if arm == pilot.ARM_ORDER[1] and crash[0]:
            crash[0] = False
            raise KeyboardInterrupt("simulated lost reset receipt")
        return start
    def run_arm(arm, _start, _progress):
        events.append(("run", arm))
        return {"termination": "done", "protocol_error": False,
                "resource_complete": True, "cost": {"total_tokens": 10}}
    def observe(*_args):
        return {"schema": SNAPSHOT_SCHEMA, "operation_id": 8, "target": "essos.local",
                "proof_contract": PROOF_CONTRACT,
                "arm_started_at": "2026-09-13T12:00:00+00:00",
                "arm_deadline_at": "2026-09-13T12:45:00+00:00",
                "observed_at": "2026-09-13T12:46:00+00:00",
                "observer_complete": {"tasks": True, "credentials": True,
                                      "callbacks": True, "bloodhound": True},
                "tasks": [], "task_accounting": {"limit": 100, "reservations": 0,
                                                  "confirmed_task_ids": []},
                "proof_candidates": []}
    def dependencies():
        return pilot.PilotDependencies(
            preflight=lambda _: {"ready": True}, reset=reset,
            runtime_identity=lambda: {"operation_id": 8, "callback_id": 4},
            run_arm=run_arm,
            stop_and_settle=lambda _arm, _start, execution: {"complete": True, "execution": execution},
            observe=observe,
            teardown=lambda _arm, _start: {"complete": True},
            attest_preserved_state=lambda *_: {"complete": True},
            restore_runtime=lambda arm, _start, _identity, _records: events.append(("restore", arm)),
            resume_arm=lambda *_: pytest.fail("producer had never started"),
        )
    contract = pilot.PilotContract()
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pair(contract, path, dependencies(), resumable=True)
    before = list(events)
    pilot.append_record(path / "pilot.jsonl", {"record_type": "reset_recovery_intent",
                    "arm": pilot.ARM_ORDER[1], "schema": "sage-reset-recovery-v1"})
    pilot.append_record(path / "pilot.jsonl", {"record_type": "reset_recovery_audit",
                    "arm": pilot.ARM_ORDER[1], "complete": True, "schema": "sage-reset-recovery-v1"})
    pilot.append_record(path / "pilot.jsonl", {"record_type": "reset_completed",
                    "arm": pilot.ARM_ORDER[1], "arm_index": 1, "start": start,
                    "runtime_identity": {"operation_id": 8, "callback_id": 4},
                    "setup_cleanup_elapsed_seconds": 0.0,
                    "recovered_from_audit": "sage-reset-recovery-v1"})
    result = pilot.run_pair(contract, path, dependencies(), resume=True)
    assert result["run_complete"] is True
    assert events[len(before):].count(("reset", pilot.ARM_ORDER[0])) == 0
    assert events[len(before):].count(("reset", pilot.ARM_ORDER[1])) == 0
    assert events[len(before):].count(("run", pilot.ARM_ORDER[0])) == 0
    assert events[len(before):].count(("run", pilot.ARM_ORDER[1])) == 1


def test_recovered_reset_checkpoint_resumes_only_missing_arm(tmp_path):
    root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONPATH": str(root / "Payload_Type/sage")}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr[-3000:]


@pytest.mark.parametrize("section", ["task", "credential", "callback"])
def test_attestation_rejects_nonempty_operation_or_changed_callback(tmp_path, monkeypatch, section):
    async def login():
        return SimpleNamespace(current_operation_id=8)
    async def query(*_args, **_kwargs):
        return {"chat_request": []}
    monkeypatch.setitem(sys.modules, "mythic", SimpleNamespace(mythic=SimpleNamespace(execute_custom_query=query)))
    callback = {"display_id": 4, "active": True, "domain": "NORTH", "user": "samwell.tarly",
                "host": "CASTELBLACK", "payload": {"payloadtype": {"name": "apollo"}}}
    snapshot = {"task": [{}] if section == "task" else [],
                "credential": [{}] if section == "credential" else [],
                "callback": [callback]}
    readiness = {"ready": True, "foothold": {"callbacks": [
        {"display_id": 5 if section == "callback" else 4, "payloadtype": "apollo"}]},
        "ludus": {"range_profile": {"id": "goad-trust-walker",
                                      "qualification": {"state": "live_qualified"},
                                      "scenarios": [recover.__name__]}, "vms": []}}
    pair = SimpleNamespace(
        _validate_protected_at=lambda *_: None,
        _fixture_at=lambda *_: {},
        _validate_external_paths=lambda: {},
        _validate_route_smoke=lambda *_: {},
        _select_unique_active_callback=lambda callbacks, callback_id: callbacks[0],
        live_seams=SimpleNamespace(bloodhound_empty_state_evidence=lambda: {"returncode": 0, "domain_count": 0}),
    )
    runtime = SimpleNamespace(protected_manifest=tmp_path / "manifest", source_root=tmp_path,
                              _staged_interactive_foothold=lambda: None,
                              route_smoke=tmp_path / "smoke", _configured_route=lambda: {},
                              native=SimpleNamespace(login=login),
                              _post_reset_readiness=lambda _: readiness,
                              _operation_snapshot=lambda *_: snapshot)
    contract = SimpleNamespace(route=SimpleNamespace(), alternate_start=None,
                               arm_order=("shipping-sage", "raw-mythic-bloodhound"))
    with pytest.raises(recover.RecoveryError, match="tasks or credentials|callback differs"):
        recover._attest(pair, runtime, contract, {}, {}, {})


def test_recovery_writer_appends_only_after_successful_attestation(tmp_path, monkeypatch):
    root = tmp_path / "run/slot-001-attempt-001/pair"
    root.mkdir(parents=True)
    (tmp_path / "run/.driver.lock").touch()
    lock_path = tmp_path / "pair.lock"
    records = [
        {"record_type": "preflight", "live_authority": {"authority_id": "a", "authority_sha256": "h"},
         "protected_source": {"manifest_sha256": "hash"}, "route_smoke": {"sha256": "hash"}},
        {"record_type": "arm_started", "arm": "shipping-sage", "start_identity": {"source_identity": "source-a"}},
        {"record_type": "cleanup", "arm": "shipping-sage", "setup_cleanup_elapsed_seconds": 20.0},
    ]
    contract = SimpleNamespace(source_identity="source-a", arm_order=("shipping-sage", "raw-mythic-bloodhound"),
                               setup_cleanup_seconds=float("inf"))
    appended = []
    pair = SimpleNamespace(
        validate_artifact_dir=lambda _: root,
        pilot_core=SimpleNamespace(_pair_lock_path=lambda _: lock_path),
        _resolve_live_authority=lambda *_: {"authority_id": "a", "authority_sha256": "h"},
        _sha256=lambda _: "hash",
        _runtime=lambda *_: SimpleNamespace(),
        append_record=lambda _path, row: appended.append(row),
    )
    monkeypatch.setattr(recover, "_load_snapshot", lambda _: pair)
    monkeypatch.setattr(recover, "_validate_ledger", lambda *_: (
        contract, records, {"recorded_at": "2026-10-02T13:00:00+00:00"},
        {"recorded_at": "2026-10-02T13:20:00+00:00"}))
    expected = ({"source_identity": "source-a"}, {"operation_id": 8, "callback_id": 4},
                {"task_count": 0, "credential_count": 0})
    monkeypatch.setattr(recover, "_attest", lambda *_: expected)
    args = (tmp_path, root, tmp_path / "manifest", tmp_path / "smoke", tmp_path / "authority", "operator authorized")
    assert recover.recover(*args, execute=False)["mode"] == "check"
    assert appended == []
    monkeypatch.setattr(recover, "_attest", lambda *_: (_ for _ in ()).throw(recover.RecoveryError("not clean")))
    with pytest.raises(recover.RecoveryError, match="not clean"):
        recover.recover(*args, execute=True)
    assert appended == []
    monkeypatch.setattr(recover, "_attest", lambda *_: expected)
    assert recover.recover(*args, execute=True)["complete"] is True
    assert [row["record_type"] for row in appended] == [
        "reset_recovery_intent", "reset_recovery_audit", "reset_completed"]


def test_readiness_diagnostic_uses_only_known_section_names():
    readiness = {"ready": False, "runtime_identity": {"ready": False, "blockers": ["secret value"]},
                 "bloodhound_api": {"ready": False, "blockers": ["another secret"]}}
    labels = recover._readiness_failure_labels(readiness, {"returncode": 0, "domain_count": 0})
    assert labels == "bloodhound_api, runtime_identity"
    assert "secret" not in labels


if __name__ == "__main__":
    _kernel_resume_probe(Path(sys.argv[1]))
