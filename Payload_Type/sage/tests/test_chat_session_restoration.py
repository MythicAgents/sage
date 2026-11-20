"""An existing native chat continues after a Sage process restart.

Before this change, a channel whose Model was lost with the process was refused: "Sage restarted or
this channel's session was replaced". These cases drive the real HeadlessSageChat handler, the real
Model and LangGraph graph over the real file-backed ``SageAsyncSqliteSaver``, the real channel
record and the real approval validator. A "restart" closes the first process's Model and its
SQLite connection, clears every process-local registry, and serves the next request from a new
SageChat. Only the chat model (scripted: wiring only, not model choice), Mythic task issuance and
Mythic GraphQL rows are stand-ins; each stand-in row is labelled synthetic where it is built.

Requests left open by a restart are reconciled when Mythic's on-start message arrives, through the
real ``on_container_start``; only its login and the Watcher it restores first are stand-ins.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from mythic_container import ChatBase
from mythic_container.SharedClasses import ContainerOnStartMessage

import sage_chat.service as service
from ai.langgraph import model as model_module
from ai.langgraph.model import Model
from ai.langgraph.mythic_tools import MythicTools
from sage_chat import session as chat_session
from sage_chat.headless import HeadlessSageChat, build_chat_request
from sage_chat.session import load_channel_session_record
from tests.test_hitl_steer_current_intent import _NativeScriptedModel, _call, _cards, _input_response
from tests.test_prior_reply_context import _transfer

OPERATION = 1
CHANNEL = 41
LABEL_PROMPT = "Remember the label amber-seven. Do not run tools."
RECALL_PROMPT = "What label did I ask you to remember? Do not run tools."
REFUSAL = "Sage restarted or this channel's session was replaced"
PWD = {"callback_display_id": 5, "command": "pwd", "parameters": ""}


_PROCESSES: list["_Process"] = []


@pytest.fixture(autouse=True)
def _close_processes():
    asyncio.run(chat_session.remove_session(str(CHANNEL)))
    yield
    async def close():
        await chat_session.remove_session(str(CHANNEL))
        for process in _PROCESSES:
            for model in process.models:
                conn = getattr(getattr(model, "memory", None), "conn", None)
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass
    asyncio.run(close())
    _PROCESSES.clear()
    service._CHANNEL_TURN_LOCKS.clear()
    service._NEW_CHANNEL_WITHOUT_SESSION.clear()


@pytest.fixture(autouse=True)
def _mythic_rows(monkeypatch):
    """Route restoration's GraphQL reads to the process's synthetic Mythic rows."""
    import sage_chat.restoration as restoration

    async def execute_custom_query(client, query, variables=None):
        return await client.execute(query, dict(variables or {}))

    monkeypatch.setattr(restoration.mythic, "execute_custom_query", execute_custom_query)


class _ProcessDeath(BaseException):
    """Hard process death inside a tool: no handler below the event loop runs its cleanup."""


class _Process:
    """One Sage process: a fresh SageChat whose Models use the scripted chat model."""

    def __init__(self, script, *, card_rows=None, request_rows=None, effects=None, die_after_effect=False,
                 task_records=None, open_requests=None, onstart_error=None, watcher_error=None,
                 withhold_task_tool=False):
        _PROCESSES.append(self)
        # Synthetic: this process's worker is built without the task tool, so a resumed approved
        # call has no tool to run (the restored worker's tool access narrowed after the card).
        self.withhold_task_tool = withhold_task_tool
        self.open_requests = open_requests if open_requests is not None else []
        self.onstart_error = onstart_error
        self.watcher_error = watcher_error
        self.watcher_calls: list[int] = []
        self.die_after_effect = die_after_effect
        self.task_records = task_records if task_records is not None else {}
        self.fake = _NativeScriptedModel(messages=iter(script))
        self.effects = effects if effects is not None else []
        self.card_rows = card_rows if card_rows is not None else []
        self.request_rows = request_rows if request_rows is not None else {}
        self.models: list[Model] = []
        process = self

        class _Chat(HeadlessSageChat):
            async def _construct_model(self, kwargs):
                model = Model(**{
                    **kwargs,
                    "model": "gpt-4o-mini",
                    "system_prompt": "offline restoration case",
                    "config": {"configurable": {"api_key": "offline-only",
                                                "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
                })
                model._get_base_chat_model = lambda *a, **k: process.fake
                process.models.append(model)
                return model

            async def _initialize_model(self, model):
                model.mythic_client = process._sink()
                model._payload_names = []
                model._rebuild_graph()

            async def _ensure_bloodhound_connected(self, **_kwargs):
                return False

            async def _mythic_read_client(self, request, model=None):
                return process

            async def _onstart_client(self, message):
                if process.onstart_error is not None:
                    raise process.onstart_error
                return process

            def _findings_watcher(self):
                return process

        self.chat = _Chat()

    # Watcher stand-in: on-start restores it first; its own behaviour is tested elsewhere.
    async def restore_operation(self, operation_id, **_kwargs):
        self.watcher_calls.append(int(operation_id))
        if self.watcher_error is not None:
            raise self.watcher_error

    def _sink(self):
        effects = self.effects
        process = self
        sink = MagicMock()

        @tool
        async def issue_task_and_waitfor_task_output(
            command: str, parameters: str, callback_display_id: int
        ) -> str:
            """Issue one callback task and wait for its output."""
            effects.append({"command": command, "callback_display_id": callback_display_id})
            # The real sink hands Mythic's task id to the bound receipt writer before waiting.
            bound = sink.set_approval_receipt_sink.call_args
            if bound and process.die_after_effect != "before_receipt":
                await bound.args[0]({"approval_id": process.active_approval_id(),
                                     "task_display_id": 700 + len(effects),
                                     "callback_display_id": callback_display_id, "command": command})
            if process.die_after_effect:
                raise _ProcessDeath()
            return "C:\\Users\\offline"

        preparer = MythicTools(agent_task_id="offline-restoration")
        preparer.client = object()

        async def live_command(command, callback):
            return ({"status": "available", "command": command} if callback == 5 and command == "pwd"
                    else {"status": "missing", "payload_type": "apollo"})

        async def known_schema(command, callback):
            return [] if callback == 5 and command == "pwd" else None

        preparer._authenticate_live_command = live_command
        preparer._fetch_command_schema = known_schema
        sink._prepare_supervised_issue_action = preparer._prepare_supervised_issue_action
        sink.get_tools.side_effect = lambda names: (
            [issue_task_and_waitfor_task_output]
            if "issue_task_and_waitfor_task_output" in names and not process.withhold_task_tool else [])
        sink._recon_snapshot_cache = {("list_callbacks", "all"): [{"id": 5, "host": "WORKSTATION-5"}]}
        sink.client = None

        async def step_task_records(task_ids):
            return {task_id: process.task_records[task_id] for task_id in task_ids
                    if task_id in process.task_records}

        sink._step_task_records = step_task_records
        return sink

    def active_approval_id(self) -> str:
        for model in reversed(self.models):
            claim = getattr(model, "_active_approval_claim", None)
            if isinstance(claim, dict):
                return str(claim.get("approval_id") or "")
        return ""

    # Mythic GraphQL stand-in for the restoration reads (rows are synthetic, built from the
    # exact card Sage emitted or the prompt it recorded; their shape follows Mythic's columns).
    async def execute(self, query, variables):
        if "sageOpenRequests" in query:
            assert variables == {"op": OPERATION, "container": "sage"}, variables
            return {"chat_request": [dict(row) for row in self.open_requests]}
        if "chat_message" in query:
            ((data_key, identity),) = variables["ctx"]["input_requested"]["data"].items()
            ((id_key, wanted),) = identity.items()
            return {"chat_message": [
                row for row in self.card_rows
                if (row["metadata"]["input_requested"].get("data") or {}).get(data_key, {}).get(id_key) == wanted
            ]}
        if "chat_request" in query:
            row = self.request_rows.get(variables["id"])
            return {"chat_request": [row] if row else []}
        raise AssertionError(f"unexpected Mythic query {query}")


def _request(prompt, request_id, *, mode="conversation", context_ids=()):
    request = build_chat_request(prompt, channel_id=CHANNEL, operation_id=OPERATION,
                                 request_id=request_id, config={"mode": mode})
    return request


async def _restart(process: _Process) -> None:
    """Process death: close its Models' SQLite connections and clear process-local registries."""
    for model in process.models:
        conn = getattr(getattr(model, "memory", None), "conn", None)
        if conn is not None:
            await conn.close()
    service._CHANNEL_TURN_LOCKS.clear()
    service._NEW_CHANNEL_WITHOUT_SESSION.clear()
    await chat_session.remove_session(str(CHANNEL))


