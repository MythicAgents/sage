"""Phase 2: one record per (Mythic task, typed effect), one terminal-success rule for every ledger writer.

Reproduces the shape of the supervised DCSync recorded on 2026-11-06 (Mythic task 18): a capability issued the
DCSync, the raw issue hook under it and the capability writer each recorded the same account secret under a different
row identity, and they applied different terminal-success rules to a task Mythic marked with a completion-function
error after the agent had returned the secret. The task metadata below follows that record; the account, realm and
key material are synthetic.

Only Mythic's transport is stubbed (task issue, output wait, the task record and the operation's task history). The
intent classifier, raw hook, capability writer, shared task classifier, proof boundary, reducer, ``sage.db`` store,
reconciliation and the ledger views are production code.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ai.langgraph import capabilities, credential_artifacts, engagement_state, engagement_store, proof_boundary
from ai.langgraph import model as model_module
from ai.langgraph import mythic_tools as mythic_tools_module
from ai.langgraph import task_evidence
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract
from ai.langgraph.turn_authority import authority_from_request_contract
from tests.esl_fixtures import AGENT_PROCESSED, stub_mythic_operation

ENGAGEMENT = "eng-phase2-unified-proof"
REALM = "north.example.local"
ACCOUNT = "fixture.user"
TYPED = f"account-secret:{ACCOUNT}@{REALM}"
WORKING = f"creds:{ACCOUNT}@{REALM}"
# What the credential review may establish for a completion-function-error task: material, never a working credential.
MATERIAL = f"cred-material:hash:{ACCOUNT}@{REALM}"
NTLM = "0123456789abcdef0123456789abcdef"
CALLBACK = 2
TASK = 18
# The raw command spells the user as the operator typed it: NETBIOS\\account.
DCSYNC_PARAMS = {"Domain": REALM, "User": f"north\\{ACCOUNT}", "DC": f"dc01.{REALM}"}
DCSYNC_OUTPUT = (
    f"Object RDN           : {ACCOUNT}\n** SAM ACCOUNT **\nSAM Username         : {ACCOUNT}\nCredentials:\n"
    f"  Hash NTLM: {NTLM}\n"
)
# Mythic's record of task 18: the agent processed the task and answered (three responses), then Apollo's completion
# function failed and Mythic overwrote the status.
COMPLETION_ERROR = {"completed": True, "status": task_evidence.MYTHIC_COMPLETION_FUNCTION_ERROR,
                    "callback": {"display_id": CALLBACK}, "responses": [{"id": 1}], **AGENT_PROCESSED}
SUCCESS = {"completed": True, "status": "success", "callback": {"display_id": CALLBACK}, "responses": [{"id": 1}],
           **AGENT_PROCESSED}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(mythic_tools_module, "SAGE_ENGAGEMENT_ID", ENGAGEMENT)

    def make(name: str = "phase2", request: int = 1) -> MythicTools:
        tools = MythicTools(agent_task_id=name)
        tools.client = object()
        contract = build_request_contract(
            request_id=f"chat:6:request:{request}", channel_id="channel-6", operation_id="operation-1",
            mode="auto", autonomous_solve=True,
        )
        tools.set_request_contract(contract)
        tools._turn_authority = authority_from_request_contract(contract)
        tools._engagement_objective = lambda: ""  # type: ignore[method-assign]
        tools._require_ledger_binding()
        return tools

    return make


@pytest.fixture
def mythic_issue(monkeypatch):
    """Mythic task issue and output wait for the DCSync; each issue returns the next display ID."""
    issued: list = []
    ids = {"next": TASK}

    async def issue_task(mythic, command_name, parameters, callback_display_id, wait_for_complete=True, timeout=None):
        issued.append((command_name, callback_display_id))
        display_id = ids["next"]
        ids["next"] += 1
        return {"display_id": display_id}

    async def waitfor(mythic, task_display_id, timeout=None):
        return DCSYNC_OUTPUT

    monkeypatch.setattr(mythic_tools_module.mythic, "issue_task", issue_task)
    monkeypatch.setattr(mythic_tools_module.mythic, "waitfor_for_task_output", waitfor)
    return ids


def _action(account: str = ACCOUNT) -> capabilities.CapabilityAction:
    return capabilities.CapabilityAction(
        name="dcsync-account", target=f"domain={REALM};account={account}",
        preconditions=[f"ds-replication-rights:{REALM}", "live-foothold:*"], effects=[f"creds:{account}@{REALM}"],
        intent={"capability": "dcsync-account", "domain": REALM, "account": account},
    )


def _raw(tools: MythicTools) -> str:
    """The raw issue path: the intent classifier arms the hop and the raw writer records the result."""
    return asyncio.run(tools.issue_task_and_waitfor_task_output("dcsync", dict(DCSYNC_PARAMS), CALLBACK))


def _capability(tools: MythicTools, task_id: int = TASK, account: str = ACCOUNT, output: str = DCSYNC_OUTPUT):
    """The capability writer as the executor calls it: output-derived terminal status and the verifier's probe."""
    probe = dict(credential_artifacts.extract_credential_probe(output), callback_id=str(CALLBACK))
    return tools.record_capability_result(_action(account), probe, evidence={
        "source": "execute_capability", "provenance": "run", "mythic_task_id": task_id,
        "callback_id": str(CALLBACK), "terminal_task_status": "completed", "command": "dcsync",
        "transaction_id": f"capability-exec:dcsync-account:{account}",
    })


