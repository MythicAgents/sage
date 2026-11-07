"""Engagement ledger Phase 4: progress rows from any issuer, per-request retry memory, credential material.

These reproduce the observed failures with real `MythicTools` sessions on a scratch `sage.db`. Only the Mythic
transport readers are stubbed. The store, reducers, proof boundary, verifiers and request contract are real.

Evidence these fixtures follow (shapes only; no range output, credential or hash is copied):

- Channel 4, task 17: the operator ran a step by hand and no ledger row recorded it.
- Channel 3/4: a failed capability row kept suppressing that capability for every later request and channel.
- The #130 channel 3 run: a task-linked credential-store entry from a Kerberoast became a working credential.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from ai.langgraph import access_reconciler, autonomous_controller, capabilities, engagement_state
from ai.langgraph import controller_recovery, engagement_ledger, engagement_store
from ai.langgraph import model as model_module
from ai.langgraph import mythic_tools as mythic_tools_module
from ai.langgraph import state_reconcile as sr
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract
from ai.langgraph.turn_authority import authority_from_request_contract
from tests.esl_fixtures import (
    AGENT_PROCESSED, channel34_ledger, ingest_proof, seed_ledger, stub_mythic_operation, write_legacy_ledger,
)

ENGAGEMENT = "eng-phase4"
DOMAIN = "sevenkingdoms.local"
NORTH = "north.sevenkingdoms.local"
# Synthetic, secret-free key material shaped like DCSync output.
NTLM = "0123456789abcdef0123456789abcdef"
AES256 = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
TGS_HASH = "$krb5tgs$23$*svc_fixture$" + DOMAIN.upper() + "$fixture/spn*$" + "ab" * 16 + "$" + "cd" * 40


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _contract(request_id: str, channel_id: str, mode: str = "supervised"):
    return build_request_contract(
        request_id=request_id,
        channel_id=channel_id,
        operation_id="operation-1",
        mode=mode,
        autonomous_solve=False,
    )


def _foothold(callback_id: str = "3") -> engagement_state.Foothold:
    return engagement_state.Foothold(
        callback_id=callback_id, agent="apollo", host="castelblack", forest=NORTH,
        identity="north\\samwell.tarly", integrity="medium", alive=True, source="mythic", timestamp=_now(),
    )


def _session(name: str, channel: int, request: int, mode: str = "supervised") -> MythicTools:
    tools = MythicTools(agent_task_id=name)
    tools.client = object()
    tools.set_request_contract(_contract(f"chat:{channel}:request:{request}", f"channel-{channel}", mode))
    tools._engagement_footholds = [_foothold()]
    tools._engagement_objective = lambda: ""  # type: ignore[method-assign]
    tools._require_ledger_binding()
    return tools


@pytest.fixture
def operation(monkeypatch):
    monkeypatch.setattr(mythic_tools_module, "SAGE_ENGAGEMENT_ID", ENGAGEMENT)

    async def live_footholds(client, _now):  # Mythic transport stub: the session's callbacks are live
        return list(client._engagement_footholds)

    monkeypatch.setattr(access_reconciler, "reconcile_access", live_footholds)
    return _session


def _frontier(tools: MythicTools) -> list:
    """The capability frontier as the auto controller observes it for this session's current request."""
    model = SimpleNamespace(mythic_client=tools)
    state = asyncio.run(model_module.Model._build_current_engagement_state(model))
    state.hops, state.graph_facts, state.corrected_hops = model_module._ledger_lists(tools)
    return capabilities.actions_from_state(state)


def _achieved(tools: MythicTools) -> set[str]:
    return engagement_state.EngagementState(
        objective="", hops=list(tools._engagement_hops), engagement_id=tools._eng_key(), runtime_scope=True,
    ).achieved_effects()


# --- operator work from another channel ----------------------------------------------------------------------------

def _dcsync_task(display_id: int, callback: int) -> dict:
    return {
        "display_id": display_id,
        "id": display_id,
        "command_name": "dcsync",
        "original_params": json.dumps({"Domain": DOMAIN, "User": "krbtgt", "DC": f"kingslanding.{DOMAIN}"}),
        "status": "completed",
        "completed": True,
        **AGENT_PROCESSED,
        "operator": {"username": "operator_by_hand"},
        "callback": {"display_id": callback, "host": "CASTELBLACK", "user": "samwell.tarly"},
        "callback_display_id": callback,
    }


def _dcsync_output() -> str:
    return (
        "Object RDN           : krbtgt\n** SAM ACCOUNT **\nCredentials:\n"
        f"  Hash NTLM: {NTLM}\n* Primary:Kerberos-Newer-Keys *\n    Credentials\n"
        f"      aes256_hmac       (4096) : {AES256}\n"
    )


@pytest.fixture
def mythic_records(monkeypatch):
    """Serve the operation's recorded tasks, outputs, credentials and callbacks through stubbed Mythic transport."""
    return stub_mythic_operation(monkeypatch)


def _turn_end(tools: MythicTools) -> str:
    model = SimpleNamespace(mythic_client=tools)
    return asyncio.run(model_module.Model._engagement_state_turn_summary_text(model))


def _position(tools: MythicTools) -> engagement_store.ReconciliationState:
    return tools._ledger_store().reconciliation_state(tools._require_ledger_binding())


def _rows(tools: MythicTools, effect_prefix: str) -> list:
    return [hop for hop in tools._ledger_view().hops if hop.effect.startswith(effect_prefix)]


def test_a_task_the_operator_ran_by_hand_is_recorded_at_the_next_turn_end(operation, mythic_records):
    channel_a = operation("channel-a", channel=3, request=1)
    mythic_records["tasks"] = [_dcsync_task(17, callback=13)]
    mythic_records["outputs"] = {17: _dcsync_output()}

    _turn_end(channel_a)  # channel A issued nothing; the task came from the operator on another channel

    rows = [hop for hop in channel_a._engagement_hops if hop.effect == f"krbtgt-hash:{DOMAIN}"]
    assert [(hop.status, str(hop.evidence.get("mythic_task_id"))) for hop in rows] == [("achieved", "17")]
    count = len(channel_a._ledger_snapshot().hop_rows())
    events = len(channel_a._ledger_store().export(channel_a._require_ledger_binding())["history"])
    _turn_end(channel_a)  # replaying the pass changes nothing
    assert len(channel_a._ledger_snapshot().hop_rows()) == count
    channel_b = operation("channel-b", channel=4, request=1)
    asyncio.run(channel_b._reconcile_operation(["17"]))  # another channel citing the same task adds nothing
    assert len(channel_a._ledger_store().export(channel_a._require_ledger_binding())["history"]) == events


# --- callback gains from any issuer ----------------------------------------------------------------------------

def _callback_row(display_id: int, host: str, user: str, integrity: int, init: str) -> dict:
    return {"display_id": display_id, "agent_callback_id": f"agent-{display_id}", "host": host, "user": user,
            "integrity_level": integrity, "init_callback": init}


def _task(display_id: int, command: str = "ps", *, callback: int = 13, status: str = "completed",
          at: str = "2026-11-05T10:00:00", operation_id: int = 1, params: str = "") -> dict:
    return {"display_id": display_id, "id": display_id, "operation_id": operation_id, "command_name": command,
            "original_params": params, "status": status, "completed": status not in _RUNNING,
            **(AGENT_PROCESSED if status not in _RUNNING else {}),
            "timestamp": at, "status_timestamp_preprocessing": at, "operator": {"username": "operator_by_hand"},
            "callback": {"display_id": callback, "host": "CASTELBLACK", "user": "samwell.tarly"}}


_RUNNING = {"submitted", "processing", "preprocessing"}
FOOTHOLD = _callback_row(13, "CASTELBLACK", "NORTH\\samwell.tarly", 2, "2026-11-05T09:00:00")


def test_a_new_callback_spawned_by_another_channels_task_is_credited_to_that_task(operation, mythic_records):
    tools = operation("channel-a", channel=3, request=1)
    mythic_records["callbacks"] = [FOOTHOLD]
    _turn_end(tools)  # the operation's first reconciliation stores the baseline
    assert _position(tools).callback_baseline == {"13": "medium"} and _rows(tools, "session:") == []

    mythic_records["tasks"] = [_task(20, "spawn", at="2026-11-05T10:00:00")]
    mythic_records["callbacks"].append(_callback_row(21, "WINTERFELL", "NORTH\\eddard.stark", 3, "2026-11-05T10:01:00"))
    _turn_end(tools)

    rows = {hop.effect: hop for hop in _rows(tools, "session:") + _rows(tools, "admin:")}
    assert set(rows) == {"session:north\\eddard.stark@winterfell", "admin:winterfell"}
    assert {(hop.status, hop.evidence["mythic_task_id"], hop.evidence["credited_task"]) for hop in rows.values()} == {
        ("achieved", "20", 20)}
    assert _position(tools).callback_baseline == {"13": "medium", "21": "high"}


