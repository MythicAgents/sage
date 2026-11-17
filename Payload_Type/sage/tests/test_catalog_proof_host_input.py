"""An operator-named proof host reaches a bound supervised catalog step's card and tasks; nothing else does.

In ordinary supervised chat a `forge-golden-ticket` or `ensure-kerberos-context` step bound to a current candidate may
carry a `proof_host` from the Supervisor's brief. Sage keeps it only when an operator input of this chat names it and it
lies in the domain the procedure's service proof must prove; it then pins it on the bound action, so the card shows it
and the approved call runs its service proof there. Any other host is dropped: it never refuses the step, never reaches
the card or a task, and the step outcome names it with the reason.

Everything on the path is real except the models' choices and Mythic's network I/O (the inert `_split_issue` seam): the
graph, the ledger with a krbtgt key proved through the production dcsync executor, the candidate producer, the brief
check, the binder, the native approval card and its resolution, and MythicTools' executor. BloodHound knows no domain
controller. Briefs are scripted (no recording of a Supervisor filling `proof_host` exists, so they prove wiring, not
model choice); the near-match shapes come from the renamed recorded rows run2-758 and run5-1186. All state is synthetic.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ai.langgraph import catalog_binding as cb
from ai.langgraph import handoff_brief as hb
from tests import test_supervised_catalog_step_binding as binding
from tests.test_hitl_steer_current_intent import _call
from tests.test_supervised_catalog_step_binding import (
    ENSURE, ISSUE, _RECORDED, _client, _commands, _forge_variant, _isolated, _outcome_text, _run, _select, _state_for,
)

FORGE = "forge-golden-ticket"
HOST = "dc01.lab.local"
# What the executor issues once the forge's proof target resolves (the readiness control R3).
SEQUENCE = ["shell", "shell", "execute_assembly", "make_token", "ticket_store_add", "ticket_store_list", "shell"]


def _no_dc(mt):
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="")
    return mt


def _operator_says(monkeypatch, text: str) -> None:
    """The operator's request (the native chat input and the scripted brief's instruction) gains `text`."""
    if text:
        monkeypatch.setattr(binding, "PROMPT", binding.PROMPT + " " + text)


def _brief_event(run) -> dict:
    [event] = run.model._request_event_ledger.actual_events(kind="brief")
    return dict(event.metadata)


def _stored(run) -> dict:
    """The brief Sage stored on the step's delegated message; {} when no step was delegated."""
    found = [m.additional_kwargs.get("_handoff_brief") or {} for m in run.values.get("messages", [])
             if isinstance(m, HumanMessage) and m.additional_kwargs.get("_delegated_to") == "Mythic_Operator"]
    return found[-1] if found else {}


def _arguments(run) -> list[dict]:
    """The cards' arguments without the executor's per-run transaction id."""
    def drop(value):
        return {key: drop(item) for key, item in value.items() if key != "transaction_id"} \
            if isinstance(value, dict) else value
    return [drop(card["data"]["arguments"]) for card in run.cards]


def _names(run) -> list[str]:
    return [row["command_name"] for row in run.issued]


def _forge(monkeypatch, tmp_path, said: str, decision="accept", **brief):
    _operator_says(monkeypatch, said)
    mt, split = _client(monkeypatch, tmp_path)
    return asyncio.run(_run(_no_dc(mt), split, [_select(FORGE, **brief), AIMessage(content="Read.")],
                            decision=decision))


# --- (a) an operator-named host in any accepted form is carded and proved on ---------------------------------------

ACCEPTED = {
    "same": (f"Prove it on {HOST}.", HOST),
    "label-in-brief": ("Prove it on DC01.LAB.LOCAL.", "dc01"),
    "label-said": ("Prove it on dc01 please", "DC01.lab.local."),
    "unc-said": ("Prove it against \\\\dc01.lab.local\\C$ now", HOST),
    "unc-in-brief": (f"Prove it on {HOST}.", "\\\\DC01\\ADMIN$"),
}


