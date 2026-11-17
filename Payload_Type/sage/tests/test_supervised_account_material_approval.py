"""An ordinary supervised request can approve an account Kerberos context from reviewed credential material.

A `dcsync-account` whose Mythic task ended with a completion-function error proves credential material only (a valid
review-backed `cred-material:` row), never `creds:<account>@<realm>`. Before this change no catalog candidate could
use that material, so the one step that could prove the credential was gated on that proof. Now the ordinary
supervised catalog offers `ensure-account-kerberos-context` from the material row, pinned to its source task and
review; its card asks the operator to authorize trying that material, and Accept runs the existing procedure from
exactly that source. Approval permits an attempt: only the procedure's verifier records a context.

Real on the path: the production `dcsync-account` executor and credential review (producer), the ledger, the
candidate producer, the brief check and binder, grounding, the native card and its resolution, the executor and its
task sink. Substituted, and labelled: the models' choices (scripted), Mythic's transport (task records, output and
issue seams), identities (`alice@foreign.test`, task 37, callback 13: the recorded shape is task 37's on channel 3)
and the key (synthetic). A scripted Supervisor selection proves code enforcement, not model planning.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
from types import SimpleNamespace

import pytest

from ai.langgraph import capabilities, engagement_state as es, task_evidence
from ai.langgraph import mythic_tools as package_mythic_tools
from tests.esl_fixtures import AGENT_PROCESSED, ingest_proof, seed_ledger
from tests.test_engagement_wiring import _proof_hop
from tests.test_step_bounds import DCSYNC_OK
from tests.test_supervised_catalog_step_binding import NOW, _client, _isolated, _run, _select

ACCOUNT, REALM, PROCEDURE = "alice", "foreign.test", "ensure-account-kerberos-context"
MATERIAL, WORKING = f"cred-material:hash:{ACCOUNT}@{REALM}", f"creds:{ACCOUNT}@{REALM}"
CONTEXT = f"kerberos-account-context:{ACCOUNT}@{REALM}@callback:13"
NTLM, OTHER_NTLM = "89abcdef01234567" * 2, "fedcba9876543210" * 2


def _dcsync_output(key: str) -> str:
    return (f"[DC] '{REALM}' will be the domain\n[DC] '{ACCOUNT}' will be the user account\n** SAM ACCOUNT **\n"
            f"SAM Username         : {ACCOUNT}\nCredentials:\n  Hash NTLM: {key}\n")


# Mythic's record of channel 3 task 37: the agent answered, then the completion function failed.
COMPLETION_ERROR = {"completed": True, "status": task_evidence.MYTHIC_COMPLETION_FUNCTION_ERROR,
                    "callback": {"display_id": 13}, "responses": [{"id": 1}], **AGENT_PROCESSED}
CONTEXT_COMMANDS = ["shell", "shell", "execute_assembly", "make_token", "ticket_store_add", "ticket_store_list",
                    "shell"]


def _produce(mt, split, records, task: int, key: str) -> dict:
    """Run the production `dcsync-account` as `task`, whose Mythic record is a completion-function error."""
    records[task] = COMPLETION_ERROR
    mt._outputs[task] = _dcsync_output(key)
    with split(lambda: _dcsync_output(key), display_id=task):
        return json.loads(asyncio.run(mt.execute_capability(
            {"capability": "dcsync-account", "domain": REALM, "account": ACCOUNT, "callback_id": "13"},
            {"timeout": 5})))


def _material_client(monkeypatch, tmp_path, records, *, working: bool = False, key: str = NTLM, responses: int = 2):
    """`_client`'s callbacks and lab.local krbtgt, plus replication rights on foreign.test (task 12's collection) and
    the material-only DCSync of alice as task 37. With `working`, a terminal-success proof of `creds:` as well.

    Mythic returns a DCSync's output as `responses` rows. The issue sink reviews their aggregate, while a re-read gets
    the rows. Row 94 records this shape: task 37 has two valid reviews, one of the aggregate (citation 1394-1426) and
    one of the re-read rows joined by a line break (1395-1427), and only the first is the row's proof."""
    mt, split = _client(monkeypatch, tmp_path)
    mt._outputs = {21: DCSYNC_OK}

    async def task_output(mythic, task_display_id, custom_return_attributes=None):
        text = mt._outputs.get(int(task_display_id)) or ""
        cut = text.find("Credentials:") if responses == 2 and int(task_display_id) != 21 else -1
        rows = [text[:cut], text[cut:]] if cut > 0 else [text] if text else []
        return [{"response_text": base64.b64encode(row.encode()).decode()} for row in rows]

    monkeypatch.setattr(package_mythic_tools.mythic, "get_all_task_output_by_id", task_output)
    proof = ingest_proof(mt._require_ledger_binding().engagement_key, task_id="12", job_id="job-12", callback_id="14")
    seed_ledger(mt, graph_facts=[{"predicate": f"ds-replication-rights:{REALM}", "source": "fixture", "timestamp": NOW,
                                  "ttl_seconds": 86400, "proof_envelope": proof}])
    mt._produced = _produce(mt, split, records, 37, key)
    if working:
        seed_ledger(mt, hops=[_proof_hop(WORKING, 38, callback_id="13")])
    return mt, split