def test_an_uncredited_callback_is_admitted_only_once_a_task_on_it_completes(operation, mythic_records):
    tools = operation("channel-a", channel=3, request=1)
    mythic_records["callbacks"] = [FOOTHOLD]
    _turn_end(tools)
    mythic_records["callbacks"].append(_callback_row(22, "MEEREEN", "ESSOS\\daenerys", 2, "2026-11-05T12:00:00"))
    _turn_end(tools)
    assert _rows(tools, "session:") == []  # nothing to credit and nothing completed on it

    mythic_records["tasks"] = [_task(23, callback=22, status="submitted", at="2026-11-05T12:30:00")]
    _turn_end(tools)
    assert _rows(tools, "session:") == [] and _position(tools).held_tasks == (23,)

    mythic_records["tasks"] = [_task(23, callback=22, at="2026-11-05T12:30:00")]
    _turn_end(tools)
    [row] = _rows(tools, "session:")
    assert (row.effect, row.status, row.evidence["mythic_task_id"], row.evidence["credited_task"]) == (
        "session:essos\\daenerys@meereen", "achieved", "23", "none")
    assert _position(tools).held_tasks == () and _position(tools).task_cursor == 23


@pytest.mark.parametrize("baseline, integrity, expected", [
    (2, 4, ["system:castelblack"]), (2, 3, ["admin:castelblack"]), (2, 2, []), (2, 1, []),
    (3, 3, []), (4, 4, []), (4, 3, []),
])
def test_an_integrity_rise_on_an_existing_callback_records_only_a_higher_level(operation, mythic_records, baseline,
                                                                                 integrity, expected):
    tools = operation("channel-a", channel=3, request=1)
    mythic_records["callbacks"] = [{**FOOTHOLD, "integrity_level": baseline}]
    _turn_end(tools)
    mythic_records["tasks"] = [_task(25, "getsystem")]
    mythic_records["callbacks"] = [{**FOOTHOLD, "integrity_level": integrity}]
    _turn_end(tools)
    assert [hop.effect for hop in _rows(tools, "system:") + _rows(tools, "admin:")] == expected


# --- credential material from the operation --------------------------------------------------------------------

def _store_credential(credential_id: int, task: dict | None, *, account: str = "svc_fixture",
                      text: str = TGS_HASH, operation_id: int = 1) -> dict:
    linked = None if task is None else {
        "display_id": task["display_id"], "operation_id": task.get("operation_id", 1), "status": task["status"],
        "completed": task["completed"], "command_name": task["command_name"], "original_params": task["original_params"],
        "status_timestamp_processing": task.get("status_timestamp_processing"),
        "status_timestamp_processed": task.get("status_timestamp_processed"),
        "callback": {"display_id": task["callback"]["display_id"]}}
    return {"id": credential_id, "operation_id": operation_id, "account": account, "realm": DOMAIN.upper(),
            "type": "hash", "credential_text": text, "comment": "", "task": linked}


KERBEROAST = json.dumps({"assembly_name": "Rubeus.exe", "assembly_arguments": "kerberoast /nowrap"})


def test_a_task_linked_hash_from_any_issuer_is_material_and_opens_no_working_credential(operation, mythic_records):
    tools = operation("channel-a", channel=3, request=1)
    roast = _task(30, "execute_assembly", params=KERBEROAST)
    mythic_records["tasks"] = [roast]
    mythic_records["credentials"] = [_store_credential(41, roast)]
    _turn_end(tools)

    [row] = _rows(tools, "cred-material:")
    assert (row.effect, row.status, row.evidence["mythic_task_id"]) == (
        f"cred-material:hash:svc_fixture@{DOMAIN}", "achieved", "30")
    assert f"creds:svc_fixture@{DOMAIN}" not in _achieved(tools)  # a working-credential precondition stays unmet


def test_cited_ids_reply_one_line_each_with_the_row_or_the_reason(operation, mythic_records):
    tools = operation("channel-a", channel=3, request=1)
    roast = _task(30, "execute_assembly", params=KERBEROAST)
    mythic_records["tasks"] = [_dcsync_task(17, callback=13) | {"operation_id": 1, "timestamp": ""}, roast,
                               _task(31, status="submitted"), _task(32, operation_id=2)]
    mythic_records["outputs"] = {17: _dcsync_output()}
    mythic_records["credentials"] = [_store_credential(41, roast), _store_credential(42, None, account="hand")]

    lines = asyncio.run(tools._reconcile_operation(["17", "30", "31", "32", "99", "cred:42"]))
    assert lines[:6] == [
        "- 17: added row #1.",
        "- 30: added row #2.",
        "- 31: not added, task not terminal.",
        "- 32: not added, not in this operation.",
        "- 99: not added, no such task.",
        "- cred:42: not added, no producing task (a later action must prove it; not supported yet).",
    ]
    assert asyncio.run(tools._reconcile_operation(["17", "cred:41"]))[:2] == [
        "- 17: already recorded (row #1).", "- cred:41: already recorded (row #2)."]
    assert asyncio.run(tools._reconcile_operation(["5"]))[0] == "- 5: not added, no such task."
    assert _position(tools).task_cursor == 0  # citations never move the cursor


def test_near_matches_write_no_row(operation, mythic_records):
    tools = operation("channel-a", channel=3, request=1)
    elsewhere = _dcsync_task(40, callback=13) | {"operation_id": 2}
    errored = _task(41, "execute_assembly", status="error: access denied", params=KERBEROAST)
    other_op = _task(42, "execute_assembly", operation_id=2, params=KERBEROAST)
    mythic_records["tasks"] = [elsewhere, errored, other_op, _task(43, status="processing")]
    mythic_records["outputs"] = {40: _dcsync_output()}
    mythic_records["credentials"] = [_store_credential(51, errored), _store_credential(52, other_op),
                                     _store_credential(53, _task(41, "execute_assembly", operation_id=2,
                                                                 params=KERBEROAST))]
    _turn_end(tools)
    assert tools._ledger_view().hops == [] and _position(tools).held_tasks == (43,)
    assert asyncio.run(tools._reconcile_operation(["40", "cred:52", "cred:51"]))[:3] == [
        "- 40: not added, not in this operation.",
        "- cred:52: not added, the producing task is not in this operation.",
        "- cred:51: not added, no proven gain (the producing task's lineage is incomplete)."]


# --- the cursor ------------------------------------------------------------------------------------------------

def test_a_task_that_never_finishes_does_not_stall_later_reconciliation(operation, mythic_records):
    tools = operation("channel-a", channel=3, request=1)
    # A task queued on a dead callback stays running; 50 per pass would otherwise re-read 100..149 forever.
    mythic_records["tasks"] = ([_task(100, status="submitted")] + [_task(i) for i in range(101, 160)]
                               + [_dcsync_task(160, callback=13) | {"operation_id": 1, "timestamp": ""}])
    mythic_records["outputs"] = {160: _dcsync_output()}
    _turn_end(tools)
    assert (_position(tools).task_cursor, _position(tools).held_tasks) == (149, (100,))  # 50 per pass
    assert _rows(tools, "krbtgt-hash:") == []
    _turn_end(tools)
    assert [hop.status for hop in _rows(tools, "krbtgt-hash:")] == ["achieved"]
    assert (_position(tools).task_cursor, _position(tools).held_tasks) == (160, (100,))
    mythic_records["tasks"][0] = _task(100)  # it finally completes: evaluated once and released
    _turn_end(tools)
    assert (_position(tools).task_cursor, _position(tools).held_tasks) == (160, ())