@pytest.mark.parametrize("shape", list(ACCEPTED))
def test_an_operator_named_host_reaches_the_card_and_the_forges_service_proof(monkeypatch, tmp_path, shape):
    said, sent = ACCEPTED[shape]

    run = _forge(monkeypatch, tmp_path, said, proof_host=sent)

    assert run.before.issued == [] and run.before.worker_calls == 0
    [card] = run.cards
    inputs = card["data"]["arguments"]["inputs"]
    assert (inputs["proof_host"], inputs["proof_resource_source"]) == (HOST, "operator-named"), inputs
    assert _stored(run)["proof_host"] == HOST
    admitted = json.loads(_brief_event(run)["proof_host"])
    assert admitted["host"] == HOST and admitted["request_id"].startswith("chat:1:request:") and "text" not in admitted
    assert _names(run) == SEQUENCE, _names(run)
    assert [text for text in _commands(run) if HOST in text and "C$" in text], _commands(run)
    assert "unresolved_inputs" not in json.dumps(run.emissions, default=str)


@pytest.mark.parametrize("decision", ["reject", "respond"])
def test_a_carded_host_that_the_operator_does_not_accept_issues_nothing(monkeypatch, tmp_path, decision):
    run = _forge(monkeypatch, tmp_path, f"Prove it on {HOST}.", decision=decision, proof_host=HOST)

    [card] = run.cards
    assert card["data"]["arguments"]["inputs"]["proof_host"] == HOST
    assert run.issued == []


def test_a_parent_kerberos_context_carries_the_operator_named_host(monkeypatch, tmp_path):
    _operator_says(monkeypatch, f"Prove it on {HOST}.")
    mt, split = _forge_variant(monkeypatch, tmp_path)    # callback 14 lives in child.lab.local; the context is lab.local

    run = asyncio.run(_run(_no_dc(mt), split, [_select(ENSURE, "domain=lab.local", targets=["lab.local"],
                                                       proof_host="DC01"), AIMessage(content="Read.")]))

    [card] = run.cards
    assert card["data"]["arguments"]["action"]["name"] == ENSURE
    assert card["data"]["arguments"]["inputs"]["proof_host"] == HOST
    assert "execute_assembly" in _names(run) and [text for text in _commands(run) if HOST in text], _names(run)


# --- (b) and (c) no host, or a host Sage may not use: the same card and refusal as without it ---------------------

NEAR = {
    "unnamed-run2-758": ("", _RECORDED["run2-758"]["targets"][1], "no operator input of this chat names it"),
    "child-domain": ("Prove it on dc02.child.lab.local.", "dc02.child.lab.local", "not a host in lab.local"),
    "label-prefix": ("Prove it on dc012.lab.local.", HOST, "no operator input of this chat names it"),
    "label-inside-fqdn": ("Prove it on dc01.child.lab.local.", "dc01", "no operator input of this chat names it"),
    "bare-domain": ("Prove it on lab.local.", "lab.local", "a domain, not a host"),
    "domain-label": ("Prove it on lab.", "lab", "a domain, not a host"),
    "ip-literal": ("Prove it on 192.0.2.10.", "192.0.2.10", "not a host name"),
}


@pytest.mark.parametrize("shape", ["none", *NEAR])
def test_a_missing_or_unusable_host_leaves_todays_card_and_refusal(monkeypatch, tmp_path, shape):
    said, sent, reason = NEAR.get(shape, ("", "", ""))
    brief = {"targets": _RECORDED["run2-758"]["targets"]} if shape.startswith("unnamed") else {}

    run = _forge(monkeypatch, tmp_path, said, **brief, **({"proof_host": sent} if sent else {}))

    assert [phase for phase, _ in run.briefs] == ["accepted"], run.briefs
    [card] = run.cards
    assert not {"proof_host", "proof_resource_source"} & set(card["data"]["arguments"]["inputs"])
    assert "proof_host" not in _stored(run)
    assert run.issued == []                                   # today's refusal: the service proof has no target
    emitted = json.dumps(run.emissions, default=str)
    assert "kerberos_service_resource" in emitted
    outcome = _outcome_text(run)
    if sent:
        assert f"Sage dropped the proof host `{sent}`" in outcome and reason in outcome, outcome
        dropped = json.loads(_brief_event(run)["proof_host_dropped"])
        assert dropped["host"] == sent and reason in dropped["reason"], dropped
    else:
        assert "proof host" not in outcome and "proof_host_dropped" not in _brief_event(run)