def _context_split(split, *, proved: bool = True):
    """The task sink's outputs for the account-context procedure, by command and order; `proved` decides whether the
    final service proof succeeds."""
    def wrapped(_output, calls, display_id):
        def output():
            issued = calls["issued"]
            command = issued[-1]["command_name"]
            shells = sum(row["command_name"] == "shell" for row in issued)
            ticket = base64.b64encode(b"A" * 80).decode()
            access = " Directory of \\\\dc01.lab.local\\SYSVOL\r\nPolicies\r\nThe command completed successfully."
            return {
                "execute_assembly": f"[*] Action: Ask TGT\n[*] base64(ticket.kirbi):\n{ticket}\n",
                "make_token": f"Successfully impersonated local\\user for local access and {REALM}\\{ACCOUNT} for "
                              "remote access.",
                "ticket_store_add": "Added Ticket to Ticket Store",
                "ticket_store_list": f"Cached Tickets\r\nClient: {ACCOUNT} @ {REALM.upper()}\r\n"
                                     f"Server: krbtgt/{REALM} @ {REALM.upper()}\r\n",
                "shell": ("Cached Tickets: (0)" if shells == 1 else access if proved and shells > 2
                          else "Access is denied."),
            }.get(command, "ok")
        # Procedure tasks are numbered from 101, apart from the DCSync tasks the fixture already holds.
        return split(output, calls, display_id=lambda: 100 + len(calls.get("issued", [])))
    return wrapped


def _cards_for(run) -> list:
    return [card for card in run.cards if card["data"]["arguments"]["action"]["name"] == PROCEDURE]


def _material_refs(card) -> dict:
    return card["data"]["arguments"]["action"]["intent"].get("credential_material") or {}


def _row_review(mt) -> str:
    row = es.typed_row(mt._ledger_view().hops, f"account-secret:{ACCOUNT}@{REALM}")
    return row.proof_envelope["metadata"]["review_id"]


def _secret_free(run, *keys: str) -> None:
    for blob in (json.dumps(run.emissions, default=str), json.dumps(run.values, default=str),
                 json.dumps(run.outcomes, default=str)):
        assert not any(key in blob for key in keys)


def _store_rows(mt) -> list:
    return asyncio.run(mt._fetch_credentials_cached("now"))


def _executor_material(run) -> list:
    return [result.get("material") for outcome in run.outcomes for result in outcome.get("executor_results") or []]


# --- (a) the material-only producer stays partial; its candidate reaches one explicit material-use card -----------