def _capability_issue(tools: MythicTools) -> None:
    """A capability issues the DCSync: the raw hook records under it, then the capability writer records."""
    output, task_id, _trace, _authorization = asyncio.run(tools._issue_capability_callback_command(
        {"command": "dcsync", "parameters": dict(DCSYNC_PARAMS), "purpose": "replicate the account"},
        "dcsync", dict(DCSYNC_PARAMS), CALLBACK, None, capability_name="dcsync-account",
    ))
    assert task_id == TASK
    _capability(tools, task_id, output=output)


def _reader() -> MythicTools:
    reader = MythicTools(agent_task_id="reader")
    reader.client = object()
    reader._require_ledger_binding()
    return reader


def _typed_rows(tools: MythicTools) -> list:
    return [row for row in tools._ledger_snapshot().hop_rows()
            if task_evidence.payload_typed_identity(row.payload) == TYPED]


def _observations(row) -> dict:
    """{task: {writer: (slot, classification, admitted)}} for one typed row."""
    out: dict = {}
    for contribution in row.payload["contributions"]:
        for writer, slots in contribution["writers"].items():
            for slot in ("admitted", "rejected"):
                if slot in slots:
                    out.setdefault(contribution["task_id"], {})[writer] = (
                        slot, slots[slot]["evidence"].get("task_classification"))
    return out


def _events(tools: MythicTools) -> int:
    return len(tools._ledger_store().history(tools._require_ledger_binding()))


def _achieved(tools: MythicTools) -> set[str]:
    return engagement_state.EngagementState(
        objective="", hops=list(tools._ledger_view().hops), engagement_id=tools._eng_key(), runtime_scope=True,
    ).achieved_effects()


# --- the task 18 shape --------------------------------------------------------------------------------------------

