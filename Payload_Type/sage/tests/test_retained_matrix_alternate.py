"""Recovery recognizes historical and prospective Jorah contract schemas."""
from copy import deepcopy

import pytest

from ai.hillclimb import trust_walker_pilot as pilot


def alternate_binding():
    return pilot.AlternateStartBinding(
        schema=pilot.ALTERNATE_START_SCHEMA, case_id=pilot.ALTERNATE_START_CASE,
        snapshot=pilot.ALTERNATE_START_SNAPSHOT, range_profile=pilot.RANGE_PROFILE,
        inventory=pilot.CANONICAL_INVENTORY, principal=pilot.ALTERNATE_START_PRINCIPAL,
        host=pilot.ALTERNATE_START_HOST, host_ip=pilot.ALTERNATE_START_IP,
        payload_type="apollo", retained_config_path="", retained_config_sha256="a" * 64,
        payload_uuid="11111111-2222-3333-4444-555555555555", task_name="SageApolloJorahBootstrap",
        task_prelaunch_state="Ready", remote_path=r"C:\Users\Public\apollo.exe",
        remote_sha256="b" * 64,
    )


def matrix_record(wall_seconds, continuation=False):
    return pilot.MatrixPairContract(
        matrix_identity="a" * 64,
        route=pilot.MatrixRoute("synthetic", "openai", "synthetic/model", "https://example.invalid/v1"),
        start_case=pilot.ALTERNATE_START_CASE,
        arm_order=tuple(reversed(pilot.ARM_ORDER)),
        alternate_start=alternate_binding(),
        source_identity="source-at-start",
        wall_seconds=wall_seconds,
        continue_after_time_limit=continuation,
    ).to_record()


@pytest.mark.parametrize("wall_seconds,continuation", [(None, False), (3600, False), (3600, True)])
def test_recovers_matrix_alternate_without_reinterpreting_as_fixed_pilot(wall_seconds, continuation):
    record = matrix_record(wall_seconds, continuation)
    before = deepcopy(record)
    binding = pilot.retained_alternate_start_binding([{"record_type": "contract", "contract": record}])
    assert binding.to_record() == record["alternate_start"]
    assert record == before


def test_fixed_pilot_alternate_remains_supported():
    record = pilot.PilotContract(alternate_start=alternate_binding()).to_record()
    assert pilot.retained_alternate_start_binding(
        [{"record_type": "contract", "contract": record}]
    ).to_record() == record["alternate_start"]


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(case_identity="0" * 64),
    lambda r: r.update(start_case="samwell-castelblack"),
    lambda r: r.update(wall_seconds=1800),
    lambda r: r.update(schema="unknown-schema"),
    lambda r: r.update(continue_after_time_limit="true"),
    lambda r: r["route"].update(api_key="not-a-contract-field"),
    lambda r: r["alternate_start"].update(host="DIFFERENT"),
])
def test_rejects_changed_or_malformed_retained_matrix(mutation):
    record = matrix_record(3600)
    mutation(record)
    with pytest.raises(pilot.PilotError):
        pilot.retained_alternate_start_binding([{"record_type": "contract", "contract": record}])


def test_rejects_ambiguous_contracts():
    row = {"record_type": "contract", "contract": matrix_record(3600)}
    with pytest.raises(pilot.PilotError, match="ambiguous"):
        pilot.retained_alternate_start_binding([row, deepcopy(row)])
