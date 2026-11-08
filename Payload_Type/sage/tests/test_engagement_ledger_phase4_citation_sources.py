"""Phase 4 remaining sources, checkpoint 3 part 2: non-task citation of operator material.

A credential entered directly in Mythic or an operator-provided file artifact is not proof on its own. Cited with
the proving task's ID, `/state reconcile` verifies the task's own action used that exact material and reached a
resource, and records the proven access with the proving task as lineage and the material kept only as the source
id. Matching account names alone never validate; the secret never enters an observation, event or reply.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ai.langgraph import mythic_tools
from sage_chat.slash import _reconcile_citations

SECRET = "P@ssw0rd-not-logged"
REALM = "north.sevenkingdoms.local"
DIR_LISTING = "Directory of \\\\winterfell.north.sevenkingdoms.local\\c$\n 11/07/2026  <DIR>  Windows\n"
TICKET_USE = "Client: samwell.tarly @ NORTH.SEVENKINGDOMS.LOCAL\nAP-REQ succeeded\n" + DIR_LISTING


# --- Unit: parsing and binding ---------------------------------------------------------------------------------

def test_citations_parse_file_ids_and_reject_non_numeric():
    cited, apply, invalid = _reconcile_citations("cred:5, file:7, 42 apply")
    assert cited == ["cred:5", "file:7", "42"] and apply is True and invalid == []
    assert _reconcile_citations("file:abc")[2] == ["file:abc"]


@pytest.mark.parametrize("material, params, expected", [
    ({"kind": "credential", "id": 5, "cred_text": SECRET}, f"net_dir /user:samwell /pass:{SECRET}", "material"),
    ({"kind": "credential", "id": 5, "cred_text": SECRET}, "make_token @cred:5 /host:winterfell", "reference"),
    ({"kind": "credential", "id": 5, "cred_text": SECRET, "account": "samwell"}, "runas /user:samwell /pass:other", ""),
    ({"kind": "file", "id": 7, "agent_file_id": "abc-123"}, "Rubeus.exe ptt @file:7", "reference"),
    ({"kind": "file", "id": 7, "agent_file_id": "abc-123"}, "ptt /ticket:abc-123", "material"),
    ({"kind": "file", "id": 7, "agent_file_id": "abc-123"}, "ptt /ticket:other", ""),
])
def test_material_binding_requires_reference_or_exact_material(material, params, expected):
    assert mythic_tools.MythicTools._material_binds(material, params) == expected


# --- Assembled: operation reconciliation ----------------------------------------------------------------------

from tests.esl_fixtures import stub_mythic_operation  # noqa: E402
from tests.test_engagement_ledger_phase2_unified_proof import SUCCESS, session  # noqa: E402,F401
from tests.test_engagement_ledger_phase4_existing_rows import _record_fields  # noqa: E402
from tests.test_engagement_ledger_phase4_observation_joins import _events  # noqa: E402


def _task(task_id, command, params):
    return {"display_id": task_id, "id": task_id, "operation_id": 1, "command_name": command,
            "original_params": params, "operator": {"username": "operator"},
            "callback": {"display_id": 2, "host": "WINTERFELL", "user": "north\\samwell.tarly",
                         "agent_callback_id": "agent-2"},
            **_record_fields(SUCCESS)}


@pytest.fixture
def citation_mythic(monkeypatch, mythic_task_records):
    records = stub_mythic_operation(monkeypatch)
    records["tasks"], records["outputs"] = [], {}

    def task(task_id, command, params, output=""):
        records["tasks"].append(_task(task_id, command, params))
        records["outputs"][task_id] = output
        mythic_task_records[task_id] = SUCCESS

    def credential(cred_id, *, account="samwell.tarly", text=SECRET, producing_task=None):
        records["credentials"].append({"id": cred_id, "operation_id": 1, "account": account, "realm": REALM,
                                       "type": "plaintext", "credential_text": text,
                                       "task": producing_task or {}})

    def file_artifact(file_id, *, agent_file_id="ticket-abc-123"):
        records["files"].append({"id": file_id, "operation_id": 1, "agent_file_id": agent_file_id,
                                 "filename_utf8": "samwell.kirbi", "md5": "", "sha1": "", "task": {}})

    return records, task, credential, file_artifact


def _reconcile(tools, *cited):
    return asyncio.run(tools._reconcile_operation(tuple(cited)))


def _achieved(tools):
    return {h.effect: h for h in tools._ledger_view().hops if h.status == "achieved"}


def test_a_credential_bound_to_its_proving_task_records_the_access(session, citation_mythic):
    _records, task, credential, _file = citation_mythic
    credential(5)
    task(42, "net_dir", f"net_dir /user:north\\samwell.tarly /pass:{SECRET} /target:winterfell")
    # the proving task's own output reached the resource
    _records["outputs"][42] = DIR_LISTING
    tools = session()
    lines = _reconcile(tools, "cred:5", "42")

    achieved = _achieved(tools)
    [effect] = [e for e in achieved if e.startswith("authenticated-session:")]
    hop = achieved[effect]
    assert str(hop.evidence["mythic_task_id"]) == "42" and hop.evidence["cited_source"] == "cred:5"
    assert any("cred:5" in line and "row #" in line for line in lines), lines


def test_a_credential_cited_without_its_proving_task_reports_missing_use(session, citation_mythic):
    _records, _task, credential, _file = citation_mythic
    credential(5)
    tools = session()
    lines = _reconcile(tools, "cred:5")
    assert not any(e.startswith("authenticated-session:") for e in _achieved(tools))
    assert any("cred:5" in line and "not proof" in line for line in lines), lines


def test_a_different_secret_for_the_same_account_does_not_validate(session, citation_mythic):
    _records, task, credential, _file = citation_mythic
    credential(5, text=SECRET)
    task(42, "net_dir", "net_dir /user:north\\samwell.tarly /pass:a-different-password /target:winterfell")
    _records["outputs"][42] = DIR_LISTING
    tools = session()
    lines = _reconcile(tools, "cred:5", "42")
    assert not any(e.startswith("authenticated-session:") for e in _achieved(tools))
    assert any("cred:5" in line and "not added" in line for line in lines), lines


def test_a_binding_without_proven_access_records_nothing(session, citation_mythic):
    _records, task, credential, _file = citation_mythic
    credential(5)
    # the task used the exact secret, but its output proves no resource access
    task(42, "net_dir", f"net_dir /user:north\\samwell.tarly /pass:{SECRET} /target:winterfell", "access is denied")
    tools = session()
    lines = _reconcile(tools, "cred:5", "42")
    assert not any(e.startswith("authenticated-session:") for e in _achieved(tools))
    assert any("cred:5" in line and "not added" in line for line in lines), lines


def test_a_credential_with_no_account_or_realm_records_no_empty_principal(session, citation_mythic):
    _records, task, credential, _file = citation_mythic
    credential(5, account="", text=SECRET)
    _records["credentials"][-1]["realm"] = ""
    task(42, "net_dir", f"net_dir /pass:{SECRET} /target:winterfell", DIR_LISTING)
    tools = session()
    lines = _reconcile(tools, "cred:5", "42")
    assert not any(e.startswith("authenticated-session:") for e in _achieved(tools))
    assert any("cred:5" in line and "not added" in line for line in lines), lines


def test_a_file_artifact_bound_by_reference_records_the_kerberos_session(session, citation_mythic):
    _records, task, _credential, file_artifact = citation_mythic
    file_artifact(7)
    task(42, "execute_assembly", "Rubeus.exe ptt @file:7")
    _records["outputs"][42] = TICKET_USE
    tools = session()
    lines = _reconcile(tools, "file:7", "42")

    achieved = _achieved(tools)
    [effect] = [e for e in achieved if e.startswith("kerberos-account-context:")]
    assert achieved[effect].evidence["cited_source"] == "file:7"
    assert any("file:7" in line and "row #" in line for line in lines), lines


def test_an_unknown_file_id_reports_it(session, citation_mythic):
    tools = session()
    lines = _reconcile(tools, "file:99")
    assert any("file:99" in line and "no such file" in line for line in lines), lines


def test_the_cited_secret_never_enters_events_or_observations(session, citation_mythic):
    _records, task, credential, _file = citation_mythic
    credential(5)
    task(42, "net_dir", f"net_dir /user:north\\samwell.tarly /pass:{SECRET} /target:winterfell")
    _records["outputs"][42] = DIR_LISTING
    tools = session()
    _reconcile(tools, "cred:5", "42")

    store = tools._ledger_store()
    binding = tools._require_ledger_binding()
    blob = json.dumps(_events(tools), default=str)
    for kind in ("session_use", "session_context"):
        for obs in store.observations(binding, kind=kind):
            blob += json.dumps(obs, default=str)
    assert SECRET not in blob
