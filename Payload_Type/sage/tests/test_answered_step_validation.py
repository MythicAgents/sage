"""An answered capability step is not dispatched again, and an already-proved effect never reaches a card.

In ordinary supervised requests a catalog step runs only an action current evidence supports (catalog binding). A
proved effect removes its procedure from those actions, so a step asking for it ends blocked before a card and
issues nothing; the ledger rows the Supervisor reads carry the proof. A step that ran an approved procedure is never
refused as an answered repeat; whether it can run again depends on whether its candidate is still current. The
Supervisor's answered-step record is consulted only in assisted lanes.

Everything on the path is real except the models and Mythic's network I/O: the public `MythicTools.execute_capability`
tool with its request-contract check and ledger read, the approval card and its acceptance, the worker stop, step
accounting, the handoff tools, and the native chat service that projects the terminal. The ledger proof is written
through the real ledger store, and every Mythic task issue is recorded instead of sent. The scripted Supervisor
deliberately resends; whether a real Supervisor resends is a live question this file does not answer.
"""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from ai.langgraph import model as model_module
from sage_chat.headless import build_chat_request
from sage_chat.service import bind_channel_thread_id
from sage_chat.session import drop_channel_session
from tests.esl_fixtures import seed_ledger
from tests.test_engagement_wiring import _proof_hop
from tests.test_hitl_steer_current_intent import (
    _NativeDriverChat, _NativeScriptedModel, _call, _cards, _input_response,
)
from tests.test_step_bounds import (
    FORGE_ACTION, FORGE_PROMPT, _bound_forge_handoff, _live_on_13, _proved_krbtgt_client,
)

OPERATOR = "Mythic_Operator"
HANDOFF = f"transfer_to_{OPERATOR}"
FIRST_REFUSAL = f"Delegation to {OPERATOR} was not dispatched: this is the step that just returned"
FINAL_REFUSAL = f"{OPERATOR} was sent the step that already answered again"
NO_PROGRESS = ("Halted: no progress", "Stopped — no progress")

FORGE_BRIEF = {
    "handoff_title": "forge the golden ticket", "handoff_instruction": FORGE_PROMPT,
    "allowed_capabilities": ["forge-golden-ticket"], "callback_display_id": 13, "targets": ["lab.local"],
    "expected_result": "A verified golden ticket for lab.local.",
}
FORGE = {"action": dict(FORGE_ACTION), "inputs": {}}


class _RoutedModel(_NativeScriptedModel):
    """The Mythic Operator speaks from its own script and every other agent from the Supervisor's, so a resend that
    is dispatched is answered by the worker instead of desynchronising one shared script."""

    worker: Any = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        speaker = any(isinstance(m, SystemMessage) and "You are the **Mythic Operator**" in str(m.content)
                      for m in messages)
        if not speaker:
            self.inputs.append(list(messages))  # what every non-worker model call was given, in order
        scripted = next(self.worker if speaker else self.messages)
        scripted = scripted(messages) if callable(scripted) else scripted
        return ChatResult(generations=[ChatGeneration(message=scripted)])


def _filler(text: str, count: int = 8) -> list[AIMessage]:
    return [AIMessage(content=text) for _ in range(count)]


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("unexpected network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def achieved(monkeypatch, tmp_path, no_network):
    """A session whose ledger already proves `da:lab.local` (task 41, callback 13), with Mythic I/O inert."""
    mt, split_issue = _proved_krbtgt_client(monkeypatch, tmp_path)
    seed_ledger(mt, hops=[_proof_hop("da:lab.local", "41", callback_id="13")])
    return mt, split_issue


def _spy_capability(mt) -> list[dict]:
    """Count real capability executions without replacing them."""
    ran: list[dict] = []
    real = mt._execute_capability_impl

    async def spy(action, inputs=None):
        result = await real(action, inputs)
        ran.append(json.loads(result))
        return result

    mt._execute_capability_impl = spy
    return ran


async def _native_run(mt, split_issue, supervisor: list, worker: list, *, prompts: list[str],
                      mode: str = "supervised") -> SimpleNamespace:
    """Drive native chat: each prompt is one request, and a pending card is accepted in the next request."""
    model = model_module.Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline answered-step validation",
        config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
        task_id=0, agent_task_id="answered-step", mode=mode, operation_id=1, channel_id=1, apitoken_id=0,
    )
    model.mythic_client = mt
    model._payload_names = []
    model.memory = InMemorySaver()
    fake = _RoutedModel(messages=iter(supervisor), worker=iter(worker))
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = _NativeDriverChat(model)
    calls: dict = {}
    request_id = handled = 0
    try:
        with split_issue(lambda: "ok", calls, display_id=lambda: 30 + len(calls.get("issued", []))):
            for prompt in prompts:
                request_id += 1
                request = build_chat_request(prompt, channel_id=1, operation_id=1, request_id=request_id,
                                             config={"mode": mode})
                bind_channel_thread_id(request, model)
                await chat.chat(request)
                while True:  # accept each card raised since the last one handled, as the operator would
                    raised = [card for card in _cards(chat)[handled:]
                              if card.get("status") not in {"accepted", "rejected"}]
                    handled = len(_cards(chat))
                    if not raised:
                        break
                    request_id += 1
                    accept = build_chat_request("", channel_id=1, operation_id=1, request_id=request_id,
                                                config={"mode": mode})
                    accept.InputResponse = _input_response("accept", raised[-1])
                    await chat.chat(accept)
        snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
        return SimpleNamespace(model=model, chat=chat, emissions=chat.emissions, issued=calls.get("issued", []),
                               supervisor=snapshot.values.get("supervisor_messages", []), values=snapshot.values,
                               fake=fake)
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