@pytest.mark.parametrize("responses", [2, 1])
def test_a_material_only_account_dcsync_reaches_one_material_use_card_and_nothing_else(
        monkeypatch, tmp_path, mythic_task_records, responses):
    mt, split = _material_client(monkeypatch, tmp_path, mythic_task_records, responses=responses)
    produced = mt._produced
    assert produced["verdict"] == "partial" and produced["material_only"] is True
    assert produced["recorded_effects"] == [MATERIAL] and WORKING not in json.dumps(produced["recorded_effects"])
    assert PROCEDURE in produced["recovery"] and "37" in produced["recovery"]
    rows_before = len(_store_rows(mt))

    run = asyncio.run(_run(mt, split, [_select(PROCEDURE, contains=ACCOUNT)], decision=None))

    [card] = run.cards
    assert _cards_for(run) == [card]
    action = card["data"]["arguments"]["action"]
    assert action["preconditions"] == [MATERIAL, "live-callback:13"] and action["effects"] == [CONTEXT]
    assert _material_refs(card) == {"effect": MATERIAL, "task_id": "37", "review_id": _row_review(mt),
                                    "output_sha256": _material_refs(card)["output_sha256"]}
    assert len(_material_refs(card)["output_sha256"]) == 64
    text = card["description"]
    assert all(part in text for part in (f"{ACCOUNT}@{REALM}", "task 37", "callback 13", "material-only"))
    assert run.issued == [] and run.before.worker_calls == 0 and len(_store_rows(mt)) == rows_before
    _secret_free(run, NTLM)


# --- (b) Accept runs the existing procedure from the pinned input; Reject and Respond issue nothing ----------------

@pytest.mark.parametrize("proved", [True, False])
def test_accept_runs_the_proved_procedure_from_the_pinned_material_once(monkeypatch, tmp_path, mythic_task_records,
                                                                        proved):
    control_mt, control_split = _isolated(monkeypatch, tmp_path / "control", client=lambda m, p: _material_client(
        m, p, mythic_task_records, working=True))
    control = asyncio.run(_run(control_mt, _context_split(control_split), [_select(PROCEDURE, contains=ACCOUNT)]))
    assert not _material_refs(control.all_cards[0])          # a proved credential keeps the proved variant
    mt, split = _isolated(monkeypatch, tmp_path / "material", client=lambda m, p: _material_client(
        m, p, mythic_task_records))

    run = asyncio.run(_run(mt, _context_split(split, proved=proved), [_select(PROCEDURE, contains=ACCOUNT)]))

    commands = [row["command_name"] for row in run.issued]
    assert commands == [row["command_name"] for row in control.issued] == CONTEXT_COMMANDS
    [asktgt] = [json.dumps(row["parameters"]) for row in run.issued if row["command_name"] == "execute_assembly"]
    assert f"/rc4:{NTLM}" in asktgt
    # The card the operator accepted (Mythic's approval record) names the source the executor used.
    assert [_material_refs(card)["task_id"] for card in run.cards] == ["37"]
    assert _executor_material(run) == [{"effect": MATERIAL, "task_id": "37", "review_id": _row_review(mt)}]
    step_text = "\n".join(str(m.content) for m in run.values["supervisor_messages"] if "[step outcome]" in str(m.content))
    assert f"approved reviewed credential material `{MATERIAL}` from task 37" in step_text
    achieved = set(mt._capability_achieved_effects())
    assert (CONTEXT in achieved) is proved
    assert WORKING not in achieved and MATERIAL in achieved
    _secret_free(run, NTLM)


@pytest.mark.parametrize("decision", ["reject", "respond"])
def test_reject_or_respond_issues_nothing(monkeypatch, tmp_path, mythic_task_records, decision):
    mt, split = _material_client(monkeypatch, tmp_path, mythic_task_records)
    run = asyncio.run(_run(mt, _context_split(split), [_select(PROCEDURE, contains=ACCOUNT)], decision=decision))
    assert len(_cards_for(run)) == 1 and run.issued == []


def test_a_replayed_accept_never_executes_twice(monkeypatch, tmp_path, mythic_task_records):
    from tests.test_hitl_steer_current_intent import _NativeDriverChat

    original = _NativeDriverChat.chat

    async def twice(self, request):
        result = await original(self, request)
        if getattr(request, "InputResponse", None) is not None:
            await original(self, request)                     # the same Accept delivered again
        return result

    monkeypatch.setattr(_NativeDriverChat, "chat", twice)
    mt, split = _material_client(monkeypatch, tmp_path, mythic_task_records)
    run = asyncio.run(_run(mt, _context_split(split), [_select(PROCEDURE, contains=ACCOUNT)]))
    assert [row["command_name"] for row in run.issued] == CONTEXT_COMMANDS


# --- (c) equivalent selections bind the same card; every near-match refuses before any task -------------------------

@pytest.mark.parametrize("overrides", [{"callback_display_id": "#13"}, {"callback_display_id": 13},
                                       {"inputs_by_reference": ["task:37"]}])
