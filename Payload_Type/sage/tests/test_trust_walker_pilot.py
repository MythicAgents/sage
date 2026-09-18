import json
import sys

import pytest

from ai.hillclimb import effect_registry, trust_walker_pilot as pilot
from ai.hillclimb.range_state import Milestone
from ai.hillclimb.trust_walker_outcome import PROOF_CONTRACT, SNAPSHOT_SCHEMA


@pytest.fixture(autouse=True)
def isolate_rejected_kernel_module():
    names = [
        name for name in tuple(sys.modules)
        if name == "development_row_kernel" or name.endswith(".development_row_kernel")
    ]
    saved = {name: sys.modules.pop(name) for name in names}
    yield
    sys.modules.update(saved)


def start_manifest(**changes):
    value = {
        "snapshot": pilot.SNAPSHOT,
        "range_profile": "goad-trust-walker",
        "inventory": list(pilot.CANONICAL_INVENTORY),
        "clock_ready": True,
        "mythic_clean": True,
        "bloodhound_clean": True,
        "foothold": {
            "count": 1, "principal": r"NORTH\samwell.tarly",
            "host": "CASTELBLACK", "payload_type": "apollo",
        },
        "route": {"provider": pilot.PROVIDER, "model": pilot.MODEL, "endpoint_identity": "route-a"},
        "objective_sha256": pilot.PilotContract().to_record()["objective_sha256"],
        "source_identity": "tree-a",
        "tool_contract": "tools-a",
        "dependency_identity": "deps-a",
    }
    value.update(changes)
    return value


def alternate_binding(**changes):
    values = {
        "schema": pilot.ALTERNATE_START_SCHEMA,
        "case_id": pilot.ALTERNATE_START_CASE,
        "snapshot": pilot.ALTERNATE_START_SNAPSHOT,
        "range_profile": pilot.RANGE_PROFILE,
        "inventory": pilot.CANONICAL_INVENTORY,
        "principal": pilot.ALTERNATE_START_PRINCIPAL,
        "host": pilot.ALTERNATE_START_HOST,
        "host_ip": pilot.ALTERNATE_START_IP,
        "payload_type": "apollo",
        "retained_config_path": "/private/fixture.json",
        "retained_config_sha256": "a" * 64,
        "payload_uuid": "11111111-2222-3333-4444-555555555555",
        "task_name": "SageApolloJorahBootstrap",
        "task_prelaunch_state": "Ready",
        "remote_path": r"C:\Users\Public\apollo.exe",
        "remote_sha256": "b" * 64,
    }
    values.update(changes)
    return pilot.AlternateStartBinding(**values)


def alternate_start_manifest(binding, **changes):
    value = start_manifest(
        snapshot=binding.snapshot,
        foothold={
            "count": 1,
            "principal": binding.principal,
            "host": binding.host,
            "payload_type": binding.payload_type,
        },
    )
    value["case_identity"] = binding.identity
    value.update(changes)
    return value


def terminal_snapshot(disposition="disproven"):
    proof_candidates = []
    return {
        "schema": SNAPSHOT_SCHEMA,
        "operation_id": 8,
        "target": "essos.local",
        "proof_contract": PROOF_CONTRACT,
        "arm_started_at": "2026-09-13T12:00:00+00:00",
        "arm_deadline_at": "2026-09-13T12:45:00+00:00",
        "observed_at": "2026-09-13T12:46:00+00:00",
        "observer_complete": {
            "tasks": True, "credentials": True, "callbacks": True, "bloodhound": True,
        },
        "tasks": [{
            "operation_id": 8, "display_id": 12, "completed": True,
            "created_at": "2026-09-13T12:01:00+00:00",
            "last_update_at": "2026-09-13T12:02:00+00:00",
        }],
        "task_accounting": {"limit": 100, "reservations": 1, "confirmed_task_ids": [12]},
        "proof_candidates": proof_candidates,
    }