def test_a_mythic_or_store_error_skips_the_pass_and_still_renders_the_ledger(operation, mythic_records, monkeypatch):
    tools = operation("channel-a", channel=3, request=1)
    seed_ledger(tools, hops=[{"id": "h1", "technique": "dcsync", "target": DOMAIN, "effect": f"krbtgt-hash:{DOMAIN}",
                              "status": "achieved", "evidence": {"mythic_task_id": 5}, "preconditions": [],
                              "satisfied_effects": [f"krbtgt-hash:{DOMAIN}"], "source": "test", "timestamp": _now()}])
    mythic_records["tasks"] = [_dcsync_task(17, callback=13)]
    mythic_records["outputs"] = {17: _dcsync_output()}
    mythic_records["fail"] = True
    text = _turn_end(tools)
    assert "krbtgt-hash" in text and _position(tools).task_cursor == 0  # the stored rows, not an empty ledger

    mythic_records["fail"] = False
    store = tools._ledger_store()

    def refuse(*args, **kwargs):
        raise engagement_store.LedgerWriteFailed("disk full")

    monkeypatch.setattr(store, "apply", refuse)
    text = _turn_end(tools)
    assert "krbtgt-hash" in text and _position(tools).task_cursor == 0
    assert "disk full" in tools._ledger_last_write_error


# --- retry memory ends with the request ----------------------------------------------------------------------------

def _seed_gpo_control(tools: MythicTools) -> None:
    proof = ingest_proof(tools._eng_key(), task_id="101", job_id="11")
    seed_ledger(tools, graph_facts=[
        {"predicate": predicate, "source": "bloodhound:cypher", "timestamp": _now(), "ttl_seconds": 3600,
         "proof_envelope": proof}
        for predicate in ("generic-write:gpo:starkwallpaper", f"gpo-domain:starkwallpaper:{NORTH}")
    ])


def _record_terminal_failure(tools: MythicTools, action) -> list[str]:
    payload = {"ok": False, "reason": "Access is denied.", "failure_class": "genuine"}
    issued = [{"task_id": 201, "command": "shell", "_output": "Access is denied."}]
    return tools._capability_executor_record_failed_attempt(payload, action, {}, "3", issued, {})


def _names(actions) -> set[tuple[str, str]]:
    return {(action.name, action.target) for action in actions}


def test_a_terminal_failure_suppresses_its_capability_only_for_the_rest_of_its_request(operation):
    channel_a = operation("channel-a", channel=3, request=1)
    _seed_gpo_control(channel_a)
    offered = _frontier(channel_a)
    assert offered, "fixture must offer a capability"
    action = offered[0]
    assert _record_terminal_failure(channel_a, action)

    assert (action.name, action.target) not in _names(_frontier(channel_a))  # same request: suppressed

    channel_b = operation("channel-b", channel=4, request=1)
    assert (action.name, action.target) in _names(_frontier(channel_b))  # another channel: offered

    channel_a.set_request_contract(_contract("chat:3:request:2", "channel-3"))
    assert (action.name, action.target) in _names(_frontier(channel_a))  # the next request: offered again
    assert not any(hop.status in {"failed", "blocked"} for hop in channel_a._ledger_view().hops)  # no ESL row


def _failed_once(operation):
    tools = operation("channel-a", channel=3, request=1)
    _seed_gpo_control(tools)
    action = _frontier(tools)[0]
    assert _record_terminal_failure(tools, action)
    return tools, (action.name, action.target)


def test_an_approval_resume_and_a_same_request_authority_change_keep_the_suppression(operation):
    tools, key = _failed_once(operation)
    # The approval resume reinstalls the same request; an authority change alters only the contract digest.
    resumed = _contract("chat:3:request:1", "channel-3", mode="auto")
    assert resumed.digest != tools._request_contract.digest
    tools.set_request_contract(resumed)
    assert key not in _names(_frontier(tools))


def test_an_unbound_session_never_inherits_a_bound_request_attempt(operation):
    tools, key = _failed_once(operation)
    tools._request_contract = None  # a session with no bound request
    assert tools._current_request_id() == "request:unbound"
    assert key in _names(_frontier(tools))


def test_a_recovered_auto_run_restores_its_attempts_and_an_old_journal_is_blocked(operation):
    tools, key = _failed_once(operation)
    resources = {"request_attempts": tools._request_attempts_payload()}

    fresh = operation("channel-a-after-restart", channel=3, request=1)  # same original request ID
    assert key in _names(_frontier(fresh))
    model = SimpleNamespace(mythic_client=fresh)
    model_module.Model._restore_controller_request_attempts(model, resources, "chat:3:request:1")
    assert key not in _names(_frontier(fresh))

    with pytest.raises(controller_recovery.RecoveryBlocked, match="missing"):
        model_module.Model._restore_controller_request_attempts(model, {"policy_model_calls": 1}, "chat:3:request:1")
    other = operation("channel-a-other-request", channel=3, request=2)
    with pytest.raises(controller_recovery.RecoveryBlocked, match="not restorable"):
        model_module.Model._restore_controller_request_attempts(
            SimpleNamespace(mythic_client=other), resources, "chat:3:request:2"
        )


class _ProcessDied(Exception):
    """The auto run's process ends at a clean controller boundary; only its recovery journal survives."""


def _auto_model(tools: MythicTools, resume_state: dict | None = None):
    """A Model on the auto lane carrying only the identity the recovery journal binds. Symbolic policy: no provider."""
    model = object.__new__(model_module.Model)
    model.mythic_client = tools
    model.mode = "auto"
    model._autonomous_solve = True
    model.policy_mode = "symbolic"
    model.provider = model.model = "fixture"
    model.operation_id, model.channel_id, model.apitoken_id = 1, 3, 1
    model._chat_request_config_signature = "fixture"
    model._request_contract = tools._request_contract
    model._controller_resume_state = resume_state
    return model


@pytest.fixture
def journal(monkeypatch, tmp_path):
    """The controller recovery journal on a scratch sage.db, under the explicitly unbounded auto arm it requires.
    The controller dies right after each `clean` journal write, the only boundary recovery resumes from."""
    path = str(tmp_path / "sage.db")

    class ScratchStore(controller_recovery.ControllerRecoveryStore):
        def __init__(self, operation_id, channel_id):
            super().__init__(operation_id, channel_id, db_path=path)

    monkeypatch.setenv("SAGE_AUTO_CONTROLLER_RECOVERY", "1")
    monkeypatch.setenv("SAGE_EVAL_UNBOUNDED_ARM", "1")
    monkeypatch.setattr(controller_recovery, "ControllerRecoveryStore", ScratchStore)
    write = autonomous_controller.AutonomousController._recovery_write

    def write_then_die(controller, phase):
        write(controller, phase)
        if phase == "clean":
            raise _ProcessDied(phase)

    monkeypatch.setattr(autonomous_controller.AutonomousController, "_recovery_write", write_then_die)
    return ScratchStore(1, 3)


def _run_auto(tools: MythicTools, resume_state: dict | None = None) -> None:
    asyncio.run(_auto_model(tools, resume_state)._run_autonomous_controller("obtain control of the GPO"))


def test_the_auto_journal_write_carries_the_attempt_memory_into_the_recovered_run(operation, journal):
    """The production journal path, not the restore helper alone: the controller writes its journal through the
    model's recovery resources, the process dies, and the recovered run restores the attempts from that journal."""
    tools = operation("channel-a", channel=3, request=1, mode="auto")
    _seed_gpo_control(tools)
    action = _frontier(tools)[0]
    key = (action.name, action.target)
    assert _record_terminal_failure(tools, action)  # a terminal failure earlier in this request
    with pytest.raises(_ProcessDied):
        _run_auto(tools)
    binding = controller_recovery.binding_for_model(_auto_model(tools), tools._request_contract)
    saved = journal.require_clean(binding, "chat:3:request:1")
    written = saved["resources"]["request_attempts"]
    assert written["request_id"] == "chat:3:request:1" and len(written["attempts"]) == 1, written

    fresh = operation("channel-a-after-restart", channel=3, request=1, mode="auto")  # same original request ID
    assert key in _names(_frontier(fresh)), "a new process starts with an empty attempt memory"
    with pytest.raises(_ProcessDied):
        _run_auto(fresh, resume_state=saved)
    assert key not in _names(_frontier(fresh)), "the recovered run must keep the request's terminal failure"
    rewritten = journal.require_clean(binding, "chat:3:request:1")["resources"]["request_attempts"]
    assert rewritten["attempts"] == written["attempts"], "the recovered run's own journal keeps the attempts"

    stripped = {**saved, "resources": {k: v for k, v in saved["resources"].items() if k != "request_attempts"}}
    journal.acquire()
    journal.write(binding, "clean", stripped)
    journal.release()
    old_journal = operation("channel-a-old-journal", channel=3, request=1, mode="auto")
    with pytest.raises(controller_recovery.RecoveryBlocked, match="missing"):
        _run_auto(old_journal, resume_state=stripped)
    assert key in _names(_frontier(old_journal)), "a blocked recovery restores nothing"


# --- credential material is not a working credential ---------------------------------------------------------------

