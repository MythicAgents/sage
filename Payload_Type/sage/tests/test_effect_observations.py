"""Normalized source observations: allow-listed, secret-free values, stable identities, and store joins.

Pure checks of ``effect_observations`` plus the ``sage.db`` observation index it feeds. The reconciler path that
records and resolves joins is exercised in ``test_engagement_ledger_phase4_observation_joins.py``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from ai.langgraph import effect_observations as eo
from ai.langgraph import engagement_store, task_evidence
from tests.esl_fixtures import AGENT_PROCESSED

SECRET = "0123456789abcdef0123456789abcdef"
NOW = "2026-11-07T12:00:00+00:00"


def _task(status: str = "success", *, command: str = "dcsync", processed: bool = True, **extra) -> dict:
    return {"display_id": 18, "operation_id": 1, "command_name": command, "status": status, "completed": True,
            "original_params": json.dumps({"Domain": "north.example.local", "User": "fixture.user",
                                           "Password": SECRET}),
            "display_params": f"-Password {SECRET}", "responses": [{"id": 1}],
            "callback": {"display_id": 2, "agent_callback_id": "agent-2"},
            **(AGENT_PROCESSED if processed else {}), **extra}


def test_task_values_carry_identity_and_classification_but_never_parameters():
    values = eo.normalize_task(_task())
    assert values == {"operation_id": 1, "command_name": "dcsync",
                      "status_timestamp_processed": AGENT_PROCESSED["status_timestamp_processed"],
                      "task_id": "18", "callback_id": "2", "agent_callback_id": "agent-2",
                      "task_classification": task_evidence.TERMINAL_SUCCESS}
    assert SECRET not in json.dumps(values)
    assert eo.normalize_task({"status": "success"}) == {} and eo.normalize_task(None) == {}


def test_credential_values_never_carry_the_secret_or_comment():
    values = eo.normalize_credential({
        "id": 42, "operation_id": 1, "account": "Fixture.User", "realm": "NORTH.EXAMPLE.LOCAL", "type": "hash",
        "credential_text": SECRET, "comment": f"pasted {SECRET}", "task": {"display_id": 18, "operation_id": 1}})
    assert values == {"id": 42, "operation_id": 1, "account": "fixture.user", "realm": "north.example.local",
                      "type": "hash", "task_id": "18", "task_operation_id": 1}
    assert eo.normalize_credential({"credential_text": SECRET}) == {}


@pytest.mark.parametrize("task, technique, expected", [
    (_task(), "dcsync-user", True),
    (_task(command="mimikatz"), "lsass-dump", True),
    (_task(), "kerberoast", False),  # not a technique whose entry the credential store is expected to hold
    (_task("error"), "dcsync-user", False),  # a failed task's entry will not come
    (_task(task_evidence.MYTHIC_COMPLETION_FUNCTION_ERROR), "dcsync-user", False),
    (_task(task_evidence.MYTHIC_COMPLETION_FUNCTION_RUNNING), "dcsync-user", False),  # held by the cursor instead
    (_task(processed=False), "dcsync-user", False),  # never processed by the agent: unknown
])
def test_only_a_terminal_success_credential_task_leaves_an_awaiting_join(task, technique, expected):
    join = eo.credential_join(task, technique, "fixture.user@north.example.local", NOW)
    assert (join is not None) is expected
    if join is not None:
        assert join.join_state == eo.JOIN_AWAITING and join.supporting == ("task:18",)
        assert (join.task_id, join.callback_id, join.agent_callback_id) == ("18", "2", "agent-2")
        assert SECRET not in json.dumps(join.record())


def test_observation_identity_is_stable_and_commitments_follow_the_values():
    first = eo.credential_join(_task(), "dcsync-user", "fixture.user@north.example.local", NOW)
    later = eo.credential_join(_task(), "dcsync-user", "fixture.user@north.example.local", "2026-11-08T00:00:00Z")
    other = eo.credential_join(_task(display_id=19), "dcsync-user", "fixture.user@north.example.local", NOW)
    assert first.observation_id == later.observation_id != other.observation_id
    assert first.values_sha256 != other.values_sha256
    assert first.payload()["values_sha256"] == first.values_sha256


@pytest.mark.parametrize("observed, now, expected", [
    (NOW, "2026-11-07T13:00:00+00:00", False),  # exactly the window: still awaiting
    (NOW, "2026-11-07T13:00:01+00:00", True),
    ("2026-11-07T12:00:00Z", "2026-11-07T13:30:00Z", True),
    ("", NOW, False),
    ("not a time", NOW, False),
])
def test_join_expiry_boundaries(observed, now, expected):
    assert eo.join_expired(observed, now) is expected


# --- the sage.db observation index -----------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    store = engagement_store.EngagementStore(tmp_path / "sage.db")
    return store, store.bind_namespace("eng-observations")


def _record(task_id: int = 18) -> engagement_store.ObservationRecord:
    join = eo.credential_join(_task(display_id=task_id), "dcsync-user", "fixture.user@north.example.local", NOW)
    return engagement_store.ObservationRecord(**join.record())


def test_indexing_is_idempotent_and_a_closed_join_never_reopens(store):
    store, binding = store
    result = store.apply(binding, [], observations=[_record(), _record()])
    assert len(result.indexed) == 1
    assert store.apply(binding, [], observations=[_record()]).indexed == []
    [join] = store.awaiting_joins(binding, eo.CREDENTIAL_ENTRY_JOIN)

    close = engagement_store.JoinTransition(join["observation_id"], eo.JOIN_RESOLVED, "entry", "cred:42")
    assert store.apply(binding, [], joins=[close]).joins_closed == [join["observation_id"]]
    expire = engagement_store.JoinTransition(join["observation_id"], eo.JOIN_EXPIRED, "late")
    assert store.apply(binding, [], joins=[close, expire], observations=[_record()]).joins_closed == []
    [stored] = store.observations(binding)
    assert (stored["join_state"], stored["resolved_by"]) == (eo.JOIN_RESOLVED, "cred:42")
    assert store.awaiting_joins(binding, eo.CREDENTIAL_ENTRY_JOIN) == []


def test_observations_are_operation_scoped_and_ordered_oldest_first(store, tmp_path):
    store, binding = store
    other = store.bind_namespace("eng-other")
    store.apply(binding, [], observations=[_record(20), _record(18)])
    store.apply(other, [], observations=[_record(19)])
    assert [join["task_id"] for join in store.awaiting_joins(binding, eo.CREDENTIAL_ENTRY_JOIN)] == ["20", "18"]
    assert [join["task_id"] for join in store.awaiting_joins(binding, eo.CREDENTIAL_ENTRY_JOIN, limit=1)] == ["20"]
    assert [join["task_id"] for join in store.awaiting_joins(other, eo.CREDENTIAL_ENTRY_JOIN)] == ["19"]


@pytest.mark.parametrize("state", ["resolved", "expired", "bogus"])
def test_an_observation_cannot_be_indexed_already_closed(store, state):
    store, binding = store
    record = engagement_store.ObservationRecord(**{**_record().__dict__, "join_state": state})
    with pytest.raises(engagement_store.LedgerWriteFailed):
        store.apply(binding, [], observations=[record])
    with pytest.raises(engagement_store.LedgerWriteFailed):
        store.apply(binding, [], joins=[engagement_store.JoinTransition("x", "awaiting")])


def test_a_store_from_before_the_index_reads_no_joins_and_carries_it_forward(store, tmp_path):
    store, binding = store
    path = store.path
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE esl_observations")
    connection.commit()
    connection.close()
    reader = engagement_store.EngagementStore(path)
    assert reader.awaiting_joins(binding, eo.CREDENTIAL_ENTRY_JOIN) == []  # read-only: no table, no joins

    writer = engagement_store.EngagementStore(path)
    writer.apply(binding, [], observations=[_record()])  # the first write creates the index
    copied = engagement_store.copy_ledger_tables(path, tmp_path / "fresh.db")
    assert copied["esl_observations"] == 1
    fresh = engagement_store.EngagementStore(tmp_path / "fresh.db")
    assert [join["task_id"] for join in fresh.awaiting_joins(binding, eo.CREDENTIAL_ENTRY_JOIN)] == ["18"]