def _open(request_id: int, channel: int = CHANNEL, status: str = "streaming") -> dict:
    """A Mythic chat_request row left non-terminal by the restart (synthetic, Mythic's columns)."""
    return {"id": request_id, "channel_id": channel, "operation_id": OPERATION, "status": status}


async def _onstart(process: "_Process"):
    return await process.chat.on_container_start(ContainerOnStartMessage(
        container_name="sage", operation_id=OPERATION, server_name="mythic", apitoken="onstart-token"))


def _closings(chat) -> list[dict]:
    return [e for e in chat.emissions if e.get("complete_request")]


def _texts(chat) -> list[str]:
    return [str(item.get("content") or "") for item in chat.emissions]


def _human_texts(messages) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, HumanMessage)]


def test_new_chat_continues_with_its_history_after_restart():
    async def scenario():
        first = _Process([AIMessage(content="Noted: amber-seven.")])
        await first.chat.chat(_request(LABEL_PROMPT, 1))
        assert any("amber-seven" in text for text in _texts(first.chat))
        record = await load_channel_session_record(OPERATION, CHANNEL)
        thread_id = record["thread_id"]
        await _restart(first)

        second = _Process([AIMessage(content="You asked me to remember amber-seven.")])
        await second.chat.chat(_request(RECALL_PROMPT, 2))
        return first, second, thread_id

    first, second, thread_id = asyncio.run(scenario())
    texts = _texts(second.chat)
    assert not any(REFUSAL in text for text in texts), texts
    # The model's real input on the restored turn carries the earlier prompt and the new one.
    assert second.fake.inputs, "restored turn never reached the model"
    seen = _human_texts(second.fake.inputs[-1])
    assert any("amber-seven" in text for text in seen), seen
    assert any(RECALL_PROMPT in text for text in seen), seen
    assert second.models[0]._thread_id_override == thread_id
    assert not second.effects
    assert any("restored" in text for text in texts)


SUPERVISED_PROMPT = "Run pwd once on callback 5; do nothing else."


def _card_script():
    return [
        _transfer("Run pwd", "Run pwd once on callback 5.", "The current directory.", 2),
        _call("issue_task_and_waitfor_task_output", PWD, 3),
    ]


def _after_card_script(final="pwd ran once."):
    return [AIMessage(content="pwd returned C:\\Users\\offline."), AIMessage(content=final)]


def _card_row(card: dict, response) -> dict:
    """A resolved Mythic card row as `chat_update_webhook.go` writes it (synthetic, built from the
    exact card Sage emitted plus Mythic's stored response fields)."""
    return {
        "id": response.InputRequestMessageID,
        "channel_id": CHANNEL,
        "operation_id": OPERATION,
        "metadata": {"special_type": "input_requested", "input_requested": copy.deepcopy(response.InputRequest)},
    }


async def _raise_card():
    first = _Process(_card_script())
    await first.chat.chat(_request(SUPERVISED_PROMPT, 1, mode="supervised"))
    cards = _cards(first.chat)
    assert len(cards) == 1 and not first.effects, [(e.get("kind"), e.get("metadata"), str(e.get("content"))[:120]) for e in first.chat.emissions]
    await _restart(first)
    return first, cards[0]


@pytest.mark.parametrize("action,expected_effects", [("accept", 1), ("reject", 0)])
def test_a_pending_card_decided_after_restart_resolves_exactly_once(action, expected_effects):
    async def scenario():
        _first, card = await _raise_card()
        second = _Process(_after_card_script() if action == "accept" else [
            AIMessage(content="Not run."), AIMessage(content="The operator rejected pwd; nothing ran.")])
        request = _request("", 2, mode="supervised")
        request.InputResponse = _input_response(action, card)
        await second.chat.chat(request)
        # A duplicate delivery of the same decision cannot act again.
        duplicate = _request("", 3, mode="supervised")
        duplicate.InputResponse = _input_response(action, card)
        await second.chat.chat(duplicate)
        return second

    second = asyncio.run(scenario())
    assert len(second.effects) == expected_effects
    texts = _texts(second.chat)
    assert not any(REFUSAL in text for text in texts), texts
    assert any("no longer active" in text for text in texts)


def _lifecycle_errors(chat) -> list[str]:
    return [text for text in (str(e.get("content") or "") + str(e.get("error") or "") for e in chat.emissions)
            if "reconciliation failed" in text.lower()]


@pytest.mark.parametrize("action,expected_effects", [("accept", 1), ("reject", 0)])
def test_a_restored_approval_continues_the_original_request_lifecycle(action, expected_effects):
    """Mythic dispatches a card decision in the card's own request (`chat_update_webhook.go:772`).

    Live requests 11 and 22 ended `error` here: the tool and sub-agent lifecycle opened before the
    restart lived only in the dead process, so the resumed tool end had no start.
    """
    async def scenario():
        first, card = await _raise_card()
        first_keys = {e["response_key"] for e in first.chat.emissions}
        ledger = first.models[0]._request_event_ledger
        started = [event.event_id for event in ledger.actual_events(kind="tool", phase="started")]
        opened = [event.event_id for event in ledger.actual_events(kind="delegation", phase="opened")]
        second = _Process(_after_card_script() if action == "accept" else [
            AIMessage(content="Not run."), AIMessage(content="The operator rejected pwd; nothing ran.")])
        request = _request("", 1, mode="supervised")
        request.InputResponse = _input_response(action, card)
        await second.chat.chat(request)
        return first_keys, started, opened, second

    first_keys, started, opened, second = asyncio.run(scenario())
    assert len(second.effects) == expected_effects
    assert not _lifecycle_errors(second.chat), _lifecycle_errors(second.chat)
    closings = _closings(second.chat)
    assert len(closings) == 1 and closings[0]["kind"] == "complete", closings
    ledger = second.models[-1]._request_event_ledger
    report = ledger.reconcile()
    assert report["ok"], report
    # The guarded call's card and the sub-agent card opened before the restart end after it, once,
    # under their original identities.
    assert started and opened
    for event_id in started:
        assert ledger.phase_count(event_id, "tool", "started", projected=False) == 1
        assert sum(ledger.phase_count(event_id, "tool", phase, projected=False)
                   for phase in ("completed", "error", "stopped", "cancelled")) == 1
    for event_id in opened:
        assert sum(ledger.phase_count(event_id, "delegation", phase, projected=False)
                   for phase in ("finished", "error", "stopped", "cancelled")) == 1
        assert any(e["response_key"] == f"event:{event_id}" and e.get("complete")
                   for e in second.chat.emissions)
    # New text never overwrites a block the request created before the restart.
    reused = [e["response_key"] for e in second.chat.emissions
              if e["kind"] in {"text", "delta", "streaming"} and e["response_key"] in first_keys]
    assert not reused, reused