def dependencies(events, *, second_start=None, first_cleanup=True):
    def preflight(_contract):
        events.append("preflight")
        return {"ready": True}

    def reset(arm, env):
        events.append(("reset", arm, dict(env)))
        if arm == pilot.ARM_ORDER[1] and second_start is not None:
            return second_start
        return start_manifest()

    def run_arm(arm, _start, progress):
        events.append(("run", arm))
        progress({"phase": "model"})
        return {
            "termination": "done",
            "protocol_error": False,
            "resource_complete": True,
            "cost": {"total_tokens": 10, "payload_tasks": 1},
        }

    def runtime_identity():
        return {"operation_id": 8, "callback_id": 4}

    def settle(arm, _start, execution):
        events.append(("settle", arm))
        return {"complete": True, "execution": execution}

    def observe(arm, _start, _execution, _settlement):
        events.append(("observe", arm))
        return terminal_snapshot()

    def teardown(arm, _start):
        events.append(("cleanup", arm))
        return {"complete": first_cleanup if arm == pilot.ARM_ORDER[0] else True}

    def attest(arm, _start, _execution, stop_reason):
        events.append(("preserve", arm))
        return {
            "schema": "sage-trust-walker-preserved-state-v1",
            "complete": True,
            "stop_reason": dict(stop_reason),
            "sections": {},
            "unknown_sections": [],
            "inspection_instructions": ["inspect"],
        }

    return pilot.PilotDependencies(
        preflight, reset, runtime_identity, run_arm, settle, observe, teardown, attest,
    )


def failing_execution_dependencies(events):
    deps = dependencies(events)

    def fail_arm(arm, _start, _progress):
        events.append(("run", arm))
        raise RuntimeError("assembled runner failed")

    deps.run_arm = fail_arm
    return deps


def test_contract_and_environment_are_fixed():
    contract = pilot.PilotContract()
    assert contract.arm_order == ("shipping-sage", "raw-mythic-bloodhound")
    assert contract.setup_cleanup_seconds == 7200
    assert contract.task_settlement_seconds == 300
    assert contract.recovery_cleanup_attempt_limit == 1
    assert contract.setup_rehearsal_successes == 2
    assert contract.setup_rehearsal_attempt_limit == 4
    assert pilot.arm_environment(contract) == {
        "SAGE_CONTROLLER_WALL_S": "2700",
        "SAGE_EVAL_MODEL_TOKEN_BUDGET": "1000000",
        "SAGE_EVAL_PAYLOAD_TASK_BUDGET": "100",
        "SAGE_AUTONOMOUS_CONTROLLER": "1",
        "SAGE_POLICY_MODE": "llm",
        "SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS": "1",
        "SAGE_EVAL_EXPECTED_RESPONSE_PROVIDER": "openai",
        "SAGE_EVAL_EXPECTED_RESPONSE_MODEL": "bedrock-claude-4-6-sonnet",
    }
    with pytest.raises(pilot.PilotError, match="fixed contract"):
        pilot.PilotContract(wall_seconds=2701).validate()
    reverse = pilot.PilotContract(arm_order=tuple(reversed(pilot.ARM_ORDER)))
    reverse.validate()
    assert pilot.arm_environment(reverse) == pilot.arm_environment(contract)


def test_alternate_start_contract_is_identity_bound_and_default_record_is_unchanged():
    default = pilot.PilotContract().to_record()
    binding = alternate_binding()
    selected = pilot.PilotContract(alternate_start=binding)

    selected.validate()
    assert "alternate_start" not in default
    assert "case_identity" not in default
    assert selected.to_record()["case_identity"] == binding.identity
    assert "retained_config_path" not in json.dumps(selected.to_record())


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"case_id": "another-case"}, "approved Jorah/MEEREEN"),
        ({"snapshot": "another-snapshot"}, "qualified Jorah/MEEREEN snapshot"),
        ({"principal": r"ESSOS\other.user"}, "start semantics"),
        ({"host": "BRAAVOS"}, "start semantics"),
        ({"host_ip": "10.4.10.23"}, "start semantics"),
        ({"retained_config_sha256": "A" * 64}, "canonical"),
        ({"task_prelaunch_state": "Running"}, "incomplete"),
    ],
)
def test_alternate_start_contract_rejects_semantic_and_fixture_drift(change, reason):
    with pytest.raises(pilot.PilotError, match=reason):
        pilot.PilotContract(alternate_start=alternate_binding(**change)).validate()


