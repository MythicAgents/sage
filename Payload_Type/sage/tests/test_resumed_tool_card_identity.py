"""An approved call that fails after the approval resume closes its own tool card.

Recorded 2026-11-06 (channel 3, request 3): the operator accepted a `create_payload` card. LangGraph re-ran the
worker node on the resume with a fresh capture callback that had never seen the call's name, so the tool's schema
error was reported as `unknown_tool` under a new lifecycle identity. The real card stayed open until it was swept to
`cancelled`, and reconciliation failed the request on the terminal-only lifecycle (`tool started count=0`).

The assembled cases drive native chat on the production graph: the real HITL middleware, `create_payload` tool
schema, capture callback, card emitter, request ledger and reconciliation. A scripted model replays the recorded
call (host values renamed in the fixture). Mythic's network I/O is a stand-in and is never reached, because the
schema fails first. The near-match cases run the real card owner and ledger directly; each must still fail
reconciliation exactly as before.
"""

from __future__ import annotations

import asyncio
import copy
import json
import socket
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from ai.langgraph import model as model_module
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_events import RequestEventLedger
from sage_chat.headless import build_chat_request
from sage_chat.service import bind_channel_thread_id
from sage_chat.session import drop_channel_session
from tests.test_hitl_steer_current_intent import (
    _NativeDriverChat, _NativeScriptedModel, _call, _cards, _input_response,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/model_input_boundaries/approved_call_errors.json").read_text()
)
ROW = FIXTURE["rows"][0]
CALL = ROW["call"]
PAYLOAD_WORKER = "Mythic_Payload"
PAYLOAD_STEP = {
    "handoff_title": "build the spawn payload",
    "handoff_instruction": "Build an Apollo x64 shellcode payload; inherit C2 config from reference callback 1.",
    "allowed_tools": ["create_payload"],
    "expected_result": "A built Apollo shellcode payload.",
}
PROMPT = "In supervised mode, build an Apollo x64 shellcode payload that inherits callback 1's C2 config."
WORKER_ANSWER = "The build was rejected: c2_profile_parameters must hold string values."
FINAL = "The payload build was rejected by its argument schema; nothing was built."


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("unexpected network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


class _StepModel(_NativeScriptedModel):
    """Answer the payload worker and every other agent from what each was actually given."""

    worker_fn: object = None
    supervisor_fn: object = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.inputs.append(list(messages))
        worker = any(isinstance(m, SystemMessage) and "You are the Mythic Payload Agent" in str(m.content)
                     for m in messages)
        message = (self.worker_fn if worker else self.supervisor_fn)(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])


def _worker_results(messages) -> list[ToolMessage]:
    start = max((i for i, m in enumerate(messages)
                 if m.additional_kwargs.get("_delegated_to") == PAYLOAD_WORKER), default=-1)
    return [m for m in messages[start + 1:] if isinstance(m, ToolMessage)]


def _recorded_call(call: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": call["name"], "args": copy.deepcopy(call["args"]), "id": call["id"], "type": "tool_call",
    }])


async def _assembled_run(row: dict, *, restore_ledger: bool) -> SimpleNamespace:
    call = row["call"]
    counter = iter(range(100, 200))
    proposed: list[str] = []

    def worker(messages):
        if not _worker_results(messages):
            proposed.append(call["name"])
            return _recorded_call(call)
        return AIMessage(content=WORKER_ANSWER)

    def supervisor(messages):
        if not any(m.additional_kwargs.get("_step_outcome") for m in messages):
            return _call(f"transfer_to_{PAYLOAD_WORKER}", dict(PAYLOAD_STEP), next(counter))
        return AIMessage(content=FINAL)

    tools = MythicTools(agent_task_id="resumed-tool-card-identity")
    tools.client = object()
    model = model_module.Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline resumed tool card identity",
        config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
        task_id=0, agent_task_id="resumed-tool-card", mode="supervised", operation_id=1, channel_id=1,
        apitoken_id=0,
    )
    model.mythic_client = tools
    model._payload_names = []
    model.memory = InMemorySaver()
    fake = _StepModel(messages=iter(()), worker_fn=worker, supervisor_fn=supervisor)
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = _NativeDriverChat(model)
    restored = False
    request_id = handled = 0
    try:
        request_id += 1
        request = build_chat_request(PROMPT, channel_id=1, operation_id=1, request_id=request_id,
                                     config={"mode": "supervised"})
        bind_channel_thread_id(request, model)
        await chat.chat(request)
        while True:  # accept each card raised since the last one handled, as the operator did
            raised = [card for card in _cards(chat)[handled:]
                      if card.get("status") not in {"accepted", "rejected"}]
            handled = len(_cards(chat))
            if not raised:
                break
            if restore_ledger:
                # The paused request's ledger as a restart restores it: only what was exported.
                ledger = model._request_event_ledger
                model._request_event_ledger = RequestEventLedger.from_export(ledger.export_state())
                restored = True
            request_id += 1
            accept = build_chat_request("", channel_id=1, operation_id=1, request_id=request_id,
                                        config={"mode": "supervised"})
            accept.InputResponse = _input_response("accept", raised[-1])
            await chat.chat(accept)
        snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
        return SimpleNamespace(model=model, chat=chat, values=snapshot.values, next=snapshot.next,
                               proposed=proposed, restored=restored)
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


