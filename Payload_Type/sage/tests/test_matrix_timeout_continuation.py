"""Offline producer checks for prospectively bound timeout continuation."""

import hashlib
import json
from pathlib import Path
import sys

import pytest

from ai.hillclimb import trust_walker_pilot as pilot
from .test_trust_walker_matrix_pair import _start
from .test_trust_walker_pilot import terminal_snapshot


@pytest.fixture(autouse=True)
def isolate_rejected_kernel_module():
    names = [
        name for name in tuple(sys.modules)
        if name == "development_row_kernel" or name.endswith(".development_row_kernel")
    ]
    saved = {name: sys.modules.pop(name) for name in names}
    yield
    sys.modules.update(saved)


def _pair(tmp_path, monkeypatch, *, policy=True, fault=None, artifact_root=None):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    root = artifact_root or tmp_path / "pair"
    matrix = pilot.MatrixContract(schema=pilot.MATRIX_SCHEMA, routes=(
        pilot.MatrixRoute("test-route", "openai", "test-model", "https://example.invalid/api/v1"),
    ))
    contract = pilot.MatrixPairContract.for_route(
        matrix, "test-route", start_case="samwell-castelblack", wall_seconds=3600,
        continue_after_time_limit=policy,
    )
    events = []

    def run(arm, _start, _progress):
        events.append(("run", arm))
        termination = "time_limit" if arm == "shipping-sage" else {"cause": "wall_time", "subtype": "time_limit"}
        if fault == "provider_error":
            termination = "provider_error"
        return {"termination": termination, "protocol_error": False,
                "resource_complete": False, "cost_error": "in-flight response", "cost": {"total_tokens": None}}

    def settle(arm, _start, execution):
        events.append(("settle", arm))
        return {"complete": False, "producer_terminal": fault != "producer_active",
                "objective_effect_quiescent": False, "outstanding_task_ids": [13],
                "execution": execution}

    def observe(arm, _start, _execution, _settlement):
        events.append(("observe", arm))
        snapshot = terminal_snapshot()
        snapshot.update({
            "measurement_policy": "bounded-matrix-wall",
            "arm_deadline_at": "2026-09-13T13:00:00+00:00",
            "arm_stopped_at": "2026-09-13T13:00:00+00:00",
            "observed_at": "2026-09-13T13:01:00+00:00",
            "task_accounting": {"limit": None, "reservations": 2, "confirmed_task_ids": [12, 13]},
        })
        snapshot["tasks"].append({
            "operation_id": 8, "display_id": 13, "command_name": "jump_wmi", "completed": False,
            "created_at": "2026-09-13T12:03:00+00:00",
            "last_update_at": "2026-09-13T12:04:00+00:00",
        })
        if fault != "missing_evidence":
            observed = {"task": [
                {"operation_id": 8, "display_id": 12, "responses": []},
                {"operation_id": 8, "display_id": 13, "responses": []},
            ], "credential": [], "callback": []}
            path = root / f"{arm}-native-observation.json"
            data = json.dumps(observed).encode()
            path.write_bytes(data)
            snapshot["native_evidence"] = {
                "path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                "arm": arm, "operation_id": 8, "query_sha256": "0" * 64,
            }
        return snapshot

    def cleanup(arm, _start):
        events.append(("cleanup", arm))
        return {"complete": fault != "cleanup_failed"}

    dependencies = pilot.PilotDependencies(
        preflight=lambda _contract: {"ready": True},
        reset=lambda _arm, _env: _start(contract),
        runtime_identity=lambda: {"operation_id": 8, "callback_id": 4},
        run_arm=run, stop_and_settle=settle, observe=observe, teardown=cleanup,
        attest_preserved_state=lambda *_args: {"complete": True},
    )
    return contract, root, dependencies, events


def test_time_limit_unknown_outcome_cleans_and_processes_next_arm(tmp_path, monkeypatch):
    contract, root, deps, events = _pair(tmp_path, monkeypatch)
    report = pilot.run_pair(contract, root, deps)
    assert report["run_complete"] is True
    assert report["comparison_ready"] is False
    assert report["lifecycle_complete"] is False
    assert report["lifecycle_continuation_complete"] is True
    assert report["isolation_complete"] is True
    assert [event[0] for event in events] == ["run", "settle", "observe", "cleanup"] * 2
    rows = pilot.read_records(root / "pilot.jsonl")
    assert all(row["time_limit_continuation_eligible"] for row in rows if row.get("record_type") == "arm_result")
    assert all(row["outcome"]["disposition"] == "unmeasured" for row in rows if row.get("record_type") == "arm_result")
    assert pilot.summarize_pair(rows, contract, artifact_dir=root) == {k: v for k, v in report.items() if k != "ledger"}
    native = root / "shipping-sage-native-observation.json"
    native.write_text("{}")
    assert pilot.summarize_pair(rows, contract, artifact_dir=root)["run_complete"] is False


