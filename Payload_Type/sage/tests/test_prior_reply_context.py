"""A supervised worker receives the reply the operator's follow-up refers to (2026-11-10 channel 4, requests 8-15).

The operator answered a menu with "do option B". The Supervisor read the menu; each worker it delegated to had only the
exact follow-up text and the Supervisor's untrusted assignment, so it asked what option B meant. Sage now carries its
previous reply in the chat into every supervised worker call as one labelled data block, invocation-only.

The native cases run the real HeadlessSageChat -> Model -> graph; only the chat model and Mythic I/O are scripted.
"""

from __future__ import annotations

import asyncio
import json
from itertools import count
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from ai.langgraph.model import Model
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import RequestLane, build_request_contract
from ai.langgraph.turn_authority import authority_from_request_contract
from sage_chat.headless import build_chat_request
from sage_chat.service import bind_channel_thread_id
from sage_chat.session import drop_channel_session
from tests.test_hitl_steer_current_intent import (
    _BRIEF, _NativeDriverChat, _NativeScriptedModel, _call, _cards, _input_response,
)

TAG = "previous_reply"
MENU = ("Two ways forward:\n\n**Option A** - re-run graph collection.\n\n**Option B** - MENU-B enumerate lab users "
        "over LDAP from callback 5.\n\nShall I proceed with Option B?")
OTHER_MENU = "Collection finished. **Option C** - MENU-C read the domain trusts. **Option D** - stop here."
FOLLOW_UP = "Yeah, do option B"
ASSIGN_ONE = "Option B: enumerate lab users over LDAP from callback 5."
ASSIGN_TWO = "Option B, second step: summarise the enumerated users."
STEER = "Do not run whoami. Run getprivs on callback 5 instead."
WHOAMI = {"callback_display_id": 5, "command": "whoami", "parameters": ""}
GETPRIVS = {"callback_display_id": 5, "command": "getprivs", "parameters": ""}


def _blocks(messages) -> list:
    return [m for m in messages if (getattr(m, "additional_kwargs", {}) or {}).get("_carried_context") == TAG]


def _delegated(messages) -> list:
    return [m for m in messages if isinstance(m, HumanMessage) and m.additional_kwargs.get("_delegated_to")]


def _assignments(messages) -> list:
    return [m for m in messages if (getattr(m, "additional_kwargs", {}) or {}).get("_assisted_assignment_context")]


def _transfer(title: str, instruction: str, expected: str, index: int) -> AIMessage:
    return _call("transfer_to_Mythic_Operator", {"handoff_title": title, "handoff_instruction": instruction,
                                                 **_BRIEF, "expected_result": expected}, index)


class _Workers:
    """Script entries for worker calls that record the worker's real model input."""

    def __init__(self):
        self.inputs: list[list] = []
        self.requests: list[int] = []
        self.request = 0

    def __call__(self, reply):
        def worker(messages):
            self.inputs.append(list(messages))
            self.requests.append(self.request)
            return reply
        return worker


async def _drive(turns, script, workers: _Workers, *, mode: str = "supervised"):
    """Run each turn in one native channel: a prompt string, or ("accept"|"respond", text) for the pending card."""
    effects: list[dict] = []

    @tool
    async def issue_task_and_waitfor_task_output(command: str, parameters: str, callback_display_id: int) -> str:
        """Issue one callback task and wait for its output."""
        effects.append({"command": command, "callback_display_id": callback_display_id})
        return "offline effect"

    model = Model(provider="openai", model="gpt-4o-mini", system_prompt="offline prior-reply case",
                  config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
                  task_id=0, agent_task_id="prior-reply", mode=mode, operation_id=1, channel_id=1, apitoken_id=0)
    sink = MagicMock()
    preparer = MythicTools(agent_task_id="offline-prior-reply")
    preparer.client = object()

    async def live_command(command, callback):
        return ({"status": "available", "command": command} if callback == 5 and command in {"whoami", "getprivs"}
                else {"status": "missing", "payload_type": "apollo"})

    async def known_schema(command, callback):
        return [] if callback == 5 and command in {"whoami", "getprivs"} else None

    preparer._authenticate_live_command = live_command
    preparer._fetch_command_schema = known_schema
    sink._prepare_supervised_issue_action = preparer._prepare_supervised_issue_action
    sink.get_tools.side_effect = lambda names: (
        [issue_task_and_waitfor_task_output] if "issue_task_and_waitfor_task_output" in names else [])
    sink._recon_snapshot_cache = {("list_callbacks", "all"): [{"id": 5, "host": "WORKSTATION-5"}]}
    model.mythic_client = sink
    model._payload_names = []
    model.memory = InMemorySaver()
    fake = _NativeScriptedModel(messages=iter(script))
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = _NativeDriverChat(model)
    request_ids = count(1)
    try:
        for turn in turns:
            request_id = next(request_ids)
            prompt = turn if isinstance(turn, str) else ""
            request = build_chat_request(prompt, channel_id=1, operation_id=1, request_id=request_id,
                                         config={"mode": mode})
            if request_id == 1:
                bind_channel_thread_id(request, model)
            if not isinstance(turn, str):
                action, text = turn
                request.InputResponse = _input_response(action, _cards(chat)[-1], response=text)
            workers.request = request_id
            await chat.chat(request)
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))
    return model, fake, chat, effects


