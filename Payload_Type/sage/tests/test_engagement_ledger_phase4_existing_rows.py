"""Phase 4 remaining sources, checkpoint 1: existing rows keep one canonical claim and every writer's provenance.

Two observed defects drive these probes:

- A duplicate row for an achieved effect stayed pending beside the achieved one (2026-11-06, channels 5 and 6).
  Phase 2's canonical rows and the schema 3 fold of older rows should already prevent it; the cases below are the
  regression evidence, through the production writers.
- The ordinary-supervised step writer's credential observation was absorbed by whichever writer recorded the row
  first: the step writer and the credential-store observer shared one observation slot, so the second writer's
  admitted observation was dropped with no event (reproduced on ``ea3e941``).

Only Mythic's transport is stubbed (task issue and output wait, task records, the operation's tasks, outputs,
credential-store entries and callbacks). Writers, the shared classifier, proof boundary, reducer, ``sage.db`` store and
views are production code. Account, realm and key material are synthetic.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from types import SimpleNamespace

import pytest

from ai.langgraph import engagement_state, task_evidence
from ai.langgraph.mythic_tools import MythicTools
from sage_chat import slash
from sage_chat.headless import build_chat_request
from tests.esl_fixtures import AGENT_PROCESSED, stub_mythic_operation
from tests.test_engagement_ledger_phase2_unified_proof import (  # noqa: F401 - fixtures used by name
    ACCOUNT, CALLBACK, DCSYNC_OUTPUT, DCSYNC_PARAMS, NTLM, REALM, SUCCESS, TASK, TYPED, WORKING, _capability, _raw,
    _reader, mythic_issue, session,
)

CREDENTIAL = 42
STEP_SLOT = f"supervised_step:credential_store:{CREDENTIAL}"
STORE_SLOT = f"credential_store:{CREDENTIAL}"
KERBEROAST = {"assembly_name": "Rubeus.exe", "assembly_arguments": "kerberoast /nowrap"}
# A task Mythic shows finished but that never recorded the agent processing it: the shared classifier says unknown.
UNPROCESSED = {"completed": True, "status": "success", "callback": {"display_id": CALLBACK}, "responses": [{"id": 1}]}


def _record_fields(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in {"callback", "responses"}}


def _task_row(task_id: int = TASK, record: dict = SUCCESS, *, command: str = "dcsync",
              params: dict = DCSYNC_PARAMS) -> dict:
    return {"display_id": task_id, "id": task_id, "operation_id": 1, "command_name": command,
            "original_params": json.dumps(params), "operator": {"username": "operator"},
            "callback": {"display_id": CALLBACK, "host": "WS01", "user": f"north\\{ACCOUNT}",
                         "agent_callback_id": "agent-2"},
            **_record_fields(record)}


def _entry(credential_id: int = CREDENTIAL, task_id: int = TASK, *, record: dict = SUCCESS, account: str = ACCOUNT,
           realm: str = REALM, kind: str = "hash", command: str = "dcsync", params: dict = DCSYNC_PARAMS) -> dict:
    return {"id": credential_id, "operation_id": 1, "account": account, "realm": realm.upper(), "type": kind,
            "credential_text": NTLM, "comment": "",
            "task": {"display_id": task_id, "operation_id": 1, "command_name": command,
                     "original_params": json.dumps(params), "callback": {"display_id": CALLBACK},
                     **_record_fields(record)}}


@pytest.fixture
def mythic(monkeypatch, mythic_task_records):
    """The operation's records: DCSync task 18 (terminal success) and its credential-store entry 42."""
    records = stub_mythic_operation(monkeypatch)
    records["tasks"] = [_task_row()]
    records["outputs"] = {TASK: DCSYNC_OUTPUT}
    records["credentials"] = [_entry()]
    mythic_task_records[TASK] = SUCCESS
    return records


def _step(tools: MythicTools, entry: dict) -> dict:
    """The supervised step writer at a worker step boundary whose references name the entry."""
    async def credentials(_now, force=False):
        return [entry]

    tools._fetch_credentials_cached = credentials  # type: ignore[method-assign]
    references = {"tasks": [], "graph_reads": [],
                  "credentials": [{"credential_id": entry["id"], "source_task_id": entry["task"]["display_id"]}]}
    return asyncio.run(tools._record_step_ledger(references, []))