def test_equivalent_selections_bind_the_same_card(monkeypatch, tmp_path, mythic_task_records, overrides):
    runs = []
    for name, brief in (("baseline", {}), ("variant", overrides)):
        mt, split = _isolated(monkeypatch, tmp_path / name,
                              client=lambda m, p: _material_client(m, p, mythic_task_records))
        runs.append(asyncio.run(_run(mt, split, [_select(PROCEDURE, contains=ACCOUNT, **brief)], decision=None)))
    baseline, variant = runs

    [first], [second] = _cards_for(baseline), _cards_for(variant)
    strip = lambda card: {**card["data"]["arguments"], "inputs": {}}  # noqa: E731 - inputs carry request-scoped ids
    assert strip(first) == strip(second) and _material_refs(second)["task_id"] == "37"
    assert variant.issued == []


@pytest.mark.parametrize("overrides", [
    {"inputs_by_reference": ["task:21"]},                    # another held task cannot replace the pinned source
    {"candidate_ref": "cand-0000000000000000"},              # a stale or unknown reference
    {"callback_display_id": 14},                             # another callback
    {"allowed_capabilities": ["dcsync-account"]},            # another procedure
])
def test_conflicting_or_stale_selections_refuse_before_any_card(monkeypatch, tmp_path, mythic_task_records,
                                                                  overrides):
    mt, split = _material_client(monkeypatch, tmp_path, mythic_task_records)
    run = asyncio.run(_run(mt, split, [_select(PROCEDURE, contains=ACCOUNT, **overrides)], decision="accept"))
    assert run.cards == [] and run.issued == []
    assert run.briefs and run.briefs[0][0] == "rejected" and "catalog binding" in run.briefs[0][1]


def _replace_material_row(mt, only_task: str = ""):
    """Synthetic ledger change: the material row is withdrawn, or (with `only_task`) proves only that task's valid
    review, its own admitted observation becoming the row's proof."""
    original, identity = mt._ledger_view, f"account-secret:{ACCOUNT}@{REALM}"

    def replaced(hop):
        if hop.id != identity:
            return [hop]
        if not only_task:
            return []
        [kept] = [item for item in hop.contributions if item["task_id"] == only_task]
        return [dataclasses.replace(hop, contributions=[kept],
                                    proof_envelope=kept["writers"]["review"]["admitted"]["proof_envelope"])]

    def view(*args):
        found = original(*args)
        return SimpleNamespace(**{name: getattr(found, name) for name in ("graph_facts", "corrected_hops")},
                               hops=[item for hop in found.hops for item in replaced(hop)],
                               snapshot=getattr(found, "snapshot", None))
    mt._ledger_view = view


@pytest.mark.parametrize("change", ["withdrawn", "replaced_by_another_source", "output_changed", "output_rebound"])
def test_evidence_changed_after_the_card_refuses_before_any_task(monkeypatch, tmp_path, mythic_task_records, change):
    mt, split = _material_client(monkeypatch, tmp_path, mythic_task_records)
    if change == "replaced_by_another_source":
        _produce(mt, split, mythic_task_records, 41, OTHER_NTLM)   # a second valid source; task 37 stays the proof
    rows_before = len(_store_rows(mt))

    def mutate():
        if change == "withdrawn":
            _replace_material_row(mt)
        elif change == "replaced_by_another_source":
            _replace_material_row(mt, only_task="41")
        else:   # another key; or the reviewed value now named only for another account
            mt._outputs[37] = (_dcsync_output(OTHER_NTLM) if change == "output_changed"
                               else _dcsync_output(NTLM).replace(ACCOUNT, "bob"))
            mt._task_output_cache = {}

    run = asyncio.run(_run(mt, _context_split(split), [_select(PROCEDURE, contains=ACCOUNT)],
                           before_decision=mutate))
    assert len(_cards_for(run)) == 1
    assert run.issued == [] and len(_store_rows(mt)) == rows_before   # not even the sacrificial logon row
    [outcome] = run.outcomes
    if change.startswith("output_"):                         # the executor's binder: the key no longer re-reads
        assert [(r["verdict"], r["task_ids"], r["material"]["task_id"]) for r in outcome["executor_results"]] == [
            ("failed", [], "37")]
    else:                                                    # the pre-execution re-check: the pinned source is gone
        assert "no longer matches current evidence" in outcome["detail"]
    _secret_free(run, NTLM, OTHER_NTLM)