def _run(turns, script, workers, **kwargs):
    return asyncio.run(_drive(turns, script, workers, **kwargs))


def _menu_reply(kind: str, index: int):
    if kind == "direct":
        return AIMessage(content=MENU)
    return _call("respond_to_user", {"final_response": MENU, "outcome": "checkpoint"}, index)


def _carried_menu(messages, text: str = MENU) -> bool:
    blocks = _blocks(messages)
    return len(blocks) == 1 and text in str(blocks[0].content)


# --- the follow-up's workers carry the reply it refers to -------------------------------------------------------------

@pytest.mark.parametrize("kind", ["direct", "final"])
def test_every_worker_call_in_a_follow_up_carries_the_previous_reply(kind):
    workers = _Workers()
    script = [
        _menu_reply(kind, 1),
        _transfer("Enumerate users (option B)", ASSIGN_ONE, "The lab's users.", 2),
        workers(AIMessage(content="Step one done.")),
        _transfer("Summarise users (option B)", ASSIGN_TWO, "A summary of the users.", 3),
        workers(AIMessage(content="Step two done.")),
        AIMessage(content="final"),
    ]
    model, fake, _chat, effects = _run(["Which way should we go next?", FOLLOW_UP], script, workers)

    assert effects == []
    assert workers.requests == [2, 2]
    # Premise: the Supervisor's request-2 input already holds the menu (it is the message the block repeats).
    supervisor_r2 = fake.inputs[1]
    assert MENU in "\n".join(str(m.content) for m in supervisor_r2)
    for messages, assignment in zip(workers.inputs, (ASSIGN_ONE, ASSIGN_TWO)):
        [block] = _blocks(messages)
        assert MENU in str(block.content)
        assert str(block.content).startswith(f"<{TAG}>") and str(block.content).endswith(f"</{TAG}>")
        assert "not operator input" in str(block.content)
        # The exact operator text is still the only delegated HumanMessage, and the block comes before it.
        [delegated] = _delegated(messages)
        assert delegated.content == FOLLOW_UP
        assert messages.index(block) < messages.index(delegated)
        # The Supervisor's assignment is still its own untrusted block, unchanged by the menu.
        [assigned] = _assignments(messages)
        payload = json.loads(str(assigned.content).split("\n", 2)[2].rsplit("\n", 1)[0])
        assert payload == {"request_id": payload["request_id"], "worker": "Mythic_Operator", "assignment": assignment}
        assert MENU not in str(assigned.content)


def test_a_guarded_proposal_in_a_follow_up_still_needs_exactly_one_card():
    workers = _Workers()
    script = [
        AIMessage(content=MENU),
        _transfer("Enumerate users (option B)", ASSIGN_ONE, "The lab's users.", 2),
        workers(_call("issue_task_and_waitfor_task_output", WHOAMI, 3)),
    ]
    model, _fake, chat, effects = _run(["Which way should we go next?", FOLLOW_UP], script, workers)

    assert effects == [] and len(_cards(chat)) == 1 and model._hitl_card_pending is True
    [proposing] = workers.inputs
    assert _carried_menu(proposing)
    [action] = _cards(chat)[0]["data"]["actions"]
    assert {key: action["arguments"][key] for key in WHOAMI} == WHOAMI