def _writers(tools: MythicTools, mythic_issue: dict) -> dict:
    def raw():
        mythic_issue["next"] = TASK
        _raw(tools)

    def capability():
        asyncio.run(tools._observe_task_record(TASK, CALLBACK))
        _capability(tools, TASK)

    return {
        "raw": raw,
        "capability": capability,
        "reconcile": lambda: asyncio.run(tools._reconcile_operation([str(TASK)])),
        "step": lambda: _step(tools, _entry()),
    }


def _current_typed(tools: MythicTools, typed: str = TYPED) -> list:
    """Unretracted rows that claim the typed effect: there must never be two."""
    return [row for row in tools._ledger_snapshot().hop_rows()
            if row.retraction is None and task_evidence.payload_typed_identity(row.payload) == typed]


def _slots(row) -> dict:
    return {contribution["task_id"]: {writer: next(slot for slot in ("admitted", "rejected") if slot in slots)
                                      for writer, slots in contribution["writers"].items()}
            for contribution in row.payload["contributions"]}


def _events(tools: MythicTools) -> list:
    return tools._ledger_store().history(tools._require_ledger_binding())


def _achieved(tools: MythicTools) -> set[str]:
    return engagement_state.EngagementState(
        objective="", hops=list(tools._ledger_view().hops), engagement_id=tools._eng_key(), runtime_scope=True,
    ).achieved_effects()


# --- #171: the supervised step writer's credential observation survives any writer order ----------------------------

@pytest.mark.parametrize("order", list(itertools.permutations(("raw", "capability", "reconcile", "step"))),
                         ids="-".join)
def test_every_writer_order_keeps_one_row_and_every_writers_observation(session, mythic_issue, mythic, order):
    tools = session()
    writers = _writers(tools, mythic_issue)
    numbers = []
    for name in order:
        writers[name]()
        [row] = _current_typed(tools)
        numbers.append(row.number)

    [row] = _current_typed(_reader())
    assert set(numbers) == {row.number}  # the first writer's number, whoever it was
    assert _slots(row) == {str(TASK): {"raw": "admitted", "capability": "admitted", "reconcile": "admitted",
                                       STORE_SLOT: "admitted", STEP_SLOT: "admitted"}}
    step = row.payload["contributions"][0]["writers"][STEP_SLOT]["admitted"]
    assert (step["source"], str(step["evidence"]["credential_id"])) == ("supervised_step", str(CREDENTIAL))
    assert row.payload["status"] == "achieved" and WORKING in _achieved(tools)

    events = len(_events(tools))
    for name in order:  # replaying every writer appends nothing
        writers[name]()
    assert len(_events(tools)) == events


def test_the_step_writer_appends_its_observation_to_a_row_the_reconciler_created(session, mythic_issue, mythic):
    tools = session()
    asyncio.run(tools._reconcile_operation([f"cred:{CREDENTIAL}"]))
    [row] = _current_typed(tools)
    assert _slots(row) == {str(TASK): {STORE_SLOT: "admitted"}}
    before = len(_events(tools))

    summary = _step(tools, _entry())

    assert summary["recorded"] == [{"effect": WORKING, "task_id": TASK, "status": "achieved"}]
    [after] = _current_typed(_reader())
    assert after.number == row.number
    assert _slots(after) == {str(TASK): {STORE_SLOT: "admitted", STEP_SLOT: "admitted"}}
    [event] = _events(tools)[before:]  # the step writer's own persisted observation, as one update event
    assert event["kind"] == "update" and event["row_id"] == row.row_id and STEP_SLOT in json.dumps(event["payload"])
    assert NTLM not in json.dumps(_events(tools)) and NTLM not in json.dumps(after.payload)
    shown = "\n".join(_state_lines(tools))
    assert "admitted: credential store, supervised step" in shown


def test_a_step_session_writing_from_a_stale_snapshot_keeps_the_reconcilers_observation(session, mythic_issue,
                                                                                        mythic):
    reconciler, step_session = session("reconciler"), session("step-session")
    stale = list(step_session._ledger_view().hops)  # taken before the reconciler writes
    asyncio.run(reconciler._reconcile_operation([f"cred:{CREDENTIAL}"]))
    step_session._ledger_view = lambda snapshot=None: SimpleNamespace(  # type: ignore[method-assign]
        hops=stale, graph_facts=[], corrected_hops=[], snapshot=None)

    _step(step_session, _entry())

    [row] = _current_typed(_reader())
    assert _slots(row) == {str(TASK): {STORE_SLOT: "admitted", STEP_SLOT: "admitted"}}


# --- #170: one current claim per canonical gain ---------------------------------------------------------------------