def _credential_task(display_id: int, command: str, params: dict) -> dict:
    return {"display_id": display_id, "status": "success", "completed": True, **AGENT_PROCESSED, "command_name": command,
            "original_params": json.dumps(params), "callback": {"display_id": 3}}


def _step_with_credential(tools: MythicTools, credential: dict) -> dict:
    async def no_callbacks():
        return []

    async def no_records(_task_ids):
        return {}

    async def credentials(_now, force=False):
        return [credential]

    tools._step_callback_rows, tools._step_task_records, tools._fetch_credentials_cached = (
        no_callbacks, no_records, credentials)
    references = {"tasks": [], "graph_reads": [],
                  "credentials": [{"credential_id": credential["id"],
                                   "source_task_id": credential["task"]["display_id"]}]}
    return asyncio.run(tools._record_step_ledger(references, []))


def test_a_kerberoast_hash_in_the_credential_store_is_material_not_a_working_credential(operation):
    tools = operation("channel-a", channel=3, request=1)
    _step_with_credential(tools, {
        "id": 41, "account": "svc_fixture", "realm": DOMAIN.upper(), "type": "hash", "credential_text": TGS_HASH,
        "task": _credential_task(30, "execute_assembly",
                                 {"assembly_name": "Rubeus.exe", "assembly_arguments": "kerberoast /nowrap"}),
    })

    achieved = _achieved(tools)
    assert f"creds:svc_fixture@{DOMAIN}" not in achieved
    assert any(effect.startswith("cred-material:") and effect.endswith(f":svc_fixture@{DOMAIN}")
               for hop in tools._engagement_hops for effect in hop.satisfied_effects)


def test_a_dcsync_key_in_the_credential_store_stays_a_working_credential(operation):
    tools = operation("channel-a", channel=3, request=1)
    _step_with_credential(tools, {
        "id": 42, "account": "eddard.stark", "realm": DOMAIN.upper(), "type": "hash", "credential_text": NTLM,
        "task": _credential_task(31, "dcsync", {"Domain": DOMAIN, "User": "eddard.stark"}),
    })

    assert f"creds:eddard.stark@{DOMAIN}" in _achieved(tools)


# --- the ledger records only achieved and pending rows ---------------------------------------------------------

@pytest.mark.parametrize("status", ["failed", "blocked", "legacy_unverified"])
def test_no_writer_can_record_an_attempt_status_as_a_row(operation, status):
    tools = operation("channel-a", channel=3, request=1)
    state = engagement_state.EngagementState(objective="", engagement_id=tools._eng_key(), runtime_scope=True)
    with pytest.raises(ValueError, match="invalid hop status"):
        engagement_state.record_effect_result(state, "dcsync", DOMAIN, f"krbtgt-hash:{DOMAIN}", status, {}, _now())
    # The store refuses the status too, so no writer can bypass the reducer.
    row = {"technique": "dcsync", "target": DOMAIN, "effect": f"krbtgt-hash:{DOMAIN}", "status": status}
    with pytest.raises(engagement_store.LedgerWriteFailed, match="not a ledger row status"):
        tools._ledger_store().apply(tools._require_ledger_binding(), engagement_store.hop_mutations([], [row]))
    assert tools._ledger_snapshot().hop_rows() == []


def test_a_pending_row_satisfies_no_precondition(operation):
    tools = operation("channel-a", channel=3, request=1)
    state = engagement_state.EngagementState(objective="", engagement_id=tools._eng_key(), runtime_scope=True)
    for reason in ("awaiting_effect", "proof_missing"):
        pending = engagement_state.record_effect_result(
            state, "dcsync", DOMAIN, f"krbtgt-hash:{DOMAIN}", "pending", {}, _now(), pending_reason=reason)
        assert pending.hops[0].pending_reason == reason
        assert pending.achieved_effects() == set()
        assert "forge-golden-ticket" not in {
            action.name for action in capabilities.actions_from_state(
                engagement_state.EngagementState(objective="", footholds=[_foothold()], hops=pending.hops))
        }
    with pytest.raises(ValueError, match="invalid pending reason"):
        engagement_state.record_effect_result(
            state, "dcsync", DOMAIN, f"krbtgt-hash:{DOMAIN}", "pending", {}, _now(), pending_reason="failed")


def test_a_raw_task_that_does_not_prove_its_effect_is_a_request_attempt(operation):
    tools = operation("channel-a", channel=3, request=1)
    tools._pending_engagement_hop = ("dcsync", DOMAIN, _now())
    tools._record_engagement_success("ERROR_DS_DRA_ACCESS_DENIED", task_id=55, callback_id="3", command="dcsync")
    assert tools._ledger_snapshot().hop_rows() == []
    [attempt] = tools._request_attempt_view()
    assert (attempt.effect, attempt.terminal, attempt.task_id) == (f"krbtgt-hash:{DOMAIN}", True, "55")
    tools.set_request_contract(_contract("chat:3:request:2", "channel-3"))
    assert tools._request_attempt_view() == ()


# --- stored rows are migrated once ------------------------------------------------------------------------------

def _phase3_database(tmp_path, monkeypatch):
    """A schema-1 store as the Phase 3 import wrote it: blocked, failed, legacy_unverified and achieved rows, plus
    an operator retraction and a "no longer holds" marker."""
    data = channel34_ledger("phase3-fixture")
    data["hops"].append({**data["hops"][4], "number": 30, "id": "dcsync-user:x", "technique": "dcsync-user",
                         "target": f"x@{NORTH}", "effect": f"creds:x@{NORTH}", "status": "failed",
                         "satisfied_effects": [f"creds:x@{NORTH}"]})
    data["hops"].append({**data["hops"][4], "number": 31, "id": "dcsync-user:y", "technique": "dcsync-user",
                         "target": f"y@{NORTH}", "effect": f"creds:y@{NORTH}", "status": "failed",
                         "satisfied_effects": [f"creds:y@{NORTH}"]})
    data["next_hop_number"] = 32
    legacy = tmp_path / "phase3-legacy"
    source = write_legacy_ledger(legacy, data)
    database = tmp_path / "phase3.db"
    with monkeypatch.context() as patch:  # Phase 3 had no status migration
        patch.setattr(engagement_store, "_migrate_operation", lambda *_args: {"retracted": 0, "pending": 0})
        patch.setattr(engagement_store, "migrate_row_statuses", lambda _connection: {})
        engagement_store.import_legacy_file(source, database=database)
        store = engagement_store.EngagementStore(database, legacy_dir=str(legacy))
        binding = store.find_binding("phase3-fixture")
        operator = engagement_store.Actor(kind="operator", name="op")
        store.retract(binding, ["31"], reason="operator removed", actor=operator)
        store.invalidate(binding, ["25"], reason="password rotated", actor=operator)
    assert engagement_store.schema_status(database)[0]["schema_version"] == 1
    return database, legacy


def _ledger_state(database, legacy):
    store = engagement_store.EngagementStore(database, legacy_dir=str(legacy))
    binding = store.bind_namespace("phase3-fixture")  # the first write transaction initializes the store
    snapshot = store.snapshot(binding)
    connection = sqlite3.connect(database)
    events = connection.execute("SELECT id, row_id, kind, actor_name, reason FROM esl_events ORDER BY id").fetchall()
    op = connection.execute("SELECT next_hop_number, schema_version FROM esl_operations").fetchone()
    connection.close()
    return store, binding, snapshot, events, op


