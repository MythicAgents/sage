"""An ordinary supervised catalog step runs one action that current evidence supports, bound by code.

The Supervisor sees the catalog actions `actions_from_state` derives from one fresh ledger snapshot, each with a
`candidate_ref`. A non-collection catalog step names one; Sage binds it at brief admission, proposes exactly its
action without a worker-model call, and checks it again before execution. An unsupported selection ends with a
factual blocker before any card or task. The brief's targets are recorded with it: they never refuse the step or
change what it runs. Raw, collection and Autonomous Solve steps still check theirs.

Everything on the path is real except the models' choices and Mythic's network I/O: the graph, the ledger store with
a krbtgt key proved through the production dcsync executor, the producer, the brief check, the binding, the step
bound, grounding, the native approval card and its resolution, MythicTools' executor and its final task issue (the
inert `_split_issue` seam). Scripted selections test code enforcement, not model planning.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from ai.langgraph import catalog_binding as cb
from ai.langgraph import engagement_state as es
from ai.langgraph import handoff_brief as hb
from ai.langgraph import model as model_module
from ai.langgraph import mythic_tools as package_mythic_tools
from ai.langgraph import step_bounds as sb
from ai.langgraph.mythic_tools import MythicTools
from sage_chat.headless import build_chat_request
from sage_chat.service import bind_channel_thread_id
from sage_chat import session as chat_session
from sage_chat.session import drop_channel_session
from tests.esl_fixtures import AGENT_PROCESSED, ingest_proof, seed_ledger
from tests.test_engagement_wiring import _proof_hop
from tests.test_hitl_steer_current_intent import (
    _NativeDriverChat, _NativeScriptedModel, _call, _cards, _input_response,
)
from tests.test_step_bounds import KRBTGT_KEY, _callback, _live_callbacks, _outcomes, _proved_krbtgt_client

ISSUE = "issue_task_and_waitfor_task_output"
PROMPT = "Work toward administrative control of lab.local, child.lab.local and foreign.test from callbacks 13 and 14."
NOW = "2026-11-14T13:00:00+00:00"
GPOS = {"alpha-policy": "0a93e998-2599-4da8-9717-6744993ded3a", "beta-policy": "1b93e998-2599-4da8-9717-6744993ded3b"}
_LINE = re.compile(r"candidate_ref=(cand-[0-9a-f]{16}) \| (\S+) -> (\S+) \| callback (\S+)")


def _foothold(callback: str, host: str, forest: str, identity: str) -> es.Foothold:
    return es.Foothold(callback_id=callback, agent="apollo", host=host, forest=forest, identity=identity,
                       integrity="medium", alive=True, source="fixture", timestamp=NOW)


def _client(monkeypatch, tmp_path):
    """A proved krbtgt for lab.local (task 21) on live callback 13, and two writable GPOs on live callback 14.

    Candidates: `forge-golden-ticket` for lab.local and `gpo-controlled-system-exec` for each GPO."""
    mt, split = _proved_krbtgt_client(monkeypatch, tmp_path)
    # Mythic reports both callbacks live; Sage reconciles its footholds from that before each catalog check.
    mt._callbacks = _live_callbacks(mt, monkeypatch, [_callback(13, "DC01", "lab.local", "operator"),
                                                     _callback(14, "WS02", "child.lab.local", "analyst")])
    assert mt._engagement_footholds == []                   # nothing seeds the cache: candidates need the live read
    mt._recon_snapshot_cache[("list_callbacks", "all")] += [{"id": 14, "display_id": 14, "host": "WS02"}]
    proof = ingest_proof(mt._require_ledger_binding().engagement_key, task_id="12", job_id="job-12", callback_id="14")
    facts = [predicate for gpo, guid in GPOS.items() for predicate in (
        f"generic-write:gpo:{gpo}", f"gpo-domain:{gpo}:child.lab.local", f"gpo-guid:{gpo}:{guid}",
        f"gpo-affects-computer:{gpo}:ws02:child.lab.local")]
    seed_ledger(mt, graph_facts=[{"predicate": predicate, "source": "fixture", "timestamp": NOW,
                                  "ttl_seconds": 86400, "proof_envelope": proof} for predicate in facts])
    return mt, split


def _state_for(mt, procedure: str):
    """The synthetic state a procedure's candidate needs beyond `_client`: DCSync of child.lab.local on callback 14
    from a replication right (of one account, `alice`, for `dcsync-account`), and that callback's Kerberos context
    refresh from an admitted Domain Admins proof."""
    if procedure in {"dcsync-krbtgt", "dcsync-account"}:
        seed_ledger(mt, graph_facts=[{"predicate": "ds-replication-rights:child.lab.local", "source": "fixture",
                                      "timestamp": NOW, "ttl_seconds": 86400}])
    if procedure == "dcsync-account":
        seed_ledger(mt, graph_facts=[{"predicate": "credential-target:alice@child.lab.local", "source": "fixture",
                                      "timestamp": NOW, "ttl_seconds": 86400}])
    elif procedure == "ensure-kerberos-context":
        seed_ledger(mt, hops=[_proof_hop("da:child.lab.local", 40, callback_id="14")])
    return mt


def _brief_targets(run) -> list:
    """The targets the brief event recorded for the request's one brief."""
    [event] = run.model._request_event_ledger.actual_events(kind="brief")
    return json.loads(dict(event.metadata)["brief"])["targets"]


def _view(messages) -> str:
    return next(str(m.content) for m in reversed(messages) if "[Sage catalog candidates" in str(m.content))


def _listed(messages) -> list[tuple[str, str, str, str]]:
    return _LINE.findall(_view(messages))


def _select(procedure: str, contains: str = "", *, index: int = 1, before=None, **overrides):
    """The Supervisor's scripted choice: the listed candidate for `procedure` (whose target contains `contains`),
    copied into a catalog brief; `overrides` replace brief fields; `before()` runs once the choice is made."""
    def choose(messages):
        ref, name, target, callback = next(line for line in _listed(messages)
                                           if line[1] == procedure and contains in line[2])
        domain = dict(part.split("=", 1) for part in target.split(";") if "=" in part).get("domain", target)
        brief = {"handoff_title": f"run {name}", "handoff_instruction": PROMPT, "allowed_capabilities": [name],
                 "callback_display_id": callback, "targets": [domain], "expected_result": "The procedure's result.",
                 "candidate_ref": ref, **overrides}
        _select.chosen = ref
        if before is not None:
            before()
        return _call("transfer_to_Mythic_Operator", brief, index)
    return choose


def _worker_calls(inputs) -> int:
    return sum(any("<step_scope>" in str(m.content) for m in messages) for messages in inputs)


