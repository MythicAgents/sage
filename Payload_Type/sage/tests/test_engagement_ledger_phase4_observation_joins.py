"""Phase 4 remaining sources, checkpoint 1: source observations and joins that outlive the task cursor.

Traced on ``ea3e941``: the reconciliation cursor treated a task as finished once Mythic marked it ``completed``,
even while Mythic's completion function was still running, and fetched credential-store entries only for the tasks
evaluated in that pass. A credential entry that appeared after the cursor passed its task, or a task evaluated while
still settling, was never revisited by an uncited pass.

Now a settling task is held like a running one, and a terminal credential task with no entry yet leaves an awaiting
join in ``sage.db`` that a later pass resolves, across process restarts, without holding the cursor. Only Mythic's
transport is stubbed; the reconciler, store, proof boundary and views are production code. Key material is synthetic.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ai.langgraph import effect_observations, engagement_store, task_evidence
from ai.langgraph import model as model_module
from ai.langgraph.mythic_tools import MythicTools
from sage_chat import slash
from sage_chat.headless import build_chat_request
from tests.esl_fixtures import AGENT_PROCESSED, stub_mythic_operation
from tests.test_engagement_ledger_phase2_unified_proof import (  # noqa: F401 - fixtures used by name
    CALLBACK, DCSYNC_OUTPUT, DCSYNC_PARAMS, NTLM, SUCCESS, TASK, TYPED, WORKING, session,
)
from tests.test_engagement_ledger_phase4_existing_rows import _current_typed, _entry, _slots, _task_row

SETTLING = {"completed": True, "status": task_evidence.MYTHIC_COMPLETION_FUNCTION_RUNNING, **AGENT_PROCESSED}
AGENT_ERROR = {"completed": True, "status": "error", **AGENT_PROCESSED}
STORE_SLOT = "credential_store:42"


@pytest.fixture
def mythic(monkeypatch, mythic_task_records):
    """DCSync task 18 finished with no output a verifier credits and, so far, no credential-store entry."""
    records = stub_mythic_operation(monkeypatch)
    records["tasks"] = [_task_row()]
    records["outputs"] = {TASK: ""}
    mythic_task_records[TASK] = SUCCESS
    return records


def _pass(tools: MythicTools) -> list[str]:
    return asyncio.run(tools._reconcile_operation())


def _turn_end(tools: MythicTools) -> str:
    return asyncio.run(model_module.Model._engagement_state_turn_summary_text(SimpleNamespace(mythic_client=tools)))


def _joins(tools: MythicTools) -> list[dict]:
    return tools._ledger_store().observations(tools._require_ledger_binding(),
                                              kind=effect_observations.CREDENTIAL_ENTRY_JOIN)


def _position(tools: MythicTools):
    return tools._ledger_store().reconciliation_state(tools._require_ledger_binding())


def _events(tools: MythicTools) -> list:
    return tools._ledger_store().history(tools._require_ledger_binding())


def _restarted(tools: MythicTools, name: str = "restarted") -> MythicTools:
    """A new Sage process: a fresh session and store object over the same sage.db."""
    fresh = MythicTools(agent_task_id=name)
    fresh.client = object()
    fresh.set_request_contract(tools._request_contract)
    fresh._turn_authority = tools._turn_authority
    fresh._engagement_objective = lambda: ""  # type: ignore[method-assign]
    fresh._require_ledger_binding()
    assert fresh._ledger_store() is not tools._ledger_store()
    return fresh


def _database_text(tools: MythicTools) -> str:
    connection = sqlite3.connect(tools._ledger_store().path)
    try:
        return json.dumps([list(row) for table in ("esl_observations", "esl_rows", "esl_events")
                           for row in connection.execute(f"SELECT * FROM {table}").fetchall()])
    finally:
        connection.close()


def _state(tools: MythicTools) -> str:
    model = SimpleNamespace(mythic_client=tools, mode="supervised", provider="test", model="test", state={})
    return asyncio.run(slash._handle_state(model, build_chat_request("x", channel_id=6, operation_id=0), ""))


# --- a task Mythic is still finishing --------------------------------------------------------------------------------

def test_a_task_mythic_is_still_finishing_is_held_then_evaluated_once_it_settles(session, mythic):
    mythic["tasks"] = [_task_row(record=SETTLING)]
    mythic["outputs"] = {TASK: DCSYNC_OUTPUT}
    tools = session()

    _pass(tools)
    assert (_position(tools).task_cursor, _position(tools).held_tasks) == (TASK, (TASK,))
    assert tools._ledger_snapshot().hop_rows() == [] and _joins(tools) == []  # nothing evaluated while settling

    mythic["tasks"] = [_task_row()]  # the completion function finished and stored the entry
    mythic["credentials"] = [_entry()]
    _pass(tools)

    [row] = _current_typed(tools)
    assert row.payload["status"] == "achieved"
    assert _slots(row) == {str(TASK): {"reconcile": "admitted", STORE_SLOT: "admitted"}}
    assert _position(tools).held_tasks == ()


def test_a_cited_settling_task_is_not_terminal(session, mythic):
    mythic["tasks"] = [_task_row(record=SETTLING)]
    tools = session()
    assert asyncio.run(tools._reconcile_operation([str(TASK)]))[0] == f"- {TASK}: not added, task not terminal."


# --- a credential entry that arrives after the cursor passed its task ------------------------------------------------

def test_a_late_credential_entry_is_recorded_by_a_later_pass_without_moving_the_cursor_back(session, mythic):
    tools = session()
    lines = _pass(tools)
    assert lines[0].endswith(f"cursor at task {TASK}, 1 task(s) awaiting a credential-store entry.")
    assert tools._ledger_snapshot().hop_rows() == []
    [join] = _joins(tools)
    assert (join["join_state"], join["task_id"], join["callback_id"], join["agent_callback_id"]) == (
        effect_observations.JOIN_AWAITING, str(TASK), str(CALLBACK), "agent-2")
    assert join["source_kind"] == "task" and join["payload"]["values"]["task_classification"] == "terminal_success"
    assert join["payload"]["supporting"] == [f"task:{TASK}"]

    mythic["credentials"] = [_entry()]  # Mythic stores the entry after the pass
    _pass(tools)

    [row] = _current_typed(tools)
    assert row.payload["status"] == "achieved" and _slots(row) == {str(TASK): {STORE_SLOT: "admitted"}}
    [join] = _joins(tools)
    assert (join["join_state"], join["resolved_by"]) == (effect_observations.JOIN_RESOLVED, "cred:42")
    assert _position(tools).task_cursor == TASK  # the cursor never moved back or waited

    events, observations = len(_events(tools)), len(tools._ledger_store().observations(tools._require_ledger_binding()))
    _pass(tools)  # replaying the pass changes nothing
    assert len(_events(tools)) == events
    assert len(tools._ledger_store().observations(tools._require_ledger_binding())) == observations


def test_a_join_survives_a_restart_and_the_new_process_resolves_it(session, mythic):
    tools = session()
    _turn_end(tools)
    assert [join["join_state"] for join in _joins(tools)] == [effect_observations.JOIN_AWAITING]

    mythic["credentials"] = [_entry()]
    fresh = _restarted(tools)
    _turn_end(fresh)

    [row] = _current_typed(fresh)
    assert row.payload["status"] == "achieved" and WORKING in row.payload["satisfied_effects"]
    assert [join["join_state"] for join in _joins(fresh)] == [effect_observations.JOIN_RESOLVED]


def test_a_citation_before_ordinary_reconciliation_records_the_join_and_keeps_the_cursor(session, mythic):
    tools = session()
    lines = asyncio.run(tools._reconcile_operation([str(TASK)]))
    assert lines[0] == f"- {TASK}: not added, no proven gain yet; awaiting its Mythic credential-store entry."
    assert _position(tools).task_cursor == 0
    [join] = _joins(tools)
    assert join["join_state"] == effect_observations.JOIN_AWAITING

    assert _pass(tools)[0].endswith(", 1 task(s) awaiting a credential-store entry.")  # one join, counted once

    mythic["credentials"] = [_entry()]
    _pass(tools)  # the ordinary pass finds task 18's entry

    assert [row.payload["status"] for row in _current_typed(tools)] == ["achieved"]
    assert [join["join_state"] for join in _joins(tools)] == [effect_observations.JOIN_RESOLVED]
    assert _position(tools).task_cursor == TASK


def test_an_entry_from_another_task_does_not_resolve_the_join(session, mythic):
    tools = session()
    _pass(tools)
    mythic["tasks"].append(_task_row(19))
    mythic["outputs"][19] = ""
    mythic["credentials"] = [_entry(43, 19)]

    _pass(tools)

    joins = {join["task_id"]: join["join_state"] for join in _joins(tools)}
    assert joins == {str(TASK): effect_observations.JOIN_AWAITING}  # task 19's own entry arrived with it: no join
    assert _slots(_current_typed(tools)[0]) == {"19": {"credential_store:43": "admitted"}}


@pytest.mark.parametrize("record, command, params", [
    (AGENT_ERROR, "dcsync", None),  # a failed task's entry will not come
    ({"completed": True, "status": task_evidence.MYTHIC_COMPLETION_FUNCTION_ERROR, **AGENT_PROCESSED}, "dcsync", None),
    (SUCCESS, "ps", {}),  # not a credential technique
])
def test_tasks_whose_entry_is_not_expected_leave_no_join(session, mythic, record, command, params):
    mythic["tasks"] = [_task_row(record=record, command=command,
                                 **({"params": params} if params is not None else {}))]
    tools = session()
    _pass(tools)
    assert _joins(tools) == []


# --- failures and bounds ---------------------------------------------------------------------------------------------

def test_a_mythic_failure_leaves_the_join_and_cursor_and_a_later_pass_resolves_it(session, mythic):
    tools = session()
    _pass(tools)
    mythic["credentials"] = [_entry()]
    mythic["fail"] = True
    _turn_end(tools)  # the turn end swallows the failed pass
    assert [join["join_state"] for join in _joins(tools)] == [effect_observations.JOIN_AWAITING]
    assert tools._ledger_snapshot().hop_rows() == [] and _position(tools).task_cursor == TASK

    mythic["fail"] = False
    _turn_end(tools)
    assert [join["join_state"] for join in _joins(tools)] == [effect_observations.JOIN_RESOLVED]


def test_an_interrupted_store_write_commits_nothing_and_the_replay_converges(session, mythic, monkeypatch):
    tools = session()
    _pass(tools)
    mythic["credentials"] = [_entry()]
    real = engagement_store._close_join

    def interrupted(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(engagement_store, "_close_join", interrupted)
    with pytest.raises(engagement_store.LedgerWriteFailed):
        _pass(tools)
    assert tools._ledger_snapshot().hop_rows() == []  # the row in the same transaction rolled back with it
    assert [join["join_state"] for join in _joins(tools)] == [effect_observations.JOIN_AWAITING]

    monkeypatch.setattr(engagement_store, "_close_join", real)
    _pass(tools)
    _pass(tools)
    [row] = _current_typed(tools)
    assert [event["kind"] for event in _events(tools) if event["row_id"] == row.row_id] == ["create"]
    assert [join["join_state"] for join in _joins(tools)] == [effect_observations.JOIN_RESOLVED]


def test_an_expired_join_stops_polling_but_a_citation_still_records_the_entry(session, mythic, monkeypatch):
    tools = session()
    _pass(tools)
    monkeypatch.setattr(effect_observations, "CREDENTIAL_JOIN_WINDOW_SECONDS", -1)
    _pass(tools)
    [join] = _joins(tools)
    assert join["join_state"] == effect_observations.JOIN_EXPIRED and "cred:<id>" in join["join_reason"]

    assert asyncio.run(tools._reconcile_operation([str(TASK)]))[0] == (
        f"- {TASK}: not added, no proven gain; its credential-store entry did not appear within the window "
        "(cite cred:<id> once it exists).")  # re-citing the task does not promise a poll that will not happen

    mythic["credentials"] = [_entry()]
    _pass(tools)
    assert tools._ledger_snapshot().hop_rows() == []  # an expired join is no longer polled
    assert asyncio.run(tools._reconcile_operation(["cred:42"]))[0] == "- cred:42: added row #1."
    assert [join["join_state"] for join in _joins(tools)] == [effect_observations.JOIN_EXPIRED]  # never reopened


def test_no_secret_reaches_observations_events_notes_or_the_view(session, mythic):
    mythic["tasks"] = [_task_row(params={**DCSYNC_PARAMS, "Password": NTLM})]  # a parameter can hold a secret
    tools = session()
    notes = _pass(tools)
    mythic["credentials"] = [_entry()]
    notes += _pass(tools)
    assert _joins(tools) and _current_typed(tools)
    assert NTLM not in _database_text(tools)
    assert NTLM not in "\n".join(notes) and NTLM not in _state(tools)


def test_a_task_whose_own_output_proved_the_effect_leaves_no_join(session, mythic):
    mythic["outputs"] = {TASK: DCSYNC_OUTPUT}
    tools = session()
    lines = _pass(tools)
    assert [row.payload["status"] for row in _current_typed(tools)] == ["achieved"]
    assert _joins(tools) == [] and "awaiting" not in lines[0]