def test_store_initialization_migrates_each_legacy_status_once(tmp_path, monkeypatch):
    database, legacy = _phase3_database(tmp_path, monkeypatch)
    before = sqlite3.connect(database)
    numbers_before = dict(before.execute("SELECT identity, number FROM esl_rows WHERE kind='hop'").fetchall())
    facts_before = before.execute("SELECT identity, payload FROM esl_rows WHERE kind='graph_fact'").fetchall()
    before.close()

    store, binding, snapshot, events, op = _ledger_state(database, legacy)
    rows = {row.number: row for row in snapshot.hop_rows()}
    migration = [event for event in events if event[3] == "system:schema-migration"]
    assert sorted((rows_by_id := {row.row_id: row.number for row in rows.values()})[event[1]] for event in migration) == [27, 28, 30]
    assert {rows_by_id[event[1]]: (event[2], event[4]) for event in migration} == {
        27: ("retract", "attempt record, not progress"),
        28: ("update", "unverified achievement: proof missing"),
        30: ("retract", "attempt record, not progress"),
    }
    assert rows[27].payload["status"] == "blocked" and rows[27].retraction["actor"] == "system:schema-migration"
    assert rows[30].payload["status"] == "failed" and rows[30].retraction["actor"] == "system:schema-migration"
    assert rows[31].retraction["actor"] == "op"  # the operator's retraction is kept, not replaced
    assert (rows[28].payload["status"], rows[28].payload["pending_reason"]) == ("pending", "proof_missing")
    assert rows[25].invalidation["reason"] == "password rotated" and rows[25].payload["status"] == "achieved"
    assert op == (32, engagement_store.SCHEMA_VERSION)
    after = sqlite3.connect(database)
    assert dict(after.execute("SELECT identity, number FROM esl_rows WHERE kind='hop'").fetchall()) == numbers_before
    assert after.execute("SELECT identity, payload FROM esl_rows WHERE kind='graph_fact'").fetchall() == facts_before
    after.close()

    # The runtime view shows the migrated rows as retracted attempt records, never as current failures.
    eligible, ineligible = snapshot.runtime_hop_dicts()
    assert not any(hop["status"] in {"failed", "blocked", "legacy_unverified"} for hop in eligible)
    table = "\n".join(engagement_ledger.render_hop_table(snapshot.display_hops()))
    assert "`blocked — retracted attempt record`" in table
    assert "`pending (proof missing)`" in table
    assert engagement_store.schema_status(database) == [{
        "engagement_key": "phase3-fixture", "schema_version": engagement_store.SCHEMA_VERSION,
        "migrated_rows": {"retracted": 2, "pending": 1},
        # Schema 3: the krbtgt row written before the shared task classification is revalidated (the retracted
        # attempt record for the same secret owns nothing).
        "revalidated_rows": {"updated": 1, "merged": 0},
    }]

    # A second initialization changes nothing.
    _store, _binding, _snapshot, events_again, op_again = _ledger_state(database, legacy)
    assert events_again == events and op_again == op


def test_a_legacy_import_is_migrated_in_the_same_transaction(tmp_path):
    legacy = tmp_path / "legacy"
    source = write_legacy_ledger(legacy, channel34_ledger("imported-fixture"))
    database = tmp_path / "imported.db"
    assert engagement_store.import_legacy_file(source, database=database) == "imported"
    [status] = engagement_store.schema_status(database)
    assert status["schema_version"] == engagement_store.SCHEMA_VERSION
    assert status["migrated_rows"] == {"retracted": 1, "pending": 1}


# --- request-scoped windows follow the request ID; the session breakers do not ------------------------------------

_ROTATION_FAIL = "[-] failed to parse arguments for ldap_query: ldap_query takes no command line arguments."
_ROTATIONS = ({"user": "a"}, {"user": "b"}, {"user": "c"}, {"user": "d"})


def _bind(tools: MythicTools, request: int, *, autonomous_solve: bool = True) -> None:
    contract = build_request_contract(
        request_id=f"chat:3:request:{request}", channel_id="channel-3", operation_id="operation-1",
        mode="auto", autonomous_solve=autonomous_solve,
    )
    tools.set_request_contract(contract)
    tools._turn_authority = authority_from_request_contract(contract)  # the final-sink projection of that request


@pytest.fixture
def mythic_issue(monkeypatch):
    """Mythic transport stub for task issue: every task fails with ``output``; issued parameters are recorded."""
    issued: list = []
    output = {"text": _ROTATION_FAIL}

    async def issue_task(mythic, command_name, parameters, callback_display_id, wait_for_complete=True, timeout=None):
        issued.append((command_name, parameters))
        return {"display_id": 4000 + len(issued)}

    async def waitfor(mythic, task_display_id, timeout=None):
        return output["text"]

    monkeypatch.setattr(mythic_tools_module.mythic, "issue_task", issue_task)
    monkeypatch.setattr(mythic_tools_module.mythic, "waitfor_for_task_output", waitfor)
    return issued, output


def _issue(tools: MythicTools, command: str, params, callback: int = 7) -> str:
    return asyncio.run(tools.issue_task_and_waitfor_task_output(command, params, callback))


def test_the_shape_window_follows_the_request_id_not_the_contract_digest(mythic_issue):
    issued, _output = mythic_issue
    tools = MythicTools(agent_task_id="shape-window")
    tools.client = object()
    _bind(tools, 1)
    _issue(tools, "ldap_query", _ROTATIONS[0])
    _issue(tools, "ldap_query", _ROTATIONS[1])
    _bind(tools, 1, autonomous_solve=False)  # same request, different authority: a digest change only
    kept = _issue(tools, "ldap_query", _ROTATIONS[2])
    _bind(tools, 2)  # a new request
    fresh = _issue(tools, "ldap_query", _ROTATIONS[3])
    assert kept.startswith("STOP"), f"a same-request digest change must keep the window: {kept[:200]}"
    assert not fresh.startswith("STOP"), f"a new request opens its own window: {fresh[:200]}"
    assert len(issued) == 3


def test_the_session_breaker_still_refuses_an_identical_failed_command_in_a_new_request(mythic_issue):
    """Two identical failures in request 1, then a real new request binds on the same channel: the session
    breaker keeps refusing the identical command, while request-scoped retry memory starts empty."""
    issued, output = mythic_issue
    output["text"] = "[-] failed to parse arguments for rev2self: rev2self takes no command line arguments."
    tools = MythicTools(agent_task_id="session-breaker")
    tools.client = object()
    _bind(tools, 1)
    _issue(tools, "rev2self", {}, 11)
    _issue(tools, "rev2self", {}, 11)
    _bind(tools, 2)
    assert tools._request_attempt_view() == () and tools._request_shape_counts() == {}
    again = _issue(tools, "rev2self", {}, 11)
    assert again.startswith("STOP"), f"the session breaker must survive the request boundary: {again[:200]}"
    assert len(issued) == 2


# --- the reconciliation position in the store ------------------------------------------------------------------

def test_the_store_moves_the_cursor_forward_and_never_drops_a_held_task(tmp_path):
    store = engagement_store.EngagementStore(tmp_path / "sage.db", legacy_dir=str(tmp_path / "legacy"))
    binding = store.bind_namespace("position")
    assert store.reconciliation_state(binding) == engagement_store.ReconciliationState()
    update = engagement_store.ReconciliationUpdate
    store.apply(binding, [], reconciliation=update(task_cursor=20, held_added=(12, 15),
                                                   callback_baseline_initial={"3": "medium"}))
    store.apply(binding, [], reconciliation=update(task_cursor=18, held_added=(17,), held_resolved=(12,),
                                                   callback_baseline_initial={"9": "system"}))  # a stale pass
    store.apply(binding, [], reconciliation=update(callback_baseline_entries={"9": "high"}))
    assert store.reconciliation_state(binding) == engagement_store.ReconciliationState(
        task_cursor=20, held_tasks=(15, 17), callback_baseline={"3": "medium", "9": "high"})
    assert store.snapshot(binding).revision == 0  # the position is not a ledger row change

    copy = tmp_path / "copy.db"
    engagement_store.copy_ledger_tables(tmp_path / "sage.db", copy)
    copied = engagement_store.EngagementStore(copy, legacy_dir=str(tmp_path / "legacy"))
    assert copied.reconciliation_state(copied.bind_namespace("position")).task_cursor == 20


def test_a_store_from_before_the_position_table_reads_as_a_fresh_start(tmp_path):
    database = tmp_path / "sage.db"
    store = engagement_store.EngagementStore(database, legacy_dir=str(tmp_path / "legacy"))
    binding = store.bind_namespace("old-store")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE esl_reconciliation")
    reader = engagement_store.EngagementStore(database, legacy_dir=str(tmp_path / "legacy"))
    assert reader.reconciliation_state(binding) == engagement_store.ReconciliationState()


# --- the real Mythic queries -----------------------------------------------------------------------------------