def test_task_18_two_writers_make_one_row_and_neither_credits_output_alone(session, mythic_issue,
                                                                           mythic_task_records, monkeypatch):
    mythic_task_records[TASK] = COMPLETION_ERROR
    tools = session()
    _capability_issue(tools)

    [row] = _typed_rows(tools)
    assert len(tools._ledger_snapshot().hop_rows()) == 1
    # Both writers saw the same Mythic record and applied the same rule; neither credited output alone. Only the
    # credential review (test_engagement_ledger_phase2_credential_review.py) adds material, never a working credential.
    assert _observations(row) == {"18": {
        "raw": ("rejected", task_evidence.POSTPROCESSING_ERROR_WITH_OUTPUT),
        "capability": ("rejected", task_evidence.POSTPROCESSING_ERROR_WITH_OUTPUT),
        "review": ("admitted", task_evidence.POSTPROCESSING_ERROR_WITH_OUTPUT),
    }}
    reasons = {slots["rejected"]["admission_reason"] for writer, slots in
               row.payload["contributions"][0]["writers"].items() if writer != "review"}
    assert reasons == {"task is not terminal-success"}
    assert row.payload["satisfied_effects"] == [MATERIAL]
    assert WORKING not in _achieved(tools)

    # The same task through end-of-turn reconciliation: still one row, one contribution, a third observation.
    records = stub_mythic_operation(monkeypatch)
    records["tasks"] = [{"display_id": TASK, "id": TASK, "operation_id": 1, "command_name": "dcsync",
                         "original_params": json.dumps(DCSYNC_PARAMS), "operator": {"username": "operator"},
                         "callback": {"display_id": CALLBACK, "host": "WS01", "user": f"north\\{ACCOUNT}"},
                         **{k: v for k, v in COMPLETION_ERROR.items() if k not in {"callback", "responses"}}}]
    records["outputs"] = {TASK: DCSYNC_OUTPUT}
    asyncio.run(model_module.Model._engagement_state_turn_summary_text(SimpleNamespace(mythic_client=tools)))

    reader = _reader()
    [stored] = _typed_rows(reader)
    assert stored.number == row.number and len(reader._ledger_snapshot().hop_rows()) == 1
    assert _observations(stored)["18"]["reconcile"] == ("rejected", task_evidence.POSTPROCESSING_ERROR_WITH_OUTPUT)
    assert stored.payload["satisfied_effects"] == [MATERIAL]
    assert WORKING not in _achieved(reader)


def test_a_terminal_success_task_is_one_achieved_row_with_both_envelopes(session, mythic_issue, mythic_task_records):
    mythic_task_records[TASK] = SUCCESS
    tools = session()
    _capability_issue(tools)

    [row] = _typed_rows(tools)
    assert row.payload["status"] == "achieved" and row.payload["effect"] == WORKING
    assert _observations(row) == {"18": {"raw": ("admitted", task_evidence.TERMINAL_SUCCESS),
                                         "capability": ("admitted", task_evidence.TERMINAL_SUCCESS)}}
    envelopes = [slots["admitted"]["proof_envelope"] for slots in row.payload["contributions"][0]["writers"].values()]
    assert {env["terminal_status"] for env in envelopes} == {"success"}
    assert {env["metadata"]["task_classification"] for env in envelopes} == {task_evidence.TERMINAL_SUCCESS}
    assert WORKING in _achieved(_reader())


# --- one row, stable number, whatever the writer order, replay, concurrency or restart -------------------------------

def test_writer_order_and_replay_keep_one_row_and_one_contribution(session, mythic_issue, mythic_task_records):
    mythic_task_records[TASK] = SUCCESS
    tools = session()
    asyncio.run(tools._observe_task_record(TASK, CALLBACK))
    _capability(tools, TASK)  # the capability writer first this time
    [first] = _typed_rows(tools)
    mythic_issue["next"] = TASK  # the raw writer's observation of the same Mythic task
    _raw(tools)
    [row] = _typed_rows(tools)
    assert row.number == first.number and set(_observations(row)["18"]) == {"capability", "raw"}

    events = _events(tools)
    _capability(tools, TASK)  # an identical replay of a writer's observation
    assert _events(tools) == events
    [again] = _typed_rows(_reader())
    assert again.number == row.number and len(again.payload["contributions"]) == 1