async def _run(mt, split, script, *, decision="accept", mode="supervised", autonomous_solve=False,
               before_decision=None, response="Explain the current state instead.") -> SimpleNamespace:
    model = model_module.Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline catalog binding",
        config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
        task_id=0, agent_task_id="catalog-binding", mode=mode, autonomous_solve=autonomous_solve, operation_id=1,
        channel_id=1, apitoken_id=0,
    )
    model.mythic_client = mt
    model._payload_names = []
    model.memory = InMemorySaver()
    fake = _NativeScriptedModel(messages=iter(list(script) + [AIMessage(content="Final answer.")] * 6))
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = _NativeDriverChat(model)
    calls: dict = {}
    try:
        with split(lambda: "ok", calls, display_id=lambda: 30 + len(calls.get("issued", []))):
            first = build_chat_request(PROMPT, channel_id=1, operation_id=1, request_id=1, config={"mode": mode})
            bind_channel_thread_id(first, model)
            await chat.chat(first)
            cards = [card for card in _cards(chat) if card.get("status") not in {"accepted", "rejected", "responded"}]
            before = SimpleNamespace(issued=list(calls.get("issued", [])), worker_calls=_worker_calls(fake.inputs),
                                     inputs=len(fake.inputs))
            if decision and cards:
                if before_decision is not None:
                    before_decision()
                second = build_chat_request("", channel_id=1, operation_id=1, request_id=2, config={"mode": mode})
                second.InputResponse = _input_response(decision, cards[0],
                                                       response=response if decision == "respond" else "")
                await chat.chat(second)
        snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
        return SimpleNamespace(
            model=model, fake=fake, cards=cards, before=before, issued=calls.get("issued", []),
            values=snapshot.values, all_cards=_cards(chat),
            terminals=[e for e in chat.emissions if e.get("complete_request") and not (
                isinstance(e.get("metadata"), dict) and e["metadata"].get("special_type") == "input_requested")],
            outcomes=_outcomes(snapshot.values.get("supervisor_messages", [])),
            briefs=[(e.phase, e.content) for e in model._request_event_ledger.actual_events(kind="brief")])
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


def _isolated(monkeypatch, path, client=None):
    """A fresh `_client` (or `client`) with its own ledger database, for a test that builds more than one client."""
    path.mkdir()
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(path / "sage.db"))
    monkeypatch.setattr(chat_session, "_CHANNEL_MARKER_DB_PATH", str(path / "sage.db"))
    return (client or _client)(monkeypatch, path)


def _commands(run) -> list[str]:
    return [json.dumps(row["parameters"], sort_keys=True) for row in run.issued]


# --- (a) and (c) at admission: an unsupported or conflicting selection ends blocked before any card ---------------

RAW_FALLBACK = [_call("transfer_to_Mythic_Operator", {
    "handoff_title": "raw fallback", "handoff_instruction": PROMPT, "callback_display_id": 13,
    "targets": ["foreign.test"], "expected_result": "A replication result.", "allowed_tools": [ISSUE]}, 7),
    _call(ISSUE, {"command": "dcsync", "parameters": "-Domain foreign.test", "callback_display_id": 13}, 8)]

def _unsupported(**fields):
    return _call("transfer_to_Mythic_Operator", {
        "handoff_title": "replicate foreign.test", "handoff_instruction": PROMPT,
        "allowed_capabilities": ["dcsync-krbtgt"], "callback_display_id": 13, "targets": ["foreign.test"],
        "expected_result": "The foreign krbtgt key.", **fields}, 1)


UNSUPPORTED = _unsupported()


def _tool_scoped(**fields):
    return [_call("transfer_to_Mythic_Operator", {
        "handoff_title": "run a procedure", "handoff_instruction": PROMPT, "allowed_tools": ["execute_capability"],
        "callback_display_id": 14, "targets": ["lab.local"], "expected_result": "The procedure's result.", **fields}, 1),
        _call("execute_capability", {"action": {"capability": "forge-golden-ticket", "domain": "lab.local",
                                                "callback_id": "14"}}, 2)]


def _unavailable(mt):
    def fail(*_args, **_kwargs):
        raise RuntimeError("ledger offline")
    return lambda: setattr(mt, "_ledger_view", fail)


@pytest.mark.parametrize("named", [True, False], ids=["targets", "no-targets"])
@pytest.mark.parametrize("case", ["unsupported", "fabricated", "callback", "procedure", "unavailable", "tool-scoped"])
def test_an_unbindable_catalog_step_ends_blocked_with_no_card_or_task(monkeypatch, tmp_path, case, named):
    mt, split = _client(monkeypatch, tmp_path)
    omit = {} if named else {"targets": None}      # recorded targets neither cause nor excuse a refusal
    choice = {
        "unsupported": _unsupported(**omit),
        "fabricated": _select("forge-golden-ticket", candidate_ref="cand-0000000000000000", **omit),
        "callback": _select("forge-golden-ticket", callback_display_id=14, **omit),
        "procedure": _select("forge-golden-ticket", allowed_capabilities=["dcsync-krbtgt"], **omit),
        "unavailable": _select("forge-golden-ticket", before=_unavailable(mt), **omit),
        "tool-scoped": _tool_scoped(**omit),  # execute_capability named as a tool is a catalog step, not a bypass
    }[case]

    run = asyncio.run(_run(mt, split, [*(choice if isinstance(choice, list) else [choice]), *RAW_FALLBACK]))

    # Nothing was carded or issued, and the raw fallback scripted next never ran: exactly one terminal.
    assert run.cards == [] and run.issued == [] and run.outcomes == []
    [terminal] = run.terminals
    content = str(terminal.get("content") or "")
    assert "Blocked" in content and "could not bind this catalog step" in content, content
    assert "not a finding that the objective is impossible" in content
    assert run.briefs == [("rejected", run.briefs[0][1])] and "catalog binding:" in run.briefs[0][1]
    assert {"unsupported": "names no candidate_ref", "fabricated": "not supported by current evidence",
            "callback": "not callback 14", "procedure": "not `dcsync-krbtgt`",
            "unavailable": "unavailable", "tool-scoped": "`execute_capability` runs a catalog procedure"}[case] in content, content


# --- (b) two supported actions of one capability stay distinct and each runs exactly -----------------------------

@pytest.mark.parametrize("gpo", sorted(GPOS))
def test_each_supported_action_of_one_capability_reaches_its_exact_card_and_tasks(monkeypatch, tmp_path, gpo):
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(mt, split, [_select("gpo-controlled-system-exec", gpo),
                                       AIMessage(content="The procedure ran.")]))

    listed = _listed(run.fake.inputs[0])
    gpo_refs = {ref for ref, name, _target, _callback in listed if name == "gpo-controlled-system-exec"}
    assert len(gpo_refs) == 2 and _select.chosen in gpo_refs      # same capability, two distinct candidates
    # Sage proposed the bound action: no worker-model call and no task before the operator's decision.
    assert run.before.worker_calls == 0 and run.before.issued == []
    [card] = run.cards
    arguments = card["data"]["arguments"]
    assert arguments["action"]["name"] == "gpo-controlled-system-exec"
    assert arguments["action"]["intent"]["gpo"] == gpo and arguments["inputs"]["callback_id"] == "14"
    # The accepted sequence is that GPO's procedure on callback 14, and no other GPO's.
    other = next(name for name in GPOS if name != gpo)
    assert run.issued and {row["callback_display_id"] for row in run.issued} == {14}
    assert any(GPOS[gpo].upper() in text.upper() for text in _commands(run)), _commands(run)
    assert not any(GPOS[other].upper() in text.upper() for text in _commands(run))


# --- (c) at execution: support withdrawn, bindings changed or ledger unavailable while the card waits ------------