def test_the_operation_queries_are_scoped_to_the_operation_and_bounded(monkeypatch):
    seen: list[tuple[str, dict]] = []

    async def query(client, text, variables=None):
        seen.append((text, dict(variables or {})))
        if "SageCitedTask" in text:
            return {"task": [{"display_id": 7, "operation_id": 2}, {"display_id": 7, "operation_id": 1}]}
        if "SageCitedCredential" in text:
            return {"credential": [{"id": 4, "operation_id": 2}]}
        if "SageCreditTasks" in text:
            return {"window": [{"display_id": 5}], "on_callback": [{"display_id": 5}, {"display_id": 3}]}
        return {"task": [], "credential": []}

    monkeypatch.setattr(sr.mythic, "execute_custom_query", query)
    asyncio.run(sr.fetch_operation_tasks(object(), 1, 40, [33, 31]))
    text, variables = seen[-1]
    assert "operation_id: {_eq: $op}" in text and "order_by: {display_id: asc}" in text
    assert "callback { display_id host user domain " in text  # a dead callback's forest comes from its own row
    assert variables == {"cursor": 40, "held": [31, 33], "limit": sr.PASS_LIMIT + 2, "op": 1}
    assert asyncio.run(sr.fetch_cited_task(object(), 1, 7)) == ({"display_id": 7, "operation_id": 1}, "")
    assert asyncio.run(sr.fetch_cited_task(object(), 3, 7)) == ({}, "not in this operation")
    assert asyncio.run(sr.fetch_cited_credential(object(), 1, 4)) == ({}, "not in this operation")
    asyncio.run(sr.fetch_task_credentials(object(), 1, [9, 8]))
    assert "task: {display_id: {_in: $ids}, operation_id: {_eq: $op}}" in seen[-1][0]
    assert [row["display_id"] for row in asyncio.run(
        sr.fetch_callback_credit_tasks(object(), 1, "2026-11-05T09:50:00", "2026-11-05T10:00:00", [21]))] == [3, 5]

    async def unreachable(client, text, variables=None):
        return {"errors": ["permission denied"]}

    monkeypatch.setattr(sr.mythic, "execute_custom_query", unreachable)
    with pytest.raises(RuntimeError):  # never an empty batch that would advance the cursor
        asyncio.run(sr.fetch_operation_tasks(object(), 1, 0, []))


# --- /state reconcile ------------------------------------------------------------------------------------------

def test_state_reconcile_cites_tasks_and_credentials_through_the_chat_command(operation, mythic_records):
    from sage_chat import slash
    from sage_chat.headless import build_chat_request

    tools = operation("channel-a", channel=3, request=1)
    mythic_records["tasks"] = [_dcsync_task(17, callback=13) | {"operation_id": 1, "timestamp": ""}]
    mythic_records["outputs"] = {17: _dcsync_output()}
    mythic_records["credentials"] = [_store_credential(42, None, account="hand")]
    assert slash._reconcile_citations("17,cred:42 apply") == (["17", "cred:42"], True, [])
    assert slash._reconcile_citations("17 #3 cred:x") == (["17"], False, ["#3", "cred:x"])

    model = SimpleNamespace(mythic_client=tools, mode="supervised", provider="t", model="t", state={})
    request = build_chat_request("x", channel_id=3, operation_id=0)
    reply = asyncio.run(slash._handle_state(model, request, "reconcile 17, cred:42"))
    assert "- 17: added row #1." in reply and "- cred:42: not added, no producing task" in reply
    assert "`/state reconcile <id>`" in asyncio.run(slash._handle_state(model, request, "set 1 achieved"))


# --- cold-review regressions -----------------------------------------------------------------------------------

DA_OUTPUT = ("Group name     Domain Admins\nComment        Designated administrators of the domain\n\nMembers\n\n"
             "-------------------------------------------------------------------------------\n"
             "Administrator            eddard.stark            samwell.tarly\nThe command completed successfully.\n")


@pytest.mark.parametrize("domain, netbios_map", [(NORTH, ""), ("NORTH", json.dumps({"NORTH": NORTH}))])
def test_a_task_on_a_dead_callback_is_verified_from_its_own_callback_row(operation, mythic_records, monkeypatch,
                                                                          domain, netbios_map):
    monkeypatch.setenv("SAGE_ENGAGEMENT_NETBIOS_MAP", netbios_map)
    tools = operation("channel-a", channel=3, request=1)
    task = _task(30, "shell", params='net group "Domain Admins" /domain')
    task["callback"] = {"display_id": 13, "host": "CASTELBLACK", "user": "samwell.tarly", "domain": domain}
    mythic_records["tasks"], mythic_records["outputs"] = [task], {30: DA_OUTPUT}
    mythic_records["callbacks"] = []  # the callback died before the turn end; no live foothold names its forest

    _turn_end(tools)
    assert f"da:{NORTH}" in _achieved(tools) and _position(tools).task_cursor == 30


def test_a_verified_achievement_lifts_the_migration_retraction_but_not_an_operator_retraction(
        operation, mythic_records, monkeypatch):
    import os
    legacy = os.environ["SAGE_ENGAGEMENT_STATE_DIR"]
    database = os.environ["SAGE_DATABASE_PATH"]
    data = channel34_ledger(ENGAGEMENT)
    template = data["hops"][4]
    data["hops"] = [
        {**template, "number": 1, "id": f"dcsync:{DOMAIN}", "technique": "dcsync", "target": DOMAIN,
         "effect": f"krbtgt-hash:{DOMAIN}", "status": "failed", "satisfied_effects": [f"krbtgt-hash:{DOMAIN}"]},
        {**template, "number": 2, "id": f"dcsync:{NORTH}", "technique": "dcsync", "target": NORTH,
         "effect": f"krbtgt-hash:{NORTH}", "status": "achieved", "satisfied_effects": [f"krbtgt-hash:{NORTH}"]},
    ]
    data["next_hop_number"] = 3
    source = write_legacy_ledger(legacy, data)
    with monkeypatch.context() as patch:  # the stored rows as Phase 3 left them
        patch.setattr(engagement_store, "_migrate_operation", lambda *_args: {"retracted": 0, "pending": 0})
        patch.setattr(engagement_store, "migrate_row_statuses", lambda _connection: {})
        assert engagement_store.import_legacy_file(source, database=database) == "imported"
        store = engagement_store.EngagementStore(database, legacy_dir=legacy)
        store.retract(store.find_binding(ENGAGEMENT), ["2"], reason="operator removed",
                      actor=engagement_store.Actor(kind="operator", name="op"))
    source.unlink()

    tools = operation("channel-a", channel=3, request=1)  # first open after deployment: the migration runs
    north = _dcsync_task(18, callback=13) | {"original_params": json.dumps({"Domain": NORTH, "User": "krbtgt"})}
    mythic_records["tasks"] = [_dcsync_task(17, callback=13), north]
    mythic_records["outputs"] = {17: _dcsync_output(), 18: _dcsync_output()}
    _turn_end(tools)

    rows = {row.number: row for row in tools._ledger_snapshot().hop_rows()}
    assert (rows[1].payload["status"], rows[1].retraction) == ("achieved", None)
    assert f"krbtgt-hash:{DOMAIN}" in _achieved(tools)
    assert rows[2].retraction["actor"] == "op" and f"krbtgt-hash:{NORTH}" not in _achieved(tools)
    history = tools._ledger_store().export(tools._require_ledger_binding())["history"]
    assert [(event["kind"], event["actor_name"]) for event in history if event.get("row_id") == 1][-1] == (
        "undo_retraction", "system:schema-migration")


def test_a_dcsync_krbtgt_entry_is_the_krbtgt_key_and_a_cleared_task_is_not_held(operation, mythic_records):
    tools = operation("channel-a", channel=3, request=1)
    dcsync = _task(60, "dcsync", params=json.dumps({"Domain": DOMAIN, "User": "krbtgt"}))
    mythic_records["tasks"] = [dcsync, _task(61, status="cleared")]
    mythic_records["credentials"] = [_store_credential(70, dcsync, account="krbtgt", text=NTLM)]
    _turn_end(tools)
    assert [hop.effect for hop in _rows(tools, "krbtgt-hash:")] == [f"krbtgt-hash:{DOMAIN}"]
    assert not _rows(tools, "creds:krbtgt") and _position(tools).held_tasks == ()


def test_a_cited_task_whose_row_the_operator_retracted_says_so(operation, mythic_records):
    tools = operation("channel-a", channel=3, request=1)
    mythic_records["tasks"] = [_dcsync_task(17, callback=13) | {"operation_id": 1, "timestamp": ""}]
    mythic_records["outputs"] = {17: _dcsync_output()}
    asyncio.run(tools._reconcile_operation(["17"]))
    tools._ledger_store().retract(tools._require_ledger_binding(), ["1"], reason="operator removed",
                                  actor=engagement_store.Actor(kind="operator", name="op"))
    assert asyncio.run(tools._reconcile_operation(["17"]))[0] == (
        "- 17: row #1 is retracted; `/state restore 1` counts it again.")