def _approval_cards(chat) -> list[dict]:
    return [card for card in _cards(chat) if "sage_approval_context" in (card.get("data") or {})]


def _controls(chat) -> list[dict]:
    return [card for card in _cards(chat) if "sage_continuation" in (card.get("data") or {})]


_CONTROL_MESSAGE_ID = 95


def _control_response(control: dict, choice_id: str, *, message_id: int = _CONTROL_MESSAGE_ID):
    """Mythic's dispatch for a selected Continue/Stop control (synthetic: no recording exists yet;
    shape follows `chat_update_webhook.go:657-786` for a single_choice card)."""
    choice = next(dict(item) for item in control["choices"] if item["id"] == choice_id)
    resolved = {"input_request_message_id": message_id, "resolved_by_operator_id": 19,
                "resolved_by": "offline operator", "resolved_at": "2026-11-20T00:00:00Z"}
    echo = copy.deepcopy(control)
    echo.update({"status": "selected", "response": {**resolved, "action": "select", "choice": choice},
                 "resolved_by_operator_id": 19, "resolved_by": "offline operator",
                 "resolved_at": resolved["resolved_at"]})
    return ChatBase.ChatInputResponse(
        action="select", response="", choice=choice, input_request_message_id=message_id,
        input_request=echo, resolved_by_operator_id=19, resolved_by="offline operator",
        resolved_at=resolved["resolved_at"])


def _control_row(control: dict, *, answer=None, cancelled: bool = False,
                 message_id: int = _CONTROL_MESSAGE_ID) -> dict:
    """A stored Continue/Stop row (synthetic, Mythic's columns): pending, answered or cancelled."""
    card = copy.deepcopy(answer.InputRequest if answer is not None else control)
    if cancelled:
        card.update({"status": "cancelled", "response": {
            "action": "cancel", "reason": "request cancelled", "input_request_message_id": message_id,
            "resolved_by_operator_id": 19, "resolved_by": "offline operator",
            "resolved_at": "2026-11-20T00:00:00Z"}})
    return {"id": message_id, "channel_id": CHANNEL, "operation_id": OPERATION,
            "metadata": {"special_type": "input_requested", "input_requested": card}}


def _decided_answer(action: str, card: dict, request_id: int = 1):
    request = _request("", request_id, mode="supervised")
    request.InputResponse = _input_response(action, card)
    return request


def _answer(response, request_id: int = 1):
    request = _request("", request_id, mode="supervised")
    request.InputResponse = response
    return request


async def _offer(card, decided="accept", *, script=None, extra_rows=()):
    """Restart with the card decided in Mythic and its request open, then run on-start."""
    stored = _input_response(decided, card)
    process = _Process(_after_card_script() if script is None else script,
                       card_rows=[_card_row(card, stored), *extra_rows], open_requests=[_open(1)])
    await _onstart(process)
    return process, stored


@pytest.mark.parametrize("decided,expected_effects", [("accept", 1), ("reject", 0)])
def test_a_decision_saved_while_sage_was_down_is_offered_and_continue_applies_it_once(
        decided, expected_effects):
    """Operator decision 2026-11-20: a provably unconsumed stored decision is recovered."""
    async def scenario():
        _first, card = await _raise_card()
        second, stored = await _offer(card, decided, script=_after_card_script() if decided == "accept" else [
            AIMessage(content="Not run."), AIMessage(content="The operator rejected pwd; nothing ran.")])
        at_offer = list(second.chat.emissions)
        record = await load_channel_session_record(OPERATION, CHANNEL)
        effects_at_offer = list(second.effects)
        control = _controls(second.chat)[0]
        second.card_rows.append(_control_row(control, answer=_control_response(control, "continue")))
        await second.chat.chat(_answer(_control_response(control, "continue")))
        effects_after = list(second.effects)
        # Duplicates in both orders neither act again nor end the request again.
        before = len(second.chat.emissions)
        await second.chat.chat(_answer(_control_response(control, "continue")))
        await second.chat.chat(_decided_answer(decided, card))
        late = second.chat.emissions[before:]
        return second, card, at_offer, record, effects_at_offer, effects_after, late

    second, card, at_offer, record, effects_at_offer, effects_after, late = asyncio.run(scenario())
    assert effects_at_offer == [] and not [e for e in at_offer if e.get("complete_request")]
    offered = [e for e in at_offer if (e.get("metadata") or {}).get("special_type") == "input_requested"]
    assert len(offered) == 1 and offered[0]["response_key"].startswith("restoration:1:continue:")
    source_id = card["data"]["sage_approval_context"]["approval_id"]
    assert record["pending"]["context"]["approval_id"] == source_id
    assert record["continuation"]["status"] == "offered"
    assert record["continuation"]["source_approval_id"] == source_id
    assert effects_after == second.effects and len(second.effects) == expected_effects
    assert not _lifecycle_errors(second.chat), _lifecycle_errors(second.chat)
    closings = _closings(second.chat)
    assert len(closings) == 1 and closings[0]["kind"] == "complete", closings
    assert not [e for e in late if e.get("complete_request")]
    assert any("already handled" in str(e.get("content") or "") for e in late)


def test_a_queued_original_decision_wins_once_and_retires_the_unused_offer():
    """Live cards 418/430: the decision was queued while Sage booted, and arrived after on-start."""
    async def scenario():
        _first, card = await _raise_card()
        second, stored = await _offer(card, "accept")
        control = _controls(second.chat)[0]
        second.card_rows.append(_control_row(control))
        await second.chat.chat(_decided_answer("accept", card))
        mid = list(second.chat.emissions)
        await second.chat.chat(_answer(_control_response(control, "continue")))
        return second, control, mid

    second, control, mid = asyncio.run(scenario())
    assert second.effects == [{"command": "pwd", "callback_display_id": 5}]
    assert not _lifecycle_errors(second.chat)
    closings = _closings(second.chat)
    assert len(closings) == 1 and closings[0]["kind"] == "complete"
    key = next(e["response_key"] for e in mid
               if (e.get("metadata") or {}).get("input_requested", {}).get("data", {}).get("sage_continuation"))
    retired = [i for i, e in enumerate(mid) if e["response_key"] == key
               and e["metadata"]["input_requested"]["status"] == "cancelled"]
    terminal = next(i for i, e in enumerate(mid) if e.get("complete_request"))
    assert retired and retired[0] < terminal, "the unused Continue/Stop card must be retired first"
    texts = _texts(second.chat)
    assert not any("no longer active" in t or "already handled" in t for t in texts[:len(mid)]), texts


@pytest.mark.parametrize("how", ["stop_answer", "stop_seen_by_original", "request_cancelled"])
def test_stop_or_cancel_discards_the_saved_decision(how):
    async def scenario():
        _first, card = await _raise_card()
        second, stored = await _offer(card, "accept", script=[AIMessage(content="Nothing pending.")])
        control = _controls(second.chat)[0]
        if how == "stop_answer":
            second.card_rows.append(_control_row(control, answer=_control_response(control, "stop")))
            await second.chat.chat(_answer(_control_response(control, "stop")))
        elif how == "stop_seen_by_original":
            # Stop's own delivery is still queued behind the original decision.
            second.card_rows.append(_control_row(control, answer=_control_response(control, "stop")))
            await second.chat.chat(_decided_answer("accept", card))
            await second.chat.chat(_answer(_control_response(control, "stop")))
        else:
            second.card_rows.append(_control_row(control, cancelled=True))
            await second.chat.chat(_decided_answer("accept", card))
        record = await load_channel_session_record(OPERATION, CHANNEL)
        await second.chat.chat(_request("Is anything pending? Do not run tools.", 2, mode="supervised"))
        return second, record

    second, record = asyncio.run(scenario())
    assert second.effects == []
    stopped = [e for e in second.chat.emissions if e.get("complete_request")
               and "Stopped" in str(e.get("content") or "")]
    assert len(stopped) == 1, second.chat.emissions
    assert not _lifecycle_errors(second.chat), _lifecycle_errors(second.chat)
    assert record["pending"] is None
    # Same-channel conversation continues with no open tool call left behind.
    restored = second.models[-1]
    for channel, messages in restored.state.items():
        if channel.endswith("messages") and isinstance(messages, list):
            answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
            assert not [c["id"] for m in messages if isinstance(m, AIMessage)
                        for c in (m.tool_calls or []) if c["id"] not in answered], channel