def _withdraw(mt):
    return lambda: mt._callbacks.pop()                      # callback 14 is no longer active in Mythic


def _rebind(mt):
    return lambda: mt._callbacks[1].update(user="other")    # callback 14 now runs as another account


def _mythic_down(mt):
    def unreachable():
        raise RuntimeError("Mythic unreachable")
    return lambda: setattr(mt, "get_all_active_callbacks", unreachable)


def _unrelated(mt):
    return lambda: seed_ledger(mt, graph_facts=[{"predicate": "domain-controller:dc09:other.test", "source": "fixture",
                                                 "timestamp": NOW, "ttl_seconds": 86400}])


@pytest.mark.parametrize("change, runs", [(_withdraw, False), (_rebind, False), (_unavailable, False),
                                          (_mythic_down, False), (_unrelated, True)],
                         ids=["withdrawn", "rebound", "unavailable", "mythic-down", "unrelated"])
def test_a_waiting_card_runs_only_while_its_candidate_is_unchanged(monkeypatch, tmp_path, change, runs):
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(mt, split, [_select("gpo-controlled-system-exec", "alpha"),
                                       AIMessage(content="The procedure result was read.")],
                           before_decision=change(mt)))

    assert len(run.cards) == 1 and run.before.issued == []
    assert bool(run.issued) is runs
    [outcome] = run.outcomes
    if not runs:
        assert outcome["status"] == sb.StepStatus.PRECONDITION_UNSATISFIABLE.value, outcome
        assert "no longer matches current evidence" in outcome["detail"] and outcome["tasks_issued"] == []


# --- (d) Accept runs exactly the carded procedure, Reject runs none, Respond cancels the binding -----------------

@pytest.mark.parametrize("targets", [["lab.local"], ["WS02.CHILD.LAB.LOCAL", "krbtgt@CHILD.LAB.LOCAL"]],
                         ids=["own-domain", "other-names"])
@pytest.mark.parametrize("decision", ["accept", "reject", "respond"])
def test_the_decision_on_a_bound_forge_card_is_final(monkeypatch, tmp_path, decision, targets):
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(mt, split, [_select("forge-golden-ticket", targets=targets),
                                       AIMessage(content="The forge ran.")], decision=decision))

    [card] = run.cards
    arguments = card["data"]["arguments"]
    assert arguments["action"]["name"] == "forge-golden-ticket" and arguments["inputs"]["callback_id"] == "13"
    # The proved key is bound by reference; no card, stream or report carries it.
    assert str(arguments["inputs"].get("credential_proof_task_id")) == "21"
    assert KRBTGT_KEY not in json.dumps(run.cards, default=str) + json.dumps(run.terminals, default=str)
    rubeus = [text for row, text in zip(run.issued, _commands(run)) if row["command_name"] == "execute_assembly"]
    assert len(rubeus) == (decision == "accept")
    if decision == "accept":
        assert f"/aes256:{KRBTGT_KEY}" in rubeus[0]
    else:
        assert run.issued == []
    assert len(run.terminals) == 1


# --- (a2) a cross-domain candidate cards its own action whichever host form the brief records ---------------------

CROSS_DOMAIN_SECRET = ("can-read-managed-local-admin-secret:account=operator;account_domain=lab.local;target=ws02;"
                       "target_domain=child.lab.local")


@pytest.mark.parametrize("target", ["ws02.child.lab.local", "WS02", "ws02.lab.local"])
def test_a_cross_domain_candidate_cards_its_action_for_any_recorded_host_form(monkeypatch, tmp_path, target):
    mt, split = _client(monkeypatch, tmp_path)
    seed_ledger(mt, graph_facts=[{"predicate": CROSS_DOMAIN_SECRET, "source": "fixture", "timestamp": NOW,
                                  "ttl_seconds": 86400}])

    run = asyncio.run(_run(mt, split, [_select("read-managed-local-admin-secret", targets=[target])], decision=None))

    assert run.issued == [] and run.before.worker_calls == 0 and run.briefs == [("accepted", "Mythic_Operator")]
    [card] = run.cards
    assert card["data"]["arguments"]["action"]["target"] == CROSS_DOMAIN_SECRET.split(":", 1)[1] + ";callback=13"
    assert _brief_targets(run) == [target]


# --- (d2) a forge brief naming several key rows of one proved krbtgt binds that key set --------------------------

OTHER_AES, OTHER_RC4, TYPED = "b" * 64, "e" * 32, "f" * 64


def _reported(row_id, key, task_id, *, account="krbtgt", realm="lab.local", row_type="key"):
    """A store row an agent reported from a completed Mythic task: its own lineage proves it."""
    return {"id": row_id, "account": account, "realm": realm, "type": row_type, "credential_text": key,
            "comment": "reported by the agent", "timestamp": NOW,
            "task": {"display_id": task_id, "status": "completed", "completed": True, **AGENT_PROCESSED,
                     "command_name": "dcsync", "callback": {"display_id": 13}}}


def _store_client(monkeypatch, tmp_path, *extra):
    """The catalog fixture with its rows 500 (aes256) and 501 (ntlm) of task 21 read through Sage's own credential
    reader; only Mythic's GraphQL call is inert. Rows carry no task lineage, as run 2's did. `extra` rows join the
    store; the returned list is the store, so a test can change it."""
    mt, split = _client(monkeypatch, tmp_path)
    store = [{**row, "task": None} for row in asyncio.run(mt._fetch_credentials_cached(NOW))] + list(extra)
    del mt._fetch_credentials_cached                       # the fixture's stand-in; Sage's reader from here on
    original = package_mythic_tools.mythic.execute_custom_query

    async def query(client, text, *args, **kwargs):
        if "SageReadCredentials" in text:
            return {"credential": json.loads(json.dumps(store))}
        return await original(client, text, *args, **kwargs)

    monkeypatch.setattr(package_mythic_tools.mythic, "execute_custom_query", query)
    asyncio.run(mt._fetch_credentials_cached(NOW, force=True))    # the Supervisor read the store before naming rows
    return mt, split, store


@pytest.mark.parametrize("references, credential_id, credential_ids", [
    (["cred:500", "cred:501"], 500, ["500", "501"]),
    (["cred:501", "cred:500"], 500, ["500", "501"]),
    (["cred:501", "cred:500", "cred:501"], 500, ["500", "501"]),
    (["@cred:501", "cred:500"], 500, ["500", "501"]),
    (["cred:500", "cred:501", "task:21"], 500, ["500", "501"]),
    (["cred:501"], 501, None),                             # one reference still pins that row
    ([], 500, None),                                       # no reference keeps the existing selection
], ids=["pair", "reversed", "duplicated", "at-spelling", "with-task", "one", "none"])
def test_several_key_rows_of_one_proved_krbtgt_bind_its_best_key(monkeypatch, tmp_path, references, credential_id,
                                                                credential_ids):
    mt, split, _store = _store_client(monkeypatch, tmp_path)

    run = asyncio.run(_run(mt, split, [_select("forge-golden-ticket", inputs_by_reference=references),
                                       AIMessage(content="The forge ran.")]))

    assert run.briefs == [("accepted", "Mythic_Operator")] and run.before.worker_calls == 0
    assert run.before.issued == []
    [card] = run.cards
    inputs = card["data"]["arguments"]["inputs"]
    assert str(inputs["credential_id"]) == str(credential_id) and str(inputs["credential_proof_task_id"]) == "21"
    assert inputs.get("credential_ids") == credential_ids, inputs
    assert KRBTGT_KEY not in json.dumps(run.cards, default=str) + json.dumps(run.terminals, default=str)
    rubeus = [text for row, text in zip(run.issued, _commands(run)) if row["command_name"] == "execute_assembly"]
    key = f"/aes256:{KRBTGT_KEY}" if credential_id == 500 else "/rc4:"
    assert len(rubeus) == 1 and key in rubeus[0], rubeus
    assert len(run.terminals) == 1