# --- callback attribution from task creation time --------------------------------------------------------------
#
# Live run 1 (2026-11-05, conversation channel 7): the operator hand-ran task 53 (`spawn` on callback 2) and
# callback 3 checked in 29.63 s after the task was created, but Mythic's mutable `timestamp` (the last update)
# read 1.84 s after the check-in, so no callback-gain row was written. These keep the recorded UTC times and the
# task/callback relationship; hosts, users and callback UUIDs are synthetic. Only `mythic.execute_custom_query`
# and the output reader are stubbed: the transport below evaluates each query's own predicate, bounds, operation
# filter and selection set against the recorded rows, so the production query builder, adapter, verifier, proof
# boundary and SQLite store all run.

T53_CREATED = "2026-11-05T16:46:30.21"  # status_timestamp_preprocessing
T53_SUBMITTED = "2026-11-05T16:46:38.31"
T53_PROCESSED = "2026-11-05T16:46:44.82"
T53_UPDATED = "2026-11-05T16:47:01.68"  # timestamp
CB3_CHECKED_IN = "2026-11-05T16:46:59.84"
FAVORABLE = "2026-11-05T16:46:40.00"  # inside callback 3's window, for clocks that must not be read
LIVE_BASELINE = {"1": "medium", "2": "medium"}
CB3_SESSION = "session:fixture\\user-3@host-3"
NO_REFERENCES = {"tasks": [], "credentials": [], "graph_reads": []}
_ABSENT = object()


def _live_callback(display_id: int, init: str, *, operation_id: int = 1) -> dict:
    return {"display_id": display_id, "operation_id": operation_id,
            "agent_callback_id": f"00000000-0000-4000-8000-{display_id:012d}", "host": f"HOST-{display_id}",
            "user": f"FIXTURE\\user-{display_id}", "domain": "FIXTURE", "integrity_level": 2, "init_callback": init}


def _live_task(display_id: int = 53, *, callback: int = 2, created: Any = T53_CREATED, updated: str = T53_UPDATED,
               submitted: str = T53_SUBMITTED, processed: str = T53_PROCESSED, status: str = "completed",
               operation_id: int = 1) -> dict:
    row = {"id": 1000 + display_id, "display_id": display_id, "operation_id": operation_id, "command_name": "spawn",
           "original_params": "", "display_params": "", "status": status, "completed": status not in _RUNNING,
           "timestamp": updated, "status_timestamp_submitted": submitted, "status_timestamp_processing": submitted,
           "status_timestamp_processed": processed,
           "operator": {"username": "operator_by_hand"},
           "callback": {"display_id": callback, "host": f"HOST-{callback}", "user": f"FIXTURE\\user-{callback}",
                        "domain": "FIXTURE"}}
    if created is not _ABSENT:
        row["status_timestamp_preprocessing"] = created
    return row


def _balanced(text: str, start: int, opening: str, closing: str) -> tuple[str, int]:
    """The text inside the bracket pair opening at ``start``, and the index just past it."""
    depth = 0
    for index in range(start, len(text)):
        depth += (text[index] == opening) - (text[index] == closing)
        if depth == 0:
            return text[start + 1:index], index + 1
    raise AssertionError(f"unbalanced {opening}{closing}: {text!r}")


def _field(query: str, head: str) -> tuple[str, dict]:
    """The arguments and parsed selection set of the query field that starts with ``head``."""
    start = query.index(head) + len(head)
    args, after = ("", start) if query[start] != "(" else _balanced(query, start, "(", ")")
    selection, _ = _balanced(query, query.index("{", after), "{", "}")
    spec: dict = {}
    stack, last = [spec], ""
    for token in re.findall(r"[{}]|\w+", selection):
        if token == "{":
            stack[-1][last] = {}
            stack.append(stack[-1][last])
        elif token == "}":
            stack.pop()
        else:
            stack[-1][token], last = None, token
    return " ".join(args.split()), spec


def _project(row: dict, spec: dict) -> dict:
    """What Hasura returns for ``row``: only the selected fields."""
    return {name: _project(row[name], nested) if nested and isinstance(row.get(name), dict) else row.get(name)
            for name, nested in spec.items()}