def test_concurrent_writers_from_one_stale_snapshot_both_survive(session, mythic_task_records):
    """Two sessions reduce from the same snapshot; the store merges each onto the stored head in its transaction."""
    mythic_task_records[TASK] = SUCCESS
    raw_session, capability_session = session("raw-session"), session("capability-session")
    for tools in (raw_session, capability_session):
        asyncio.run(tools._observe_task_record(TASK, CALLBACK))
    stale = list(capability_session._ledger_view().hops)  # taken before the raw session writes

    raw_session._pending_engagement_hop = ("dcsync-user", f"north\\{ACCOUNT}@{REALM}", _now())
    raw_session._record_engagement_success(DCSYNC_OUTPUT, task_id=TASK, callback_id=CALLBACK, command="dcsync")
    capability_session._ledger_view = lambda snapshot=None: SimpleNamespace(  # type: ignore[method-assign]
        hops=stale, graph_facts=[], corrected_hops=[], snapshot=None)
    _capability(capability_session, TASK)

    [row] = _typed_rows(_reader())
    assert set(_observations(row)["18"]) == {"raw", "capability"}
    assert row.payload["status"] == "achieved"


def test_without_the_transactional_merge_a_stale_writer_discards_the_other_envelope(session, mythic_task_records,
                                                                                     monkeypatch):
    """The guard above can fail: replacing the head instead of merging loses the first writer's observation."""
    monkeypatch.setattr(engagement_store._task_evidence, "merge_rows",
                        lambda current, incoming, engagement_id: task_evidence.derive_row(incoming, engagement_id))
    mythic_task_records[TASK] = SUCCESS
    raw_session, capability_session = session("raw-session"), session("capability-session")
    for tools in (raw_session, capability_session):
        asyncio.run(tools._observe_task_record(TASK, CALLBACK))
    stale = list(capability_session._ledger_view().hops)
    raw_session._pending_engagement_hop = ("dcsync-user", f"north\\{ACCOUNT}@{REALM}", _now())
    raw_session._record_engagement_success(DCSYNC_OUTPUT, task_id=TASK, callback_id=CALLBACK, command="dcsync")
    capability_session._ledger_view = lambda snapshot=None: SimpleNamespace(  # type: ignore[method-assign]
        hops=stale, graph_facts=[], corrected_hops=[], snapshot=None)
    _capability(capability_session, TASK)

    [row] = _typed_rows(_reader())
    assert set(_observations(row)["18"]) == {"capability"}


def test_one_task_with_two_effects_is_two_rows_and_a_later_task_appends_its_own_contribution(
        session, mythic_task_records):
    for task_id in (30, 31):
        mythic_task_records[task_id] = dict(SUCCESS)
    tools = session()
    for task_id in (30, 31):
        asyncio.run(tools._observe_task_record(task_id, CALLBACK))
    _capability(tools, 30, ACCOUNT)
    _capability(tools, 30, "second.user", DCSYNC_OUTPUT.replace(ACCOUNT, "second.user"))
    rows = {row.payload["canonical_effect"]: row for row in tools._ledger_snapshot().hop_rows()}
    assert set(rows) == {TYPED, f"account-secret:second.user@{REALM}"}

    _capability(tools, 31, ACCOUNT)  # a later task proves the same effect
    [row] = _typed_rows(_reader())
    assert row.number == rows[TYPED].number
    assert [c["task_id"] for c in row.payload["contributions"]] == ["30", "31"]
    assert all(c["writers"]["capability"].get("admitted") for c in row.payload["contributions"])


# --- the shared classifier, through every writer -------------------------------------------------------------------

_RECORDS = {
    "terminal-success": (SUCCESS, task_evidence.TERMINAL_SUCCESS),
    "agent-error": (dict(SUCCESS, status="error"), task_evidence.AGENT_ERROR_OR_FAILED),
    "not-agent-processed": ({"completed": False, "status": "submitted", "callback": {"display_id": CALLBACK}},
                            task_evidence.NOT_AGENT_PROCESSED),
    "completion-error-with-output": (COMPLETION_ERROR, task_evidence.POSTPROCESSING_ERROR_WITH_OUTPUT),
    "completion-error-without-output": (dict(COMPLETION_ERROR, responses=[]), task_evidence.UNKNOWN),
    "missing-timestamps": ({"completed": True, "status": "success", "callback": {"display_id": CALLBACK},
                            "responses": [{"id": 1}]}, task_evidence.UNKNOWN),
}