@pytest.mark.parametrize("extra, references, named", [
    ([_reported(600, OTHER_AES, 24, realm="child.lab.local")], ["cred:500", "cred:600"], "credentials 500, 600"),
    ([_reported(601, OTHER_AES, 30)], ["cred:500", "cred:601"], "credentials 500, 601"),
    ([{"id": 603, "account": "krbtgt", "realm": "lab.local", "type": "key", "credential_text": TYPED,
       "comment": "entered by hand", "timestamp": NOW, "task": None}], ["cred:500", "cred:603"], "credentials 500, 603"),
    ([_reported(604, OTHER_AES, 21, account="administrator")], ["cred:500", "cred:604"], "credentials 500, 604"),
    ([_reported(601, OTHER_AES, 30), _reported(602, OTHER_RC4, 30, row_type="hash")],
     ["cred:601", "cred:602", "task:21"], "proof task 21, credentials 601, 602"),
], ids=["other-realm", "other-proof", "unproved", "not-krbtgt", "conflicting-task"])
def test_key_rows_that_are_not_one_proved_krbtgt_key_set_card_nothing(monkeypatch, tmp_path, extra, references,
                                                                     named):
    mt, split, _store = _store_client(monkeypatch, tmp_path, *extra)

    run = asyncio.run(_run(mt, split, [_select("forge-golden-ticket", inputs_by_reference=references),
                                       AIMessage(content="The forge was refused.")]))

    assert run.briefs == [("accepted", "Mythic_Operator")]   # admitted: every row exists; the key set is the question
    assert run.cards == [] and run.issued == [] and len(run.terminals) == 1
    [outcome] = run.outcomes
    assert outcome["status"] == sb.StepStatus.PRECONDITION_UNSATISFIABLE.value and outcome["tasks_issued"] == []
    assert f"({named})" in outcome["detail"] and "No forge was issued" in outcome["detail"], outcome["detail"]


def test_a_waiting_key_set_card_whose_row_disappears_issues_nothing(monkeypatch, tmp_path):
    mt, split, store = _store_client(monkeypatch, tmp_path)

    def remove_501():
        store[:] = [row for row in store if row["id"] != 501]
        mt._cred_cache_ts = None                           # the card waited past the credential cache's lifetime

    run = asyncio.run(_run(mt, split, [_select("forge-golden-ticket", inputs_by_reference=["cred:500", "cred:501"]),
                                       AIMessage(content="The forge was refused.")], before_decision=remove_501))

    [card] = run.cards
    assert card["data"]["arguments"]["inputs"]["credential_ids"] == ["500", "501"]
    assert run.issued == [] and len(run.terminals) == 1    # row 500 alone is not the approved key set
    [outcome] = run.outcomes
    assert outcome["tasks_issued"] == []
    # The executor's stop names the approved set (and the proof it was bound from) in what the Supervisor reads next.
    assert any("(proof task 21, credentials 500, 501) no longer resolves" in str(message.content)
               for messages in run.fake.inputs[run.before.inputs:] for message in messages)


# --- (e) settlement reaches the next Supervisor call with a fresh candidate view --------------------------------

@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_settlement_reaches_the_next_supervisor_call_with_a_fresh_candidate_view(monkeypatch, tmp_path, decision):
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(mt, split, [_select("gpo-controlled-system-exec", "alpha"),
                                       AIMessage(content="The procedure result was read.")], decision=decision))

    [outcome] = run.outcomes
    issued = [str(31 + index) for index in range(len(run.issued))]     # the seam numbers tasks from 31
    assert outcome["tasks_issued"] == issued and outcome["guarded_rejected"] == (decision == "reject")
    after = next(messages for messages in run.fake.inputs[run.before.inputs:]
                 if any("[step outcome]" in str(m.content) for m in messages) and "[Sage catalog candidates"
                 in "".join(str(m.content) for m in messages))
    # The outcome (executed or denied) is retained, and the view after it is rebuilt from current state.
    assert any("[step outcome]" in str(m.content) for m in after)
    current, _reason = run.model._catalog_candidates()
    assert {ref for ref, *_rest in _listed(after)} == {item.ref for item in current}


# --- (f) zero-hop state, empty and unavailable views, and the lanes ------------------------------------------------

def _stand_in(facts, footholds):
    view = SimpleNamespace(hops=[], graph_facts=[es.GraphFact(predicate=p, source="fixture", timestamp=NOW,
                                                              ttl_seconds=600) for p in facts], corrected_hops=[])
    return SimpleNamespace(_ledger_view=lambda *a: view, _engagement_footholds=footholds,
                           _engagement_objective=lambda: "")


def test_graph_facts_alone_support_a_candidate_and_an_empty_list_is_not_impossibility():
    self = SimpleNamespace(mythic_client=_stand_in(
        [f"generic-write:gpo:alpha-policy", "gpo-domain:alpha-policy:child.lab.local",
         f"gpo-guid:alpha-policy:{GPOS['alpha-policy']}"], [_foothold("14", "WS02", "child.lab.local", "C\\a")]),
        _request_contract=None)
    found, reason = model_module.Model._catalog_candidates(self)
    assert reason == "" and [item.procedure for item in found] == ["gpo-controlled-system-exec"]

    self.mythic_client = _stand_in([], [_foothold("14", "WS02", "child.lab.local", "C\\a")])
    empty, _ = model_module.Model._catalog_candidates(self)
    assert empty == () and "not a finding that nothing is possible" in cb.render(empty)
    assert "`collect-graph`" in cb.render(empty)
    self.mythic_client = None
    assert model_module.Model._catalog_candidates(self)[0] is None
    assert "UNAVAILABLE" in cb.render(None, reason="x") and "not an empty list" in cb.render(None, reason="x")


def test_an_empty_frontier_still_compiles_graph_collection():
    collection = {"allowed_capabilities": ["collect-graph"], "callback_display_id": "14", "targets": ["foreign.test"]}
    assert not model_module._needs_catalog_binding(collection)
    assert sb.bound_procedure_call(collection, ()) == {
        "action": {"capability": "collect-graph", "callback_id": "14", "domain": "foreign.test"}}
    assert sb.bound_procedure_call({**collection, "targets": ["lab.local"]}, ())["action"]["domain"] == "lab.local"
    assert not model_module._needs_catalog_binding({"allowed_capabilities": ["control_plane"]})
    assert not model_module._needs_catalog_binding({"allowed_tools": [ISSUE]})
    assert hb.BRIEF_COMPILED_PROCEDURES == sb.BRIEF_BOUND_PROCEDURES