def _moment(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


_SCOPED = r"(, operation_id: \{_eq: \$op\})?"
_BOUNDED = r", order_by: \{display_id: desc\}, limit: \$limit"
_WINDOW = re.compile(r"where: \{(\w+): \{_gte: \$since, _lte: \$until\}" + _SCOPED + r"\}" + _BOUNDED)
_ON_CALLBACK = re.compile(r"where: \{callback: \{display_id: \{_in: \$cbs\}\}" + _SCOPED + r"\}" + _BOUNDED)


def _credit_branches(records: dict, query: str, variables: dict) -> dict:
    """Both SageCreditTasks branches, each evaluated from its own emitted predicate."""
    result = {}
    for alias, shape in (("window", _WINDOW), ("on_callback", _ON_CALLBACK)):
        args, spec = _field(query, f"{alias}: task")
        match = shape.fullmatch(args)
        assert match, f"unexpected {alias} branch: {args}"
        rows = list(records["tasks"])
        if alias == "window":
            since, until = _moment(variables["since"]), _moment(variables["until"])
            if not records["lax_window"]:  # a timestamp column compares; a NULL or absent value never matches
                rows = [row for row in rows
                        if (moment := _moment(row.get(match.group(1)))) is not None and since <= moment <= until]
        else:
            rows = [row for row in rows if (row.get("callback") or {}).get("display_id") in variables["cbs"]]
        if match.groups()[-1]:
            rows = [row for row in rows if row["operation_id"] == variables["op"]]
        rows = sorted(rows, key=lambda row: row["display_id"], reverse=True)[:variables["limit"]]
        result[alias] = [_project(row, spec) for row in rows]
    return result


@pytest.fixture
def live_mythic(monkeypatch):
    """Mythic's GraphQL transport serving the recorded operation: callbacks 1–3 and task 53, operation 1."""
    from mythic import mythic as mythic_module

    records: dict = {"callbacks": [_live_callback(1, "2026-11-05T16:00:00.00"),
                                   _live_callback(2, "2026-11-05T16:05:00.00"),
                                   _live_callback(3, CB3_CHECKED_IN)],
                     "tasks": [_live_task()], "lax_window": False, "queries": [], "credit": [], "errors": []}

    def serve(name: str, query: str, variables: dict) -> dict:
        op = variables.get("op")
        if name == "SageOpList":
            return {"operation": [{"id": 1, "name": "Operation Fixture"}]}
        if name == "sageStepCallbacks":
            _args, spec = _field(query, "callback")
            return {"callback": [_project(row, spec) for row in records["callbacks"]
                                 if op is None or row["operation_id"] == op]}
        if name == "SageReconcileTasks":
            _args, spec = _field(query, "task")
            rows = [row for row in records["tasks"] if (op is None or row["operation_id"] == op)
                    and (row["display_id"] > variables["cursor"] or row["display_id"] in variables["held"])]
            rows = sorted(rows, key=lambda row: row["display_id"])[:variables["limit"]]
            return {"task": [_project(row, spec) for row in rows]}
        if name == "SageCitedTask":
            _args, spec = _field(query, "task")
            return {"task": [_project(row, spec) for row in records["tasks"] if row["display_id"] == variables["id"]]}
        if name == "SageTaskCredentials":
            return {"credential": []}
        if name == "SageCreditTasks":
            records["credit"].append((query, dict(variables)))
            return _credit_branches(records, query, variables)
        raise AssertionError(f"unexpected Mythic query {name}")

    async def execute_custom_query(*args, **kwargs):
        query = kwargs.get("query", args[1] if len(args) > 1 else "")
        variables = dict(kwargs.get("variables", args[2] if len(args) > 2 else None) or {})
        name = re.match(r"\s*query (\w+)", query).group(1)
        records["queries"].append(name)
        try:
            return serve(name, query, variables)
        except Exception as exc:  # the readers fail open; keep the reason for the assertion
            records["errors"].append(f"{name}: {exc!r}")
            raise

    async def task_output(*args, **kwargs):
        return []

    monkeypatch.setattr(mythic_module, "execute_custom_query", execute_custom_query)
    monkeypatch.setattr(mythic_module, "get_all_task_output_by_id", task_output)
    return records


def _seed_position(tools: MythicTools, cursor: int = 0) -> None:
    """The stored position the run started from: callbacks 1 and 2 in the baseline, callback 3 not yet."""
    position = engagement_store.ReconciliationUpdate(task_cursor=cursor, callback_baseline_initial=dict(LIVE_BASELINE))
    tools._ledger_store().apply(tools._require_ledger_binding(), [], reconciliation=position)


def _callback_rows(tools: MythicTools) -> dict:
    return {hop.effect: hop for hop in tools._ledger_view().hops if hop.technique == "callback-context"}


def _assert_callback_3_credited_to(tools: MythicTools, task_id: int = 53, credited: Any = 53) -> None:
    rows = _callback_rows(tools)
    assert set(rows) == {CB3_SESSION}
    hop = rows[CB3_SESSION]
    assert (hop.status, str(hop.evidence["mythic_task_id"]), hop.evidence["credited_task"]) == (
        "achieved", str(task_id), credited)
    assert hop.proof_envelope["origin"] == "mythic_callback"
    assert _position(tools).callback_baseline == {**LIVE_BASELINE, "3": "medium"}
    # A fresh session reads the shared store through the runtime load quarantine: the proof is admitted again.
    reloaded = _session("reload", channel=8, request=1, mode="conversation")
    assert _callback_rows(reloaded)[CB3_SESSION].status == "achieved"


def _assert_nothing_for_callback_3(tools: MythicTools) -> None:
    assert _callback_rows(tools) == {} and "3" not in _position(tools).callback_baseline


@pytest.mark.parametrize("cursor", [0, 60], ids=["cursor-before-task", "cursor-past-task"])
def test_task_53s_creation_time_credits_callback_3_at_a_conversation_turn_end(operation, live_mythic, cursor):
    tools = operation("channel-7", channel=7, request=53, mode="conversation")  # this session issued nothing
    _seed_position(tools, cursor)

    _turn_end(tools)
    assert live_mythic["errors"] == []
    _assert_callback_3_credited_to(tools)
    rows = [(row.number, row.identity) for row in tools._ledger_snapshot().hop_rows()]
    position = _position(tools)

    _turn_end(tools)  # replay: callback 3 is in the baseline now, so nothing is written again
    reloaded = _session("channel-7-reload", channel=7, request=54, mode="conversation")
    assert [(row.number, row.identity) for row in reloaded._ledger_snapshot().hop_rows()] == rows
    assert _position(reloaded) == position and position.callback_baseline == {**LIVE_BASELINE, "3": "medium"}


def test_the_old_update_time_predicate_returns_no_task_53(operation, live_mythic):
    tools = operation("channel-7", channel=7, request=53, mode="conversation")
    _seed_position(tools)
    _turn_end(tools)
    [(query, variables)] = live_mythic["credit"][:1]
    assert [row["display_id"] for row in _credit_branches(live_mythic, query, variables)["window"]] == [53]
    old = re.sub(r"window: task\(where: \{\w+:", "window: task(where: {timestamp:", query)
    assert _credit_branches(live_mythic, old, variables) == {"window": [], "on_callback": []}  # nothing to rescue it


def test_the_supervised_step_writer_credits_task_53_from_the_same_mythic_rows(operation, live_mythic):
    tools = operation("channel-7", channel=7, request=53)
    _seed_position(tools)
    summary = asyncio.run(tools._record_step_ledger(NO_REFERENCES, []))
    assert live_mythic["errors"] == []
    assert summary["recorded"] == [{"effect": CB3_SESSION, "task_id": 53, "status": "achieved"}]
    _assert_callback_3_credited_to(tools)


def test_citing_task_53_credits_callback_3_and_leaves_the_cursor(operation, live_mythic):
    tools = operation("channel-7", channel=7, request=53, mode="conversation")
    _seed_position(tools)
    lines = asyncio.run(tools._reconcile_operation(["53"]))
    assert live_mythic["errors"] == [] and lines[0] == "- 53: added row #1."
    _assert_callback_3_credited_to(tools)
    assert _position(tools).task_cursor == 0


@pytest.mark.parametrize("updated", ["2026-11-05T16:46:50.00", T53_UPDATED, "2026-11-06T09:00:00.00"],
                         ids=["before-check-in", "after-check-in", "far-after"])
def test_the_mutable_update_time_does_not_change_eligibility(operation, live_mythic, updated):
    live_mythic["tasks"] = [_live_task(updated=updated)]
    tools = operation("channel-7", channel=7, request=53, mode="conversation")
    _seed_position(tools)
    _turn_end(tools)
    _assert_callback_3_credited_to(tools)


@pytest.mark.parametrize("created, credited", [
    (CB3_CHECKED_IN, True),  # created at the check-in: a zero gap
    ("2026-11-05T16:36:59.84", True),  # exactly 600 s before it
    ("2026-11-05T16:46:59.85", False),  # 0.01 s after the check-in
    ("2026-11-05T16:36:59.83", False),  # 600.01 s before it
], ids=["zero-gap", "600s", "after-check-in", "beyond-600s"])
@pytest.mark.parametrize("lax", [False, True], ids=["query-window", "adapter-decides"])
def test_the_window_bounds_are_inclusive_and_only_the_creation_time_counts(operation, live_mythic, created, credited,
                                                                           lax):
    live_mythic["lax_window"] = lax  # True: the row reaches the adapter and verifier whatever the query bounds say
    live_mythic["tasks"] = [_live_task(created=created, updated=FAVORABLE, submitted=FAVORABLE, processed=FAVORABLE)]
    tools = operation("channel-7", channel=7, request=53, mode="conversation")
    _seed_position(tools)
    _turn_end(tools)
    assert live_mythic["errors"] == []
    if credited:
        _assert_callback_3_credited_to(tools)
    else:
        _assert_nothing_for_callback_3(tools)


@pytest.mark.parametrize("created", [_ABSENT, None, "", "yesterday 16:46"],
                         ids=["absent", "null", "empty", "malformed"])
def test_a_missing_or_malformed_creation_time_is_never_replaced_by_another_clock(operation, live_mythic, created):
    live_mythic["lax_window"] = True  # the row reaches the adapter anyway: only the adapter and the verifier decide
    live_mythic["tasks"] = [_live_task(created=created, updated=FAVORABLE, submitted=FAVORABLE, processed=FAVORABLE)]
    tools = operation("channel-7", channel=7, request=53)
    _seed_position(tools)
    summary = asyncio.run(tools._record_step_ledger(NO_REFERENCES, []))  # the lane that writes even unproven gains
    assert live_mythic["errors"] == [] and summary["recorded"] == []
    assert summary["unattributed"] == [{"callback_display_id": 3, "kind": "new_callback", "effects": [CB3_SESSION]}]
    _assert_nothing_for_callback_3(tools)


def test_a_task_in_another_operation_enters_through_neither_branch(operation, live_mythic):
    live_mythic["tasks"] = [_live_task(operation_id=2), _live_task(70, callback=3, created="2026-11-05T16:47:10.00",
                                                                   updated=FAVORABLE, operation_id=2)]
    tools = operation("channel-7", channel=7, request=53, mode="conversation")
    _seed_position(tools)
    _turn_end(tools)
    assert asyncio.run(tools._record_step_ledger(NO_REFERENCES, []))["recorded"] == []
    assert live_mythic["errors"] == [] and live_mythic["credit"]
    assert all(_credit_branches(live_mythic, query, variables) == {"window": [], "on_callback": []}
               for query, variables in live_mythic["credit"])
    _assert_nothing_for_callback_3(tools)


@pytest.mark.parametrize("status", ["error: the service did not respond", "processing"])
def test_an_errored_or_running_task_53_gives_no_achieved_row_or_baseline_entry(operation, live_mythic, status):
    live_mythic["tasks"] = [_live_task(status=status)]
    tools = operation("channel-7", channel=7, request=53)
    _seed_position(tools)
    _turn_end(tools)
    _assert_nothing_for_callback_3(tools)
    summary = asyncio.run(tools._record_step_ledger(NO_REFERENCES, []))
    assert summary["recorded"] == [{"effect": CB3_SESSION, "task_id": 53, "status": "pending"}]
    assert {(hop.status, hop.pending_reason, hop.evidence["credited_task"])
            for hop in _callback_rows(tools).values()} == {("pending", "proof_missing", 53)}
    assert "3" not in _position(tools).callback_baseline


def test_with_no_valid_creation_time_a_completed_task_on_callback_3_still_proves_the_gain(operation, live_mythic):
    live_mythic["tasks"] = [_live_task(created="2026-11-05T16:30:00.00"),  # 1,019.84 s before the check-in
                            _live_task(70, callback=3, created=_ABSENT, updated=FAVORABLE)]
    tools = operation("channel-7", channel=7, request=53, mode="conversation")
    _seed_position(tools)
    _turn_end(tools)
    assert live_mythic["errors"] == []
    _assert_callback_3_credited_to(tools, task_id=70, credited="none")