@pytest.mark.parametrize("case", list(_RECORDS))
def test_every_writer_classifies_the_task_from_its_mythic_record(session, mythic_issue, mythic_task_records,
                                                                 monkeypatch, case):
    record, expected = _RECORDS[case]
    mythic_task_records[TASK] = record
    tools = session()
    _capability_issue(tools)
    records = stub_mythic_operation(monkeypatch)
    task_row = {k: v for k, v in record.items() if k not in {"callback", "responses"}}
    records["tasks"] = [{"display_id": TASK, "id": TASK, "operation_id": 1, "command_name": "dcsync",
                         "original_params": json.dumps(DCSYNC_PARAMS), "operator": {"username": "operator"},
                         "callback": {"display_id": CALLBACK, "host": "WS01"}, **task_row}]
    without_output = case == "completion-error-without-output"
    records["outputs"] = {TASK: "" if without_output else DCSYNC_OUTPUT}
    asyncio.run(tools._reconcile_operation([str(TASK)], apply=False))

    [row] = _typed_rows(_reader())
    observed = _observations(row)["18"]
    # Reconciliation reads only terminal tasks, and records only a verified secret in the output it read.
    writers = {"raw", "capability"} | ({"reconcile"} if record.get("completed") and not without_output else set())
    reviewed = expected == task_evidence.POSTPROCESSING_ERROR_WITH_OUTPUT  # only this class enters review
    assert set(observed) == writers | ({"review"} if reviewed else set())
    assert {classification for _slot, classification in observed.values()} == {expected}
    achieved = expected == task_evidence.TERMINAL_SUCCESS
    assert {slot for writer, (slot, _c) in observed.items() if writer != "review"} == {
        "admitted" if achieved else "rejected"}
    assert row.payload["status"] == ("achieved" if achieved or reviewed else "pending")
    assert (WORKING in _achieved(tools)) is achieved
    assert (MATERIAL in _achieved(tools)) is reviewed


def test_a_task_with_no_mythic_record_is_unknown_and_never_achieved(session, mythic_task_records):
    mythic_task_records[TASK] = None  # Mythic returned no readable row
    tools = session()
    asyncio.run(tools._observe_task_record(TASK, CALLBACK))
    _capability(tools, TASK)
    [row] = _typed_rows(tools)
    assert _observations(row) == {"18": {"capability": ("rejected", task_evidence.UNKNOWN)}}
    assert row.payload["status"] == "pending"


@pytest.mark.parametrize(("record", "expected"), [
    ({"completed": True, "status": "success", **AGENT_PROCESSED}, task_evidence.TERMINAL_SUCCESS),
    ({"completed": True, "status": "completed", **AGENT_PROCESSED}, task_evidence.TERMINAL_SUCCESS),
    ({"completed": True, "status": "error", **AGENT_PROCESSED}, task_evidence.AGENT_ERROR_OR_FAILED),
    ({"completed": True, "status": "Error: subtask completion function - click cog to check stderr",
      **AGENT_PROCESSED, "responses": [{"id": 1}]}, task_evidence.AGENT_ERROR_OR_FAILED),
    ({"completed": True, "status": "Error: processing arguments"}, task_evidence.NOT_AGENT_PROCESSED),
    ({"completed": False, "status": "submitted"}, task_evidence.NOT_AGENT_PROCESSED),
    ({"completed": False, "status": "agent processing", **AGENT_PROCESSED}, task_evidence.UNKNOWN),
    ({"completed": True, "status": "Completion Function Running...", **AGENT_PROCESSED}, task_evidence.UNKNOWN),
    ({"completed": True, "status": task_evidence.MYTHIC_COMPLETION_FUNCTION_ERROR, **AGENT_PROCESSED,
      "responses": [{"id": 1}]}, task_evidence.POSTPROCESSING_ERROR_WITH_OUTPUT),
    ({"completed": True, "status": task_evidence.MYTHIC_COMPLETION_FUNCTION_ERROR,
      "status_timestamp_processing": "2026-11-07T00:00:01Z", "responses": [{"id": 1}]}, task_evidence.UNKNOWN),
    ({"completed": True, "status": "success", "status_timestamp_processing": "2026-11-07T00:00:01Z"},
     task_evidence.UNKNOWN),
    ({"completed": True, "status": "success"}, task_evidence.UNKNOWN),
    (None, task_evidence.UNKNOWN),
])
def test_the_classifier_reads_mythic_constants_and_stage_timestamps(record, expected):
    row = dict(record, display_id=TASK) if isinstance(record, dict) else record
    assert task_evidence.classify_task(row) == expected