@pytest.mark.parametrize("mode, solve, contract_lane, active", [
    ("supervised", False, "supervised_workflow", True), ("supervised", True, "supervised_workflow", False),
    ("conversation", False, "conversational", False), ("auto", False, "autonomous_objective", False)])
def test_only_ordinary_supervised_requests_bind_catalog_steps(mode, solve, contract_lane, active):
    async def build():
        return model_module.Model(
            provider="openai", model="gpt-4o-mini", system_prompt="lanes",
            config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
            task_id=0, agent_task_id="lanes", mode=mode, autonomous_solve=solve, operation_id=1, channel_id=1,
            apitoken_id=0)

    model = asyncio.run(build())
    model._request_contract = SimpleNamespace(lane=SimpleNamespace(value=contract_lane), request_id="r")
    model._turn_authority = None
    assert model._catalog_binding_active() is active
    # Only there does a catalog step's candidate own its targeting; elsewhere admission still checks its targets.
    assert model._candidate_bound("Mythic_Operator", {"allowed_capabilities": ["dcsync-krbtgt"]}) is active
    assert not model._candidate_bound("Mythic_Operator", {"allowed_tools": [ISSUE]})
    assert not model._candidate_bound("Mythic_Operator", {"allowed_capabilities": ["collect-graph"]})


def test_conversation_sees_no_candidates_and_cards_nothing(monkeypatch, tmp_path):
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(mt, split, [UNSUPPORTED], mode="conversation"))

    assert run.cards == [] and run.issued == []
    assert not any("[Sage catalog candidates" in str(m.content) for messages in run.fake.inputs for m in messages)


# --- (g) a catalog step's targets are recorded: they never refuse it or change what it runs -----------------------

# Target lists as a Supervisor wrote them, relative to the candidate's own domain {d} and another observed domain {o}.
VARIANTS = {
    "own-domain": ["{d}"], "upper-case": ["{D}"], "observed-host": ["WS02.CHILD.LAB.LOCAL"], "short-host": ["ws02"],
    "principal": ["krbtgt@{D}"], "gpo-label": ["ALPHA-POLICY GPO"], "unobserved-host": ["srv03.foreign.test"],
    "other-domain": ["{o}"], "arbitrary": ["not a name at all"], "order-duplicates": ["ws02", "{d}", "ws02"],
    "empty": [], "omitted": None}
# procedure: (candidate filter, its domain, another observed domain)
INVARIANT = {"gpo-controlled-system-exec": ("alpha-policy", "child.lab.local", "lab.local"),
             "dcsync-krbtgt": ("", "child.lab.local", "lab.local"),
             "forge-golden-ticket": ("", "lab.local", "child.lab.local")}
_BASELINES: dict = {}


def _bound(run) -> tuple:
    """The card's action and inputs and the accepted task sequence; only per-run transaction ids are dropped."""
    arguments = json.loads(json.dumps(run.cards[0]["data"]["arguments"]))
    arguments["inputs"].pop("transaction_id", None)
    return arguments, [(row["command_name"], row["parameters"], row["callback_display_id"]) for row in run.issued]


@pytest.mark.parametrize("variant", list(VARIANTS))
@pytest.mark.parametrize("procedure", list(INVARIANT))
def test_a_catalog_steps_targets_never_refuse_or_change_its_action(monkeypatch, tmp_path, procedure, variant):
    contains, domain, other = INVARIANT[procedure]

    def accepted(targets, path):
        mt, split = _isolated(monkeypatch, path)          # each client in this test gets its own ledger database
        return asyncio.run(_run(_state_for(mt, procedure), split, [_select(procedure, contains, targets=targets),
                                                                   AIMessage(content="The procedure ran.")]))

    if procedure not in _BASELINES:
        baseline = accepted([domain], tmp_path / "baseline")
        assert len(baseline.cards) == 1 and baseline.issued, baseline.briefs
        _BASELINES[procedure] = _bound(baseline)
    targets = None if VARIANTS[variant] is None else [
        text.format(d=domain, D=domain.upper(), o=other) for text in VARIANTS[variant]]

    run = accepted(targets, tmp_path / "variant")

    assert run.briefs == [("accepted", "Mythic_Operator")] and _brief_targets(run) == (targets or [])
    assert run.before.worker_calls == 0 and run.before.issued == [] and len(run.cards) == 1
    assert _bound(run) == _BASELINES[procedure]          # the same card and the same tasks, whatever was recorded


# --- (h) raw and collection steps keep their target checks (Autonomous Solve's controller never takes a brief) -----

@pytest.mark.parametrize("scope, targets, error", [
    ({"allowed_tools": [ISSUE]}, ["foreign.test", "SRV03.FOREIGN.TEST"], "targets: SRV03.FOREIGN.TEST named"),
    ({"allowed_tools": [ISSUE]}, None, "missing required field(s): targets"),
    ({"allowed_capabilities": ["collect-graph"]}, ["srv03.foreign.test"], "targets: srv03.foreign.test named"),
    ({"allowed_capabilities": ["collect-graph"]}, None, "missing required field(s): targets"),
], ids=["raw-unknown", "raw-missing", "collection-unknown", "collection-missing"])
def test_raw_and_collection_steps_still_check_targets(monkeypatch, tmp_path, scope, targets, error):
    mt, split = _client(monkeypatch, tmp_path)
    brief = {"handoff_title": "a step", "handoff_instruction": PROMPT, "callback_display_id": 14, "targets": targets,
             "expected_result": "The step's result.", **scope}

    run = asyncio.run(_run(mt, split, [_call("transfer_to_Mythic_Operator", brief, 1)], decision=None))

    assert run.cards == [] and run.issued == [] and run.before.worker_calls == 0
    assert run.briefs and run.briefs[0][0] == "rejected" and error in run.briefs[0][1], run.briefs


# --- (i) a brief that also lists the procedure its candidate feeds binds that candidate alone --------------------

ENSURE, DCSYNC = "ensure-kerberos-context", "dcsync-krbtgt"
_RECORDED = {row["id"]: row for row in json.loads(
    (Path(__file__).parent / "fixtures/model_input_boundaries/catalog_brief_targets.json").read_text())["rows"]}
# The recorded shapes (renamed; run 4 TOOL span 422, run 2 TOOL span 494): the prerequisite's candidate selected,
# the procedure it feeds listed with it. The view listed only the prerequisite. Plus the reversed order.
LISTED = {**{name: (_RECORDED[name]["allowed_capabilities"], _RECORDED[name]["targets"])
             for name in ("run4-422", "run2-494")}, "reversed": ([DCSYNC, ENSURE], ["child.lab.local"])}


def _delegated_brief(run) -> dict:
    """The brief Sage stored on the step's delegated message, which every later check of the step reads."""
    [message] = [m for m in run.values.get("messages", []) if isinstance(m, HumanMessage)
                 and m.additional_kwargs.get("_delegated_to") == "Mythic_Operator"]
    return message.additional_kwargs["_handoff_brief"]