def test_startup_repairs_a_lost_offer_and_never_resends_an_answered_control():
    """Crash boundaries: offer saved but not sent; pending; Continue chosen while down."""
    async def scenario():
        _first, card = await _raise_card()
        stored = _input_response("accept", card)
        rows = [_card_row(card, stored)]
        dying = _Process([], card_rows=rows, open_requests=[_open(1)])

        async def lost_send(*_args, **_kwargs):
            raise ConnectionError("process died before the control reached Mythic")

        dying.chat.send_single_choice_request = lost_send
        await _onstart(dying)
        saved = await load_channel_session_record(OPERATION, CHANNEL)
        await _restart(dying)
        repaired = _Process([], card_rows=rows, open_requests=[_open(1)])
        await _onstart(repaired)
        sent = _controls(repaired.chat)
        rows.append(_control_row(sent[0]))
        await _onstart(repaired)  # still pending: nothing is resent
        pending_again = _controls(repaired.chat)
        await _restart(repaired)
        rows[-1] = _control_row(sent[0], answer=_control_response(sent[0], "continue"))
        again = _Process(_after_card_script(), card_rows=rows, open_requests=[_open(1)])
        await _onstart(again)
        fresh = _controls(again.chat)
        rows.append(_control_row(fresh[0], message_id=96))
        await again.chat.chat(_answer(_control_response(sent[0], "continue")))  # the old control
        effects_after_old = list(again.effects)
        await again.chat.chat(_answer(_control_response(fresh[0], "continue", message_id=96)))
        return saved, sent, pending_again, fresh, effects_after_old, again

    saved, sent, pending_again, fresh, effects_after_old, again = asyncio.run(scenario())
    assert saved["continuation"]["status"] == "offered" and saved["continuation"]["n"] == 1
    assert len(sent) == 1 and len(pending_again) == 1
    assert sent[0]["data"]["sage_continuation"]["control_id"] == saved["continuation"]["control_id"]
    assert len(fresh) == 1
    assert fresh[0]["data"]["sage_continuation"]["control_id"] != sent[0]["data"]["sage_continuation"]["control_id"]
    assert effects_after_old == []
    assert again.effects == [{"command": "pwd", "callback_display_id": 5}]
    assert len(_closings(again.chat)) == 1 and not _lifecycle_errors(again.chat)


@pytest.mark.parametrize("alteration", ["other_control", "changed_source", "copied_choice_text"])
def test_an_altered_control_or_changed_saved_decision_never_continues(alteration):
    async def scenario():
        _first, card = await _raise_card()
        second, stored = await _offer(card, "accept", script=[AIMessage(content="Nothing ran.")])
        control = _controls(second.chat)[0]
        answer = _control_response(control, "continue")
        if alteration == "other_control":
            answer.InputRequest["data"]["sage_continuation"]["control_id"] = "f" * 32 + ":1"
        elif alteration == "copied_choice_text":
            answer.InputRequest["choices"][0]["label"] = "Approve"
        else:
            source = next(row for row in second.card_rows
                          if "sage_approval_context" in row["metadata"]["input_requested"]["data"])
            source["metadata"]["input_requested"]["response"]["resolved_at"] = "2026-11-21T00:00:00Z"
        second.card_rows.append(_control_row(control, answer=answer))
        await second.chat.chat(_answer(answer))
        return second

    second = asyncio.run(scenario())
    assert second.effects == []
    if alteration == "changed_source":
        stopped = [e for e in _closings(second.chat) if "could not read" in str(e.get("content") or "")]
        assert len(stopped) == 1
    else:
        assert not _closings(second.chat)
        assert any("already handled" in str(e.get("content") or "") for e in second.chat.emissions)


def test_a_late_duplicate_never_ends_a_request_waiting_on_a_new_card():
    async def scenario():
        _first, card = await _raise_card()
        # The resumed specialist proposes a second guarded call, which raises a new card in R.
        second = _Process([AIMessage(content="pwd done; running it once more."),
                           _call("issue_task_and_waitfor_task_output", PWD, 7)])
        await second.chat.chat(_decided_answer("accept", card))
        before = len(second.chat.emissions)
        await second.chat.chat(_decided_answer("accept", card))
        return second, before

    second, before = asyncio.run(scenario())
    new_cards = _approval_cards(second.chat)
    late = second.chat.emissions[before:]
    assert len(second.effects) == 1
    assert len(new_cards) == 1, "the resumed request must be waiting on a new card"
    assert not [e for e in late if e.get("complete_request")], late
    assert any("already handled" in str(e.get("content") or "") for e in late)


def test_a_card_saved_without_its_request_state_is_retired_with_history():
    async def scenario():
        _first, card = await _raise_card()
        record = await load_channel_session_record(OPERATION, CHANNEL)
        record["pending"].pop("lifecycle")
        await chat_session.save_channel_session_record(record)
        stored = _input_response("accept", card)
        second = _Process([AIMessage(content="Nothing is pending.")],
                          card_rows=[_card_row(card, stored)], open_requests=[_open(1)])
        await _onstart(second)
        closed = list(_closings(second.chat))
        await second.chat.chat(_decided_answer("accept", card, request_id=1))
        return second, closed

    second, closed = asyncio.run(scenario())
    assert second.effects == []
    assert len(closed) == 1 and "without its request state" in closed[0]["content"]


def test_spent_steps_and_denials_survive_a_restored_resume():
    async def scenario():
        first, card = await _raise_card()
        record = await load_channel_session_record(OPERATION, CHANNEL)
        lifecycle = record["pending"]["lifecycle"]
        lifecycle["spent"]["steps"] = 7
        lifecycle["authority"]["denied_action_keys"] = ["issue_task_and_waitfor_task_output:whoami:5"]
        await chat_session.save_channel_session_record(record)
        second = _Process(_after_card_script())
        seen = {}
        original = Model.handle_hitl_resume

        async def observe(self, *args, **kwargs):
            seen.update(steps=self._global_step_count, keys=set(self._denied_action_keys))
            return await original(self, *args, **kwargs)

        Model.handle_hitl_resume = observe
        try:
            await second.chat.chat(_decided_answer("accept", card))
        finally:
            Model.handle_hitl_resume = original
        return seen, second

    seen, second = asyncio.run(scenario())
    assert seen["steps"] == 7
    assert "issue_task_and_waitfor_task_output:whoami:5" in seen["keys"]
    assert len(second.effects) == 1


def test_an_exported_ledger_never_carries_text_bodies():
    from ai.langgraph.request_events import RequestEventLedger

    ledger = RequestEventLedger("chat:41:request:1")
    ledger.record(event_id="operator_input:x", kind="operator_input", phase="received",
                  content="secret-looking operator text")
    exported = ledger.export_state()
    assert exported["events"][0]["content"].startswith("sha256:")
    assert "secret-looking" not in json.dumps(exported)
    tampered = copy.deepcopy(exported)
    tampered["events"][0]["content"] = "raw text"
    with pytest.raises(ValueError):
        RequestEventLedger.from_export(tampered)
    restored = RequestEventLedger.from_export(exported)
    assert [e.record_id for e in restored.events] == [e.record_id for e in ledger.events]