@pytest.mark.parametrize("fault", ["provider_error", "producer_active", "missing_evidence", "cleanup_failed"])
def test_timeout_continuation_stops_on_other_error_or_missing_evidence(tmp_path, monkeypatch, fault):
    contract, root, deps, events = _pair(tmp_path, monkeypatch, fault=fault)
    report = pilot.run_pair(contract, root, deps)
    assert report["run_complete"] is False
    assert not any(event == ("run", "raw-mythic-bloodhound") for event in events)
    if fault == "cleanup_failed":
        assert ("cleanup", "shipping-sage") in events
    else:
        assert ("cleanup", "shipping-sage") not in events


def test_legacy_bounded_contract_still_stops_at_unsettled_timeout(tmp_path, monkeypatch):
    contract, root, deps, events = _pair(tmp_path, monkeypatch, policy=False)
    report = pilot.run_pair(contract, root, deps)
    assert "lifecycle_continuation_complete" not in report
    assert report["run_complete"] is False
    assert ("cleanup", "shipping-sage") not in events


def test_proven_timeout_can_be_cleaned_without_changing_proof(tmp_path, monkeypatch):
    contract, root, _deps, _events = _pair(tmp_path, monkeypatch)
    root.mkdir()
    native_path = root / "shipping-sage-native-observation.json"
    native = {"task": [{"operation_id": 8, "display_id": 13}], "credential": [], "callback": []}
    payload = json.dumps(native).encode()
    native_path.write_bytes(payload)
    receipt = {"arm": "shipping-sage", "operation_id": 8, "path": str(native_path),
               "sha256": hashlib.sha256(payload).hexdigest()}
    assert pilot._time_limit_continuation_eligible(
        contract, "shipping-sage", root, {"operation_id": 8},
        {"termination": "time_limit", "protocol_error": False},
        {"complete": False, "producer_terminal": True, "objective_effect_quiescent": False,
         "outstanding_task_ids": [13]},
        {"operation_id": 8, "observer_complete": dict.fromkeys(
            ("tasks", "credentials", "callbacks", "bloodhound"), True),
         "native_evidence": receipt},
        {"disposition": "proven"},
    )


@pytest.mark.parametrize("refusal", [False, True])
def test_driver_advances_to_next_slot_after_producer_timeout(tmp_path, monkeypatch, refusal):
    from .test_matrix_cohort_driver import driver, plan_file, write_row

    plan_path, work = plan_file(tmp_path)
    plan = json.loads(plan_path.read_text())
    plan["continue_after_time_limit"] = True
    plan["continue_after_refusal"] = refusal
    plan["arm_wall_seconds"] = 3600
    next_slot = json.loads(json.dumps(plan["slots"][0]))
    next_slot["slot_id"] = "002"
    plan["slots"].append(next_slot)
    plan_path.write_text(json.dumps(plan))
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    contracts = {}
    phases = []

    def invoke(argv, _lock_fd, *, progress=None):
        phase = argv[2]
        phases.append(phase)
        if phase == "matrix-preflight":
            return 0, {"ready": True}
        artifact = Path(argv[argv.index("--artifact-dir") + 1])
        if phase == "rehearse-setup":
            write_row(artifact / "setup-rehearsals.jsonl", {"record_type": "setup_rehearsal_result", "complete": True})
            return 0, {"complete": True}
        if phase == "run-pair":
            contract, _, deps, _events = _pair(tmp_path, monkeypatch, artifact_root=artifact)
            if refusal:
                contract = pilot.MatrixPairContract(**{**contract.__dict__, "continue_after_refusal": True})
                original_run = deps.run_arm
                deps.run_arm = lambda arm, start, progress: {
                    **original_run(arm, start, progress),
                    "termination": "halted_refusal" if arm == "shipping-sage" else {
                        "cause": "explicit_terminal_decision", "subtype": "refusal"},
                }
            contracts[artifact] = contract
            report = pilot.run_pair(contract, artifact, deps)
            return 0, report
        if phase == "replay-pair":
            summary = pilot.summarize_pair(pilot.read_records(artifact / "pilot.jsonl"), contracts[artifact], artifact_dir=artifact)
            return 0, {"reproduced": True, "comparison": summary}
        raise AssertionError(phase)

    monkeypatch.setattr(driver, "_invoke", invoke)
    assert driver.main(["run", "--plan", str(plan_path), "--work-dir", str(work)]) == 0
    assert phases == ["matrix-preflight"] + ["rehearse-setup", "run-pair", "replay-pair"] * 2
    assert driver._phase_state(work / "slot-002-attempt-001") == "complete"
