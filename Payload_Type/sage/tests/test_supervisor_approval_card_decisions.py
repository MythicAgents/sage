"""Operator decisions on a Supervisor-level approval card, through the real chat service, graph and HITL path.

Real: `SageChat.chat` (via the headless `DriverChat`), the supervised graph and its `HumanInTheLoopMiddleware`, the
card emitter, Mythic-echo validation, the single durable claim, resume, and the SDK cancel dispatch wrapped by
Sage's paused-request handler. Substituted: the model (a scripted Supervisor that proposes one tool call and then
closes; it proves wiring, not model choice) and Mythic's transport (the headless recorder; the card echo is
`input_response`, built as Mythic d8a94943 resolves a card).

No Supervisor tool is guarded on this branch. As in the plan's live check, the test adds the existing read-only
`read_engagement_state` to the shared `GUARDED_TOOLS` registry before the graph is built, and counts entry to that
reader (`engagement_ledger.validate_reader_selection`, called only by the reader) independently of chat state.
Every decision is dispatched on the original request id, as Mythic dispatches an input response.
"""

import asyncio
import importlib.util
import itertools
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from mythic_container import ChatBase, chat_utils
from mythic_container.ChatBase import ChatRequest

from ai.langgraph import engagement_ledger
from ai.langgraph.model import Model
from ai.langgraph.mythic_tools import GUARDED_TOOLS, MythicTools
from sage_chat import session
from sage_chat.cancel_adapter import install_paused_cancel_handler


SCRIPT = (Path(__file__).resolve().parents[3] / "skills" /
          "sage-focused-capability-tests/scripts/run_offline_human_led_case.py")
spec = importlib.util.spec_from_file_location("supervisor_card_driver", SCRIPT)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

READER = "read_engagement_state"
PROMPT = "Read the empty test ledger once."
CLOSING = "The ledger read is finished."
STEER = "Do not read the ledger; summarize what you already know."
OPERATION_ID, CHANNEL_ID, REQUEST_ID, APITOKEN_ID = 4, 9, 125, 74
CONFIG = {"mode": "supervised", "autonomous_solve": False}


def _request(prompt=""):
    return ChatRequest(container_name="sage", operation_id=OPERATION_ID, channel_id=CHANNEL_ID,
                       request_id=REQUEST_ID, apitokens_id=APITOKEN_ID, model="Sage", prompt=prompt,
                       config=dict(CONFIG), secrets={})


def _cancel_message():
    return json.dumps({"container_name": "sage", "operation_id": OPERATION_ID,
                       "channel_id": CHANNEL_ID, "request_id": REQUEST_ID}).encode()


class Episode:
    """One supervised channel whose Supervisor proposes the guarded reader exactly once."""

    def __init__(self, monkeypatch, tmp_path):
        monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
        for name in ("connect", "connect_ex"):
            monkeypatch.setattr(f"socket.socket.{name}", lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("offline test attempted network access")))
        self.reads = 0
        real_validate = engagement_ledger.validate_reader_selection

        def counted_validate(*args, **kwargs):
            self.reads += 1
            return real_validate(*args, **kwargs)

        monkeypatch.setattr(engagement_ledger, "validate_reader_selection", counted_validate)
        self.model_inputs: list[list] = []

        def supervisor(messages):
            self.model_inputs.append(list(messages))
            if any(isinstance(message, ToolMessage) for message in messages):
                return AIMessage(content=CLOSING)
            return driver.call(READER, {}, 1)

        self.fake = driver.ScriptedModel(messages=iter(itertools.repeat(supervisor)))
        self.claims: list[dict] = []
        self.responses: list = []

        async def capture_rpc(response):
            self.responses.append(response)

        monkeypatch.setattr(ChatBase, "SendMythicRPCChatResponse", capture_rpc)
        monkeypatch.setattr(ChatBase, "chatRequestTasks", {})
        monkeypatch.setattr(ChatBase, "chatCancelledRequests", set())
        install_paused_cancel_handler()

    async def build(self):
        # Model construction opens its checkpointer on the running loop.
        model = Model(
            provider="openai", model="offline-model", system_prompt="offline test",
            config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
            task_id=0, agent_task_id="offline-supervisor-card", mode="supervised", autonomous_solve=False,
            operation_id=OPERATION_ID, channel_id=CHANNEL_ID, apitoken_id=APITOKEN_ID,
        )
        model.mythic_client = MythicTools(agent_task_id="offline-supervisor-card",
                                          operation_id=OPERATION_ID, channel_id=CHANNEL_ID)
        model._payload_names = []
        model.memory = InMemorySaver()
        model._get_base_chat_model = lambda *args, **kwargs: self.fake
        model._native_chat_explicit_hitl = True
        model.command_name = "chat"
        model._rebuild_graph()
        self.model = model
        self.chat = driver.DriverChat(model)
        ChatBase.chatServices["sage"] = self.chat
        original_update = self.chat._update_session_record

        async def record_claims(*args, **kwargs):
            if kwargs.get("claim"):
                self.claims.append(dict(kwargs["claim"]))
            return await original_update(*args, **kwargs)

        self.chat._update_session_record = record_claims
        return self

    async def open_card(self):
        first = _request(PROMPT)
        session.bind_channel_thread_id(first, self.model)
        await self.chat.chat(first)
        self.chat._used = True
        before = list(self.chat.emissions)
        # The card is the request's first and only row before a decision: Mythic leaves the parent pending.
        assert len(before) == 1, before
        assert (before[0].get("metadata") or {}).get("special_type") == "input_requested"
        assert before[0]["complete_request"] is False
        card = before[0]["metadata"]["input_requested"]
        assert card["input_type"] == "approval"
        assert card["data"]["sage_approval_context"]["tool_name"] == READER
        assert self.reads == 0
        assert self.model._hitl_card_pending is True
        return card

    def decision(self, card, action, response=""):
        request = _request()
        request.InputResponse = driver.input_response(action, card, response=response)
        return request

    def tool_finished(self):
        return [e for e in self.chat.emissions
                if (e.get("metadata") or {}).get("special_type") == "tool_use"
                and e.get("status") == "complete" and READER in (e.get("content") or "")]

    def terminals(self):
        return self.chat.terminal_emissions

    def cancelled(self):
        recorded = [e for e in self.chat.emissions if e.get("status") == "cancelled"]
        return recorded + [r for r in self.responses if getattr(r, "Status", "") == "cancelled"]

    async def close(self):
        await session.drop_channel_session(_request())


