"""An already-achieved capability answer reaches native chat with its proof, and is not dispatched again.

A supervised step whose capability effect is already proved in the engagement ledger returns the executor's
`already_achieved` verdict with the existing effect/task/callback proof and issues no Mythic task. Sending that same
step again (same worker, title and structural scope) is not dispatched: the first resend gets the answer back, the
second ends the request at a checkpoint carrying it, without the no-progress stop. A bounded one-action request ends
directly with the answer.

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
from tests.test_step_bounds import FORGE_ACTION, FORGE_PROMPT, _proved_krbtgt_client

OPERATOR = "Mythic_Operator"
HANDOFF = f"transfer_to_{OPERATOR}"
FIRST_REFUSAL = f"Delegation to {OPERATOR} was not dispatched: this is the step that just returned"
FINAL_REFUSAL = f"{OPERATOR} was sent the step that already answered again"
VERDICT = "Executor verdict: `achieved` for `forge-golden-ticket`"
PROOF = "`da:lab.local` task=41 cb=13"  # the seeded ledger proof, as the executor report names it
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


# --- ordinary supervised: the answered step is not dispatched again ----------------------------------------------

def test_an_already_achieved_step_resent_twice_ends_at_a_checkpoint_with_its_proof(achieved):
    mt, split_issue = achieved
    ran = _spy_capability(mt)
    reworded = {**FORGE_BRIEF, "expected_result": "Golden ticket forged and verified for lab.local."}
    supervisor = [_call(HANDOFF, FORGE_BRIEF, 1), _call(HANDOFF, FORGE_BRIEF, 2), _call(HANDOFF, reworded, 3),
                  *_filler("Final answer.")]
    worker = [_call("execute_capability", FORGE, 4), *_filler("The golden ticket effect is already proved.")]

    run = asyncio.run(_native_run(mt, split_issue, supervisor, worker, prompts=[FORGE_PROMPT]))

    # The real producer answered from the ledger once and issued nothing.
    assert [r["stopped_after"] for r in ran] == ["already_achieved"], ran
    assert run.issued == []
    # One worker dispatch; both resends refused without dispatch (the second rewords expected_result).
    assert len(_dispatches(run)) == 1
    assert len(_delegation_cards(run)) == 1
    [first] = _refusals(run, FIRST_REFUSAL)
    assert len(_refusals(run, FINAL_REFUSAL)) == 1
    assert VERDICT in first and PROOF in first, first  # the first refusal hands the answer back
    # One native terminal, a checkpoint that carries the verdict and the existing proof; no no-progress stop.
    [terminal] = _terminals(run)
    assert terminal["kind"] == "complete" and terminal["metadata"].get("turn_outcome") == "checkpoint", terminal
    assert VERDICT in terminal["content"] and PROOF in terminal["content"], terminal["content"]
    assert _stops(run) == [] and _errors(run) == []


# --- bounded one-action: the answer ends the request directly ----------------------------------------------------

def test_a_bounded_one_action_already_achieved_request_completes_with_its_proof(achieved):
    mt, split_issue = achieved
    ran = _spy_capability(mt)
    # Supervised workers receive the operator's request itself, so the operator's words make the request bounded.
    prompt = "Run execute_capability exactly once for forge-golden-ticket on lab.local via callback 13, then stop."
    bounded = {**FORGE_BRIEF, "handoff_instruction": prompt}
    supervisor = [_call(HANDOFF, bounded, 1), _call(HANDOFF, bounded, 2), *_filler("Final answer.")]
    worker = [_call("execute_capability", FORGE, 3), *_filler("The golden ticket effect is already proved.")]

    run = asyncio.run(_native_run(mt, split_issue, supervisor, worker, prompts=[prompt]))

    assert [r["stopped_after"] for r in ran] == ["already_achieved"], ran
    assert run.issued == []
    assert len(_dispatches(run)) == 1 and len(_delegation_cards(run)) == 1
    assert _refusals(run, FIRST_REFUSAL) == [] and _refusals(run, FINAL_REFUSAL) == []
    [terminal] = _terminals(run)
    assert terminal["kind"] == "complete" and terminal["metadata"].get("turn_outcome") == "completed", terminal
    assert VERDICT in terminal["content"] and PROOF in terminal["content"], terminal["content"]
    assert _stops(run) == [] and _errors(run) == []


# --- controls: a different step, a new request and an effectful step are dispatched -----------------------------

@pytest.mark.parametrize("change", [
    {"callback_display_id": 14},
    {"targets": ["lab.local", "DC01"]},  # callback 13's observed host
    {"inputs_by_reference": ["task:21"]},
    {"handoff_title": "forge the golden ticket again"},
], ids=["callback", "target", "input", "title"])
def test_a_changed_step_after_an_already_achieved_answer_is_dispatched(achieved, change):
    mt, split_issue = achieved
    mt._recon_snapshot_cache[("list_callbacks", "all")].append({"id": 14, "display_id": 14, "host": "DC02"})
    ran = _spy_capability(mt)
    supervisor = [_call(HANDOFF, FORGE_BRIEF, 1), _call(HANDOFF, {**FORGE_BRIEF, **change}, 2),
                  *_filler("Final answer.")]
    worker = [_call("execute_capability", FORGE, 3), *_filler("The golden ticket effect is already proved.")]

    run = asyncio.run(_native_run(mt, split_issue, supervisor, worker, prompts=[FORGE_PROMPT]))

    assert len(ran) == 1 and run.issued == []
    assert len(_dispatches(run)) == 2 and len(_delegation_cards(run)) == 2
    assert _refusals(run, FIRST_REFUSAL) == [] and _refusals(run, FINAL_REFUSAL) == []
    assert _stops(run) == [] and _errors(run) == []


def test_the_same_step_in_a_new_request_is_dispatched(achieved):
    mt, split_issue = achieved
    ran = _spy_capability(mt)
    supervisor = [_call(HANDOFF, FORGE_BRIEF, 1), AIMessage(content="It is already proved."),
                  _call(HANDOFF, FORGE_BRIEF, 2), *_filler("Final answer.")]
    worker = [_call("execute_capability", FORGE, 3), _call("execute_capability", FORGE, 4),
              *_filler("The golden ticket effect is already proved.")]

    run = asyncio.run(_native_run(mt, split_issue, supervisor, worker, prompts=[FORGE_PROMPT, FORGE_PROMPT]))

    assert [r["stopped_after"] for r in ran] == ["already_achieved", "already_achieved"], ran
    assert run.issued == []
    assert len(_dispatches(run)) == 2
    assert _refusals(run, FIRST_REFUSAL) == [] and _refusals(run, FINAL_REFUSAL) == []
    assert len(_terminals(run)) == 2 and _stops(run) == [] and _errors(run) == []


def test_an_approved_capability_step_that_issued_a_task_is_dispatched_when_resent(monkeypatch, tmp_path, no_network):
    """The step ran its approved forge, which issued Mythic tasks: sending it again is new work, not a repeat."""
    mt, split_issue = _proved_krbtgt_client(monkeypatch, tmp_path)
    ran = _spy_capability(mt)
    supervisor = [_call(HANDOFF, FORGE_BRIEF, 1), _call(HANDOFF, FORGE_BRIEF, 2), *_filler("Final answer.")]
    worker = [_call("execute_capability", FORGE, 3), *_filler("The forge ran.")]

    run = asyncio.run(_native_run(mt, split_issue, supervisor, worker, prompts=[FORGE_PROMPT]))

    assert len(ran) == 1 and ran[0].get("stopped_after") != "already_achieved", ran
    assert run.issued, "the approved forge issued no Mythic task"
    assert len(_dispatches(run)) == 2 and len(_delegation_cards(run)) == 2
    assert _refusals(run, FIRST_REFUSAL) == [] and _refusals(run, FINAL_REFUSAL) == []
    assert _stops(run) == [] and _errors(run) == []


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
