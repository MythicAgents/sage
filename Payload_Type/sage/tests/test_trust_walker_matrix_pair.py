"""Offline contract checks for a selected model-matrix pair."""
from dataclasses import replace
import hashlib
import json
import sys

import pytest

from ai.hillclimb import trust_walker_pilot as pilot


@pytest.fixture(autouse=True)
def isolate_rejected_kernel_module():
    names = [
        name for name in tuple(sys.modules)
        if name == "development_row_kernel" or name.endswith(".development_row_kernel")
    ]
    saved = {name: sys.modules.pop(name) for name in names}
    yield
    sys.modules.update(saved)


@pytest.fixture
def matrix():
    return pilot.MatrixContract(
        schema=pilot.MATRIX_SCHEMA,
        routes=(pilot.MatrixRoute(
            route_id="qwen-openrouter", provider="openai", model="qwen/qwen3-32b",
            api_endpoint="https://openrouter.ai/api/v1",
        ),),
    )


def _contract(matrix):
    return pilot.MatrixPairContract.for_route(matrix, "qwen-openrouter", start_case="samwell-castelblack")


def _start(contract, **changes):
    row = {
        "snapshot": pilot.SNAPSHOT,
        "range_profile": pilot.RANGE_PROFILE,
        "inventory": list(pilot.CANONICAL_INVENTORY),
        "clock_ready": True,
        "mythic_clean": True,
        "bloodhound_clean": True,
        "foothold": {"count": 1, "principal": r"NORTH\samwell.tarly", "host": "CASTELBLACK", "payload_type": "apollo"},
        "route": {"provider": contract.provider, "model": contract.model, "endpoint_identity": contract.route.endpoint_identity},
        "objective_sha256": hashlib.sha256(contract.objective.encode()).hexdigest(),
        "source_identity": "source-a", "tool_contract": "tools-a", "dependency_identity": "deps-a",
    }
    row.update(changes)
    return row


def test_selected_route_contract_is_nonsecret_and_stable(matrix):
    contract = _contract(matrix)
    record = contract.to_record()
    assert record["schema"] == pilot.MATRIX_PAIR_SCHEMA
    assert record["matrix_identity"] == matrix.identity
    assert record["termination_policy"]["numeric_wall_token_task_ceilings"] is None
    assert json.dumps(record, sort_keys=True) == json.dumps(contract.to_record(), sort_keys=True)
    assert len(contract.identity) == 64
    assert "api_key" not in json.dumps(record)
    assert pilot.arm_environment(contract)["SAGE_EVAL_MODEL_TOKEN_BUDGET"] == "0"
    assert pilot.arm_environment(contract)["SAGE_EVAL_PAYLOAD_TASK_BUDGET"] == ""
    assert "SAGE_CONTROLLER_WALL_S" not in pilot.arm_environment(contract)


def test_bounded_matrix_contract_keeps_token_and_task_uncapped(matrix):
    legacy = _contract(matrix)
    bounded = replace(legacy, wall_seconds=3600)
    assert bounded.identity != legacy.identity
    assert bounded.to_record()["termination_policy"]["terminal_conditions"][-1] == "time_limit"
    assert bounded.to_record()["termination_policy"]["numeric_wall_token_task_ceilings"] == {
        "wall_seconds": 3600, "token_ceiling": None, "payload_task_limit": None}
    environment = pilot.arm_environment(bounded)
    assert environment["SAGE_CONTROLLER_WALL_S"] == "3600"
    assert environment["SAGE_EVAL_MODEL_TOKEN_BUDGET"] == "0"
    assert environment["SAGE_EVAL_PAYLOAD_TASK_BUDGET"] == ""
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(pilot.PilotError, match="wall_seconds"):
            replace(legacy, wall_seconds=invalid).validate()