# --- typed identity: the same account merges; anything else stays distinct -------------------------------------------

@pytest.mark.parametrize(("effect", "identity"), [
    (f"creds:{ACCOUNT}@{REALM}", TYPED),
    (f"creds:north\\{ACCOUNT}@{REALM}", TYPED),
    (f"CREDS:{ACCOUNT.upper()}@{REALM.upper()}", TYPED),
    (f"cred-material:ntlm:{ACCOUNT}@{REALM}", TYPED),
    (f"krbtgt-hash:{REALM}", f"account-secret:krbtgt@{REALM}"),
    (f"creds:south\\{ACCOUNT}@{REALM}", None),          # a prefix that is not the realm's label is ambiguous
    (f"creds:{ACCOUNT}@north", None),                   # no dotted realm
    (f"creds:{ACCOUNT}", None),
    ("cred-material:dump:ws01.north.example.local", None),  # host-scoped material is not an account secret
    (f"managed-local-admin-secret:ws01@{REALM}", None),
    (f"certificate-auth:{ACCOUNT}@{REALM}", None),
    (f"da:{REALM}", None),
    (f"ds-replication-rights:{REALM}", None),
])
def test_only_account_secrets_have_a_typed_identity(effect, identity):
    canonical = task_evidence.canonical_effect(effect)
    assert (canonical.identity if canonical else None) == identity


def test_near_matching_accounts_and_realms_keep_separate_rows(session, mythic_task_records):
    tools = session()
    for task_id, account, realm in ((40, ACCOUNT, REALM), (41, ACCOUNT, "south.example.local"),
                                    (42, f"{ACCOUNT}2", REALM)):
        mythic_task_records[task_id] = dict(SUCCESS)
        asyncio.run(tools._observe_task_record(task_id, CALLBACK))
        action = capabilities.CapabilityAction(
            name="dcsync-account", target=f"domain={realm};account={account}", preconditions=[],
            effects=[f"creds:{account}@{realm}"], intent={"capability": "dcsync-account"})
        probe = dict(credential_artifacts.extract_credential_probe(DCSYNC_OUTPUT), callback_id=str(CALLBACK))
        tools.record_capability_result(action, probe, evidence={
            "mythic_task_id": task_id, "callback_id": str(CALLBACK), "command": "dcsync",
            "transaction_id": f"tx-{task_id}"})
    identities = sorted(row.payload["canonical_effect"] for row in tools._ledger_snapshot().hop_rows())
    assert identities == sorted([TYPED, f"account-secret:{ACCOUNT}@south.example.local",
                                 f"account-secret:{ACCOUNT}2@{REALM}"])
    # A non-credential effect from the same task keeps its writer's row.
    mythic_task_records[43] = dict(SUCCESS)
    asyncio.run(tools._observe_task_record(43, CALLBACK))
    grant = capabilities.CapabilityAction(name="grant-directory-rights", target=f"domain={REALM}", preconditions=[],
                                          effects=[f"ds-replication-rights:{REALM}"])
    tools.record_capability_result(grant, {"ds_replication_rights": True}, evidence={
        "mythic_task_id": 43, "callback_id": str(CALLBACK), "command": "standin", "transaction_id": "tx-43"})
    grant_row = next(row for row in tools._ledger_snapshot().hop_rows()
                     if row.payload["effect"] == f"ds-replication-rights:{REALM}")
    assert grant_row.identity.startswith("capability:grant-directory-rights:")
    assert "contributions" not in grant_row.payload