# --- what the native record shows ------------------------------------------------------------------------------

def _dispatches(run) -> list[HumanMessage]:
    """The delegations Sage handed to the worker: its own channel holds each dispatched brief once."""
    return [m for m in run.values.get("mythic_operator_messages", []) if isinstance(m, HumanMessage)
            and m.additional_kwargs.get("_delegated_to") == OPERATOR]


def _delegation_cards(run) -> set[str]:
    """Distinct worker delegations native chat showed as subagent cards."""
    return {str(e["metadata"].get("delegation_id")) for e in run.emissions
            if isinstance(e.get("metadata"), dict) and e["metadata"].get("special_type") == "subagent"
            and e["metadata"].get("delegation_name") == OPERATOR}


def _handoff_results(run) -> dict[str, ToolMessage]:
    """Every handoff result, once each: the ones the Supervisor's model was given and the ones in final state.

    A first refusal lives only in the Supervisor's own turn, so its record is the Supervisor's next model input."""
    channels = [value for key, value in run.values.items() if key.endswith("messages") and isinstance(value, list)]
    return {m.tool_call_id: m for messages in [*run.fake.inputs, *channels] for m in messages
            if isinstance(m, ToolMessage) and m.name == HANDOFF}


def _refusals(run, text: str) -> list[str]:
    return [str(m.content) for m in _handoff_results(run).values() if str(m.content).startswith(text)]


def _terminals(run) -> list[dict]:
    return [e for e in run.emissions if e.get("complete_request")
            and not (isinstance(e.get("metadata"), dict) and e["metadata"].get("special_type") == "input_requested")]


def _stops(run) -> list[str]:
    return [str(e.get("content") or "") for e in run.emissions
            if any(marker in str(e.get("content") or "") for marker in NO_PROGRESS)]


def _errors(run) -> list[dict]:
    return [e for e in run.emissions if e["kind"] == "error" or e.get("status") == "error"]


def _assert_lifecycle_clean(run) -> None:
    """The request's lifecycle ledger reconciles after the terminal: every opened delegation and tool closed, the
    final response projected. The service only logs a failure of this last reconciliation, so read the ledger."""
    ledger = run.model._request_event_ledger
    assert ledger.open_lifecycles() == ()
    final = ledger.reconcile(require_final=True)
    assert final["ok"], final["errors"]
    assert not any("reconciliation failed" in str(e.get("content", "")) + str(e.get("error", ""))
                   for e in run.emissions)


# --- ordinary supervised: an already-proved forge is no current candidate ------------------------------------------
# An ordinary supervised catalog step runs only an action current evidence supports (catalog binding). A proved
# `da:lab.local` removes the forge from those actions, so the step ends blocked before a card instead of reaching the
# executor's `already_achieved` answer; the ledger rows the Supervisor reads already carry that proof.