def test_without_on_start_a_stored_decision_is_never_applied():
    """Expectation changed by plan r2: the next-prompt recovery of a stored decision is removed."""
    async def scenario():
        _first, card = await _raise_card()
        stored = _input_response("accept", card)
        second = _Process([AIMessage(content="New request answered.")], card_rows=[_card_row(card, stored)])
        await second.chat.chat(_request("Are you there?", 2, mode="supervised"))
        return second

    second = asyncio.run(scenario())
    assert second.effects == []
    assert not any("recovered" in text for text in _texts(second.chat))


@pytest.mark.parametrize("damage", ["missing_card_row", "changed_action", "pending_card_row"])
def test_a_card_that_cannot_be_reasked_closes_its_request_and_runs_nothing(damage):
    async def scenario():
        _first, card = await _raise_card()
        rows = [] if damage == "missing_card_row" else [_card_row(card, _input_response("accept", card))]
        if damage == "pending_card_row":
            rows[0]["metadata"]["input_requested"]["status"] = "pending"
            rows[0]["metadata"]["input_requested"].pop("response", None)
        if damage == "changed_action":
            record = await load_channel_session_record(OPERATION, CHANNEL)
            record["pending"]["context"]["action_digest"] = "0" * 64
            await chat_session.save_channel_session_record(record)
        second = _Process([AIMessage(content="Nothing is pending.")], card_rows=rows, open_requests=[_open(1)])
        await _onstart(second)
        reconciled = list(second.chat.emissions)
        record = await load_channel_session_record(OPERATION, CHANNEL)
        if damage != "pending_card_row":
            await second.chat.chat(_request("Is anything pending? Do not run tools.", 2, mode="supervised"))
        return second, reconciled, record

    second, reconciled, record = asyncio.run(scenario())
    assert second.effects == []
    assert not [e for e in reconciled if isinstance(e.get("metadata"), dict)
                and e["metadata"].get("special_type") == "input_requested"]
    if damage == "pending_card_row":
        # Still pending in Mythic: left open, so the operator's answer is delivered as before.
        assert reconciled == [] and record["pending"] is not None
        return
    closings = [e for e in reconciled if e.get("complete_request")]
    assert len(closings) == 1 and "was not run" in closings[0]["content"], reconciled
    assert record["pending"] is None
    assert any("not executed" in text for text in _texts(second.chat))


def test_an_open_turn_without_a_card_is_closed_with_a_notice():
    async def scenario():
        first = _Process([AIMessage(content="Noted: amber-seven.")])
        await first.chat.chat(_request(LABEL_PROMPT, 1))
        await _restart(first)
        before = await load_channel_session_record(OPERATION, CHANNEL)
        second = _Process([], open_requests=[_open(2), _open(3, channel=CHANNEL + 1, status="pending")])
        await _onstart(second)
        after = await load_channel_session_record(OPERATION, CHANNEL)
        return second, before, after

    second, before, after = asyncio.run(scenario())
    closings = _closings(second.chat)
    assert [e["response_key"] for e in closings] == ["restoration:2:closed", "restoration:3:closed"]
    assert all("could not finish" in e["content"] for e in closings)
    assert before == after and second.models == [] and second.effects == []


def test_requests_this_process_is_serving_are_never_touched():
    async def scenario():
        # A live session with its card still pending in Mythic (no restart).
        first, card = await _raise_card_without_restart()
        first.open_requests = [_open(1)]
        before = len(first.chat.emissions)
        await _onstart(first)
        live_session = first.chat.emissions[before:]
        await _restart(first)
        # The SDK's live task for the request, then a held channel lock.
        second = _Process([], card_rows=[_card_row(card, _input_response("accept", card))],
                          open_requests=[_open(1)])
        blocker = asyncio.get_running_loop().create_future()
        task = asyncio.ensure_future(blocker)
        ChatBase.chatRequestTasks[1] = task
        try:
            await _onstart(second)
        finally:
            ChatBase.chatRequestTasks.pop(1, None)
            blocker.cancel()
        live_task = list(second.chat.emissions)
        lock = service._CHANNEL_TURN_LOCKS.setdefault(str(CHANNEL), asyncio.Lock())
        async with lock:
            await _onstart(second)
        held_lock = list(second.chat.emissions)
        record = await load_channel_session_record(OPERATION, CHANNEL)
        return live_session, live_task, held_lock, record, card

    live_session, live_task, held_lock, record, card = asyncio.run(scenario())
    assert live_session == [] and live_task == [] and held_lock == []
    assert record["pending"]["context"]["approval_id"] == card["data"]["sage_approval_context"]["approval_id"]


async def _raise_card_without_restart():
    first = _Process(_card_script())
    await first.chat.chat(_request(SUPERVISED_PROMPT, 1, mode="supervised"))
    cards = _cards(first.chat)
    assert len(cards) == 1 and not first.effects
    return first, cards[0]


def test_on_start_reconciliation_never_raises_and_follows_the_watcher():
    async def scenario():
        _first, card = await _raise_card()
        failing = _Process([], open_requests=[_open(1)], onstart_error=RuntimeError("login failed"))
        ok = await _onstart(failing)
        watcher_down = _Process([], card_rows=[_card_row(card, _input_response("accept", card))],
                                open_requests=[_open(1)], watcher_error=RuntimeError("watcher"))
        failed = await _onstart(watcher_down)
        return failing, ok, watcher_down, failed

    failing, ok, watcher_down, failed = asyncio.run(scenario())
    assert failing.watcher_calls == [OPERATION] and failing.chat.emissions == []
    assert ok.EventLogErrorMessage == "" and "/watcher status" in ok.EventLogInfoMessage
    # A Watcher failure keeps its own response and still reconciles this container's requests.
    assert "watcher startup failed" in failed.EventLogErrorMessage
    assert len(_cards(watcher_down.chat)) == 1


@pytest.mark.parametrize("death", ["after_receipt", "before_receipt"])
def test_process_death_after_an_approved_task_is_reconciled_not_resent(death):
    async def scenario():
        first, card = await _raise_card()
        paused_thread = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        effects: list = []
        dying = _Process(_after_card_script(), effects=effects,
                         die_after_effect=True if death == "after_receipt" else "before_receipt",
                         task_records={701: {"status": "completed", "completed": True, "command": "pwd",
                                             "callback_display_id": 5}})
        request = _request("", 2, mode="supervised")
        request.InputResponse = _input_response("accept", card)
        with pytest.raises(_ProcessDeath):
            await dying.chat.chat(request)
        await _restart(dying)
        after = _Process([AIMessage(content="The label question is unrelated; nothing pending.")],
                         effects=effects, task_records=dying.task_records, open_requests=[_open(1)])
        # Mythic still shows the card's request streaming; on-start closes it without resuming.
        await _onstart(after)
        closed = _closings(after.chat)
        claim = (await load_channel_session_record(OPERATION, CHANNEL))["claim"]
        assert len(closed) == 1 and closed[0]["response_key"] == "restoration:1:closed", after.chat.emissions
        assert "will not resend" in closed[0]["content"] and after.models == [] and len(effects) == 1
        assert claim["status"] == "claimed"
        await after.chat.chat(_request("Is anything still running? Do not run tools.", 3, mode="supervised"))
        # The old card cannot be replayed against the retired generation.
        replay = _request("", 4, mode="supervised")
        replay.InputResponse = _input_response("accept", card)
        await after.chat.chat(replay)
        return after, effects, paused_thread

    after, effects, paused_thread = asyncio.run(scenario())
    assert len(effects) == 1
    text = "\n".join(_texts(after.chat))
    assert "will not resend" in text
    if death == "after_receipt":
        assert "Mythic task 701 on callback 5 (`pwd`) is completed" in text
    else:
        assert "unknown" in text
    # Same-channel conversation continues on a new generation whose history has no open tool call.
    restored = after.models[0]
    assert restored._thread_id_override != paused_thread and after.fake.inputs
    for channel, messages in restored.state.items():
        if not (channel.endswith("messages") and isinstance(messages, list)):
            continue
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        open_calls = [call["id"] for m in messages if isinstance(m, AIMessage)
                      for call in (m.tool_calls or []) if call["id"] not in answered]
        assert not open_calls, (channel, open_calls)