# --- an earlier row for the effect is adopted, and its output-only proof is not grandfathered -------------------------

def test_an_earlier_output_only_row_is_adopted_and_revalidated(session, mythic_task_records):
    tools = session()
    legacy_proof = proof_boundary.make_runtime_task_envelope(  # written before the shared classification existed
        engagement_id=tools._eng_key(), callback_id=CALLBACK, task_id=TASK, terminal_status="completed",
        command="dcsync", verifier_id="capability:dcsync-account", captured_at=_now(), transaction_id="tx-old",
        verifier_input={"probe": {}}, verifier_result={"verdict": "achieved"},
    )
    legacy = engagement_state.record_effect_result(
        engagement_state.EngagementState(objective="", engagement_id=tools._eng_key()),
        "capability:dcsync-account", f"domain={REALM};account={ACCOUNT}", WORKING, "achieved",
        {"mythic_task_id": str(TASK), "callback_id": str(CALLBACK), "transaction_id": "tx-old",
         "terminal_task_status": "completed", "command": "dcsync"}, _now(),
        proof_envelope=legacy_proof.to_dict(), engagement_id=tools._eng_key(),
    )
    binding = tools._require_ledger_binding()
    tools._ledger_store().apply(binding, engagement_store.hop_mutations([], legacy.hops))
    [old] = tools._ledger_snapshot().hop_rows()
    assert old.payload["status"] == "achieved"

    mythic_task_records[TASK] = COMPLETION_ERROR
    asyncio.run(tools._observe_task_record(TASK, CALLBACK))
    _capability(tools, TASK)

    [row] = tools._ledger_snapshot().hop_rows()
    assert (row.number, row.identity) == (old.number, old.identity)  # number, identity and history are kept
    assert row.payload["canonical_effect"] == TYPED and row.payload["status"] == "pending"
    legacy_observation = row.payload["contributions"][0]["writers"]["legacy"]
    assert "admitted" in legacy_observation  # the stored historical record is retained verbatim
    reasons = {obs.get("admission_reason") for slots in row.payload["contributions"][0]["writers"].values()
               for obs in slots.values()}
    assert "task is not terminal-success" in reasons
    assert WORKING not in _achieved(tools)
    kinds = [event["kind"] for event in tools._ledger_store().history(binding, row_id=row.row_id)]
    assert kinds[0] == "create" and kinds[-1] == "update"


def test_an_operator_retraction_on_the_effect_stays_in_force(session, mythic_task_records):
    mythic_task_records[TASK] = SUCCESS
    tools = session()
    asyncio.run(tools._observe_task_record(TASK, CALLBACK))
    _capability(tools, TASK)
    binding = tools._require_ledger_binding()
    [row] = _typed_rows(tools)
    tools._ledger_store().retract(binding, [str(row.number)], reason="operator removed",
                                  actor=engagement_store.Actor(kind="operator", name="op"))
    mythic_task_records[TASK + 1] = dict(SUCCESS)
    asyncio.run(tools._observe_task_record(TASK + 1, CALLBACK))
    _capability(tools, TASK + 1)  # a later proving task for the retracted effect
    [after] = _typed_rows(_reader())
    assert after.number == row.number and after.retraction is not None
    assert WORKING not in _achieved(_reader())


def test_the_store_keeps_events_append_only_across_merges(session, mythic_task_records):
    mythic_task_records[TASK] = SUCCESS
    tools = session()
    asyncio.run(tools._observe_task_record(TASK, CALLBACK))
    _capability(tools, TASK)
    with pytest.raises(sqlite3.DatabaseError):
        connection = sqlite3.connect(tools._ledger_store().path)
        try:
            connection.execute("DELETE FROM esl_events")
        finally:
            connection.close()