def _tool_cards(run) -> list[dict]:
    return [e for e in run.chat.emissions
            if isinstance(e.get("metadata"), dict) and e["metadata"].get("special_type") == "tool_use"]


def _terminals(run) -> list[dict]:
    return [e for e in run.chat.emissions if e.get("complete_request")
            and not (isinstance(e.get("metadata"), dict) and e["metadata"].get("special_type") == "input_requested")]


def _assert_own_card_closed(run, row: dict) -> None:
    call, expected, recorded = row["call"], row["expected"], row["recorded"]
    # The operator accepted exactly the recorded call, and the real schema rejected it.
    assert [action["tool_name"] for card in _cards(run.chat) for action in card["data"]["actions"]] == [call["name"]]
    assert run.proposed == [call["name"]]
    results = [m for m in run.values.get("mythic_payload_messages", [])
               if isinstance(m, ToolMessage) and m.tool_call_id == call["id"]]
    assert len(results) == 1 and results[0].status == "error", results
    assert all(loc in str(results[0].content) for loc in recorded["phoenix_span"]["error_locations"])

    # The recorded failure first: a terminal-only lifecycle fails reconciliation (`tool started count=0`).
    ledger = run.model._request_event_ledger
    final = ledger.reconcile(require_final=True)
    assert final["ok"] is expected["reconcile_ok"], final["errors"]
    [terminal] = _terminals(run)
    assert terminal["kind"] == "complete", terminal
    assert recorded["request"]["error"] not in str(terminal.get("content") or "")
    assert ledger.open_lifecycles() == ()

    cards = _tool_cards(run)
    unknown = [e for e in cards if e["metadata"]["tool_use"]["tool_name"] == "unknown_tool"]
    assert len(unknown) == expected["unknown_tool_cards"], unknown
    own = [e for e in cards if e["metadata"]["tool_use"].get("tool_call_id") == call["id"]]
    assert len({e["response_key"] for e in own}) == expected["cards_for_call"], own
    assert [e["metadata"]["tool_use"]["status"] for e in own] == expected["phases"], own
    assert {e["metadata"]["tool_use"]["tool_name"] for e in own} == {expected["tool_name"]}
    assert expected["preview_contains"] in own[-1]["metadata"]["tool_use"]["result_preview"]
    assert run.next == ()


def test_recorded_call_still_fails_todays_schema():
    """Readiness: the recorded arguments raise the same two validation errors against today's tool."""
    tool = MythicTools(agent_task_id="schema-readiness").get_tools([CALL["name"]])[0]
    with pytest.raises(Exception) as raised:
        asyncio.run(tool.ainvoke(copy.deepcopy(CALL["args"])))
    text = str(raised.value)
    assert ROW["recorded"]["phoenix_span"]["status_message_head"] in text
    assert all(location in text for location in ROW["recorded"]["phoenix_span"]["error_locations"])


def test_approved_call_that_fails_after_the_resume_closes_its_own_card(no_network):
    run = asyncio.run(_assembled_run(ROW, restore_ledger=False))
    _assert_own_card_closed(run, ROW)


def test_approved_call_closes_its_own_card_with_a_restored_ledger(no_network):
    run = asyncio.run(_assembled_run(ROW, restore_ledger=True))
    assert run.restored
    _assert_own_card_closed(run, ROW)


# --- the card owner and ledger directly: accepted forms and rejected near-matches ---------------------------------

REQUEST_ID = "chat:3:request:3"
CALL_ID = CALL["id"]
DELEGATION = "mythic_payload:chat:3:request:3:4"


class _Emitter:
    def __init__(self):
        self.cards: list[dict] = []

    async def emit_tool_use(self, **kwargs):
        self.cards.append(kwargs)
        return True


