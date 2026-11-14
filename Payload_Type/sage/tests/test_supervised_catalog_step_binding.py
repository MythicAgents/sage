"""An ordinary supervised catalog step runs one action that current evidence supports, bound by code.

The Supervisor sees the catalog actions `actions_from_state` derives from one fresh ledger snapshot, each with a
`candidate_ref`. A non-collection catalog step names one; Sage binds it at brief admission, proposes exactly its
action without a worker-model call, and checks it again before execution. An unsupported selection ends with a
factual blocker before any card or task.

Everything on the path is real except the models' choices and Mythic's network I/O: the graph, the ledger store with
a krbtgt key proved through the production dcsync executor, the producer, the brief check, the binding, the step
bound, grounding, the native approval card and its resolution, MythicTools' executor and its final task issue (the
inert `_split_issue` seam). Scripted selections test code enforcement, not model planning.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from ai.langgraph import catalog_binding as cb
from ai.langgraph import engagement_state as es
from ai.langgraph import handoff_brief as hb
from ai.langgraph import model as model_module
from ai.langgraph import step_bounds as sb
from ai.langgraph.mythic_tools import MythicTools
from sage_chat.headless import build_chat_request
from sage_chat.service import bind_channel_thread_id
from sage_chat.session import drop_channel_session
from tests.esl_fixtures import ingest_proof, seed_ledger
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
            terminals=[e for e in chat.emissions if e.get("complete_request") and not (
                isinstance(e.get("metadata"), dict) and e["metadata"].get("special_type") == "input_requested")],
            outcomes=_outcomes(snapshot.values.get("supervisor_messages", [])),
            briefs=[(e.phase, e.content) for e in model._request_event_ledger.actual_events(kind="brief")])
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


def _commands(run) -> list[str]:
    return [json.dumps(row["parameters"], sort_keys=True) for row in run.issued]


# --- (a) and (c) at admission: an unsupported or conflicting selection ends blocked before any card ---------------

RAW_FALLBACK = [_call("transfer_to_Mythic_Operator", {
    "handoff_title": "raw fallback", "handoff_instruction": PROMPT, "callback_display_id": 13,
    "targets": ["foreign.test"], "expected_result": "A replication result.", "allowed_tools": [ISSUE]}, 7),
    _call(ISSUE, {"command": "dcsync", "parameters": "-Domain foreign.test", "callback_display_id": 13}, 8)]
UNSUPPORTED = _call("transfer_to_Mythic_Operator", {
    "handoff_title": "replicate foreign.test", "handoff_instruction": PROMPT, "allowed_capabilities": ["dcsync-krbtgt"],
    "callback_display_id": 13, "targets": ["foreign.test"], "expected_result": "The foreign krbtgt key."}, 1)


TOOL_SCOPED = [_call("transfer_to_Mythic_Operator", {
    "handoff_title": "run a procedure", "handoff_instruction": PROMPT, "allowed_tools": ["execute_capability"],
    "callback_display_id": 14, "targets": ["lab.local"], "expected_result": "The procedure's result."}, 1),
    _call("execute_capability", {"action": {"capability": "forge-golden-ticket", "domain": "lab.local",
                                            "callback_id": "14"}}, 2)]


def _unavailable(mt):
    def fail(*_args, **_kwargs):
        raise RuntimeError("ledger offline")
    return lambda: setattr(mt, "_ledger_view", fail)


@pytest.mark.parametrize("case", ["unsupported", "fabricated", "callback", "target", "reference", "procedure",
                                  "unavailable", "tool-scoped"])
def test_an_unbindable_catalog_step_ends_blocked_with_no_card_or_task(monkeypatch, tmp_path, case):
    mt, split = _client(monkeypatch, tmp_path)
    choice = {
        "unsupported": UNSUPPORTED,
        "fabricated": _select("forge-golden-ticket", candidate_ref="cand-0000000000000000"),
        "callback": _select("forge-golden-ticket", callback_display_id=14),
        "target": _select("forge-golden-ticket", targets=["child.lab.local"]),
        "reference": _select("gpo-controlled-system-exec", "alpha", inputs_by_reference=["task:21"]),
        "procedure": _select("forge-golden-ticket", allowed_capabilities=["dcsync-krbtgt"]),
        "unavailable": _select("forge-golden-ticket", before=_unavailable(mt)),
        "tool-scoped": TOOL_SCOPED,       # execute_capability named as a tool is a catalog step, not a bypass
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
            "callback": "not callback 14", "target": "does not act on child.lab.local",
            "reference": "does not use the input reference task:21", "procedure": "not `dcsync-krbtgt`",
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

@pytest.mark.parametrize("decision", ["accept", "reject", "respond"])
def test_the_decision_on_a_bound_forge_card_is_final(monkeypatch, tmp_path, decision):
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(mt, split, [_select("forge-golden-ticket"), AIMessage(content="The forge ran.")],
                           decision=decision))

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


def test_conversation_sees_no_candidates_and_cards_nothing(monkeypatch, tmp_path):
    mt, split = _client(monkeypatch, tmp_path)

    run = asyncio.run(_run(mt, split, [UNSUPPORTED], mode="conversation"))

    assert run.cards == [] and run.issued == []
    assert not any("[Sage catalog candidates" in str(m.content) for messages in run.fake.inputs for m in messages)


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


def test_a_brief_binds_targets_the_candidate_names_in_any_account_form():
    account = {**ACTION, "name": "ensure-account-kerberos-context", "target": "account=jon;domain=north.test",
               "intent": {"account": "jon", "domain": "north.test"}}
    [item] = cb.candidates("r", [(account, INPUTS)])
    brief = hb.HandoffBrief.from_args(allowed_capabilities=[item.procedure], callback_display_id=14,
                                      candidate_ref=item.ref, targets=["jon@north.test", "NORTH.TEST\\jon"])
    assert cb.bind(brief, (item,)) == (item, "")
    stray = hb.HandoffBrief.from_args(allowed_capabilities=[item.procedure], callback_display_id=14,
                                      candidate_ref=item.ref, targets=["sam@north.test"])
    assert "does not act on sam@north.test" in cb.bind(stray, (item,))[1]
    assert hb.validate_brief(hb.HandoffBrief.from_args(allowed_capabilities=["collect-graph"], candidate_ref="x"),
                             worker="Mythic_Operator", require_scope=False, scope=hb.KnownScope())


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
    assert "does not use the input reference cred:500" in cb.bind(brief("cred:501", "cred:500"), (item,))[1]
    assert cb.REFERENCE_PROCEDURES == MythicTools._KRBTGT_FORGE_CAPABILITIES