def test_a_child_domain_host_never_reaches_a_parent_kerberos_context(monkeypatch, tmp_path):
    _operator_says(monkeypatch, "Prove it on dc02.child.lab.local.")
    mt, split = _forge_variant(monkeypatch, tmp_path)

    run = asyncio.run(_run(_no_dc(mt), split, [_select(ENSURE, "domain=lab.local", targets=["lab.local"],
                                                       proof_host="dc02.child.lab.local")]))

    [card] = run.cards
    assert "proof_host" not in card["data"]["arguments"]["inputs"] and run.issued == []
    assert "not a host in lab.local" in _outcome_text(run)


# --- (d) a host on a procedure that takes none changes nothing (run5-1186 shape) ----------------------------------

def test_a_host_on_a_procedure_without_a_service_proof_changes_nothing(monkeypatch, tmp_path):
    row = _RECORDED["run5-1186"]
    _operator_says(monkeypatch, f"Use {row['targets'][1]}.")
    runs = {}
    for name, extra in (("plain", {}), ("host", {"proof_host": row["targets"][1]})):
        mt, split = _isolated(monkeypatch, tmp_path / name)
        runs[name] = asyncio.run(_run(_state_for(mt, "dcsync-account"), split, [
            _select("dcsync-account", targets=row["targets"], **extra), AIMessage(content="Read.")]))

    plain, host = runs["plain"], runs["host"]
    assert _arguments(host) == _arguments(plain) and _commands(host) == _commands(plain) and host.issued
    assert "`dcsync-account` takes no proof host" in _outcome_text(host)
    assert "proof host" not in _outcome_text(plain)


# --- (e) the pin is the binder's: re-derived identically, and a changed one is refused ------------------------------

def _candidate(procedure=FORGE, target="domain=lab.local", intent=None, **inputs):
    action = {"name": procedure, "target": target, "effects": ["da:lab.local"],
              "intent": intent or {"capability": procedure, "domain": "lab.local"}}
    [found] = cb.candidates("chat:1:request:1", [(action, {"callback_id": "13", **inputs})])
    return found


def _brief(candidate, host) -> hb.HandoffBrief:
    return hb.HandoffBrief.from_args(allowed_capabilities=[candidate.procedure], callback_display_id=13,
                                     candidate_ref=candidate.ref, proof_host=host)


def test_rebinding_the_stored_brief_pins_the_same_host_and_a_changed_call_is_refused():
    found = _candidate()
    stored = _brief(found, "DC01").as_metadata()
    first, error = cb.bind(hb.HandoffBrief.from_args(**stored), (found,))
    again, _ = cb.bind(hb.HandoffBrief.from_args(**stored), (found,))
    assert error == "" and first == again and first.ref == found.ref
    assert (first.inputs["proof_host"], first.inputs["proof_resource_source"]) == (HOST, "operator-named")

    call = {"action": dict(first.action), "inputs": dict(first.inputs)}
    assert cb.call_matches(call, first) == ""
    for inputs in ({**call["inputs"], "proof_host": "dc02.lab.local"},
                   {k: v for k, v in call["inputs"].items() if k != "proof_host"}):
        assert "proof_host" in cb.call_matches({**call, "inputs": inputs}, first)
    # A resolved target the card added to an unpinned candidate's call is not a change (BloodHound's DC, today).
    unpinned, _ = cb.bind(_brief(found, ""), (found,))
    assert cb.call_matches(call, unpinned) == ""