def test_an_already_proved_forge_ends_blocked_before_a_card_and_runs_nothing(achieved, monkeypatch):
    mt, split_issue = achieved
    _live_on_13(mt, monkeypatch)
    ran = _spy_capability(mt)
    supervisor = [_bound_forge_handoff(FORGE_BRIEF, 1), _call(HANDOFF, FORGE_BRIEF, 2), *_filler("Final answer.")]

    run = asyncio.run(_native_run(mt, split_issue, supervisor, _filler("unused"), prompts=[FORGE_PROMPT]))

    assert ran == [] and run.issued == [] and _dispatches(run) == [] and _cards(run.chat) == []
    [terminal] = _terminals(run)
    assert "Blocked" in terminal["content"] and "`forge-golden-ticket`" in terminal["content"], terminal["content"]
    assert "da:lab.local" in "".join(str(m.content) for m in run.fake.inputs[0])   # the proof the Supervisor read
    assert _stops(run) == [] and _errors(run) == []
    _assert_lifecycle_clean(run)


# --- control: an effectful step is dispatched when resent -------------------------------------------------------

def test_an_approved_capability_step_that_issued_a_task_is_dispatched_when_resent(monkeypatch, tmp_path, no_network):
    """The step ran its approved forge, which issued Mythic tasks: sending it again is new work, not a repeat. While
    the forge is still a current candidate, the resend gets its own card and runs only after that approval."""
    mt, split_issue = _proved_krbtgt_client(monkeypatch, tmp_path)
    _live_on_13(mt, monkeypatch)
    ran = _spy_capability(mt)
    chosen: list[dict] = []

    def first(messages):
        call = _bound_forge_handoff(FORGE_BRIEF, 1)(messages)
        chosen.append(call.tool_calls[0]["args"])
        return call

    supervisor = [first, lambda messages: _call(HANDOFF, chosen[0], 2), *_filler("Final answer.")]

    run = asyncio.run(_native_run(mt, split_issue, supervisor, _filler("The forge ran."), prompts=[FORGE_PROMPT]))

    assert len(ran) == 2 and all(r.get("stopped_after") != "already_achieved" for r in ran), ran
    assert run.issued, "the approved forge issued no Mythic task"
    assert len(_dispatches(run)) == 2 and len(_delegation_cards(run)) == 2
    assert len([card for card in _cards(run.chat) if card.get("status") not in {"accepted", "rejected"}]) == 2
    assert _refusals(run, FIRST_REFUSAL) == [] and _refusals(run, FINAL_REFUSAL) == []
    assert _stops(run) == [] and _errors(run) == []
    _assert_lifecycle_clean(run)


# --- other lanes: no new ending policy ---------------------------------------------------------------------------
# Supervised with Autonomous Solve keeps its card for the forge (tests/test_step_bounds.py,
# test_supervised_autonomous_solve_still_needs_the_forge_card).

def test_conversation_never_runs_the_guarded_capability(achieved):
    mt, split_issue = achieved
    ran = _spy_capability(mt)
    supervisor = [_call(HANDOFF, FORGE_BRIEF, 1), _call(HANDOFF, FORGE_BRIEF, 2), *_filler("Final answer.")]
    worker = [_call("execute_capability", FORGE, 3), *_filler("I cannot run guarded actions in conversation.")]

    run = asyncio.run(_native_run(mt, split_issue, supervisor, worker, prompts=[FORGE_PROMPT], mode="conversation"))

    assert ran == [] and run.issued == [] and _cards(run.chat) == []
    assert len(_terminals(run)) == 1 and _errors(run) == []
    _assert_lifecycle_clean(run)


@pytest.mark.parametrize("mode, consulted", [("auto", False), ("supervised", True), ("conversation", True)])
def test_only_assisted_supervisors_consult_the_answered_step_record(achieved, monkeypatch, mode, consulted):
    """Auto pursues its bound objective without the repeat refusal; assisted Supervisors' handoffs consult it."""
    mt, _ = achieved
    built: list[dict] = []
    real = model_module._create_handoff_tool

    def record(*args, **kwargs):
        built.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(model_module, "_create_handoff_tool", record)

    async def build():  # the constructor's checkpointer needs a running loop
        model = model_module.Model(
            provider="openai", model="gpt-4o-mini", system_prompt="offline answered-step validation",
            config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
            task_id=0, agent_task_id="answered-step", mode=mode, operation_id=1, channel_id=1, apitoken_id=0,
        )
        model.mythic_client = mt
        model._payload_names = []
        model.memory = InMemorySaver()
        model._rebuild_graph()

    asyncio.run(build())

    operator = [kwargs for kwargs in built if kwargs.get("agent_name") == OPERATOR
                and kwargs.get("caller_agent_name") == "Supervisor"]
    assert operator, built
    for kwargs in operator:
        check = kwargs.get("repeat_check")
        assert (check is not None and check.__func__ is model_module.Model._answered_step_repeat) is consulted
        assert (check is None) is (not consulted)