def test_an_accepted_follow_up_task_runs_once_and_the_resumed_worker_still_carries_the_reply():
    workers = _Workers()
    script = [
        AIMessage(content=MENU),
        _transfer("Enumerate users (option B)", ASSIGN_ONE, "The lab's users.", 2),
        workers(_call("issue_task_and_waitfor_task_output", WHOAMI, 3)),
        workers(AIMessage(content="whoami done.")),
        AIMessage(content="final"),
    ]
    _model, _fake, chat, effects = _run(["Which way should we go next?", FOLLOW_UP, ("accept", "")], script, workers)

    assert effects == [{"command": "whoami", "callback_display_id": 5}]
    assert len(_cards(chat)) == 1
    assert len(workers.inputs) == 2 and all(_carried_menu(messages) for messages in workers.inputs)


# --- which reply, and only in the invocation ---------------------------------------------------------------------------

def test_only_the_latest_earlier_reply_is_carried_and_the_first_request_carries_none(tmp_path):
    workers = _Workers()
    script = [
        _transfer("Collect users", "Collect the lab's users.", "The lab's users.", 1),   # request 1 delegates
        workers(AIMessage(content="Users collected.")),
        AIMessage(content=OTHER_MENU.replace("Collection finished.", "Users listed.")),  # request 1's reply
        AIMessage(content=MENU),                                                       # request 2's reply
        _transfer("Enumerate users (option B)", ASSIGN_ONE, "The lab's users.", 4),      # request 3
        workers(AIMessage(content="Step done.")),
        AIMessage(content="final"),
    ]
    model, _fake, _chat, _effects = _run(["Collect users.", "What next?", FOLLOW_UP], script, workers)

    assert workers.requests == [1, 3]
    first, third = workers.inputs
    assert _blocks(first) == []
    assert _carried_menu(third, MENU)
    assert "MENU-C" not in "\n".join(str(m.content) for m in third)
    # Invocation-only: no channel, checkpointed message, ledger or observation file holds the block.
    for key, channel in model.state.items():
        if isinstance(channel, list):
            assert not _blocks(channel), key
            assert not any(f"<{TAG}>" in str(getattr(m, "content", "")) for m in channel), key
    leaked = [path for path in tmp_path.rglob("*") if path.is_file() and f"<{TAG}>".encode() in path.read_bytes()]
    assert leaked == []


def test_a_steer_still_carries_the_reply_before_the_request():
    workers = _Workers()
    script = [
        AIMessage(content=MENU),
        _transfer("Enumerate users (option B)", ASSIGN_ONE, "The lab's users.", 2),
        workers(_call("issue_task_and_waitfor_task_output", WHOAMI, 3)),
        workers(AIMessage(content="I will follow the operator's direction.")),
        _transfer("Follow the operator's direction", "Repeat whoami.", "The callback's privileges.", 4),
        workers(_call("issue_task_and_waitfor_task_output", GETPRIVS, 5)),
    ]
    _model, _fake, chat, effects = _run(["Which way should we go next?", FOLLOW_UP, ("respond", STEER)],
                                        script, workers)

    assert effects == [] and len(_cards(chat)) == 2
    steered = workers.inputs[-1]
    assert [m.content for m in _delegated(steered)] == [STEER]
    assert all(_carried_menu(messages) for messages in workers.inputs)


# --- the worker invocation itself: lanes and the cap -------------------------------------------------------------------

def _wrapper_model() -> Model:
    model = Model(provider="openai", model="gpt-4o-mini", system_prompt="offline prior-reply wrapper",
                  config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
                  task_id=0, agent_task_id="prior-reply-wrapper", mode="supervised", operation_id=1, channel_id=1,
                  apitoken_id=0)
    model.mythic_client = MagicMock()
    model._response_emitter = None
    return model


class _Recorder:
    def __init__(self):
        self.inputs: list[list] = []

    async def ainvoke(self, payload, config=None):
        self.inputs.append(list(payload["messages"]))
        return {"messages": list(payload["messages"]) + [AIMessage(content="ok", name="Mythic_Operator")]}


def _worker_input(lane: str, reply: str = MENU, *, between: tuple = (), after: tuple = ()) -> list:
    """One worker invocation for request r2 whose previous request ended with `reply`; `between` follows the reply
    before r2's opening input, `after` follows that input."""
    return asyncio.run(_worker_input_async(lane, reply, between, after))