def _model() -> model_module.Model:
    model = object.__new__(model_module.Model)
    model._request_event_ledger = RequestEventLedger(REQUEST_ID)
    model._response_emitter = _Emitter()
    model._active_delegations = {}
    ledger = model._request_event_ledger
    ledger.record_once(event_id=f"{REQUEST_ID}:input", kind="operator_input", phase="received",
                       content="build a payload", metadata={})
    ledger.record_once(event_id=f"{REQUEST_ID}:contract", kind="control_transition",
                       phase="request_installed", content="request contract installed", metadata={})
    return model


async def _start(model, *, name="create_payload", call_id=CALL_ID, delegation=DELEGATION):
    await model._emit_tool_use_card(tool_call_id=call_id, tool_name=name, status="started", complete=False,
                                    delegation_id=delegation)


def _callback(model, delegation=DELEGATION) -> model_module.MessageCaptureCallback:
    """A capture callback as the resumed node builds it: it never saw the call's model message."""
    return model_module.MessageCaptureCallback(
        agent_name=PAYLOAD_WORKER,
        tool_use_func=model._emit_tool_use_card,
        delegation_id=delegation,
        delegation_name=None,
    )


def _finish(model) -> dict:
    ledger = model._request_event_ledger
    ledger.record_once(event_id=f"{REQUEST_ID}:terminal", kind="control_transition",
                       phase="request_terminal", content="complete")
    ledger.record_once(event_id=f"{REQUEST_ID}:final", kind="final_response", phase="emitted")
    ledger.record_projection(event_id=f"{REQUEST_ID}:final", kind="final_response", phase="emitted",
                             projection_key="assistant:3:turn")
    return ledger.reconcile(require_final=True)


def _error(model, callback=None):
    callback = callback or _callback(model)
    return callback.on_tool_error(ValueError("2 validation errors for create_payload"),
                                  run_id=uuid.uuid4(), tool_call_id=CALL_ID)


def test_nameless_tool_end_closes_the_one_open_start():
    model = _model()

    async def run():
        await _start(model)
        message = ToolMessage(content="built", tool_call_id=CALL_ID)
        await _callback(model).on_tool_end(message, run_id=uuid.uuid4())

    asyncio.run(run())
    cards = model._response_emitter.cards
    assert [(c["status"], c["tool_name"]) for c in cards] == [("started", "create_payload"),
                                                              ("completed", "create_payload")]
    assert len({c["event_id"] for c in cards}) == 1
    assert _finish(model)["ok"]


def test_nameless_tool_error_on_a_restored_ledger_closes_the_one_open_start():
    model = _model()

    async def run():
        await _start(model)
        model._request_event_ledger = RequestEventLedger.from_export(model._request_event_ledger.export_state())
        await _error(model)

    asyncio.run(run())
    cards = model._response_emitter.cards
    assert [(c["status"], c["tool_name"]) for c in cards] == [("started", "create_payload"),
                                                              ("error", "create_payload")]
    assert len({c["event_id"] for c in cards}) == 1
    assert _finish(model)["ok"]


def _assert_unmatched_as_today(model) -> None:
    terminal = model._response_emitter.cards[-1]
    assert terminal["status"] == "error" and terminal["tool_name"] == "unknown_tool", terminal
    result = _finish(model)
    assert not result["ok"]
    assert any(error == f"{terminal['event_id']}: tool started count=0" for error in result["errors"]), result


def test_no_start_in_the_request_still_fails_reconciliation():
    model = _model()
    asyncio.run(_error(model))
    _assert_unmatched_as_today(model)


def test_open_start_under_another_delegation_is_not_matched():
    model = _model()

    async def run():
        await _start(model, delegation="mythic_payload:chat:3:request:3:9")
        await _error(model)

    asyncio.run(run())
    _assert_unmatched_as_today(model)


def test_two_open_starts_with_one_call_id_are_not_matched():
    model = _model()

    async def run():
        await _start(model, name="create_payload")
        await _start(model, name="delete_payload")
        await _error(model)

    asyncio.run(run())
    _assert_unmatched_as_today(model)


def test_start_that_already_has_its_terminal_is_not_matched():
    model = _model()

    async def run():
        await _start(model)
        await model._emit_tool_use_card(tool_call_id=CALL_ID, tool_name="create_payload", status="completed",
                                        complete=True, delegation_id=DELEGATION)
        await _error(model)

    asyncio.run(run())
    _assert_unmatched_as_today(model)
