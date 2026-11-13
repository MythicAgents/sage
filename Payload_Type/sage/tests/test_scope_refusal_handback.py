"""A supervised step refused for its scope keeps the refused proposal visible.

When a worker proposes a guarded action its step's scope excludes, Sage removes the call before any card. Sage's
own step outcome then names the refused proposal for the Supervisor (tool, command or capability, callback, scope),
without relying on the worker's prose. If re-sending that scope ends the request, the single native terminal names
the refused proposal and says it was neither approved nor executed. A corrected scope still reaches the ordinary
exact-action approval card.

Everything on the path is real except the models' choices and Mythic's network I/O: the graph, the handoff brief
check, the step bound, the approval card and its resolution, the native chat service, the MythicTools task method
with its schema resolution and authority rechecks (the issue itself is the inert `_split_issue` seam).
"""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from ai.langgraph import handoff_brief as hb
from ai.langgraph import model as model_module
from ai.langgraph import mythic_tools as mythic_tools_module
from ai.langgraph import step_bounds as sb
from ai.langgraph.mythic_tools import MythicTools
from sage_chat.headless import build_chat_request
from sage_chat.service import bind_channel_thread_id
from sage_chat.session import drop_channel_session
from tests.test_engagement_wiring import _split_issue
from tests.test_hitl_steer_current_intent import (
    _NativeDriverChat, _NativeScriptedModel, _call, _cards, _input_response,
)

ISSUE = "issue_task_and_waitfor_task_output"
CAPABILITY = "execute_capability"
PROMPT = "Escalate to SYSTEM on our current host."
CALLBACK = 3
OTHER_CALLBACK = 5
SCOPE = "capabilities:execute-as-local-admin"
SECRET = "Hunter2-synthetic-secret"
CALLBACKS = [{"id": CALLBACK, "display_id": CALLBACK, "host": "MEEREEN", "user": "ESSOS\\daenerys"},
             {"id": OTHER_CALLBACK, "display_id": OTHER_CALLBACK, "host": "MEEREEN", "user": "ESSOS\\viserys"}]
SCOPED = {"handoff_title": "Elevate", "handoff_instruction": PROMPT, "callback_display_id": CALLBACK,
          "targets": ["meereen.essos.local"], "expected_result": "A SYSTEM callback.",
          "allowed_capabilities": ["execute-as-local-admin"]}
# The corrected scope: the same step, its one guarded action a direct task.
TOOL_SCOPED = {**{k: v for k, v in SCOPED.items() if k != "allowed_capabilities"}, "allowed_tools": [ISSUE]}

MALFORMED = (CAPABILITY, {"action": {"capability": "getsystem", "callback_id": str(CALLBACK)}})
RAW_COMMAND = (ISSUE, {"command": "getsystem", "parameters": "", "callback_display_id": CALLBACK})
VALID_EXCLUDED = (CAPABILITY, {"action": {"capability": "dcsync-krbtgt", "callback_id": str(CALLBACK)}})

