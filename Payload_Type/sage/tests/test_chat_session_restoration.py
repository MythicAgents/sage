"""An existing native chat continues after a Sage process restart.

Before this change, a channel whose Model was lost with the process was refused: "Sage restarted or
this channel's session was replaced". These cases drive the real HeadlessSageChat handler, the real
Model and LangGraph graph over the real file-backed ``SageAsyncSqliteSaver``, the real channel
record and the real approval validator. A "restart" closes the first process's Model and its
SQLite connection, clears every process-local registry, and serves the next request from a new
SageChat. Only the chat model (scripted: wiring only, not model choice), Mythic task issuance and
Mythic GraphQL rows are stand-ins; each stand-in row is labelled synthetic where it is built.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

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
                 task_records=None):
        _PROCESSES.append(self)
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

        self.chat = _Chat()

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
            [issue_task_and_waitfor_task_output] if "issue_task_and_waitfor_task_output" in names else [])
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
        if "chat_message" in query:
            wanted = variables["ctx"]["input_requested"]["data"]["sage_approval_context"]["approval_id"]
            return {"chat_message": [
                row for row in self.card_rows
                if row["metadata"]["input_requested"]["data"]["sage_approval_context"]["approval_id"] == wanted
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


def test_a_decision_made_while_sage_was_down_is_recovered_on_the_next_request():
    async def scenario():
        _first, card = await _raise_card()
        stored = _input_response("accept", card)
        second = _Process(_after_card_script(), card_rows=[_card_row(card, stored)])
        await second.chat.chat(_request("Are you there?", 2, mode="supervised"))
        return second

    second = asyncio.run(scenario())
    assert second.effects == [{"command": "pwd", "callback_display_id": 5}]
    assert any("decision was recovered" in text for text in _texts(second.chat))


def test_a_changed_stored_decision_is_not_applied():
    async def scenario():
        _first, card = await _raise_card()
        stored = _input_response("accept", card)
        tampered = copy.deepcopy(stored.InputRequest)
        tampered["data"]["sage_approval_context"]["thread_id"] = "41:generation:" + "0" * 32
        stored.InputRequest = tampered
        row = _card_row(card, stored)
        row["metadata"]["input_requested"]["data"]["sage_approval_context"]["approval_id"] = (
            card["data"]["sage_approval_context"]["approval_id"])
        second = _Process([AIMessage(content="New request answered.")], card_rows=[row])
        await second.chat.chat(_request("Are you there?", 2, mode="supervised"))
        return second

    second = asyncio.run(scenario())
    assert second.effects == []
    assert not any("decision was recovered" in text for text in _texts(second.chat))


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
                         effects=effects, task_records=dying.task_records)
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
