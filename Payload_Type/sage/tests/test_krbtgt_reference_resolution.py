"""A krbtgt forge that names a credential binds that exact proved row, whatever its rank.

`forge-golden-ticket` and `ensure-kerberos-context` bind their krbtgt key from Mythic-origin proof by reference
(`credential_id`, `credential_proof_task_id`). A reference names identity: it resolves to that eligible row, or to
that admitted proof task, even when another eligible key ranks higher (aes256 over rc4). Every supplied reference
must match, a reference that cannot resolve never selects a replacement, and an unpinned request keeps the
existing selection order. With nothing proved at all, the refusal says no key is proved rather than calling the
reference stale.

Two proof routes reach the binder: an admitted proof task (the production dcsync executor's achieved
`krbtgt-hash` hop, whose store rows carry no task lineage) and agent-reported store rows with complete task
lineage. Fixtures run the production executor, binder, builder and adapter; only Mythic's API seam is inert and
every key is synthetic.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capabilities  # noqa: E402
from tests.esl_fixtures import AGENT_PROCESSED, seed_ledger  # noqa: E402
from test_engagement_wiring import _proof_hop, _split_issue  # noqa: E402
from test_verified_capability_input_continuity import (  # noqa: E402
    BAD_DN, FORGE, KRBTGT_KEY, _forge_from, _ground_forge, _krbtgt_tools, _prove_krbtgt, _rubeus_issues, _run_dcsync,
)

AES = KRBTGT_KEY            # row 500, ranks first
NTLM = "d" * 32             # row 501, the lower-ranked key of the same proof task
NEWER_AES = "b" * 64        # another task's higher-ranked key
TYPED = "f" * 64            # a key no task proved
SECRETS = (AES, NTLM, NEWER_AES, TYPED)
CAPABILITIES = ["forge-golden-ticket", "ensure-kerberos-context"]
ROUTES = ["admitted-task", "agent-row"]
OUTCOME_FIELD = "_krbtgt_reference_outcome"

NTLM_ONLY = ("[DC] 'lab.local' will be the domain\n** SAM ACCOUNT **\nSAM Username         : krbtgt\nCredentials:\n"
             f"  Hash NTLM: {NTLM}\n")
AES_ONLY = ("[DC] 'lab.local' will be the domain\n** SAM ACCOUNT **\nSAM Username         : krbtgt\nCredentials:\n"
            f"* Primary:Kerberos-Newer-Keys *\n  aes256_hmac       (4096) : {NEWER_AES}\n")
NO_KEY = "[DC] 'lab.local' will be the domain\n[DC] ms-DRSR connected\n"


def _action(capability: str) -> dict:
    return {**FORGE, "capability": capability}


def _reported(row_id, *, key, task_id, account="krbtgt", realm="lab.local", row_type="key", status="completed"):
    """A store row an agent reported from a Mythic task (complete lineage unless the task did not succeed)."""
    return {"id": row_id, "account": account, "realm": realm, "type": row_type, "credential_text": key,
            "comment": "reported by the agent",
            "task": {"display_id": task_id, "status": status, "completed": True, **AGENT_PROCESSED,
                     "command_name": "dcsync", "callback": {"display_id": 13}}}


def _typed(row_id, *, key, account="krbtgt", realm="lab.local"):
    """A store row typed into Mythic: it names a credential but proves nothing."""
    return {"id": row_id, "account": account, "realm": realm, "type": "key", "credential_text": key,
            "comment": "entered by hand", "task": None}


def _two_row_tools(monkeypatch, tmp_path, route):
    """Rows 500 (aes256) and 501 (rc4), both proved by task 21 through `route` only; 500 outranks 501."""
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    if route == "admitted-task":
        _prove_krbtgt(mt, store)  # the production dcsync executor: an achieved hop for task 21, rows without lineage
    else:
        seed_ledger(mt, hops=[])
        store["rows"] += [_reported(500, key=AES, task_id=21), _reported(501, key=NTLM, task_id=21, row_type="hash")]
    rows = {row["id"]: row for row in store["rows"]}
    assert rows[500]["credential_text"] == AES and rows[501]["credential_text"] == NTLM, store["rows"]
    selected = asyncio.run(mt._select_krbtgt_credential("lab.local"))
    assert selected["id"] == 500 and str(selected["proof_task_id"]) == "21"  # the unpinned rank is unchanged
    return mt, store


def _two_task_tools(monkeypatch, tmp_path, route):
    """Task 21 proved only an rc4 key; the later task 30 proved a higher-ranked aes256 key."""
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    if route == "admitted-task":
        store["task_outputs"].update({21: NTLM_ONLY, 30: AES_ONLY})
        seed_ledger(mt, hops=[
            _proof_hop("krbtgt-hash:lab.local", 21, callback_id="13", target="krbtgt-21"),
            _proof_hop("krbtgt-hash:lab.local", 30, callback_id="13", target="krbtgt-30"),
        ])
        assert store["rows"] == []  # task-only recovery: no store row names either key
    else:
        seed_ledger(mt, hops=[])
        store["rows"] += [_reported(600, key=NTLM, task_id=21, row_type="hash"),
                          _reported(601, key=NEWER_AES, task_id=30)]
    selected = asyncio.run(mt._select_krbtgt_credential("lab.local"))
    assert selected["credential"] == NEWER_AES and str(selected["proof_task_id"]) == "30"
    return mt, store


def _ground_without_issue(mt, inputs, action):
    calls: dict = {}
    with _split_issue(lambda: "ok", calls):
        message = _ground_forge(mt, inputs, action)
    assert calls.get("issued", []) == []  # grounding never issues a task
    return message


def _no_secret(value) -> None:
    text = json.dumps(value, default=str)
    for secret in SECRETS:
        assert secret not in text


# --- a reference binds the row it names -------------------------------------------------------------------------

@pytest.mark.parametrize("capability", CAPABILITIES)
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("pin", [{"credential_id": 501}, {"credential_id": 501, "credential_proof_task_id": "21"}],
                         ids=["id", "id-and-task"])
def test_a_reference_to_a_lower_ranked_row_binds_that_row(monkeypatch, tmp_path, capability, route, pin):
    mt, store = _two_row_tools(monkeypatch, tmp_path, route)
    writes_before = len(store["writes"])

    [call] = _ground_without_issue(mt, dict(pin), _action(capability)).tool_calls

    inputs = call["args"]["inputs"]
    assert str(inputs["credential_id"]) == "501" and str(inputs["credential_proof_task_id"]) == "21", inputs
    assert inputs["key_type"] == "rc4"
    _no_secret(call)  # the card names the credential; it never carries a key
    assert len(store["writes"]) == writes_before

    result, calls = _forge_from(mt, inputs, call["args"]["action"])

    [forge] = _rubeus_issues(calls)
    assert f"/rc4:{NTLM} " in forge and AES not in forge, forge
    _no_secret(result)


@pytest.mark.parametrize("capability", CAPABILITIES)
@pytest.mark.parametrize("route", ROUTES)
def test_a_proof_task_reference_binds_that_tasks_key_over_a_newer_higher_ranked_one(monkeypatch, tmp_path,
                                                                                    capability, route):
    mt, _ = _two_task_tools(monkeypatch, tmp_path, route)

    [call] = _ground_without_issue(mt, {"credential_proof_task_id": "21"}, _action(capability)).tool_calls

    inputs = call["args"]["inputs"]
    assert str(inputs["credential_proof_task_id"]) == "21" and inputs["key_type"] == "rc4", inputs
    _no_secret(call)
    _, calls = _forge_from(mt, inputs, call["args"]["action"])
    [forge] = _rubeus_issues(calls)
    assert f"/rc4:{NTLM} " in forge and NEWER_AES not in forge, forge


@pytest.mark.parametrize("capability", CAPABILITIES)
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("setup, key", [(_two_row_tools, AES), (_two_task_tools, NEWER_AES)], ids=["rows", "tasks"])
def test_an_unpinned_request_keeps_the_existing_selection(monkeypatch, tmp_path, capability, route, setup, key):
    mt, _ = setup(monkeypatch, tmp_path, route)

    [call] = _ground_without_issue(mt, {}, _action(capability)).tool_calls

    _no_secret(call)
    _, calls = _forge_from(mt, call["args"]["inputs"], call["args"]["action"])
    [forge] = _rubeus_issues(calls)
    assert key in forge, forge


# --- a reference that cannot resolve binds nothing and issues nothing --------------------------------------------

def _other_realm(mt, store):
    store["rows"].append(_reported(700, key=NEWER_AES, task_id=24, realm="other.local"))
    return {"credential_id": 700}


def _other_account(mt, store):
    store["rows"].append(_reported(701, key=NEWER_AES, task_id=24, account="administrator"))
    return {"credential_id": 701}


def _trust_account(mt, store):
    # An inter-realm trust account's key lives in the same realm; it is never a krbtgt key here.
    store["rows"].append(_reported(702, key=NEWER_AES, task_id=24, account="OTHER$"))
    return {"credential_id": 702}


def _id_and_other_admitted_task(mt, store):
    store["task_outputs"][30] = AES_ONLY
    seed_ledger(mt, hops=[_proof_hop("krbtgt-hash:lab.local", 30, callback_id="13", target="krbtgt-30")])
    return {"credential_id": 501, "credential_proof_task_id": "30"}


def _id_and_unknown_task(mt, store):
    return {"credential_id": 501, "credential_proof_task_id": "22"}


def _absent_row(mt, store):
    return {"credential_id": 9999}


def _absent_row_with_its_task(mt, store):
    return {"credential_id": 9999, "credential_proof_task_id": "21"}


def _unproved_row(mt, store):
    store["rows"].append(_typed(703, key=TYPED))
    return {"credential_id": 703}


def _unproved_row_with_a_proved_task(mt, store):
    store["rows"].append(_typed(703, key=TYPED))
    return {"credential_id": 703, "credential_proof_task_id": "21"}


def _task_without_parsed_material(mt, store):
    store["task_outputs"][22] = NO_KEY
    seed_ledger(mt, hops=[_proof_hop("krbtgt-hash:lab.local", 22, callback_id="13", target="krbtgt-22")])
    return {"credential_proof_task_id": "22"}


def _row_from_a_failed_task(mt, store):
    store["rows"].append(_reported(704, key=TYPED, task_id=25, status="error"))
    return {"credential_id": 704}


def _failed_dcsync_task(mt, store):
    result, _ = _run_dcsync(mt, BAD_DN, iter([22]))
    assert "krbtgt-hash:lab.local" not in result["recorded_effects"]
    return {"credential_proof_task_id": "22"}


def _caller_asserted_status_and_key(mt, store):
    store["rows"].append(_typed(703, key=TYPED))
    return {"credential_id": 703, "credential_source": "mythic_credential_store", "key": TYPED,
            "key_type": "aes256", OUTCOME_FIELD: "bound"}


UNRESOLVABLE = [_other_realm, _other_account, _trust_account, _id_and_other_admitted_task, _id_and_unknown_task,
                _absent_row, _absent_row_with_its_task, _unproved_row, _unproved_row_with_a_proved_task,
                _task_without_parsed_material, _row_from_a_failed_task, _failed_dcsync_task,
                _caller_asserted_status_and_key]


def _assert_refused(mt, inputs, capability, *, stale: bool):
    expected, other = ("no longer resolves", "has no proved") if stale else ("has no proved", "no longer resolves")
    message = _ground_without_issue(mt, dict(inputs), _action(capability))
    assert message.tool_calls == [], message.tool_calls
    note = str(message.content)
    assert expected in note and other not in note and "no forge was issued" in note.casefold(), note
    _no_secret(note)

    result, calls = _forge_from(mt, dict(inputs), _action(capability))

    assert calls.get("issued", []) == [] and result["issued"] == [], result  # no forge, no context refresh
    assert result["verdict"] != "achieved"
    assert expected in result["reason"] and other not in result["reason"], result["reason"]
    _no_secret(result)


@pytest.mark.parametrize("capability", CAPABILITIES)
@pytest.mark.parametrize("make_pin", UNRESOLVABLE, ids=[case.__name__.strip("_") for case in UNRESOLVABLE])
def test_a_reference_that_cannot_resolve_issues_nothing(monkeypatch, tmp_path, capability, make_pin):
    mt, store = _two_row_tools(monkeypatch, tmp_path, "admitted-task")  # a valid alternative stays eligible
    pin = make_pin(mt, store)

    _assert_refused(mt, pin, capability, stale=True)


@pytest.mark.parametrize("capability", CAPABILITIES)
@pytest.mark.parametrize("route", ROUTES)
def test_deleting_the_named_row_after_grounding_substitutes_nothing(monkeypatch, tmp_path, capability, route):
    mt, store = _two_row_tools(monkeypatch, tmp_path, route)
    [call] = _ground_without_issue(mt, {"credential_id": 501}, _action(capability)).tool_calls
    approved = call["args"]["inputs"]
    assert str(approved["credential_id"]) == "501"
    store["rows"][:] = [row for row in store["rows"] if row["id"] != 501]  # task 21 and row 500 remain proved

    result, calls = _forge_from(mt, approved, call["args"]["action"])

    assert calls.get("issued", []) == [] and result["issued"] == [], result
    assert "no longer resolves" in result["reason"] and "has no proved" not in result["reason"], result["reason"]
    _no_secret(result)


def test_a_retracted_proof_no_longer_resolves_its_row(monkeypatch, tmp_path):
    from ai.langgraph import engagement_store

    mt, store = _two_row_tools(monkeypatch, tmp_path, "admitted-task")
    [row_id] = [hop.row_id for hop in mt._ledger_view().hops if hop.effect == "krbtgt-hash:lab.local"]
    mt._ledger_store().retract(mt._require_ledger_binding(), [str(row_id)], reason="operator removed",
                               actor=engagement_store.Actor(kind="operator", name="op"))
    assert not [hop for hop in mt._ledger_view().hops if hop.effect == "krbtgt-hash:lab.local"]

    for capability in CAPABILITIES:  # the rows remain, but nothing proves them any more
        _assert_refused(mt, {"credential_id": 501, "credential_proof_task_id": "21"}, capability, stale=False)


# --- the outcome is recomputed on every binding pass -------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("setup, pin, expected", [
    (_two_row_tools, {"credential_id": 501}, (NTLM, "501", "21")),
    (_two_row_tools, {"credential_id": 501, "credential_proof_task_id": "21"}, (NTLM, "501", "21")),
    (_two_row_tools, {}, (AES, "500", "21")),
    (_two_row_tools, {"credential_id": 9999}, (None, "9999", "")),
    (_two_row_tools, {"credential_id": 501, "credential_proof_task_id": "30"}, (None, "501", "30")),
    (_two_task_tools, {"credential_proof_task_id": "21"}, (NTLM, None, "21")),
], ids=["id", "id-and-task", "unpinned", "absent", "mismatched", "older-task"])
def test_binding_the_same_inputs_three_times_keeps_their_identity(monkeypatch, tmp_path, route, setup, pin,
                                                                  expected):
    mt, _ = setup(monkeypatch, tmp_path, route)
    inputs = dict(pin)
    action = mt._capability_tool_action(dict(FORGE), inputs, capabilities)

    bound = []
    for _ in range(3):  # the card, the executor and its command build each bind the same inputs
        asyncio.run(mt._augment_capability_runtime_inputs(action, inputs))
        bound.append((inputs.get("key"),
                      str(inputs["credential_id"]) if inputs.get("credential_id") is not None else None,
                      str(inputs.get("credential_proof_task_id") or "")))

    if expected[1] is None and route == "agent-row":
        expected = (expected[0], "600", expected[2])  # the agent route names the row that holds task 21's key
    assert bound == [expected] * 3


def test_a_row_labelled_with_another_key_type_never_names_a_recovered_key(monkeypatch, tmp_path):
    # The row holds the proved aes128 text but reads as rc4 (no type label, 32 hex digits). Naming it would make
    # the next pass's reference fail to resolve; the bound key keeps only its proof task, consistently.
    aes128 = "a" * 32
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    store["task_outputs"][21] = ("[DC] 'lab.local' will be the domain\n** SAM ACCOUNT **\nSAM Username         : krbtgt\n"
                                 f"Credentials:\n* Primary:Kerberos-Newer-Keys *\n  aes128_hmac       (4096) : {aes128}\n")
    seed_ledger(mt, hops=[_proof_hop("krbtgt-hash:lab.local", 21, callback_id="13", target="krbtgt-21")])
    store["rows"].append({"id": 500, "account": "krbtgt", "realm": "lab.local", "type": "key",
                          "credential_text": aes128, "comment": "entered by hand", "task": None})
    inputs: dict = {}
    action = mt._capability_tool_action(dict(FORGE), inputs, capabilities)

    bound = []
    for _ in range(3):
        asyncio.run(mt._augment_capability_runtime_inputs(action, inputs))
        bound.append((inputs.get("key"), inputs.get("key_type"), inputs.get("credential_id"),
                      str(inputs.get("credential_proof_task_id") or "")))

    assert bound == [(aes128, "aes128", None, "21")] * 3


# --- the refusal says which proof is missing ---------------------------------------------------------------------

@pytest.mark.parametrize("capability", CAPABILITIES)
@pytest.mark.parametrize("pin", [{"credential_id": 501}, {"credential_proof_task_id": "21"},
                                 {"credential_id": 501, "credential_proof_task_id": "21"}],
                         ids=["id", "task", "both"])
def test_with_nothing_proved_a_reference_is_reported_as_no_proved_key(monkeypatch, tmp_path, capability, pin):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    seed_ledger(mt, hops=[])
    store["rows"].append(_typed(501, key=NTLM))  # a named row is not proof

    _assert_refused(mt, pin, capability, stale=False)


# --- approval boundary: the assembled native supervised request ---------------------------------------------------

def _non_top_client(monkeypatch, tmp_path):
    from test_step_bounds import _proved_krbtgt_client

    mt, split_issue = _proved_krbtgt_client(monkeypatch, tmp_path)
    rows = {row["id"]: row for row in asyncio.run(mt._fetch_credentials_cached(""))}
    assert rows[500]["credential_text"] == AES and rows[501]["credential_text"] == NTLM
    return mt, split_issue


def _proposed(run) -> list[dict]:
    return [card for card in run.cards if card.get("status") not in {"accepted", "rejected"}]


def test_a_supervised_card_for_a_lower_ranked_reference_runs_exactly_that_forge(monkeypatch, tmp_path):
    from test_step_bounds import _rescope_run, _rescope_script

    mt, split_issue = _non_top_client(monkeypatch, tmp_path)

    run = asyncio.run(_rescope_run(mt, split_issue, _rescope_script({"credential_id": 501})))

    assert run.issued_before_decision == []  # nothing ran before the operator accepted
    [card] = _proposed(run)
    arguments = card["data"]["arguments"]
    assert str(arguments["inputs"]["credential_id"]) == "501", arguments["inputs"]
    assert str(arguments["inputs"]["credential_proof_task_id"]) == "21"
    rubeus = [json.dumps(issue["parameters"]) for issue in run.issued if issue["command_name"] == "execute_assembly"]
    assert len(rubeus) == 1 and f"/rc4:{NTLM} " in rubeus[0] and AES not in rubeus[0], rubeus
    _no_secret(run.emissions)


def test_a_rejected_card_for_a_lower_ranked_reference_runs_nothing(monkeypatch, tmp_path):
    from test_step_bounds import _rescope_run, _rescope_script

    mt, split_issue = _non_top_client(monkeypatch, tmp_path)

    run = asyncio.run(_rescope_run(mt, split_issue, _rescope_script({"credential_id": 501}), decision="reject"))

    assert len(run.cards) >= 1 and run.issued == [], [issue["command_name"] for issue in run.issued]
    _no_secret(run.emissions)


@pytest.mark.parametrize("change", ["deleted", "rotated"])
def test_an_accepted_card_whose_reference_changed_runs_nothing(monkeypatch, tmp_path, change):
    from test_step_bounds import _rescope_run, _rescope_script

    mt, split_issue = _non_top_client(monkeypatch, tmp_path)
    fetch = mt._fetch_credentials_cached

    def change_row_501():
        async def changed(now, force=False):
            rows = [dict(row) for row in await fetch(now, force)]
            if change == "deleted":
                return [row for row in rows if row["id"] != 501]
            return [{**row, "credential_text": TYPED} if row["id"] == 501 else row for row in rows]
        mt._fetch_credentials_cached = changed

    run = asyncio.run(_rescope_run(mt, split_issue, _rescope_script({"credential_id": 501}),
                                   before_decision=change_row_501))

    assert len(_proposed(run)) == 1 and run.issued_before_decision == []
    assert run.issued == [], [issue["command_name"] for issue in run.issued]  # neither row 500 nor a refresh ran
    _no_secret(run.emissions)


def test_conversation_never_cards_or_issues_a_referenced_forge(monkeypatch, tmp_path):
    from test_step_bounds import _rescope_run, _rescope_script

    mt, split_issue = _non_top_client(monkeypatch, tmp_path)

    run = asyncio.run(_rescope_run(mt, split_issue, _rescope_script({"credential_id": 501}), mode="conversation"))

    assert run.cards == [] and run.issued == []
    _no_secret(run.emissions)