# --- reconciliation never lets one task's stored observation hide another task's --------------------------------------

def _history_task(display_id: int, **record) -> dict:
    return {"display_id": display_id, "id": display_id, "operation_id": 1, "command_name": "dcsync",
            "original_params": json.dumps(DCSYNC_PARAMS), "operator": {"username": "operator"},
            "callback": {"display_id": CALLBACK, "host": "WS01"}, **record}


@pytest.mark.parametrize("second_pass", ["cursor", "cited"])
def test_a_cited_later_task_does_not_hide_an_earlier_tasks_proof(session, monkeypatch, second_pass):
    """Found in review: one typed row's mutation carried tasks 18 and 25 under task 25's reconcile ID, already stored
    by ``/state reconcile 25``, so task 18's admitted proof never landed while the cursor moved past it."""
    tools = session()
    records = stub_mythic_operation(monkeypatch)
    records["tasks"] = [
        _history_task(18, completed=True, status="success", **AGENT_PROCESSED),
        _history_task(25, completed=True, status=task_evidence.MYTHIC_COMPLETION_FUNCTION_ERROR, **AGENT_PROCESSED),
    ]
    records["outputs"] = {18: DCSYNC_OUTPUT, 25: DCSYNC_OUTPUT}

    asyncio.run(tools._reconcile_operation(["25"], apply=False))  # /state reconcile 25
    [row] = _typed_rows(_reader())
    assert [c["task_id"] for c in row.payload["contributions"]] == ["25"]
    assert WORKING not in _achieved(_reader())  # task 25's completion error proves material at most

    if second_pass == "cursor":
        asyncio.run(tools._reconcile_operation())  # the end-of-turn pass reads 18 and 25 above the cursor
    else:
        asyncio.run(tools._reconcile_operation(["18", "25"], apply=False))  # 25 last: its ID is already stored
    [row] = _typed_rows(_reader())
    assert [c["task_id"] for c in row.payload["contributions"]] == ["18", "25"]
    assert row.payload["status"] == "achieved"
    assert WORKING in _achieved(_reader())


def test_two_credential_store_entries_of_one_task_are_two_observations(session, monkeypatch):
    tools = session()
    task = {"display_id": TASK, "operation_id": 1, "status": "success", "completed": True, **AGENT_PROCESSED,
            "command_name": "dcsync", "original_params": json.dumps(DCSYNC_PARAMS), "callback": {"display_id": CALLBACK}}
    state = engagement_state.EngagementState(objective="", hops=list(tools._ledger_view().hops),
                                             engagement_id=tools._eng_key(), runtime_scope=True)
    before = list(state.hops)
    for credential_id, kind in ((5, "hash"), (9, "key")):
        state, _identity, _effect, reason = tools._credential_hop(
            state, {"id": credential_id, "operation_id": 1, "account": ACCOUNT, "realm": REALM, "type": kind,
                    "credential_text": "synthetic", "task": task}, _now(), source="operation_reconcile",
            require_admitted=True)
        assert not reason
    assert tools._ledger_commit_hops(before, state.hops)
    [row] = _typed_rows(_reader())
    assert set(row.payload["contributions"][0]["writers"]) == {"credential_store:5", "credential_store:9"}
    assert row.payload["status"] == "achieved"


def test_a_no_key_reprobe_keeps_the_typed_rows_verified_achievement(session, mythic_task_records):
    mythic_task_records[TASK] = SUCCESS
    tools = session()
    asyncio.run(tools._observe_task_record(TASK, CALLBACK))
    tools._pending_engagement_hop = ("dcsync-user", f"north\\{ACCOUNT}@{REALM}", _now())
    tools._record_engagement_success(DCSYNC_OUTPUT, task_id=TASK, callback_id=CALLBACK, command="dcsync")
    assert tools._prior_verified_credential_hop("dcsync-user", f"north\\{ACCOUNT}@{REALM}") is not None