def test_two_tasks_proving_one_account_secret_are_two_contributions_on_one_row(session, mythic):
    mythic["tasks"].append(_task_row(19))
    mythic["outputs"][19] = DCSYNC_OUTPUT
    mythic["credentials"].append(_entry(43, 19))
    tools = session()

    asyncio.run(tools._reconcile_operation())

    [row] = _current_typed(_reader())
    assert _slots(row) == {"18": {"reconcile": "admitted", STORE_SLOT: "admitted"},
                           "19": {"reconcile": "admitted", "credential_store:43": "admitted"}}
    assert len(tools._ledger_snapshot().hop_rows()) == 1


def test_a_later_admitted_contribution_settles_a_pending_canonical_row(session, mythic, mythic_task_records):
    tools = session()
    unproven = _entry(record=UNPROCESSED)
    _step(tools, unproven)  # the step writer records even an unproven gain, as pending
    [pending] = _current_typed(tools)
    assert (pending.payload["status"], pending.payload["pending_reason"]) == ("pending", "proof_missing")

    mythic["tasks"] = [_task_row(19)]
    mythic["outputs"] = {19: DCSYNC_OUTPUT}
    mythic["credentials"] = [_entry(43, 19)]
    asyncio.run(tools._reconcile_operation())

    [row] = _current_typed(_reader())  # still one current row: no second pending claim beside the achieved one
    assert row.number == pending.number and row.payload["status"] == "achieved"
    # The unproven observation stays as a rejected candidate. Without an admitted envelope its lineage is untrusted,
    # so it carries no task binding (the proof boundary strips reserved lineage keys) and proves nothing.
    assert _slots(row) == {"": {"supervised_step:credential_store": "rejected"},
                           "19": {"reconcile": "admitted", "credential_store:43": "admitted"}}
    assert WORKING in _achieved(tools)


def test_material_beside_working_keeps_both_and_material_alone_proves_no_working_credential(session, mythic):
    roast = _task_row(20, command="execute_assembly", params=KERBEROAST)
    mythic["tasks"] = [roast]
    mythic["outputs"] = {}
    mythic["credentials"] = [_entry(44, 20, command="execute_assembly", params=KERBEROAST)]
    tools = session()
    asyncio.run(tools._reconcile_operation())
    [row] = _current_typed(tools)
    material = f"cred-material:hash:{ACCOUNT}@{REALM}"
    assert row.payload["satisfied_effects"] == [material]
    assert WORKING not in _achieved(tools)

    mythic["tasks"].append(_task_row(21))
    mythic["outputs"][21] = DCSYNC_OUTPUT
    mythic["credentials"].append(_entry(45, 21))
    asyncio.run(tools._reconcile_operation())

    [row] = _current_typed(_reader())
    assert row.payload["effect"] == WORKING and set(row.payload["satisfied_effects"]) == {WORKING, material}
    assert WORKING in _achieved(tools)


def test_distinct_accounts_realms_and_material_classes(session, mythic):
    south = REALM.replace("north", "south")
    hand = {"assembly_name": "SharpDump.exe", "assembly_arguments": ""}
    mythic["tasks"] = [_task_row(30), _task_row(31, command="execute_assembly", params=hand)]
    mythic["outputs"] = {}
    mythic["credentials"] = [
        _entry(50, 30), _entry(51, 30, realm=south), _entry(52, 30, account="fixture.other"),
        _entry(53, 31, account="fixture.third", command="execute_assembly", params=hand),
        _entry(54, 31, account="fixture.third", kind="ticket", command="execute_assembly", params=hand),
    ]
    tools = session()

    asyncio.run(tools._reconcile_operation())

    rows = {task_evidence.payload_typed_identity(row.payload): row for row in tools._ledger_snapshot().hop_rows()}
    assert set(rows) == {TYPED, f"account-secret:{ACCOUNT}@{south}", f"account-secret:fixture.other@{REALM}",
                         f"account-secret:fixture.third@{REALM}"}
    third = rows[f"account-secret:fixture.third@{REALM}"]
    assert set(third.payload["satisfied_effects"]) == {f"cred-material:hash:fixture.third@{REALM}",
                                                      f"cred-material:ticket:fixture.third@{REALM}"}
    assert f"creds:fixture.third@{REALM}" not in _achieved(tools)


def _state_lines(tools: MythicTools) -> list[str]:
    """The operator's `/state` reply through the chat command."""
    model = SimpleNamespace(mythic_client=tools, mode="supervised", provider="test", model="test", state={})
    request = build_chat_request("x", channel_id=6, operation_id=0)
    return asyncio.run(slash._handle_state(model, request, "")).splitlines()
