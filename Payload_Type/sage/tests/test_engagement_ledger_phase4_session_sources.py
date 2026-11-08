"""Phase 4 remaining sources, checkpoint 3 part 1: cross-task session proof.

A working session is two independent records joined — a context-changing task established an identity, and a later
network action proved that identity reaches a named resource. Neither half is proof on its own. These exercise the
extractors, the observations, the setup→use join, and the assembled ``/state reconcile`` path with only Mythic
transport stubbed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json

import pytest

from ai.langgraph import capabilities, credential_artifacts as ca, effect_observations as eo

NOW = "2026-11-07T12:00:00+00:00"
LIFETIME = "agent-2"
KERBEROS_IMPORT = "Ticket successfully imported!\nClient: samwell.tarly @ NORTH.SEVENKINGDOMS.LOCAL"
MAKE_TOKEN_OUT = "[+] Successfully impersonated user as NORTH\\samwell.tarly"
DIR_LISTING = "Directory of \\\\winterfell.north.sevenkingdoms.local\\c$\n 11/07/2026  <DIR>  Windows\n"
TICKET_VALID = "Client: samwell.tarly @ NORTH.SEVENKINGDOMS.LOCAL\nAP-REQ succeeded"


# --- Unit: extractors ------------------------------------------------------------------------------------------

def test_setup_change_recognizes_token_ticket_and_revert_only():
    assert ca.session_setup_change("make_token", MAKE_TOKEN_OUT)["change_kind"] == "set"
    ticket = ca.session_setup_change("execute_assembly", KERBEROS_IMPORT)
    assert ticket["change_kind"] == "set" and ticket["is_kerberos"] is True
    assert ca.session_setup_change("rev2self", "Reverted to self")["change_kind"] == "cleared"
    assert ca.session_setup_change("ls", "Directory of \\\\x\\c$") is None
    # a failed revert is not a sequence break
    assert ca.session_setup_change("rev2self", "rev2self failed: access is denied") is None


@pytest.mark.parametrize("output, proven", [
    (DIR_LISTING, "service_access_proven"),
    (TICKET_VALID, "ticket_valid"),
])
def test_use_probe_recognizes_real_access(output, proven):
    assert ca.extract_session_use_probe(output)[proven] is True


@pytest.mark.parametrize("output", [
    "north\\samwell.tarly",                          # a local whoami
    "Successfully impersonated user",                # make_token success alone
    "Cached tickets:\n#0> Client: samwell.tarly",    # ticket present, not used
    "Anonymous logon\nDirectory of \\\\x\\c$",        # anonymous read
])
def test_use_probe_rejects_non_use(output):
    probe = ca.extract_session_use_probe(output)
    assert not (probe["service_access_proven"] or probe["ticket_valid"] or probe["ldap_access_proven"])


# --- Unit: observations ----------------------------------------------------------------------------------------

def _stask(task_id, command, params="", *, status="success", lifetime=LIFETIME):
    from tests.test_engagement_ledger_phase2_unified_proof import CALLBACK, SUCCESS
    from tests.test_engagement_ledger_phase4_existing_rows import _record_fields

    record = dict(SUCCESS) if status == "success" else {**SUCCESS, "status": "error"}
    return {"display_id": task_id, "id": task_id, "operation_id": 1, "command_name": command,
            "original_params": params, "operator": {"username": "operator"},
            "callback": {"display_id": CALLBACK, "host": "WINTERFELL", "user": "north\\samwell.tarly",
                         "agent_callback_id": lifetime},
            **_record_fields(record)}


def _row(observation):
    return {"observation_id": observation.observation_id, "observed_at": observation.observed_at,
            "task_id": observation.task_id, "payload": observation.payload()}


def test_make_token_context_takes_its_identity_from_parameters():
    params = json.dumps({"username": "samwell.tarly", "domain": "north.sevenkingdoms.local"})
    obs = eo.session_context_observation(_stask(20, "make_token", params), MAKE_TOKEN_OUT, "success", NOW, params=params)
    values = obs.payload()["values"]
    assert (obs.kind, obs.join_state) == (eo.SESSION_CONTEXT, eo.JOIN_AWAITING)
    assert (values["identity"], values["is_kerberos"]) == ("samwell.tarly@north.sevenkingdoms.local", False)


def test_a_context_set_with_no_bindable_account_anchors_nothing():
    assert eo.session_context_observation(_stask(20, "make_token"), "Successfully impersonated user", "success", NOW) is None


def test_a_revert_records_the_break_without_opening_a_join():
    obs = eo.session_context_observation(_stask(21, "rev2self"), "Reverted to self", "success", NOW)
    assert (obs.kind, obs.join_state, obs.payload()["values"]["change_kind"]) == (eo.SESSION_CONTEXT, eo.NO_JOIN, "cleared")


def test_session_use_requires_terminal_success_and_real_access():
    assert eo.session_use_observation(_stask(22, "ls", status="error"), DIR_LISTING, "", NOW) is None
    assert eo.session_use_observation(_stask(22, "ls"), "north\\samwell.tarly", "success", NOW) is None
    use = eo.session_use_observation(_stask(22, "ls"), DIR_LISTING, "success", NOW)
    assert use.kind == eo.SESSION_USE and use.payload()["values"]["service_access_proven"] is True


# --- Unit: setup -> use join ----------------------------------------------------------------------------------

def _ctx(task_id, output, params="", command="make_token"):
    return eo.session_context_observation(_stask(task_id, command, params), output, "success", NOW, params=params)


def _use(task_id, output, command="ls"):
    return eo.session_use_observation(_stask(task_id, command), output, "success", NOW)


def test_a_later_use_proves_the_controlling_setup():
    setup = _ctx(20, KERBEROS_IMPORT, command="execute_assembly")
    use = _use(21, DIR_LISTING)
    read, reason = eo.resolve_session_use(_row(setup), [_row(setup)], [_row(use)])
    assert read is not None, reason
    assert read["payload"]["values"]["task_id"] == "21"


def test_an_anonymous_read_is_never_a_use_observation():
    assert eo.session_use_observation(_stask(21, "ls"), "Anonymous logon\n" + DIR_LISTING, "success", NOW) is None


@pytest.mark.parametrize("use_task, use_output, reason", [
    (19, DIR_LISTING, "no later network action"),                         # use before setup
    (21, "Successfully impersonated user", "no later network action"),    # no real use
    (21, "Client: jon.snow @ NORTH.SEVENKINGDOMS.LOCAL\nAP-REQ succeeded", "different account"),  # wrong account
])
def test_uses_that_do_not_prove_the_setup_leave_it_awaiting(use_task, use_output, reason):
    setup = _ctx(20, KERBEROS_IMPORT, command="execute_assembly")
    use = _use(use_task, use_output)
    uses = [_row(use)] if use is not None else []
    read, why = eo.resolve_session_use(_row(setup), [_row(setup)], uses)
    assert read is None and reason in why, why


def test_a_different_logon_id_is_a_different_session():
    setup = _ctx(20, KERBEROS_IMPORT + "\nLogon Id: 0x1111", command="execute_assembly")
    use = _use(21, DIR_LISTING + "\nLogon Id: 0x9999")
    read, why = eo.resolve_session_use(_row(setup), [_row(setup)], [_row(use)])
    assert read is None and "different logon id" in why


def test_an_intervening_context_change_breaks_attribution():
    first = _ctx(20, KERBEROS_IMPORT, command="execute_assembly")
    second = _ctx(21, KERBEROS_IMPORT.replace("samwell.tarly", "jon.snow"), command="execute_assembly")
    use = _use(22, DIR_LISTING)
    contexts = [_row(first), _row(second)]
    assert eo.resolve_session_use(_row(first), contexts, [_row(use)])[0] is None  # first is superseded
    assert eo.session_superseded(_row(first), contexts) is True
    # the later setup, now controlling, is the one a use proves
    read, _ = eo.resolve_session_use(_row(second), contexts, [_row(use)])
    assert read is not None and read["payload"]["values"]["task_id"] == "22"


def test_account_kerberos_verifier_enforces_its_mandatory_context():
    # The reused verifier (plan: "its account-ticket and active-context requirements remain mandatory"): service
    # access and a present ticket are not enough without the active logon context the setup supplies.
    base = {"callback_id": 2, "account": "samwell.tarly", "domain": "north.sevenkingdoms.local",
            "account_ticket_present": True, "service_access_proven": True}
    assert capabilities.verify_account_kerberos_context(base).verdict != "achieved"
    assert capabilities.verify_account_kerberos_context(
        {**base, "logon_context_proven": True, "account_context_proven": True}).verdict == "achieved"


def test_a_sacrificial_setup_is_replaced_by_the_real_one():
    sacrificial = _ctx(20, KERBEROS_IMPORT.replace("samwell.tarly", "throwaway"), command="execute_assembly")
    real = _ctx(21, KERBEROS_IMPORT, command="execute_assembly")
    use = _use(22, TICKET_VALID)
    contexts = [_row(sacrificial), _row(real)]
    assert eo.session_superseded(_row(sacrificial), contexts) is True
    read, _ = eo.resolve_session_use(_row(real), contexts, [_row(use)])
    assert read is not None and read["payload"]["values"]["task_id"] == "22"


# --- Assembled: operation reconciliation, store, view ---------------------------------------------------------

from tests.esl_fixtures import stub_mythic_operation  # noqa: E402
from tests.test_engagement_ledger_phase2_unified_proof import SUCCESS, session  # noqa: E402,F401
from tests.test_engagement_ledger_phase4_observation_joins import _events, _position, _restarted, _state  # noqa: E402

KERBEROS_EFFECT = "kerberos-account-context:samwell.tarly@north.sevenkingdoms.local"


@pytest.fixture
def session_mythic(monkeypatch, mythic_task_records):
    records = stub_mythic_operation(monkeypatch)
    records["tasks"], records["outputs"] = [], {}

    def add(task_id, command, params, output, *, status="success"):
        records["tasks"].append(_stask(task_id, command, params, status=status))
        records["outputs"][task_id] = output
        mythic_task_records[task_id] = SUCCESS if status == "success" else {**SUCCESS, "status": "error"}

    return add


def _reconcile(tools, *cited):
    return asyncio.run(tools._reconcile_operation(tuple(cited)))


def _session_joins(tools):
    return tools._ledger_store().observations(tools._require_ledger_binding(), kind=eo.SESSION_CONTEXT)


def _achieved(tools):
    return {h.effect: h for h in tools._ledger_view().hops if h.status == "achieved"}


def test_a_ticket_import_then_a_later_use_records_the_kerberos_session(session, session_mythic):
    session_mythic(20, "execute_assembly", "Rubeus.exe ptt", KERBEROS_IMPORT)
    session_mythic(21, "ls", "\\\\winterfell\\c$", DIR_LISTING)
    tools = session()
    _reconcile(tools)

    hop = _achieved(tools)[KERBEROS_EFFECT]
    assert (str(hop.evidence["mythic_task_id"]), str(hop.evidence["setup_task_id"])) == ("21", "20")
    assert hop.evidence["proof_envelope"]["task_id"] in ("21", 21)
    [join] = [j for j in _session_joins(tools) if j["payload"]["values"]["change_kind"] == "set"]
    assert (join["join_state"], join["resolved_by"]) == (eo.JOIN_RESOLVED, "task:21")
    events = len(_events(tools))
    _reconcile(tools)  # replay converges
    assert len(_events(tools)) == events


def test_a_make_token_session_is_resource_scoped_not_kerberos(session, session_mythic):
    params = json.dumps({"username": "samwell.tarly", "domain": "north.sevenkingdoms.local"})
    session_mythic(20, "make_token", params, MAKE_TOKEN_OUT)
    session_mythic(21, "ls", "\\\\winterfell\\c$", DIR_LISTING)
    tools = session()
    _reconcile(tools)

    achieved = _achieved(tools)
    assert KERBEROS_EFFECT not in achieved
    assert any(effect.startswith("authenticated-session:") for effect in achieved)


def test_a_setup_without_use_stays_awaiting_then_expires(session, session_mythic):
    session_mythic(20, "execute_assembly", "Rubeus.exe ptt", KERBEROS_IMPORT)
    tools = session()
    lines = _reconcile(tools)
    assert KERBEROS_EFFECT not in _achieved(tools)
    assert "1 context change(s) awaiting a later use" in lines[0]
    assert [j["join_state"] for j in _session_joins(tools) if j["payload"]["values"]["change_kind"] == "set"] == [eo.JOIN_AWAITING]


def test_a_use_after_a_restart_resolves_the_stored_setup(session, session_mythic):
    session_mythic(20, "execute_assembly", "Rubeus.exe ptt", KERBEROS_IMPORT)
    tools = session()
    _reconcile(tools)
    assert KERBEROS_EFFECT not in _achieved(tools) and _position(tools).task_cursor == 20

    session_mythic(21, "ls", "\\\\winterfell\\c$", DIR_LISTING)
    fresh = _restarted(tools)
    _reconcile(fresh)
    assert str(_achieved(fresh)[KERBEROS_EFFECT].evidence["mythic_task_id"]) == "21"


def test_an_intervening_context_change_leaves_the_earlier_session_unproven(session, session_mythic):
    session_mythic(20, "execute_assembly", "Rubeus.exe ptt", KERBEROS_IMPORT)
    session_mythic(21, "execute_assembly", "Rubeus.exe ptt",
                   KERBEROS_IMPORT.replace("samwell.tarly", "jon.snow"))
    session_mythic(22, "ls", "\\\\winterfell\\c$", DIR_LISTING)
    tools = session()
    _reconcile(tools)
    assert KERBEROS_EFFECT not in _achieved(tools)  # samwell's session was superseded before any use