def test_retry_contract_changes_identity_and_requires_a_bounded_arm(matrix):
    from ai.provider_retry import parse_policy
    legacy = _contract(matrix)
    policy = parse_policy("transient-v1").to_record()
    with pytest.raises(pilot.PilotError, match="bounded"):
        replace(legacy, provider_retry_policy=policy).validate()
    selected = replace(legacy, wall_seconds=3600, provider_retry_policy=policy)
    assert selected.identity != replace(legacy, wall_seconds=3600).identity
    assert "provider_retry_policy" not in legacy.to_record()
    assert pilot.arm_environment(selected)["SAGE_PROVIDER_RETRY_POLICY"] == "transient-v1"
    assert pilot.arm_environment(legacy)["SAGE_PROVIDER_RETRY_POLICY"] == "off"
    altered = dict(policy, max_retries_per_request=4)
    with pytest.raises(pilot.PilotError, match="provider_retry_policy"):
        replace(selected, provider_retry_policy=altered).validate()


def test_matrix_route_and_start_binding_reject_wrong_identity(matrix):
    contract = _contract(matrix)
    assert pilot.matched_start_identity(_start(contract), matrix_pair=contract)["route"]["model"] == contract.model
    with pytest.raises(pilot.PilotError, match="unknown matrix route_id"):
        pilot.MatrixPairContract.for_route(matrix, "unknown", start_case="samwell-castelblack")
    with pytest.raises(pilot.PilotError, match="selected catalog"):
        replace(contract, matrix_identity="0" * 64).validate(matrix)
    with pytest.raises(pilot.PilotError, match="model route mismatch"):
        pilot.matched_start_identity(_start(contract, route={
            "provider": "openai", "model": pilot.MODEL,
            "endpoint_identity": contract.route.endpoint_identity,
        }), matrix_pair=contract)
    with pytest.raises(pilot.PilotError, match="endpoint identity mismatch"):
        pilot.matched_start_identity(_start(contract, route={
            "provider": contract.provider, "model": contract.model,
            "endpoint_identity": "wrong",
        }), matrix_pair=contract)


def test_matrix_pair_start_source_binding_and_matching(matrix):
    contract = replace(
        _contract(matrix), source_identity="source-a", tool_contract="tools-a",
        dependency_identity="deps-a",
    )
    assert pilot.compare_start_manifests(_start(contract), _start(contract), matrix_pair=contract) == (True, [])
    with pytest.raises(pilot.PilotError, match="source_identity mismatch"):
        pilot.matched_start_identity(_start(contract, source_identity="source-b"), matrix_pair=contract)
    assert pilot.PilotContract().to_record()["schema"] == pilot.PILOT_SCHEMA


def test_matrix_pair_requires_exact_case_and_objective(matrix):
    contract = _contract(matrix)
    for modified in (
        replace(contract, start_case="elsewhere"),
        replace(contract, start_case=pilot.ALTERNATE_START_CASE),
        replace(contract, objective="different objective"),
    ):
        with pytest.raises(pilot.PilotError):
            modified.validate()


def test_matrix_pair_coordinator_stops_before_reset_on_failed_preflight(matrix, monkeypatch, tmp_path):
    contract = _contract(matrix)
    monkeypatch.setattr(pilot, "validate_artifact_dir", lambda _path: tmp_path)
    calls = []

    def preflight(selected):
        calls.append(("preflight", selected.route.route_id))
        return {"ready": False}

    def unexpected(*_args):
        calls.append(("unexpected",))
        raise AssertionError("reset or producer should not run")

    dependencies = pilot.PilotDependencies(
        preflight=preflight, reset=unexpected, runtime_identity=unexpected,
        run_arm=unexpected, stop_and_settle=unexpected, observe=unexpected,
        teardown=unexpected, attest_preserved_state=unexpected,
    )
    with pytest.raises(pilot.PilotError, match="preflight did not pass"):
        pilot.run_pair(contract, tmp_path, dependencies)
    assert calls == [("preflight", "qwen-openrouter")]
    retained = pilot.read_records(tmp_path / "pilot.jsonl")
    assert retained[0]["contract"]["schema"] == pilot.MATRIX_PAIR_SCHEMA