async def _worker_input_async(lane: str, reply: str, between: tuple, after: tuple) -> list:
    model = _wrapper_model()
    state = {
        "_request_id": "r2", "_request_lane": lane,
        "supervisor_messages": [
            HumanMessage(content="Which way?", additional_kwargs={
                "_operator_input": True, "_request_id": "r1", "_operator_input_revision": 0, "_seq": 1}),
            AIMessage(content=reply, name="Supervisor", additional_kwargs={"_seq": 2}),
            *between,
            HumanMessage(content=FOLLOW_UP, additional_kwargs={
                "_operator_input": True, "_request_id": "r2", "_operator_input_revision": 0, "_seq": 3}),
            *after,
        ],
        "mythic_operator_messages": [
            SystemMessage(content="worker prompt"),
            HumanMessage(content=FOLLOW_UP, additional_kwargs={
                "_delegated_to": "Mythic_Operator", "_handoff_request_id": "r2", "_handoff_caller": "Supervisor",
                "_assisted_assignment": ASSIGN_ONE, "_seq": 5}),
        ],
        "messages": [],
    }
    recorder = _Recorder()
    await model._wrap_create_agent(recorder, "mythic_operator_messages", "Mythic_Operator")(state)
    [messages] = recorder.inputs
    return messages


def test_the_supervised_worker_invocation_carries_the_previous_reply():
    # Positive control for the lane cases below: the same invocation in the supervised lane.
    assert _carried_menu(_worker_input(RequestLane.SUPERVISED_WORKFLOW.value))


def test_the_carried_reply_is_the_last_supervisor_reply_before_the_request():
    routing = AIMessage(content="ROUTING-TEXT", name="Supervisor", tool_calls=[
        {"name": "transfer_to_Mythic_Operator", "args": {}, "id": "route-1", "type": "tool_call"}])
    worker = AIMessage(content="WORKER-TEXT", name="Mythic_Operator", additional_kwargs={"_seq": 2})
    blank = AIMessage(content="   ", name="Supervisor")
    current = AIMessage(content="CURRENT-REQUEST-TEXT", name="Supervisor", additional_kwargs={"_seq": 4})
    messages = _worker_input(RequestLane.SUPERVISED_WORKFLOW.value, between=(routing, worker, blank),
                             after=(current,))
    [block] = _blocks(messages)
    assert MENU in str(block.content)
    assert not any(text in str(block.content) for text in ("ROUTING-TEXT", "WORKER-TEXT", "CURRENT-REQUEST-TEXT"))


@pytest.mark.parametrize("lane", [RequestLane.CONVERSATIONAL.value, RequestLane.AUTONOMOUS_OBJECTIVE.value])
def test_conversation_and_auto_worker_invocations_carry_no_previous_reply(lane):
    assert _blocks(_worker_input(lane)) == []


def test_an_over_cap_reply_keeps_its_tail_with_a_truncation_marker():
    reply = "HEAD-OF-REPLY " + ("filler line about earlier output\n" * 800) + "TAIL: Shall I proceed with Option B?"
    assert len(reply) > 20000
    [block] = _blocks(_worker_input(RequestLane.SUPERVISED_WORKFLOW.value, reply))
    content = str(block.content)
    assert "TAIL: Shall I proceed with Option B?" in content and "HEAD-OF-REPLY" not in content
    assert "[earlier part of the reply truncated]" in content
    assert reply[-12000:] in content and reply[-12001:] not in content


def test_supervised_autonomous_solve_chat_is_controller_owned_and_its_graph_fallback_is_the_supervised_lane(
        monkeypatch):
    """Supervised with Autonomous Solve stays in the supervised lane. In native chat the controller owns the turn, so
    no worker model call exists; where the graph runs instead (rollback or a non-chat command), workers take the
    supervised branch above."""
    monkeypatch.delenv("SAGE_AUTONOMOUS_CONTROLLER", raising=False)
    monkeypatch.delenv("SAGE_CONTROLLER_HITL", raising=False)
    model = object.__new__(Model)
    model._autonomous_solve, model.command_name, model.mode = True, "chat", "supervised"
    model._request_contract = build_request_contract(request_id="r", channel_id="c", operation_id="o",
                                                     mode="supervised", autonomous_solve=True)
    model._turn_authority = authority_from_request_contract(model._request_contract)
    assert model._request_contract.lane is RequestLane.SUPERVISED_WORKFLOW
    assert model._should_use_controller(is_interactive=True) is True
    monkeypatch.setenv("SAGE_CONTROLLER_HITL", "0")
    assert model._should_use_controller(is_interactive=True) is False