def test_retiring_a_generation_closes_open_calls_in_code_without_a_model_call():
    """Synthetic state: an assistant tool call with no result in a restored parent channel."""
    async def scenario():
        model = Model(provider="openai", model="gpt-4o-mini", system_prompt="offline", task_id=0,
                      agent_task_id="retire", config={"configurable": {"api_key": "offline-only"}},
                      operation_id=OPERATION, channel_id=CHANNEL)
        open_call = _call("issue_task_and_waitfor_task_output", PWD, 9)
        open_call.additional_kwargs["_seq"] = 4
        model.state["mythic_operator_messages"] = [HumanMessage(content="Run pwd."), open_call]
        model._message_seq = 5
        closed = model.retire_restored_generation("41:generation:" + "a" * 32, "[closed in code]")
        return model, closed

    model, closed = asyncio.run(scenario())
    assert closed == 1
    closure = model.state["mythic_operator_messages"][-1]
    assert isinstance(closure, ToolMessage) and closure.tool_call_id == "native-steer-9"
    assert closure.additional_kwargs["_restoration_retired"] is True and closure.content == "[closed in code]"
    assert model._thread_id_override == "41:generation:" + "a" * 32


def test_mode_override_survives_restart():
    async def scenario():
        first = _Process([AIMessage(content="Noted: amber-seven.")])
        await first.chat.chat(_request(LABEL_PROMPT, 1))
        mode = _request("", 2)
        mode.SlashCommand = type("Slash", (), {"Name": "mode", "Argument": "supervised"})()
        await first.chat.chat(mode)
        await _restart(first)
        second = _Process([AIMessage(content="amber-seven")])
        await second.chat.chat(_request(RECALL_PROMPT, 3))
        return second

    second = asyncio.run(scenario())
    assert second.models[0].mode == "supervised"
    assert second.models[0]._chat_mode_override == "supervised"


def test_intentionally_dropped_session_still_requires_a_new_chat():
    async def scenario():
        first = _Process([AIMessage(content="Noted: amber-seven.")])
        await first.chat.chat(_request(LABEL_PROMPT, 1))
        await chat_session.drop_channel_session(_request("", 1), reason="runtime_error")
        await _restart(first)
        second = _Process([AIMessage(content="unused")])
        await second.chat.chat(_request(RECALL_PROMPT, 2))
        return second

    second = asyncio.run(scenario())
    assert any(REFUSAL in text for text in _texts(second.chat))
    assert not second.fake.inputs


@pytest.mark.parametrize("damage", ["corrupt", "config"])
def test_unrestorable_saved_state_gets_a_specific_explanation(damage):
    async def scenario():
        first = _Process([AIMessage(content="Noted: amber-seven.")])
        await first.chat.chat(_request(LABEL_PROMPT, 1))
        await _restart(first)
        if damage == "corrupt":
            with sqlite3.connect(chat_session.channel_session_db_path()) as db:
                db.execute("UPDATE sage_chat_channel_sessions SET payload='{' WHERE channel_id=?", (CHANNEL,))
        second = _Process([AIMessage(content="unused")])
        request = _request(RECALL_PROMPT, 2)
        if damage == "config":
            request.Config = {"mode": "conversation", "max_steps": 17}
        await second.chat.chat(request)
        return second

    second = asyncio.run(scenario())
    text = "\n".join(_texts(second.chat))
    assert ("could not be restored" if damage == "corrupt" else "no longer matches its current") in text
    assert not second.fake.inputs and not second.effects


def test_legacy_chat_is_adopted_only_when_mythic_rows_prove_its_generation():
    async def scenario(matching: bool):
        first = _Process([AIMessage(content="Noted: amber-seven.")])
        await first.chat.chat(_request(LABEL_PROMPT, 1))
        thread_id = (await load_channel_session_record(OPERATION, CHANNEL))["thread_id"]
        await _restart(first)
        # A chat from before this change has a marker and checkpoints but no channel record.
        with sqlite3.connect(chat_session.channel_session_db_path()) as db:
            db.execute("DELETE FROM sage_chat_channel_sessions")
        rows = {1: {"id": 1, "channel_id": CHANNEL, "operation_id": OPERATION,
                    "request_message": {"id": 10, "message": LABEL_PROMPT if matching else "something else"}}}
        second = _Process([AIMessage(content="You asked me to remember amber-seven.")], request_rows=rows)
        await second.chat.chat(_request(RECALL_PROMPT, 2))
        return second, thread_id

    adopted, thread_id = asyncio.run(scenario(True))
    assert adopted.models and adopted.models[0]._thread_id_override == thread_id
    assert any("amber-seven" in text for text in _human_texts(adopted.fake.inputs[-1]))
    refused, _ = asyncio.run(scenario(False))
    assert any("could not be matched with certainty" in text for text in _texts(refused.chat))
    assert not refused.fake.inputs


@pytest.mark.parametrize("approved", [True, False])
def test_the_real_sink_saves_an_approved_tasks_receipt_before_waiting_for_output(approved):
    """Real `issue_task_and_waitfor_task_output`; Mythic issuance/output are the existing stand-ins."""
    from unittest.mock import AsyncMock, patch
    from types import SimpleNamespace
    from tests.test_supervised_task_observation import _make_tools, _split_issue

    mt = _make_tools()
    mt._request_contract = SimpleNamespace(request_id="request-one", digest="contract-one")
    if approved:
        mt._active_approval_claim = {"request_id": "request-one", "request_contract_digest": "contract-one",
                                     "approval_id": "approval-one"}
    receipts: list[dict] = []
    seen_before_output: list[int] = []

    async def receipt_sink(receipt):
        receipts.append(receipt)

    mt.set_approval_receipt_sink(receipt_sink)

    def output():
        seen_before_output.append(len(receipts))
        return "C:\\Users\\offline"

    with _split_issue(output), \
         patch.object(mt, "_turn_authority_issue_blocker", return_value=""), \
         patch.object(mt, "_bind_contract_task_issue_parameters", return_value=""), \
         patch.object(mt, "_record_contract_task_terminal"), \
         patch.object(mt, "_supervised_task_state", AsyncMock(return_value={
             "task_completed": True, "task_status": "completed"})):
        asyncio.run(mt.issue_task_and_waitfor_task_output("pwd", "", 11))
    if approved:
        assert receipts == [{"approval_id": "approval-one", "task_display_id": 4242,
                             "callback_display_id": 11, "command": "pwd"}]
        assert seen_before_output == [1]
    else:
        assert receipts == []


# --- The restored graph must contain the paused step, for the chat's current mode and tool access ---


def _mode_request(request_id: int, mode: str):
    request = _request("", request_id)
    request.SlashCommand = type("Slash", (), {"Name": "mode", "Argument": mode})()
    return request


MODE_CARD_REQUEST = 3


async def _raise_mode_card(*, restart: bool = True):
    """Live request 31's shape: a conversation-configured chat switched by `/mode supervised` raises
    the card in its third request."""
    first = _Process([AIMessage(content="Noted: amber-seven.")] + _card_script())
    await first.chat.chat(_request(LABEL_PROMPT, 1))
    await first.chat.chat(_mode_request(2, "supervised"))
    await first.chat.chat(_request(SUPERVISED_PROMPT, MODE_CARD_REQUEST))
    cards = _cards(first.chat)
    assert len(cards) == 1 and not first.effects
    assert (await load_channel_session_record(OPERATION, CHANNEL))["mode_override"] == "supervised"
    if restart:
        await _restart(first)
    return first, cards[0]