def _outcome_text(run) -> str:
    """The `[step outcome]` the Supervisor read in its first model call after the operator's decision."""
    return next(str(m.content) for messages in run.fake.inputs[run.before.inputs:] for m in messages
                if str(m.content).startswith("[step outcome]"))


def _acknowledgment(run) -> str:
    """The delegation call's acknowledgment, as the graph records it (main and worker channels)."""
    [message] = [m for m in run.values.get("messages", []) if isinstance(m, ToolMessage)
                 and m.name == "transfer_to_Mythic_Operator"]
    return str(message.content)


@pytest.mark.parametrize("shape", list(LISTED))
def test_a_brief_listing_its_candidates_procedure_with_another_cards_only_that_candidate(monkeypatch, tmp_path,
                                                                                        shape):
    listed, targets = LISTED[shape]
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(_state_for(mt, ENSURE), split, [_select(ENSURE, allowed_capabilities=listed,
                                                                   targets=targets)], decision=None))

    assert run.issued == [] and run.before.worker_calls == 0 and run.terminals == []
    [card] = run.cards
    assert card["data"]["arguments"]["action"]["name"] == ENSURE
    assert card["data"]["arguments"]["inputs"]["callback_id"] == "14"
    # Admission narrowed the step: the stored brief names only the candidate's procedure, and the brief event keeps
    # the Supervisor's own list beside the admitted one.
    assert run.briefs == [("accepted", "Mythic_Operator")] and _brief_targets(run) == targets
    [event] = run.model._request_event_ledger.actual_events(kind="brief")
    assert json.loads(dict(event.metadata)["brief"])["allowed_capabilities"] == listed
    assert json.loads(dict(event.metadata)["admitted_capabilities"]) == [ENSURE]
    assert _delegated_brief(run)["allowed_capabilities"] == [ENSURE]
    # The scope the worker invocation renders from that stored brief names the bound procedure only.
    scope = hb.render_scope(_delegated_brief(run), candidate_bound=True)
    assert f"`{ENSURE}`" in scope and DCSYNC not in scope
    acknowledgment = _acknowledgment(run)
    assert f"bound this step to `{ENSURE}`" in acknowledgment and f"does not run `{DCSYNC}`" in acknowledgment


@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("shape", list(LISTED))
def test_the_narrowed_step_runs_what_the_one_procedure_step_runs(monkeypatch, tmp_path, shape, decision):
    listed, targets = LISTED[shape]

    def run_with(capabilities, path):
        mt, split = _isolated(monkeypatch, path)
        return asyncio.run(_run(_state_for(mt, ENSURE), split, [
            _select(ENSURE, allowed_capabilities=capabilities, targets=targets),
            AIMessage(content="The procedure result was read.")], decision=decision))

    control, run = run_with([ENSURE], tmp_path / "control"), run_with(listed, tmp_path / "listed")

    assert len(control.cards) == len(run.cards) == 1 and run.before.issued == []
    assert _bound(run) == _bound(control)                 # the same card, and on Accept the same task sequence
    assert bool(run.issued) is (decision == "accept") and len(run.terminals) == 1
    assert _delegated_brief(run) == _delegated_brief(control)        # one canonical brief for every later check
    assert "bound this step to" not in _acknowledgment(control)       # a one-procedure brief passes through
    # What the Supervisor reads when the step returns names the bound procedure and what the step did not run.
    outcome = _outcome_text(run)
    assert f"Sage bound this step to `{ENSURE}`" in outcome, outcome
    assert f"also listed `{DCSYNC}`, which this step did not run" in outcome
    assert "bound this step to" not in _outcome_text(control)


REJECTS = {
    # name: (allowed_capabilities, other brief fields, refusal text, whether the binder ends the request)
    "goal-only": ([DCSYNC], {}, f"runs `{ENSURE}`, not `{DCSYNC}`", True),
    "two-others": ([DCSYNC, "forge-golden-ticket"], {}, f"runs `{ENSURE}`, not `{DCSYNC}`, `forge-golden-ticket`",
                   True),
    "collection": ([ENSURE, "collect-graph"], {}, "candidate_ref: only a catalog procedure step other than graph",
                   False),
    "control-plane": ([ENSURE, "control_plane"], {}, "`control_plane` cannot be combined with other classes", False),
    "no-ref": ([ENSURE, DCSYNC], {"candidate_ref": ""}, "names no candidate_ref", True),
    "unknown-ref": ([ENSURE, DCSYNC], {"candidate_ref": "cand-0000000000000000"}, "not supported by current evidence",
                    True),
    "callback": ([ENSURE, DCSYNC], {"callback_display_id": 13}, "runs on callback 14, not callback 13", True),
    "reference": ([ENSURE, DCSYNC], {"inputs_by_reference": ["task:21", "task:40"]},   # a second, other proof task
                  "does not use the input reference task:40", True),
}


@pytest.mark.parametrize("case", list(REJECTS))
def test_a_listed_brief_that_cannot_bind_its_candidate_stays_refused(monkeypatch, tmp_path, case):
    listed, fields, refusal, ends = REJECTS[case]
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(_state_for(mt, ENSURE), split, [_select(ENSURE, allowed_capabilities=listed,
                                                                   targets=["child.lab.local"], **fields)]))

    assert run.cards == [] and run.issued == [] and run.outcomes == []
    assert run.briefs[0][0] == "rejected" and refusal in run.briefs[0][1], run.briefs
    [terminal] = run.terminals
    content = str(terminal.get("content") or "")
    assert ("Blocked" in content and "could not bind this catalog step" in content) is ends, content


def test_unknown_names_in_a_listed_brief_are_refused_before_admission(monkeypatch, tmp_path):
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(_state_for(mt, ENSURE), split, [_select(ENSURE, allowed_capabilities=[ENSURE, "dcsync"],
                                                                   targets=["child.lab.local"])]))

    assert run.cards == [] and run.issued == [] and run.outcomes == []
    assert ("accepted", "Mythic_Operator") not in run.briefs


def test_after_the_bound_prerequisite_settles_the_next_view_offers_the_goal_procedure(monkeypatch, tmp_path):
    listed, targets = LISTED["run4-422"]
    mt, split = _client(monkeypatch, tmp_path)
    _state_for(_state_for(mt, ENSURE), DCSYNC)
    views, record = [], mt._record_step_ledger

    async def settle(references, task_ids):
        # SYNTHETIC: the fixture's inert task output proves no Kerberos context, so the effect the accepted procedure
        # exists to prove is seeded at its settlement, after its tasks were issued and before the next view.
        result = await record(references, task_ids)
        seed_ledger(mt, hops=[_proof_hop("kerberos-context:child.lab.local@callback:14", 41, callback_id="14")])
        return result

    monkeypatch.setattr(mt, "_record_step_ledger", settle)
    goal = _select(DCSYNC, index=2)

    def next_step(messages):
        views.append([line[1] for line in _listed(messages)])
        return goal(messages)                             # a scripted selection: wiring, not the model's choice

    run = asyncio.run(_run(mt, split, [_select(ENSURE, allowed_capabilities=listed, targets=targets), next_step]))

    assert DCSYNC not in [line[1] for line in _listed(run.fake.inputs[0])]
    assert run.issued and views and DCSYNC in views[0]
    # Every card emitted in order: the accepted prerequisite, then the goal procedure on the same callback.
    assert [card["data"]["arguments"]["action"]["name"] for card in run.all_cards] == [ENSURE, DCSYNC]
    assert run.all_cards[-1]["data"]["arguments"]["inputs"]["callback_id"] == "14"