# --- (d) other lanes and proof classes keep their behavior ------------------------------------------------------------

def _state(mt) -> es.EngagementState:
    view = mt._ledger_view()
    return es.EngagementState(objective="", hops=list(view.hops), graph_facts=list(view.graph_facts),
                              footholds=[es.Foothold(callback_id="13", agent="apollo", host="DC01", forest="lab.local",
                                                     identity="operator", integrity="medium", alive=True,
                                                     source="fixture", timestamp=NOW)],
                              engagement_id=mt._eng_key(), runtime_scope=True)


def test_only_the_ordinary_supervised_producer_opts_into_material(monkeypatch, tmp_path, mythic_task_records):
    mt, _split = _material_client(monkeypatch, tmp_path, mythic_task_records)
    state = _state(mt)
    named = lambda actions: [a for a in actions if a.name == PROCEDURE and ACCOUNT in a.target]  # noqa: E731
    # Auto, Autonomous Solve and the planners read the default: no candidate from material.
    assert named(capabilities.actions_from_state(state)) == []
    [action] = named(capabilities.actions_from_state(state, reviewed_account_material=True))
    assert action.preconditions == [MATERIAL, "live-callback:13"] and action.intent["credential_material"]["task_id"] == "37"


def test_a_proved_credential_keeps_the_proved_variant(monkeypatch, tmp_path, mythic_task_records):
    mt, _split = _material_client(monkeypatch, tmp_path, mythic_task_records, working=True)
    actions = [a for a in capabilities.actions_from_state(_state(mt), reviewed_account_material=True)
               if a.name == PROCEDURE and ACCOUNT in a.target]
    assert actions and all("credential_material" not in a.intent and a.preconditions[0] == WORKING for a in actions)


def test_a_rejected_review_offers_no_material_candidate(monkeypatch, tmp_path, mythic_task_records):
    mt, _split = _material_client(monkeypatch, tmp_path, mythic_task_records, key="0" * 32)
    assert MATERIAL not in set(mt._capability_achieved_effects())
    assert not [a for a in capabilities.actions_from_state(_state(mt), reviewed_account_material=True)
                if a.name == PROCEDURE and ACCOUNT in a.target]


def test_conversation_offers_no_card(monkeypatch, tmp_path, mythic_task_records):
    mt, split = _material_client(monkeypatch, tmp_path, mythic_task_records)
    run = asyncio.run(_run(mt, split, [_select(PROCEDURE, contains=ACCOUNT)], mode="conversation"))
    assert run.cards == [] and run.issued == []


@pytest.mark.parametrize("mode", ["auto", "autonomous_solve"])
def test_the_executor_never_uses_material_outside_an_approved_ordinary_supervised_call(
        monkeypatch, tmp_path, mythic_task_records, mode):
    """A model-written material action issues nothing in auto (refused by the executor's lane check: removing it
    issues the procedure) or in supervised Autonomous Solve (no exact approval, refused at the contract)."""
    from ai.langgraph.request_contract import build_request_contract
    from ai.langgraph.turn_authority import authority_from_request_contract

    mt, split = _material_client(monkeypatch, tmp_path, mythic_task_records)
    [action] = [a for a in capabilities.actions_from_state(_state(mt), reviewed_account_material=True)
                if a.name == PROCEDURE and ACCOUNT in a.target]
    contract = build_request_contract(request_id="chat:1:request:9", channel_id="1", operation_id="1",
                                      mode="auto" if mode == "auto" else "supervised", autonomous_solve=True)
    mt.set_request_contract(contract)
    mt._turn_authority = authority_from_request_contract(contract)
    calls: dict = {}
    with _context_split(split)(None, calls, display_id=lambda: 60 + len(calls.get("issued", []))):
        try:
            result = json.loads(asyncio.run(mt.execute_capability(
                {"name": action.name, "target": action.target, "preconditions": action.preconditions,
                 "effects": action.effects, "intent": action.intent}, {"callback_id": "13", "key": OTHER_NTLM})))
        except PermissionError:
            result = {"ok": False}
    assert calls.get("issued", []) == [] and result["ok"] is False