async def _deliver_after_restart(card, *, request_id: int, config_mode: str, delivery: str, script,
                                 decided: str = "accept", withhold_task_tool: bool = False):
    """Deliver the decision to a new process: as the original answer, or through Continue after
    on-start offered the decision Mythic saved while Sage was down."""
    if delivery == "original":
        process = _Process(script, withhold_task_tool=withhold_task_tool)
        request = _request("", request_id, mode=config_mode)
        request.InputResponse = _input_response(decided, card)
    else:
        stored = _input_response(decided, card)
        process = _Process(script, card_rows=[_card_row(card, stored)], open_requests=[_open(request_id)],
                           withhold_task_tool=withhold_task_tool)
        await _onstart(process)
        control = _controls(process.chat)[0]
        answer = _control_response(control, "continue")
        process.card_rows.append(_control_row(control, answer=answer))
        request = _request("", request_id, mode=config_mode)
        request.InputResponse = answer
    await process.chat.chat(request)
    return process


def _terminal_phases(ledger, kind: str) -> dict[str, list[str]]:
    starts = {"tool": "started", "delegation": "opened"}[kind]
    return {
        event.event_id: [e.phase for e in ledger.actual_events(event_id=event.event_id, kind=kind)
                         if e.phase != starts]
        for event in ledger.actual_events(kind=kind, phase=starts)
    }


@pytest.mark.parametrize("delivery", ["original", "continue"])
def test_a_mode_supervised_card_runs_its_approved_action_once_after_restart(delivery):
    """Live request 31 (2026-11-20): the accept was claimed and the request ended `complete` with no
    Mythic task, because the restored graph was compiled for the configured mode."""
    async def scenario():
        _first, card = await _raise_mode_card()
        return await _deliver_after_restart(card, request_id=MODE_CARD_REQUEST, config_mode="conversation",
                                            delivery=delivery, script=_after_card_script())

    process = asyncio.run(scenario())
    assert process.effects == [{"command": "pwd", "callback_display_id": 5}]
    closings = _closings(process.chat)
    assert len(closings) == 1 and closings[0]["kind"] == "complete", closings
    assert not _lifecycle_errors(process.chat)
    ledger = process.models[-1]._request_event_ledger
    assert ledger.reconcile()["ok"]
    assert all(phases == ["completed"] for phases in _terminal_phases(ledger, "tool").values())


MODE_CHANGED_NOTICE = "chat's mode or tool access no longer matches the card"


@pytest.mark.parametrize("delivery", ["original", "continue"])
def test_a_card_whose_mode_changed_after_it_was_raised_is_refused_after_restart(delivery):
    """Synthetic record write: `/mode conversation` saved after the card, as `/mode` writes it. Mythic's
    request lock keeps a real `/mode` from reaching Sage while the card's request is open; the
    live-reachable twin is a `/mode supervised` whose save failed (`required=False`)."""
    async def scenario():
        _first, card = await _raise_card()
        record = await load_channel_session_record(OPERATION, CHANNEL)
        record.update(mode_override="conversation", mode_override_base_signature=record["config_signature"],
                      mode_override_base_autonomous_solve=bool(record["base_autonomous_solve"]))
        await chat_session.save_channel_session_record(record)
        process = await _deliver_after_restart(card, request_id=1, config_mode="supervised", delivery=delivery,
                                               script=_after_card_script())
        after = await load_channel_session_record(OPERATION, CHANNEL)
        return process, record, after

    process, before, after = asyncio.run(scenario())
    assert process.effects == []
    closings = _closings(process.chat)
    assert len(closings) == 1 and closings[0]["kind"] == "complete", closings
    assert MODE_CHANGED_NOTICE in closings[0]["content"] and "not executed" in closings[0]["content"]
    assert not _lifecycle_errors(process.chat)
    # Nothing was claimed, and the paused work was retired into a new generation.
    assert after["claim"] is None and after["pending"] is None
    assert after["thread_id"] != before["thread_id"]
    ledger = process.models[-1]._request_event_ledger
    assert ledger.reconcile()["ok"]
    for kind in ("tool", "delegation"):
        phases = _terminal_phases(ledger, kind)
        assert phases and all(p == ["stopped"] for p in phases.values()), (kind, phases)


def test_a_restored_approved_call_that_never_runs_ends_the_request_as_an_error():
    async def scenario():
        _first, card = await _raise_card()
        return await _deliver_after_restart(card, request_id=1, config_mode="supervised", delivery="original",
                                            script=_after_card_script(), withhold_task_tool=True)

    process = asyncio.run(scenario())
    assert process.effects == []
    closings = _closings(process.chat)
    assert len(closings) == 1 and closings[0]["kind"] == "error", closings
    text = "\n".join(_texts(process.chat)) + "\n".join(str(c.get("error") or "") for c in closings)
    assert "`issue_task_and_waitfor_task_output` did not run after the restart" in text, text


_REJECT_SCRIPT = [AIMessage(content="Not run."), AIMessage(content="The operator rejected pwd; nothing ran.")]
# (how supervised mode was set, the card's request, the chat's configured mode)
_SOURCES = {"configured": (1, "supervised"), "mode": (MODE_CARD_REQUEST, "conversation")}


def _outcome(process, since: int = 0) -> dict:
    ledger = process.models[-1]._request_event_ledger
    return {
        "effects": len(process.effects),
        "terminal": [e["kind"] for e in process.chat.emissions[since:] if e.get("complete_request")],
        "tool": _terminal_phases(ledger, "tool"),
        "delegation": _terminal_phases(ledger, "delegation"),
        "reconciled": ledger.reconcile()["ok"],
    }


@pytest.mark.parametrize("delivery", ["original", "continue"])
@pytest.mark.parametrize("decided", ["accept", "reject"])
@pytest.mark.parametrize("source", ["configured", "mode"])
def test_a_restored_resume_ends_like_the_same_card_resumed_in_process(source, decided, delivery,
                                                                      tmp_path, monkeypatch):
    request_id, config_mode = _SOURCES[source]
    script = _after_card_script() if decided == "accept" else _REJECT_SCRIPT

    async def in_process():
        first, card = await (_raise_card_without_restart() if source == "configured"
                             else _raise_mode_card(restart=False))
        first.fake.messages = iter(list(script))
        since = len(first.chat.emissions)
        request = _request("", request_id, mode=config_mode)
        request.InputResponse = _input_response(decided, card)
        await first.chat.chat(request)
        return _outcome(first, since)

    async def restored():
        _first, card = await (_raise_card() if source == "configured" else _raise_mode_card())
        process = await _deliver_after_restart(card, request_id=request_id, config_mode=config_mode,
                                               delivery=delivery, script=list(script), decided=decided)
        return _outcome(process)

    expected = asyncio.run(in_process())
    asyncio.run(_restart(_PROCESSES[-1]))
    # The restored run starts from an empty runtime DB of its own: same channel, no shared rows.
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(tmp_path / "restored-run.db"))
    monkeypatch.setattr(chat_session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "restored-run.db"))
    actual = asyncio.run(restored())
    assert expected["effects"] == (1 if decided == "accept" else 0)
    assert expected["terminal"] == ["complete"] and expected["reconciled"]
    assert actual == expected