@pytest.fixture
def episode(monkeypatch, tmp_path):
    assert READER not in GUARDED_TOOLS, "this branch guards no Supervisor tool; the test guard must be its own"
    GUARDED_TOOLS.add(READER)
    services = dict(ChatBase.chatServices)
    try:
        yield Episode(monkeypatch, tmp_path)
    finally:
        GUARDED_TOOLS.discard(READER)
        ChatBase.chatServices.clear()
        ChatBase.chatServices.update(services)


def _run(episode, body):
    async def scenario():
        await episode.build()
        try:
            await body()
        finally:
            await episode.close()
    asyncio.run(scenario())


def test_accept_runs_the_reader_once_and_completes_the_request(episode):
    async def body():
        card = await episode.open_card()
        await episode.chat.chat(episode.decision(card, "accept"))
        assert episode.reads == 1
        assert len(episode.tool_finished()) == 1
        assert [c["decision"] for c in episode.claims] == ["approve"]
        terminals = episode.terminals()
        assert len(terminals) == 1 and terminals[0]["content"].strip() == CLOSING
        assert episode.model._pending_approval_context is None
    _run(episode, body)


@pytest.mark.parametrize("action", ["reject", "respond"])
def test_reject_or_steer_never_runs_the_reader(episode, action):
    async def body():
        card = await episode.open_card()
        await episode.chat.chat(episode.decision(card, action, response=STEER if action == "respond" else ""))
        assert episode.reads == 0
        assert episode.tool_finished() == []
        assert [c["decision"] for c in episode.claims] == ["deny"]
        assert len(episode.terminals()) == 1
        resumed = [str(m.content) for call in episode.model_inputs[1:] for m in call]
        assert any(f"User rejected the tool call for `{READER}`" in text for text in resumed)
        if action == "respond":
            assert any(STEER in text for text in resumed)
    _run(episode, body)


@pytest.mark.parametrize("second", ["accept", "reject"])
def test_a_replayed_decision_loses_the_claim(episode, second):
    async def body():
        card = await episode.open_card()
        await episode.chat.chat(episode.decision(card, "accept"))
        await episode.chat.chat(episode.decision(card, second))
        assert episode.reads == 1
        assert len(episode.tool_finished()) == 1
        assert [c["decision"] for c in episode.claims] == ["approve"]
    _run(episode, body)


@pytest.mark.parametrize(("first", "second"), [("accept", "accept"), ("accept", "reject"), ("reject", "accept")])
def test_concurrent_decisions_claim_once(episode, first, second):
    async def body():
        card = await episode.open_card()
        await asyncio.gather(episode.chat.chat(episode.decision(card, first)),
                             episode.chat.chat(episode.decision(card, second)))
        assert len(episode.claims) == 1
        expected_reads = 1 if episode.claims[0]["decision"] == "approve" else 0
        assert episode.reads == expected_reads
        assert len(episode.tool_finished()) == expected_reads
    _run(episode, body)


def test_cancel_retires_the_card_before_a_late_accept(episode):
    async def body():
        card = await episode.open_card()
        await chat_utils.ChatCancelHandler(_cancel_message())
        assert len(episode.cancelled()) == 1
        assert episode.model._pending_approval_context is None
        assert episode.model._hitl_card_pending is False
        await episode.chat.chat(episode.decision(card, "accept"))
        assert episode.reads == 0
        assert episode.claims == []
        assert episode.tool_finished() == []
    _run(episode, body)


def test_cancel_after_accept_is_a_no_op(episode):
    async def body():
        card = await episode.open_card()
        await episode.chat.chat(episode.decision(card, "accept"))
        emitted = len(episode.chat.emissions)
        await chat_utils.ChatCancelHandler(_cancel_message())
        assert episode.reads == 1
        assert episode.cancelled() == []
        assert len(episode.chat.emissions) == emitted
    _run(episode, body)