# --- (j) a held reference the selected candidate does not use is dropped from the step and reported ------------

ACCOUNT = "dcsync-account"
# The recorded shapes (renamed; run 5 TOOL spans 1164 and 1186): an offered dcsync-account candidate selected with
# task 21, a held task (the fixture's krbtgt proof) that this candidate does not use.
RECORDED_UNUSED = {name: (_RECORDED[name]["targets"], _RECORDED[name]["recorded_references"])
                   for name in ("run5-1164", "run5-1186")}
_ONE = RECORDED_UNUSED["run5-1164"][0]
# name: (procedure, candidate filter, brief overrides, the references Sage drops and reports, as normalized)
UNUSED = {
    **{name: (ACCOUNT, "", {"targets": targets, "inputs_by_reference": references}, references)
       for name, (targets, references) in RECORDED_UNUSED.items()},
    "several": (ACCOUNT, "", {"targets": _ONE, "inputs_by_reference": ["task:21", "cred:500"]}, ["task:21", "cred:500"]),
    "duplicates": (ACCOUNT, "", {"targets": _ONE, "inputs_by_reference": ["task:21", "task:21"]}, ["task:21"]),
    "case-space": (ACCOUNT, "", {"targets": _ONE, "inputs_by_reference": [" TASK:21 ", "@CRED:500"]},
                   ["task:21", "@cred:500"]),
    "with-procedure": (ACCOUNT, "", {"targets": _ONE, "inputs_by_reference": ["task:21"],
                                     "allowed_capabilities": [ACCOUNT, DCSYNC]}, ["task:21"]),
    "gpo": ("gpo-controlled-system-exec", "alpha", {"inputs_by_reference": ["task:21"]}, ["task:21"]),
}
_CONTROLS: dict = {}


def _unused_run(monkeypatch, path, procedure, contains, overrides, decision):
    mt, split, _store = _isolated(monkeypatch, path, _store_client)     # credential rows 500/501 are held
    return asyncio.run(_run(_state_for(mt, procedure), split, [
        _select(procedure, contains, **overrides), AIMessage(content="The procedure result was read.")],
        decision=decision))


@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("shape", list(UNUSED))
def test_a_held_reference_its_candidate_does_not_use_is_dropped_and_reported(monkeypatch, tmp_path, shape, decision):
    procedure, contains, overrides, dropped = UNUSED[shape]
    plain = {key: value for key, value in overrides.items() if key == "targets"}
    key = (procedure, contains, json.dumps(plain), decision)
    if key not in _CONTROLS:                              # the same step without references, one procedure listed
        control = _unused_run(monkeypatch, tmp_path / "control", procedure, contains, plain, decision)
        assert len(control.cards) == 1 and control.before.issued == [], control.briefs
        _CONTROLS[key] = (_bound(control), _delegated_brief(control), _outcome_text(control))
    bound, canonical, control_outcome = _CONTROLS[key]

    run = _unused_run(monkeypatch, tmp_path / "run", procedure, contains, overrides, decision)

    # The control's card, nothing before approval, its task sequence on Accept and none on Reject.
    assert run.before.issued == [] and run.before.worker_calls == 0 and len(run.cards) == 1, run.briefs
    assert _bound(run) == bound and bool(run.issued) is (decision == "accept") and len(run.terminals) == 1
    # One canonical brief without the unused references; the brief event keeps the Supervisor's own beside it.
    assert _delegated_brief(run) == canonical and canonical["inputs_by_reference"] == []
    [event] = run.model._request_event_ledger.actual_events(kind="brief")
    metadata = dict(event.metadata)
    supplied = hb.HandoffBrief.from_args(inputs_by_reference=overrides["inputs_by_reference"]).inputs_by_reference
    assert json.loads(metadata["brief"])["inputs_by_reference"] == list(supplied)
    assert json.loads(metadata["admitted_references"]) == []
    # The Supervisor's next input names the procedure and every dropped reference; the control's adds no notice.
    outcome = _outcome_text(run)
    named = ", ".join(f"`{item}`" for item in dropped)
    assert f"Sage dropped the input reference(s) {named} from this step: `{procedure}`" in outcome, outcome
    assert "dropped the input reference" not in control_outcome
    assert (f"also listed `{DCSYNC}`, which this step did not run" in outcome) is (shape == "with-procedure")


@pytest.mark.parametrize("references, refusal", [
    (["task:99999"], "does not resolve to a record Sage holds"),
    (["task:21x"], "is not `task:<id>` or `cred:<id>`"),
    (["task:21", "cred:99999"], "does not resolve to a record Sage holds"),
], ids=["unknown", "malformed", "unused-with-unknown"])
def test_an_unheld_or_malformed_reference_still_cards_nothing(monkeypatch, tmp_path, references, refusal):
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(_state_for(mt, ACCOUNT), split, [
        _select(ACCOUNT, inputs_by_reference=references, targets=_ONE)], decision=None))

    assert run.cards == [] and run.issued == [] and run.before.worker_calls == 0
    assert run.briefs[0][0] == "rejected" and refusal in run.briefs[0][1], run.briefs


def test_a_card_admitted_without_an_unused_reference_runs_only_while_its_candidate_is_current(monkeypatch, tmp_path):
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(_state_for(mt, ACCOUNT), split, [
        _select(ACCOUNT, inputs_by_reference=["task:21"], targets=_ONE),
        AIMessage(content="The procedure result was read.")], before_decision=_withdraw(mt)))

    assert len(run.cards) == 1 and run.before.issued == [] and run.issued == []
    [outcome] = run.outcomes
    assert outcome["status"] == sb.StepStatus.PRECONDITION_UNSATISFIABLE.value
    assert "no longer matches current evidence" in outcome["detail"]
    assert f"Sage dropped the input reference(s) `task:21` from this step: `{ACCOUNT}`" in _outcome_text(run)


def test_a_step_that_binds_no_candidate_keeps_its_references(monkeypatch, tmp_path):
    mt, split = _client(monkeypatch, tmp_path)
    brief = {"handoff_title": "a raw step", "handoff_instruction": PROMPT, "callback_display_id": 14,
             "targets": ["child.lab.local"], "expected_result": "The step's result.", "allowed_tools": [ISSUE],
             "inputs_by_reference": ["task:21"]}

    run = asyncio.run(_run(mt, split, [_call("transfer_to_Mythic_Operator", brief, 1)], decision=None))

    assert run.briefs == [("accepted", "Mythic_Operator")]
    assert _delegated_brief(run)["inputs_by_reference"] == ["task:21"]


# --- the pure binding --------------------------------------------------------------------------------------------

ACTION = {"name": "gpo-controlled-system-exec", "target": "gpo=alpha-policy;domain=child.lab.local",
          "effects": ["system-exec:gpo:alpha-policy@child.lab.local"],
          "intent": {"gpo": "alpha-policy", "domain": "child.lab.local", "steps": ["a"]}}