def test_alternate_start_pair_consumes_the_same_explicit_case(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    binding = alternate_binding()
    contract = pilot.PilotContract(alternate_start=binding)
    events = []
    deps = dependencies(events, second_start=alternate_start_manifest(binding))
    deps.reset = lambda arm, env: (
        events.append(("reset", arm, dict(env))) or alternate_start_manifest(binding)
    )

    result = pilot.run_pair(contract, tmp_path / "alternate", deps)

    assert result["run_complete"] is True
    assert result["pair_compatible"] is True
    assert [event[1] for event in events if isinstance(event, tuple) and event[0] == "run"] == list(contract.arm_order)
    assert pilot.summarize_pair(pilot.read_records(tmp_path / "alternate" / "pilot.jsonl"))["pair_compatible"] is True


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"snapshot": pilot.SNAPSHOT}, "snapshot"),
        ({"foothold": {"count": 1, "principal": r"NORTH\samwell.tarly", "host": "MEEREEN", "payload_type": "apollo"}}, "principal"),
        ({"foothold": {"count": 1, "principal": r"ESSOS\jorah.mormont", "host": "CASTELBLACK", "payload_type": "apollo"}}, "host"),
        ({"case_identity": "0" * 64}, "case identity"),
    ],
)
def test_alternate_start_pair_rejects_wrong_bound_start_before_execution(tmp_path, monkeypatch, change, reason):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    binding = alternate_binding()
    executed = []
    deps = dependencies([], second_start=alternate_start_manifest(binding, **change))
    deps.reset = lambda _arm, _env: alternate_start_manifest(binding, **change)
    deps.run_arm = lambda arm, *_args: executed.append(arm)

    result = pilot.run_pair(
        pilot.PilotContract(alternate_start=binding), tmp_path / f"wrong-{reason}", deps,
    )

    assert executed == []
    assert result["protocol_valid"] is False
    with pytest.raises(pilot.PilotError, match="fixed contract"):
        pilot.PilotContract(arm_order=(pilot.ARM_ORDER[0], pilot.ARM_ORDER[0])).validate()