# shape -> (proposal, worker dispatches before the stop, the recorded identity)
SHAPES = {
    "filed_malformed": (MALFORMED, 2, {"tool": CAPABILITY, "action": "getsystem", "form": "malformed_capability",
                                       "callback": "3", "kind": sb.NOT_OFFERED}),
    "raw_command": (RAW_COMMAND, 2, {"tool": ISSUE, "action": "getsystem", "form": "command", "callback": "3",
                                     "kind": sb.NOT_OFFERED}),
    "valid_excluded": (VALID_EXCLUDED, 1, {"tool": CAPABILITY, "action": "dcsync-krbtgt", "form": "capability",
                                           "callback": "3", "kind": sb.MIS_SCOPED}),
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("unexpected network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "engagement"))


def _tools(monkeypatch) -> MythicTools:
    """Real MythicTools; Mythic's control-plane reads answer locally and the task issue is inert."""
    tools = MythicTools(agent_task_id="scope-refusal-handback")
    tools.client = object()

    async def live_command(command, callback_display_id):
        return {"status": "available", "command": command}

    async def command_schema(command, callback_display_id, **_kwargs):
        return []  # getsystem takes no parameters

    async def liveness(client, display_id, **_kwargs):
        return {"status": "active", "reason": "synthetic check-in"}

    monkeypatch.setattr(tools, "_authenticate_live_command", live_command)
    monkeypatch.setattr(tools, "_resolve_payload_type", lambda callback_id: asyncio.sleep(0, result="apollo"))
    monkeypatch.setattr(tools, "_fetch_command_schema", command_schema)
    monkeypatch.setattr(tools, "_fetch_enrolled_executable_schema", command_schema)
    monkeypatch.setattr(tools, "_supervised_task_state", lambda *a, **k: asyncio.sleep(
        0, result={"task_completed": True, "task_status": "completed"}))
    monkeypatch.setattr(mythic_tools_module, "assess_callback_liveness", liveness)
    tools._recon_snapshot_cache[("list_callbacks", "all")] = list(CALLBACKS)
    tools._engagement_footholds = [SimpleNamespace(callback_id=str(CALLBACK), host="MEEREEN", forest="essos.local",
                                                   identity="ESSOS\\daenerys")]
    return tools


def _handoff(brief: dict, index: int) -> AIMessage:
    return _call("transfer_to_Mythic_Operator", dict(brief), index)


def _proposal(tool_name: str, args: dict, index: int) -> AIMessage:
    # The worker keeps neutral text that names no action, so the Supervisor can learn the proposal only from Sage.
    return AIMessage(content="A candidate action.", tool_calls=[{
        "name": tool_name, "args": json.loads(json.dumps(args)), "id": f"proposal-{index}", "type": "tool_call"}])


async def _drive(tools: MythicTools, script: list, *, prompts: tuple[str, ...] = (PROMPT,), decision: str = "",
                 mode: str = "supervised", autonomous_solve: bool = False) -> SimpleNamespace:
    model = model_module.Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline scope refusal",
        config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
        task_id=0, agent_task_id="scope-refusal", mode=mode, autonomous_solve=autonomous_solve, operation_id=1,
        channel_id=1, apitoken_id=0,
    )
    model.mythic_client = tools
    model._payload_names = []
    model.memory = InMemorySaver()
    fake = _NativeScriptedModel(messages=iter(script))  # one script shared by every agent, in call order
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = _NativeDriverChat(model)
    calls: dict = {}
    issued_before_decision: list = []
    ledgers = []
    try:
        with _split_issue(lambda: "NT AUTHORITY\\SYSTEM", calls, display_id=lambda: 40 + len(calls.get("issued", []))):
            request_id = 0
            for prompt in prompts:
                request_id += 1
                request = build_chat_request(prompt, channel_id=1, operation_id=1, request_id=request_id,
                                             config={"mode": mode})
                bind_channel_thread_id(request, model)
                await chat.chat(request)
                ledgers.append(model._request_event_ledger)
            issued_before_decision = list(calls.get("issued", []))
            cards = [card for card in _cards(chat) if card.get("status") not in {"accepted", "rejected", "responded"}]
            if decision and cards:
                request_id += 1
                second = build_chat_request("", channel_id=1, operation_id=1, request_id=request_id,
                                            config={"mode": mode})
                second.InputResponse = _input_response(
                    decision, cards[-1], response="Explain the current integrity level instead." if decision == "respond" else "")
                await chat.chat(second)
        snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
        values = snapshot.values
        supervisor = values.get("supervisor_messages", [])
        ledger = model._request_event_ledger
        return SimpleNamespace(
            model=model, chat=chat, values=values, ledger=ledger, ledgers=ledgers,
            issued=calls.get("issued", []), issued_before_decision=issued_before_decision,
            cards=list(dict.fromkeys(json.dumps(card.get("data", {}).get("arguments"), sort_keys=True, default=str)
                                     for card in _cards(chat))),
            terminals=[e for e in chat.emissions if e.get("complete_request") and not (
                isinstance(e.get("metadata"), dict) and e["metadata"].get("special_type") == "input_requested")],
            scope_events=[dict(e.metadata) for e in ledger.actual_events(kind="scope_rejected")],
            outcomes=[m.additional_kwargs["_step_outcome"] for m in supervisor
                      if "_step_outcome" in m.additional_kwargs],
            outcome_text=[str(m.content) for m in supervisor if "_step_outcome" in m.additional_kwargs],
            supervisor_text="\n".join(str(m.content) for m in supervisor),
            delegations=[m for m in values.get("mythic_operator_messages", [])
                         if m.additional_kwargs.get("_delegated_to") == "Mythic_Operator"],
        )
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


def _refusal_script(proposal: tuple[str, dict], repeats: int) -> list:
    script: list = []
    for index in range(repeats):
        script += [_handoff(SCOPED, 10 + index), _proposal(*proposal, index)]
    return script + [_handoff(SCOPED, 99)] + [AIMessage(content="Final answer.")] * 4


def _assert_lifecycle_clean(run) -> None:
    assert run.ledger.open_lifecycles() == ()
    final = run.ledger.reconcile(require_final=True)
    assert final["ok"], final["errors"]


# --- the three observed shapes ------------------------------------------------------------------------------------

@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_scope_refusal_reaches_the_supervisor_and_the_terminal(monkeypatch, shape):
    proposal, repeats, identity = SHAPES[shape]

    run = asyncio.run(_drive(_tools(monkeypatch), _refusal_script(proposal, repeats)))

    # The existing resend limit holds: the stopping re-send never reached the worker.
    assert len(run.delegations) == repeats
    # Nothing was carded or issued for the refused call.
    assert run.cards == [] and run.issued == []
    # Sage's own record of each refusal carries the proposal's identity and its rejected scope.
    assert len(run.scope_events) == repeats
    for event in run.scope_events:
        assert {key: event.get(f"proposal_{key}") for key in ("tool", "action", "form", "callback")} == {
            key: identity[key] for key in ("tool", "action", "form", "callback")}, event
        assert (event["scope_signature"], event["kind"], event["worker"]) == (SCOPE, identity["kind"],
                                                                             "Mythic_Operator")
    # The Supervisor receives it from Sage's step outcome, not from the worker's prose.
    assert len(run.outcomes) == repeats
    for outcome, text in zip(run.outcomes, run.outcome_text):
        assert outcome["status"] == "rejected_by_scope"
        refused = outcome["refused_proposal"]
        assert {key: refused[key] for key in ("tool", "action", "form", "callback", "scope")} == {
            **{key: identity[key] for key in ("tool", "action", "form", "callback")}, "scope": SCOPE}
        assert f"`{identity['action']}`" in text and "callback 3" in text, text
        assert "not approved" in text and "not executed" in text, text
    assert "getsystem" not in "A candidate action."  # the worker's text names nothing
    # One terminal; it names the refused proposal as unapproved and unexecuted.
    [terminal] = run.terminals
    content = str(terminal.get("content") or "")
    assert "Blocked" in content and "already failed at execution" in content, content
    assert f"`{identity['action']}`" in content and "callback 3" in content, content
    assert "not approved" in content and "not executed" in content, content
    if identity["form"] == "malformed_capability":
        assert "malformed" in content
    _assert_lifecycle_clean(run)


# --- corrected scope: the ordinary exact-action approval card --------------------------------------------------

def _corrected_script() -> list:
    return [_handoff(SCOPED, 10), _proposal(*MALFORMED, 1),
            _handoff(TOOL_SCOPED, 11), _proposal(*RAW_COMMAND, 2),
            AIMessage(content="The task returned NT AUTHORITY\\SYSTEM.")] + [AIMessage(content="Final answer.")] * 6


@pytest.mark.parametrize("decision, issued", [("accept", 1), ("reject", 0), ("respond", 0)])
def test_a_corrected_scope_reaches_one_fresh_card_and_runs_only_after_accept(monkeypatch, decision, issued):
    run = asyncio.run(_drive(_tools(monkeypatch), _corrected_script(), decision=decision))

    # The changed scope was admitted, not stopped as a re-send.
    assert len(run.delegations) == 2
    assert [o["status"] for o in run.outcomes][:1] == ["rejected_by_scope"]
    # Exactly one fresh card, for the corrected task, and nothing issued before the operator's decision.
    [card] = run.cards
    assert json.loads(card)["command"] == "getsystem" and json.loads(card)["callback_display_id"] == CALLBACK
    assert run.issued_before_decision == []
    assert [(row["command_name"], row["callback_display_id"]) for row in run.issued] == [("getsystem", CALLBACK)] * issued
    assert "Refused proposal" not in "\n".join(str(t.get("content") or "") for t in run.terminals
                                               if decision == "accept")


# --- negative and near-match controls --------------------------------------------------------------------------

def test_an_unknown_capability_is_still_malformed_and_never_runs(monkeypatch):
    proposal = (CAPABILITY, {"action": {"capability": "frobnicate", "callback_id": str(CALLBACK)}})

    run = asyncio.run(_drive(_tools(monkeypatch), _refusal_script(proposal, 2)))

    assert [e["kind"] for e in run.scope_events] == [sb.NOT_OFFERED] * 2
    assert run.outcomes[0]["refused_proposal"]["form"] == "malformed_capability"
    assert "not a catalog procedure" in run.outcomes[0]["detail"]
    assert run.cards == [] and run.issued == []
    [terminal] = run.terminals
    assert "`frobnicate`" in terminal["content"] and "malformed" in terminal["content"]


def test_structural_brief_errors_keep_their_own_reason(monkeypatch):
    broken = {k: v for k, v in SCOPED.items() if k != "targets"}
    script = [_handoff(SCOPED, 10), _proposal(*MALFORMED, 1), _handoff(broken, 11), _handoff(broken, 12)]
    script += [AIMessage(content="Final answer.")] * 4

    run = asyncio.run(_drive(_tools(monkeypatch), script))

    [terminal] = run.terminals
    assert "missing required field(s): targets" in terminal["content"], terminal
    assert "Refused proposal" not in terminal["content"] and "getsystem" not in terminal["content"]


def test_an_older_request_cannot_supply_the_terminals_proposal(monkeypatch):
    script = [_handoff(SCOPED, 10), _proposal(*MALFORMED, 1), AIMessage(content="The step was refused; stopping."),
              _handoff(SCOPED, 20), _proposal(*VALID_EXCLUDED, 2), _handoff(SCOPED, 21)]
    script += [AIMessage(content="Final answer.")] * 6

    run = asyncio.run(_drive(_tools(monkeypatch), script, prompts=(PROMPT, PROMPT)))

    assert len(run.terminals) == 2
    content = str(run.terminals[-1].get("content") or "")
    assert "`dcsync-krbtgt`" in content and "getsystem" not in content, content


def test_another_callback_cannot_supply_the_terminals_proposal(monkeypatch):
    elsewhere = {**SCOPED, "callback_display_id": OTHER_CALLBACK}
    script = [_handoff(SCOPED, 10), _proposal(*VALID_EXCLUDED, 1), _handoff(elsewhere, 11)]
    script += [AIMessage(content="Final answer.")] * 4

    run = asyncio.run(_drive(_tools(monkeypatch), script))

    [terminal] = run.terminals
    content = str(terminal.get("content") or "")
    assert "already failed at execution" in content           # the existing limit still stops the re-send
    assert "Refused proposal" not in content and "dcsync-krbtgt" not in content, content


def _event(signature=SCOPE, *, worker="Mythic_Operator", step_callback="3", action="getsystem", kind="not_offered"):
    return SimpleNamespace(metadata=tuple(sorted({
        "scope_signature": signature, "kind": kind, "tool": ISSUE, "worker": worker, "delegation_seq": "4",
        "step_callback": step_callback, "proposal_tool": ISSUE, "proposal_action": action,
        "proposal_form": "command", "proposal_callback": step_callback}.items())))


def test_only_the_same_worker_and_callback_in_the_failing_tail_supply_a_proposal():
    events = [_event(action="older", signature="tools:other"), _event(action="whoami"),
              _event(action="other-worker", worker="Generalist"), _event(action="other-callback", step_callback="5"),
              _event(action="getsystem")]
    proposals = hb.refused_proposals(events, SCOPE, worker="Mythic_Operator", callback="3")
    assert [p["action"] for p in proposals] == ["whoami", "getsystem"]
    assert hb.refused_proposals(events, SCOPE, worker="Generalist", callback="3") == [
        {**proposals[0], "action": "other-worker"}]
    assert hb.refused_proposals(events, "tools:other", worker="Mythic_Operator", callback="3") == []


CATALOG = hb.catalog_names()


@pytest.mark.parametrize("tool_name, args, capability, expected", [
    (CAPABILITY, {"action": json.dumps({"capability": "getsystem", "callback_id": 3})}, "getsystem",
     {"action": "getsystem", "form": "malformed_capability", "callback": "3"}),
    (CAPABILITY, {"action": {}}, "requested", {"action": "", "form": "malformed_capability", "callback": ""}),
    (CAPABILITY, {"action": {"capability": "dcsync-krbtgt", "callback_id": -1}}, "dcsync-krbtgt",
     {"action": "dcsync-krbtgt", "form": "capability", "callback": ""}),
    (ISSUE, {"command": "", "callback_display_id": True}, "", {"action": "", "form": "tool", "callback": ""}),
    ("upload_file_by_file_uuid", {"file_uuid": SECRET, "callback_display_id": 7}, "",
     {"action": "", "form": "tool", "callback": "7"}),
], ids=["json-string-action", "unnamed", "negative-callback", "boolean-callback", "other-guarded-tool"])
def test_the_refusal_record_keeps_identity_and_drops_arguments(tool_name, args, capability, expected):
    record = sb.refused_proposal(SCOPED, tool_name, args, capability, CATALOG, scope=SCOPE, worker="Mythic_Operator",
                                 delegation_seq=4)
    assert {key: record[key] for key in expected} == expected
    assert (record["tool"], record["scope"], record["step_callback"], record["delegation_seq"]) == (
        tool_name, SCOPE, "3", "4")
    text = sb.describe_refused_proposal(record)
    assert SECRET not in json.dumps(record) + text
    assert ("malformed" in text) is (expected["form"] == "malformed_capability")
    assert ("on callback" in text) is bool(expected["callback"])


@pytest.mark.parametrize("proposal", [
    (ISSUE, {"command": "getsystem", "parameters": f"-password {SECRET}", "callback_display_id": CALLBACK}),
    (CAPABILITY, {"action": {"capability": "getsystem", "callback_id": str(CALLBACK)}, "inputs": {"password": SECRET}}),
    (CAPABILITY, {"action": {"capability": f"getsystem /password:{SECRET}", "callback_id": str(CALLBACK)}}),
    (ISSUE, {"command": f"getsystem {SECRET}", "parameters": "", "callback_display_id": f"3 {SECRET}"}),
], ids=["task-parameters", "capability-inputs", "capability-name-tail", "command-tail"])
def test_no_raw_argument_is_echoed(monkeypatch, proposal):
    run = asyncio.run(_drive(_tools(monkeypatch), _refusal_script(proposal, 2)))

    assert run.issued == [] and run.cards == []
    [terminal] = run.terminals
    assert "`getsystem`" in terminal["content"]
    exposed = json.dumps([run.scope_events, run.outcomes, run.supervisor_text, terminal], default=str)
    assert SECRET not in exposed


# --- lanes -----------------------------------------------------------------------------------------------------

def test_conversation_gets_no_guarded_execution_or_refusal_record(monkeypatch):
    run = asyncio.run(_drive(_tools(monkeypatch), _refusal_script(RAW_COMMAND, 2), mode="conversation"))

    assert run.issued == [] and run.cards == [] and run.scope_events == [] and run.outcomes == []
    assert "Refused proposal" not in json.dumps(run.terminals, default=str)


def test_supervised_autonomous_solve_keeps_its_controller_lane(monkeypatch):
    controller: list[str] = []
    original = model_module.Model._run_autonomous_controller

    async def run_controller(self, objective):
        controller.append(objective)
        return await original(self, objective)

    monkeypatch.setattr(model_module.Model, "_run_autonomous_controller", run_controller)
    run = asyncio.run(_drive(_tools(monkeypatch), _refusal_script(RAW_COMMAND, 2), autonomous_solve=True))

    assert controller == [PROMPT]
    assert run.delegations == [] and run.scope_events == [] and run.issued == []
    assert "Refused proposal" not in json.dumps(run.terminals, default=str)


def test_auto_workers_carry_no_step_bound_and_no_brief_check():
    model = SimpleNamespace(mode="auto", _get_base_chat_model=lambda: None)
    middleware = model_module.Model._context_middleware(model, agent_name="Mythic_Operator")
    assert not any(isinstance(m, model_module._SupervisedStepBoundMiddleware) for m in middleware)