@pytest.mark.parametrize("procedure, target, intent, sent, expected", [
    (FORGE, "domain=north.lab.local;target_domain=lab.local",
     {"domain": "north.lab.local", "target_domain": "lab.local"}, "dc01", HOST),
    (FORGE, "domain=north.lab.local;target_domain=lab.local",
     {"domain": "north.lab.local", "target_domain": "lab.local"}, "dc02.north.lab.local", ""),
    (ENSURE, "domain=lab.local;callback=13;source_domain=north.lab.local",
     {"domain": "lab.local", "target_domain": "lab.local", "source_domain": "north.lab.local"}, "dc01.lab.local.",
     HOST),
    ("dcsync-krbtgt", "domain=lab.local", {"domain": "lab.local"}, HOST, ""),
], ids=["parent-forge", "child-host-for-parent-forge", "parent-context", "no-service-proof"])
def test_the_pin_follows_the_domain_the_service_proof_must_prove(procedure, target, intent, sent, expected):
    found = _candidate(procedure, target, intent)
    bound, error = cb.bind(_brief(found, sent), (found,))
    assert error == "" and bound.inputs.get("proof_host", "") == expected


# --- provenance: exact operator naming, never a guess ----------------------------------------------------------------

@pytest.mark.parametrize("text, named", [
    ("prove on dc01.lab.local", True), ("prove on DC01", True), ("prove on dc01.lab.local.", True),
    ("list \\\\DC01.LAB.LOCAL\\C$", True), ("list \\\\dc01\\SYSVOL\\x", True),
    ("prove on dc011.lab.local", False), ("prove on dc01.child.lab.local", False), ("prove on xdc01", False),
    ("the lab.local domain", False), ("north\\dc01", False),
])
def test_only_an_operator_input_that_names_the_host_is_its_source(text, named):
    inputs = (hb.OperatorTargetInput("chat:1:request:1", 0, "forge lab.local"),
              hb.OperatorTargetInput("chat:1:request:2", 1, text))
    source = hb.operator_host_source(HOST, inputs)
    assert (source == inputs[1]) is named


# --- (f) other lanes and step kinds behave as without the field ------------------------------------------------------

LANES = {
    "collection": ({}, {"allowed_capabilities": ["collect-graph"], "callback_display_id": 14,
                        "targets": ["child.lab.local"]}, []),
    "raw": ({}, {"allowed_tools": [ISSUE], "callback_display_id": 13, "targets": ["lab.local"]},
            [_call(ISSUE, {"command": "shell", "parameters": "whoami", "callback_display_id": 13}, 2)]),
    "conversation": ({"mode": "conversation"}, {"allowed_capabilities": [FORGE], "callback_display_id": 13,
                                                 "targets": ["lab.local"]}, []),
    "autonomous-solve": ({"autonomous_solve": True}, {"allowed_capabilities": [FORGE], "callback_display_id": 13,
                                                       "targets": ["lab.local"]}, []),
}


@pytest.mark.parametrize("lane", list(LANES))
def test_steps_outside_catalog_binding_run_as_without_the_field(monkeypatch, tmp_path, lane):
    options, scope, rest = LANES[lane]
    _operator_says(monkeypatch, f"Prove it on {HOST}.")
    runs = {}
    for name, extra in (("plain", {}), ("host", {"proof_host": HOST})):
        mt, split = _isolated(monkeypatch, tmp_path / name)
        brief = {"handoff_title": "a step", "handoff_instruction": binding.PROMPT,
                 "expected_result": "The step's result.", **scope, **extra}
        runs[name] = asyncio.run(_run(_no_dc(mt), split, [_call("transfer_to_Mythic_Operator", brief, 1), *rest],
                                      decision=None, **options))

    plain, host = runs["plain"], runs["host"]
    assert _arguments(host) == _arguments(plain) and _commands(host) == _commands(plain)
    assert [phase for phase, _ in host.briefs] == [phase for phase, _ in plain.briefs]
    assert "proof_host" not in _stored(host) and "proof_host" not in json.dumps(host.cards)
    # Admission saw the field and dropped it; Autonomous Solve's controller never dispatches the brief at all.
    events = [dict(event.metadata) for event in host.model._request_event_ledger.actual_events(kind="brief")]
    assert [json.loads(event["proof_host_dropped"])["host"] for event in events] == ([] if lane == "autonomous-solve"
                                                                                     else [HOST]), events