INPUTS = {"callback_id": "14", "controlled_principal": "CHILD\\analyst"}


def test_a_ref_is_scoped_to_its_request_and_its_exact_bound_arguments():
    [item] = cb.candidates("request-1", [(ACTION, INPUTS)])
    assert item.ref == cb.candidate_ref("request-1", ACTION, INPUTS)
    assert item.ref != cb.candidate_ref("request-2", ACTION, INPUTS)
    assert item.ref != cb.candidate_ref("request-1", ACTION, {**INPUTS, "controlled_principal": "CHILD\\other"})
    assert item.ref != cb.candidate_ref("request-1", {**ACTION, "intent": {**ACTION["intent"], "gpo": "b"}}, INPUTS)


def test_an_approved_call_must_be_the_candidates_action():
    [item] = cb.candidates("r", [(ACTION, INPUTS)])
    grounded = {"action": json.loads(json.dumps(ACTION)), "inputs": {**INPUTS, "transaction_id": "capability-exec:1"}}
    assert cb.call_matches(grounded, item) == ""                         # Sage's transaction id is added after
    trimmed = json.loads(json.dumps(grounded))
    trimmed["action"]["intent"].pop("steps")
    assert cb.call_matches(trimmed, item) == ""                          # card secrets and paths are only removed
    changed = json.loads(json.dumps(grounded))
    changed["action"]["intent"]["gpo"] = "beta-policy"
    assert "differs" in cb.call_matches(changed, item)
    assert "callback" in cb.call_matches({**grounded, "inputs": {**grounded["inputs"], "callback_id": "13"}}, item)
    assert "controlled_principal" in cb.call_matches(
        {**grounded, "inputs": {**grounded["inputs"], "controlled_principal": "x"}}, item)


GPO_ACTION = {**ACTION, "intent": {**ACTION["intent"], "capability": ACTION["name"], "affected_hosts": ["ws02"],
                                   "affected_dc_hosts": [], "preferred_effect": "system-exec-proof",
                                   "steps": ["trigger or wait for Group Policy refresh"]},
              "source_facts": ["generic-write:gpo:alpha-policy", "gpo-domain:alpha-policy:child.lab.local",
                               "gpo-affects-computer:alpha-policy:ws02:child.lab.local"]}


@pytest.mark.parametrize("targets", [
    ["WS02.CHILD.LAB.LOCAL"], ["ws02"], ["gpo=alpha-policy;domain=child.lab.local"], ["ws02.foreign.test"],
    ["ws02.lab.local"], ["dc02.child.lab.local"], ["trigger or wait for Group Policy refresh"], ["14"],
    ["sam@north.test", "NORTH.TEST\\jon"], ["ws02", "child.lab.local", "ws02"], []])
def test_a_brief_binds_its_candidate_whatever_targets_it_records(targets):
    [item] = cb.candidates("r", [(GPO_ACTION, INPUTS)])
    brief = hb.HandoffBrief.from_args(allowed_capabilities=[item.procedure], callback_display_id=14,
                                      candidate_ref=item.ref, targets=targets)
    assert cb.bind(brief, (item,)) == (item, "")


def test_a_supplied_reference_pins_a_forge_key_and_is_never_ignored():
    forge = {"name": "forge-golden-ticket", "target": "domain=lab.local", "intent": {"domain": "lab.local"}}
    [item] = cb.candidates("r", [(forge, {"callback_id": "13"})])

    def brief(*references):
        return hb.HandoffBrief.from_args(allowed_capabilities=[item.procedure], callback_display_id=13,
                                         candidate_ref=item.ref, inputs_by_reference=list(references))

    bound, error = cb.bind(brief("cred:501", "task:21"), (item,))
    assert error == "" and bound.ref == item.ref
    assert bound.inputs == {"callback_id": "13", "credential_id": "501", "credential_proof_task_id": "21"}
    assert cb.call_matches({"action": forge, "inputs": {**bound.inputs, "credential_id": 501}}, bound) == ""
    assert "credential_id" in cb.call_matches({"action": forge, "inputs": {**bound.inputs, "credential_id": 500}}, bound)
    # Several rows of one key set pin it as a set, canonically; the executor resolves it to one proof.
    for references in (("cred:501", "cred:500"), ("@cred:500", "cred:501", "cred:500")):
        bound, error = cb.bind(brief(*references), (item,))
        assert error == "" and bound.inputs == {"callback_id": "13", "credential_ids": ["500", "501"]}
    grounded = {**bound.inputs, "credential_id": 500, "credential_proof_task_id": 21}
    assert cb.call_matches({"action": forge, "inputs": grounded}, bound) == ""
    assert "credential_ids" in cb.call_matches({"action": forge, "inputs": {**grounded, "credential_ids": ["500"]}},
                                               bound)
    assert cb.bind(brief("cred:500", "cred:501", "task:21"), (item,))[0].inputs["credential_proof_task_id"] == "21"
    assert "does not use the input reference task:30" in cb.bind(brief("task:21", "task:30"), (item,))[1]
    assert cb.unused_references(brief("cred:501", "task:21"), item) == ()      # a forge uses every held reference
    assert cb.REFERENCE_PROCEDURES == MythicTools._KRBTGT_FORGE_CAPABILITIES


def test_a_brief_binds_its_candidate_when_it_also_lists_other_procedures():
    [item] = cb.candidates("r", [(GPO_ACTION, INPUTS)])

    def brief(*procedures):
        return hb.HandoffBrief.from_args(allowed_capabilities=list(procedures), callback_display_id=14,
                                         candidate_ref=item.ref)

    for procedures in ([item.procedure, "dcsync-krbtgt"], ["dcsync-krbtgt", item.procedure]):
        assert cb.bind(brief(*procedures), (item,)) == (item, "")
    assert cb.bind(brief("dcsync-krbtgt", "forge-golden-ticket"), (item,)) == (
        None, f"candidate_ref {item.ref} runs `{item.procedure}`, not `dcsync-krbtgt`, `forge-golden-ticket`")


def test_a_reference_its_candidate_does_not_use_is_reported_not_refused():
    [item] = cb.candidates("r", [(GPO_ACTION, INPUTS)])

    def brief(candidate, *references):
        return hb.HandoffBrief.from_args(allowed_capabilities=[candidate.procedure], callback_display_id=14,
                                         candidate_ref=candidate.ref, inputs_by_reference=list(references))

    original = brief(item, "task:21", "cred:7", "@cred:7", "task:21")
    assert cb.bind(original, (item,)) == (item, "")
    assert cb.unused_references(original, item) == ("task:21", "cred:7", "@cred:7")
    assert cb.bind(brief(item), (item,)) == cb.bind(original, (item,))   # rebinding the admitted brief: no change
    # A reference the candidate's own action or inputs name is used, and stays in the step.
    [used] = cb.candidates("r", [(GPO_ACTION, {**INPUTS, "source_task_id": "21"})])
    kept = brief(used, "task:21", "cred:7")
    assert cb.bind(kept, (used,)) == (used, "") and cb.unused_references(kept, used) == ("cred:7",)