@pytest.mark.parametrize("order", ["original_first", "offer_first"])
@pytest.mark.parametrize("source,decided", [("mode", "accept"), ("configured", "reject")])
def test_a_queued_decision_applies_once_in_either_delivery_order(source, decided, order):
    """Live segments queued_accept (the `/mode` chat) and queued_reject (configured supervised): the
    decision was saved in Mythic while Sage was down and is delivered after it boots, either before
    on-start runs or after on-start has offered Continue/Stop for it."""
    request_id, config_mode = _SOURCES[source]
    script = _after_card_script() if decided == "accept" else _REJECT_SCRIPT

    async def scenario():
        _first, card = await (_raise_card() if source == "configured" else _raise_mode_card())
        stored = _input_response(decided, card)
        process = _Process(list(script), card_rows=[_card_row(card, stored)], open_requests=[_open(request_id)])
        if order == "offer_first":
            await _onstart(process)
            process.card_rows.append(_control_row(_controls(process.chat)[0]))
        request = _request("", request_id, mode=config_mode)
        request.InputResponse = _input_response(decided, card)
        await process.chat.chat(request)
        return process

    process = asyncio.run(scenario())
    assert len(process.effects) == (1 if decided == "accept" else 0)
    closings = _closings(process.chat)
    assert len(closings) == 1 and closings[0]["kind"] == "complete", closings
    assert not _lifecycle_errors(process.chat)
    assert process.models[-1]._request_event_ledger.reconcile()["ok"]
    offered = {c["data"]["sage_continuation"]["control_id"] for c in _controls(process.chat)}
    assert len(offered) == (1 if order == "offer_first" else 0)
    if order == "offer_first":
        key = next(e["response_key"] for e in process.chat.emissions
                   if (e.get("metadata") or {}).get("input_requested", {}).get("data", {}).get("sage_continuation"))
        assert any(e["response_key"] == key and e["metadata"]["input_requested"]["status"] == "cancelled"
                   for e in process.chat.emissions), "the unused Continue/Stop card must be retired"


_BATTERY = json.loads((Path(__file__).parent / "fixtures" / "model_input_boundaries"
                       / "approval_reask_cards.json").read_text())
_RESTORED_GRAPH_ROWS = [row for row in _BATTERY["rows"] if row["kind"] == "restored_graph"]


def _paused_checkpoint_structure(thread_id: str) -> dict:
    """The paused checkpoint's structure, read from this test's runtime DB the way the recording was."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    with sqlite3.connect(chat_session.channel_session_db_path()) as db:
        def interrupted(namespace_like):
            ns, cp = db.execute(
                "SELECT w.checkpoint_ns, w.checkpoint_id FROM writes w WHERE w.thread_id=? AND w.checkpoint_ns LIKE ? "
                "AND w.channel='__interrupt__' ORDER BY w.checkpoint_id DESC LIMIT 1", (thread_id, namespace_like)).fetchone()
            typ, blob = db.execute("SELECT type, checkpoint FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? "
                                   "AND checkpoint_id=?", (thread_id, ns, cp)).fetchone()
            values = serde.loads_typed((typ, blob)).get("channel_values") or {}
            writes = [(task, serde.loads_typed((t, v))) for task, t, v in db.execute(
                "SELECT task_id, type, value FROM writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=? "
                "AND channel='__interrupt__'", (thread_id, ns, cp))]
            [(task_id, [item])] = writes
            return ns, values, task_id, item

        _ns, root_values, root_task, root_item = interrupted("")
        worker_ns, worker_values, _task, worker_item = interrupted("%:%")
    last = worker_values["messages"][-1]
    return {
        "root_pending_branches": sorted(k for k in root_values if k.startswith("branch:to:")),
        "root_task_is_worker_namespace": worker_ns.split(":", 1)[1] == root_task,
        "worker_namespace": worker_ns.split(":", 1)[0],
        "worker_pending_branches": sorted(k for k in worker_values if k.startswith("branch:to:")),
        "last_message_type": type(last).__name__,
        "tool_calls": [{"name": c["name"], "args": c["args"]} for c in last.tool_calls],
        "same_interrupt": root_item.id == worker_item.id and root_item.value == worker_item.value,
        "interrupt_value": root_item.value,
    }


def _shape(value):
    """Keys and types, not values: the recorded callback is 2, the regenerated one 5."""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    return type(value).__name__


@pytest.mark.parametrize("override", ["recorded", "missing"])
@pytest.mark.parametrize("row", _RESTORED_GRAPH_ROWS, ids=[row["id"] for row in _RESTORED_GRAPH_ROWS])
def test_recorded_mode_card_regenerates_its_checkpoint_and_resumes_once(row, override):
    """Battery row: live request 31's checkpoint structure, card 499 and the record's `/mode` fields.

    The checkpoint is regenerated through the real graph (scripted model: wiring only) and must match
    the recording's structure; it then replays through restore, refresh, check, resume and terminal.
    Near-match: the same chat without its saved `/mode` override refuses and claims nothing.
    """
    recorded_mode = row["record_mode"]
    card_response = row["card_row"]["metadata"]["input_requested"]["response"]
    assert row["dispatch_action"] == card_response["action"] == "accept"

    async def scenario():
        first, card = await _raise_mode_card(restart=False)
        record = await load_channel_session_record(OPERATION, CHANNEL)
        structure = _paused_checkpoint_structure(record["thread_id"])
        await _restart(first)
        if override == "missing":
            record.update(mode_override="", mode_override_base_signature="",
                          mode_override_base_autonomous_solve=None)
            await chat_session.save_channel_session_record(record)
        process = await _deliver_after_restart(card, request_id=MODE_CARD_REQUEST,
                                               config_mode=recorded_mode["configured_mode"],
                                               delivery="original", script=_after_card_script(),
                                               decided=card_response["action"])
        after = await load_channel_session_record(OPERATION, CHANNEL)
        return record, structure, process, after

    record, structure, process, after = asyncio.run(scenario())
    # The regenerated chat and checkpoint have the recorded structure.
    assert recorded_mode["configured_mode"] == "conversation"
    if override == "recorded":
        assert record["mode_override"] == recorded_mode["mode_override"] == "supervised"
        assert record["mode_override_base_autonomous_solve"] == recorded_mode["mode_override_base_autonomous_solve"]
        assert record["base_autonomous_solve"] == recorded_mode["base_autonomous_solve"]
        assert (record["mode_override_base_signature"] == record["config_signature"]) is \
            recorded_mode["override_base_signature_matches_config"]
    root, worker = row["root_checkpoint"], row["worker_checkpoint"]
    assert structure["root_pending_branches"] == root["pending_branches"]
    assert structure["root_task_is_worker_namespace"] is root["interrupt_task_is_worker_namespace"]
    assert structure["worker_namespace"] == worker["namespace"]
    assert structure["worker_pending_branches"] == worker["pending_branches"]
    assert structure["last_message_type"] == worker["last_message_type"]
    assert structure["same_interrupt"] is worker["same_interrupt_as_root"]
    assert _shape(structure["tool_calls"]) == _shape(worker["tool_calls"])
    assert [c["name"] for c in structure["tool_calls"]] == [c["name"] for c in worker["tool_calls"]]
    recorded_value, value = root["interrupt_value"], structure["interrupt_value"]
    assert value["review_configs"] == recorded_value["review_configs"]
    assert [a["name"] for a in value["action_requests"]] == [a["name"] for a in recorded_value["action_requests"]]
    assert _shape(value["action_requests"]) == _shape(recorded_value["action_requests"])
    # Replay through the restored entry.
    closings = _closings(process.chat)
    assert len(closings) == 1 and closings[0]["kind"] == row["expected"]["terminal"], closings
    assert process.models[-1]._request_event_ledger.reconcile()["ok"]
    if override == "recorded":
        assert len(process.effects) == row["expected"]["effects"]
        assert after["claim"]["status"] == "consumed" and after["claim"]["decision"] == "approve"
    else:
        assert row["expected"]["near_match_without_override"] == "refused"
        assert process.effects == [] and after["claim"] is None
        assert MODE_CHANGED_NOTICE in closings[0]["content"]