def test_pair_runs_exactly_two_arms_in_order_and_retains_comparison(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    events = []
    result = pilot.run_pair(pilot.PilotContract(), tmp_path / "attempt", dependencies(events))
    assert [event[1] for event in events if isinstance(event, tuple) and event[0] == "run"] == list(pilot.ARM_ORDER)
    assert result["protocol_valid"] is True
    assert result["comparison_ready"] is True
    assert result["lifecycle_complete"] is True
    assert result["isolation_complete"] is True
    assert result["resources_complete"] is True
    assert result["run_complete"] is True
    records = pilot.read_records(tmp_path / "attempt" / "pilot.jsonl")
    assert [row["arm"] for row in records if row["record_type"] == "attempted_start"] == list(pilot.ARM_ORDER)
    assert [row["outcome"]["disposition"] for row in records if row["record_type"] == "arm_result"] == [
        "disproven", "disproven",
    ]
    for arm in pilot.ARM_ORDER:
        arm_events = [event[0] for event in events if isinstance(event, tuple) and event[1] == arm]
        assert arm_events == ["reset", "run", "settle", "observe", "cleanup"]


def test_objective_inert_unfinished_ps_reaches_teardown_and_next_arm(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    events = []
    deps = dependencies(events)

    def settle(arm, _start, execution):
        events.append(("settle", arm))
        if arm == pilot.ARM_ORDER[0]:
            return {
                "complete": True,
                "producer_terminal": True,
                "task_quiescent": False,
                "objective_effect_quiescent": True,
                "outstanding_task_ids": [13],
                "objective_inert_outstanding_task_ids": [13],
                "execution": execution,
            }
        return {"complete": True, "task_quiescent": True, "execution": execution}

    def observe(arm, _start, _execution, _settlement):
        events.append(("observe", arm))
        value = terminal_snapshot()
        if arm == pilot.ARM_ORDER[0]:
            value["tasks"].append({
                "operation_id": 8,
                "display_id": 13,
                "command_name": "ps",
                "completed": False,
                "created_at": "2026-09-13T12:03:00+00:00",
                "last_update_at": "2026-09-13T12:04:00+00:00",
            })
            value["task_accounting"] = {
                "limit": 100,
                "reservations": 2,
                "confirmed_task_ids": [12, 13],
            }
        return value

    deps.stop_and_settle = settle
    deps.observe = observe
    result = pilot.run_pair(pilot.PilotContract(), tmp_path / "attempt", deps)

    assert result["run_complete"] is True
    assert [event[1] for event in events if isinstance(event, tuple) and event[0] == "run"] == list(
        pilot.ARM_ORDER
    )
    records = pilot.read_records(tmp_path / "attempt" / "pilot.jsonl")
    first_settlement = next(
        row for row in records
        if row["record_type"] == "settlement" and row["arm"] == pilot.ARM_ORDER[0]
    )
    assert first_settlement["task_quiescent"] is False
    assert first_settlement["objective_effect_quiescent"] is True
    assert first_settlement["outstanding_task_ids"] == [13]
    assert [row["arm"] for row in records if row["record_type"] == "cleanup"] == list(pilot.ARM_ORDER)


def test_pair_runs_reverse_order_when_prospectively_selected(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    order = tuple(reversed(pilot.ARM_ORDER))
    events = []
    result = pilot.run_pair(
        pilot.PilotContract(arm_order=order),
        tmp_path / "reverse-attempt",
        dependencies(events),
    )

    assert result["run_complete"] is True
    assert [event[1] for event in events if isinstance(event, tuple) and event[0] == "run"] == list(order)
    records = pilot.read_records(tmp_path / "reverse-attempt" / "pilot.jsonl")
    contract = next(row["contract"] for row in records if row["record_type"] == "contract")
    assert contract["arm_order"] == list(order)


def test_cleanup_failure_retains_first_attempt_and_blocks_second(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    events = []
    result = pilot.run_pair(
        pilot.PilotContract(), tmp_path / "attempt", dependencies(events, first_cleanup=False),
    )
    assert [event[1] for event in events if isinstance(event, tuple) and event[0] == "run"] == [pilot.ARM_ORDER[0]]
    assert ("preserve", pilot.ARM_ORDER[0]) in events
    assert result["protocol_valid"] is False


def test_settlement_failure_preserves_objective_observation_and_blocks_second(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    events = []
    deps = dependencies(events)

    def fail_settle(arm, _start, execution):
        events.append(("settle", arm))
        return {
            "complete": False,
            "producer_terminal": True,
            "task_quiescent": False,
            "outstanding_task_ids": [13],
            "execution": execution,
        }

    def observe_with_proof_and_unrelated_unfinished_task(arm, _start, _execution, _settlement):
        events.append(("observe", arm))
        contract = effect_registry.contract_for(Milestone.OBJECTIVE)
        observation = effect_registry.OracleObservation(
            registry_version=effect_registry.REGISTRY_VERSION,
            oracle_id=contract.oracle_id,
            origin=effect_registry.ProofOrigin.MYTHIC_ARTIFACT,
            proof_kind="remote_exec_callback",
            outcome=True,
            observation_id="callback-22",
            target="essos.local",
        )
        proof = effect_registry.admit_observation(
            Milestone.OBJECTIVE,
            observation,
            expected_target="essos.local",
        ).to_record()
        value = terminal_snapshot()
        value["tasks"].append({
            "operation_id": 8,
            "display_id": 13,
            "completed": False,
            "created_at": "2026-09-13T12:02:00+00:00",
            "last_update_at": "2026-09-13T12:03:00+00:00",
        })
        value["task_accounting"] = {
            "limit": 100,
            "reservations": 2,
            "confirmed_task_ids": [12, 13],
        }
        value["proof_candidates"] = [{
            "operation_id": 8,
            "target": "essos.local",
            "task_ids": [12],
            "effect_at": "2026-09-13T12:02:00+00:00",
            "proof_record": proof,
        }]
        return value

    deps.stop_and_settle = fail_settle
    deps.observe = observe_with_proof_and_unrelated_unfinished_task
    result = pilot.run_pair(pilot.PilotContract(), tmp_path / "attempt", deps)
    assert [event[1] for event in events if isinstance(event, tuple) and event[0] == "run"] == [
        pilot.ARM_ORDER[0]
    ]
    assert ("observe", pilot.ARM_ORDER[0]) in events
    assert ("settle", pilot.ARM_ORDER[0]) in events
    assert ("cleanup", pilot.ARM_ORDER[0]) not in events
    assert ("preserve", pilot.ARM_ORDER[0]) in events
    records = pilot.read_records(tmp_path / "attempt" / "pilot.jsonl")
    arm_result = next(row for row in records if row["record_type"] == "arm_result")
    assert arm_result["outcome"]["disposition"] == "proven"
    arm_report = result["arms"][0]
    assert arm_report["protocol_valid"] is True
    assert arm_report["objective"]["meaning"] == "success"
    assert arm_report["settlement"]["outstanding_task_ids"] == [13]
    assert arm_report["isolation_cleanup"]["state"] == "preserved"
    assert result["protocol_valid"] is False


def test_start_mismatch_is_retained_and_second_arm_does_not_execute(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    events = []
    changed = start_manifest(source_identity="tree-b")
    result = pilot.run_pair(
        pilot.PilotContract(), tmp_path / "attempt", dependencies(events, second_start=changed),
    )
    assert [event[1] for event in events if isinstance(event, tuple) and event[0] == "run"] == [pilot.ARM_ORDER[0]]
    records = pilot.read_records(tmp_path / "attempt" / "pilot.jsonl")
    mismatch = next(row for row in records if row["record_type"] == "start_invalid")
    assert mismatch["differences"] == ["source_identity"]
    assert ("cleanup", pilot.ARM_ORDER[1]) not in events
    assert ("preserve", pilot.ARM_ORDER[1]) in events
    assert result["pair_compatible"] is False


def test_retry_zero_refuses_existing_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    attempt = tmp_path / "attempt"
    events = []
    pilot.run_pair(pilot.PilotContract(), attempt, dependencies(events))
    with pytest.raises(pilot.PilotError, match="retry-zero"):
        pilot.run_pair(pilot.PilotContract(), attempt, dependencies([]))


def test_execution_protocol_error_is_retained_and_blocks_second_arm(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    events = []
    result = pilot.run_pair(
        pilot.PilotContract(), tmp_path / "attempt", failing_execution_dependencies(events),
    )
    assert [event[1] for event in events if isinstance(event, tuple) and event[0] == "run"] == [pilot.ARM_ORDER[0]]
    records = pilot.read_records(tmp_path / "attempt" / "pilot.jsonl")
    arm_result = next(row for row in records if row["record_type"] == "arm_result")
    assert arm_result["execution"]["protocol_error"] is True
    assert [event[0] for event in events if isinstance(event, tuple)] == [
        "reset", "run", "settle", "observe", "preserve",
    ]
    assert result["protocol_valid"] is False


def test_unmeasured_outcome_is_protocol_valid_but_not_comparison_ready():
    records = [
        {"record_type": "arm_started", "arm": arm, "start_identity": {"same": True}}
        for arm in pilot.ARM_ORDER
    ] + [
        {"record_type": "settlement", "arm": arm, "complete": True}
        for arm in pilot.ARM_ORDER
    ] + [
        {"record_type": "cleanup", "arm": arm, "complete": True}
        for arm in pilot.ARM_ORDER
    ] + [
        {
            "record_type": "arm_result", "arm": arm,
            "outcome": {"disposition": "unmeasured" if index == 0 else "disproven"},
            "execution": {"termination": "timeout", "cost": {"total_tokens": 10}},
            "terminal_snapshot": {},
        }
        for index, arm in enumerate(pilot.ARM_ORDER)
    ]
    summary = pilot.summarize_pair(records)
    assert summary["protocol_valid"] is True
    assert summary["comparison_ready"] is False


def test_missing_resource_metric_does_not_erase_interpretable_objective():
    records = [
        {"record_type": "arm_started", "arm": arm, "start_identity": {"same": True}}
        for arm in pilot.ARM_ORDER
    ] + [
        {"record_type": "settlement", "arm": arm, "complete": True}
        for arm in pilot.ARM_ORDER
    ] + [
        {"record_type": "cleanup", "arm": arm, "complete": True}
        for arm in pilot.ARM_ORDER
    ] + [
        {
            "record_type": "arm_result",
            "arm": arm,
            "outcome": {
                "disposition": "proven" if index == 0 else "disproven",
                "meaning": "success" if index == 0 else "not_demonstrated_within_budget",
            },
            "execution": {
                "termination": "done",
                "protocol_error": False,
                "resource_complete": index == 0,
                "cost_error": None if index == 0 else "usage unavailable",
                "cost": {"total_tokens": 10 if index == 0 else None},
            },
            "terminal_snapshot": {},
        }
        for index, arm in enumerate(pilot.ARM_ORDER)
    ]

    summary = pilot.summarize_pair(records)

    assert summary["protocol_valid"] is True
    assert summary["comparison_ready"] is True
    assert summary["resources_complete"] is False
    assert summary["run_complete"] is True
    assert summary["arms"][1]["objective_disposition"] == "disproven"


def test_loaded_rejected_kernel_fails_before_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "ai.hillclimb.development_row_kernel", object())
    with pytest.raises(pilot.PilotError, match="rejected"):
        pilot.run_pair(pilot.PilotContract(), tmp_path / "attempt", dependencies([]))
    assert not (tmp_path / "attempt" / "pilot.jsonl").exists()


def test_artifact_path_outside_private_history_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path / "private")
    with pytest.raises(pilot.PilotError, match="under .sage_history"):
        pilot.validate_artifact_dir(tmp_path / "public")


def test_failed_reset_preserves_state_without_teardown_and_charges_attestation(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    clock = {"now": 0.0}
    monkeypatch.setattr(pilot.time, "monotonic", lambda: clock["now"])
    deps = dependencies([])

    def failed_reset(_arm, _env):
        clock["now"] += 5.0
        raise RuntimeError("reset failed")

    def timed_attestation(_arm, _start, _execution, _reason):
        clock["now"] += 7.0
        return {"schema": "sage-trust-walker-preserved-state-v1", "complete": True}

    deps.reset = failed_reset
    deps.attest_preserved_state = timed_attestation
    pilot.run_pair(pilot.PilotContract(), tmp_path / "attempt", deps)
    records = pilot.read_records(tmp_path / "attempt" / "pilot.jsonl")
    assert not [row for row in records if row["record_type"] == "cleanup"]
    preserved = next(row for row in records if row["record_type"] == "preserved_state")
    assert preserved["setup_cleanup_elapsed_seconds"] == 12.0
    assert preserved["producer_started"] is False


def test_failed_settlement_and_observation_preserve_without_cleanup_and_charge_attestation(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    clock = {"now": 0.0}
    monkeypatch.setattr(pilot.time, "monotonic", lambda: clock["now"])
    deps = dependencies([])
    base_reset, base_attest = deps.reset, deps.attest_preserved_state

    def timed_reset(arm, env):
        clock["now"] += 3.0
        return base_reset(arm, env)

    def failed_settlement(_arm, _start, _execution):
        clock["now"] += 5.0
        raise RuntimeError("settlement failed")

    def timed_attest(arm, start, execution, reason):
        clock["now"] += 7.0
        return base_attest(arm, start, execution, reason)

    deps.reset = timed_reset
    deps.stop_and_settle = failed_settlement
    deps.attest_preserved_state = timed_attest
    pilot.run_pair(pilot.PilotContract(), tmp_path / "attempt-settle", deps)
    records = pilot.read_records(tmp_path / "attempt-settle" / "pilot.jsonl")
    settlement = next(row for row in records if row["record_type"] == "settlement")
    assert settlement["setup_cleanup_elapsed_seconds"] == 8.0
    preserved = next(row for row in records if row["record_type"] == "preserved_state")
    assert preserved["setup_cleanup_elapsed_seconds"] == 15.0
    assert not [row for row in records if row["record_type"] == "cleanup"]

    clock["now"] = 0.0
    deps = dependencies([])
    base_reset, base_settle, base_attest = deps.reset, deps.stop_and_settle, deps.attest_preserved_state

    def timed_reset_again(arm, env):
        clock["now"] += 3.0
        return base_reset(arm, env)

    def timed_settle(arm, start, execution):
        clock["now"] += 2.0
        return base_settle(arm, start, execution)

    def failed_observe(_arm, _start, _execution, _settlement):
        clock["now"] += 5.0
        raise RuntimeError("observer failed")

    def timed_attest_again(arm, start, execution, reason):
        clock["now"] += 7.0
        return base_attest(arm, start, execution, reason)

    deps.reset = timed_reset_again
    deps.stop_and_settle = timed_settle
    deps.observe = failed_observe
    deps.attest_preserved_state = timed_attest_again
    pilot.run_pair(pilot.PilotContract(), tmp_path / "attempt-observe", deps)
    records = pilot.read_records(tmp_path / "attempt-observe" / "pilot.jsonl")
    preserved = next(row for row in records if row["record_type"] == "preserved_state")
    assert preserved["setup_cleanup_elapsed_seconds"] == 17.0
    assert not [row for row in records if row["record_type"] == "cleanup"]


def test_failed_preservation_attestation_never_falls_through_to_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    events = []
    deps = failing_execution_dependencies(events)

    def fail_attestation(*_args):
        events.append(("preserve", pilot.ARM_ORDER[0]))
        raise RuntimeError("observer unavailable")

    deps.attest_preserved_state = fail_attestation
    result = pilot.run_pair(pilot.PilotContract(), tmp_path / "attempt", deps)
    records = pilot.read_records(tmp_path / "attempt" / "pilot.jsonl")
    preserved = next(row for row in records if row["record_type"] == "preserved_state")

    assert preserved["attestation"]["complete"] is False
    assert preserved["attestation"]["unknown_reason"] == "attestation failed"
    assert ("cleanup", pilot.ARM_ORDER[0]) not in events
    assert result["protocol_valid"] is False


def test_support_limit_crossed_by_authorized_teardown_is_explicit_without_preservation(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    clock = {"now": 0.0}
    monkeypatch.setattr(pilot.time, "monotonic", lambda: clock["now"])
    events = []
    deps = dependencies(events)
    base_reset, base_teardown = deps.reset, deps.teardown

    def slow_reset(arm, env):
        clock["now"] += 7_190.0
        return base_reset(arm, env)

    def slow_teardown(arm, start):
        clock["now"] += 20.0
        return base_teardown(arm, start)

    deps.reset = slow_reset
    deps.teardown = slow_teardown
    result = pilot.run_pair(pilot.PilotContract(), tmp_path / "attempt", deps)
    records = pilot.read_records(tmp_path / "attempt" / "pilot.jsonl")
    budget = next(row for row in records if row["record_type"] == "support_budget_exhausted")

    assert budget["during"] == "normal_teardown"
    assert budget["cleanup_already_attempted"] is True
    assert not [row for row in records if row["record_type"] == "preserved_state"]
    assert events[-1] == ("cleanup", pilot.ARM_ORDER[0])
    assert result["protocol_valid"] is False


def test_improvement_contract_is_shipping_only_and_identity_bound():
    contract = pilot.ImprovementContract("a" * 64, "b" * 64)

    contract.validate()
    assert contract.arm_order == ("baseline-sage", "candidate-sage")
    assert contract.source_identity_for_arm("baseline-sage") == "a" * 64
    assert contract.source_identity_for_arm("candidate-sage") == "b" * 64
    assert contract.to_record()["primary_metric"]["candidate_threshold"] == {
        "avoidable_pkinit_compatibility_error_tasks": 0,
        "certificate_auth_transaction_tasks": 5,
    }
    pilot.ImprovementContract(
        "a" * 64, "b" * 64, arm_order=tuple(reversed(pilot.IMPROVEMENT_ARM_ORDER)),
    ).validate()
    with pytest.raises(pilot.PilotError, match="must differ"):
        pilot.ImprovementContract("a" * 64, "a" * 64).validate()
    with pytest.raises(pilot.PilotError, match="pair order"):
        pilot.ImprovementContract(
            "a" * 64, "b" * 64, arm_order=("baseline-sage", "baseline-sage"),
        ).validate()


def test_improvement_pair_allows_only_the_frozen_source_difference(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    contract = pilot.ImprovementContract("a" * 64, "b" * 64)
    events = []

    def reset(arm, _env):
        events.append(("reset", arm))
        identity = contract.source_identity_for_arm(arm)
        return start_manifest(source_identity=identity)

    def run_arm(arm, _start, _progress):
        events.append(("run", arm))
        return {
            "termination": "done", "protocol_error": False, "resource_complete": True,
            "cost": {"total_tokens": 10, "payload_tasks": 5},
        }

    def observe(arm, *_args):
        value = terminal_snapshot()
        value["certificate_auth_primary_metric"] = {
            "complete": True,
            "avoidable_pkinit_compatibility_error_tasks": 1 if arm == "baseline-sage" else 0,
            "certificate_auth_transaction_tasks": 6 if arm == "baseline-sage" else 5,
        }
        return value

    deps = pilot.PilotDependencies(
        lambda _contract: {"ready": True},
        reset,
        lambda: {"operation_id": 8, "callback_id": 4},
        run_arm,
        lambda _arm, _start, execution: {"complete": True, "execution": execution},
        observe,
        lambda arm, _start: {"complete": True, "arm": arm},
        lambda *_args: {"complete": True},
    )

    result = pilot.run_pair(contract, tmp_path / "improvement", deps)

    assert result["pair_compatible"] is True
    assert result["run_complete"] is True
    assert result["primary_metric"]["meaningful_effect_met"] is True
    assert [event for event in events if event[0] == "run"] == [
        ("run", "baseline-sage"), ("run", "candidate-sage"),
    ]


def test_improvement_pair_rejects_wrong_arm_source_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_history_root", lambda: tmp_path)
    contract = pilot.ImprovementContract("a" * 64, "b" * 64)
    executed = []
    deps = pilot.PilotDependencies(
        lambda _contract: {"ready": True},
        lambda _arm, _env: start_manifest(source_identity="c" * 64),
        lambda: {"operation_id": 8, "callback_id": 4},
        lambda arm, *_args: executed.append(arm),
        lambda *_args: {"complete": True},
        lambda *_args: terminal_snapshot(),
        lambda *_args: {"complete": True},
        lambda *_args: {"complete": True},
    )

    result = pilot.run_pair(contract, tmp_path / "wrong-source", deps)

    assert executed == []
    assert result["protocol_valid"] is False
    records = pilot.read_records(tmp_path / "wrong-source" / "pilot.jsonl")
    invalid = next(row for row in records if row["record_type"] == "start_invalid")
    assert invalid["reason"] == "arm source identity mismatch"
