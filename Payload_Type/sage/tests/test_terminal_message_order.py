"""The request's final message is created after everything else the request emitted.

Mythic orders a transcript by row creation. The terminal used to finalize the last main-window block in
place, so when progress messages, tool cards or sub-agent panels were created after that block, the
request's checkpoint appeared above them (supervised run, channel 5, request 4).
"""

import asyncio

from sage_chat.headless import HeadlessSageChat, build_chat_request
from sage_chat.streaming import ChatStreamEmitter


class _Recorder:
    def __init__(self):
        self.sent = []

    async def send_text(self, request, response_key, content="", metadata=None):
        self.sent.append(response_key)

    async def send_response(self, request, response_key, content="", **kwargs):
        self.sent.append(response_key)


def _emitter():
    return ChatStreamEmitter(_Recorder(), build_chat_request("x", channel_id=5, request_id=4))


def test_block_that_is_still_the_newest_row_is_reused():
    emitter = _emitter()

    async def run():
        await emitter.emit_tool_use(tool_call_id="c1", tool_name="list_callbacks", tool_source="mythic",
                                    status="finished", content="done", complete=True)
        await emitter("final report")

    asyncio.run(run())
    assert emitter.reusable_terminal_key() == "assistant:4:1"


def test_block_followed_by_a_new_card_or_panel_is_not_reused():
    for later in ("tool", "subagent", "agent_text", "final_event"):
        emitter = _emitter()

        async def run():
            await emitter("Waiting for propagation")
            if later == "tool":
                await emitter.emit_tool_use(tool_call_id="c1", tool_name="list_callbacks",
                                            tool_source="mythic", status="started", content="",
                                            complete=False)
            elif later == "subagent":
                await emitter.emit_subagent_status(title="Mythic_Operator", delegation_id="d1",
                                                   delegation_name="Mythic_Operator")
            elif later == "agent_text":
                await emitter.emit_agent_text(content="text", delegation_id="d1",
                                              delegation_name="Mythic_Operator")
            else:
                await emitter.emit_final_response(event_id="final-1", content="final")

        asyncio.run(run())
        assert emitter.reusable_terminal_key() == "", later


def test_updating_an_earlier_card_in_place_does_not_block_reuse():
    emitter = _emitter()

    async def run():
        await emitter.emit_tool_use(tool_call_id="c1", tool_name="list_callbacks", tool_source="mythic",
                                    status="started", content="", complete=False)
        await emitter("Final answer.")
        # The same card finishing updates its existing row; it creates nothing new.
        await emitter.emit_tool_use(tool_call_id="c1", tool_name="list_callbacks", tool_source="mythic",
                                    status="finished", content="done", complete=True)

    asyncio.run(run())
    assert emitter.reusable_terminal_key() == "assistant:4:1"


def test_closing_a_panel_with_content_counts_as_a_new_row():
    emitter = _emitter()

    async def run():
        await emitter.emit_subagent_status(title="Mythic_Operator", delegation_id="d1",
                                           delegation_name="Mythic_Operator")
        await emitter("block")
        await emitter.emit_subagent_status(title="Mythic_Operator", delegation_id="d1",
                                           delegation_name="Mythic_Operator", status="finished",
                                           content="Sub-agent summary.", complete=True)

    asyncio.run(run())
    assert emitter.reusable_terminal_key() == ""


def test_no_main_window_block_has_nothing_to_reuse():
    emitter = _emitter()
    assert emitter.reusable_terminal_key() == ""


class _RequestFourModel:
    """Reproduces request 4's shape: a wait message, progress, a delegation panel, then a checkpoint."""

    provider = "test"
    model = "test"

    def __init__(self):
        self._response_emitter = None
        self._thread_id_override = None

    async def _hitl_interrupt_pending(self, thread_id):
        return False

    def install_request_contract(self, contract):
        self._request_contract = contract

    async def invoke(self, prompt, is_interactive=False):
        emit = self._response_emitter
        await emit("**Waiting for propagation**")
        for _ in range(4):
            await emit("**Propagation wait in progress**")
        await emit.emit_subagent_status(title="Mythic_Operator", delegation_id="d2",
                                        delegation_name="Mythic_Operator", prompt="Check task 29")
        await emit.emit_tool_use(tool_call_id="c9", tool_name="list_callbacks", tool_source="mythic",
                                 status="finished", content="done", complete=True)
        await emit.emit_subagent_status(title="Mythic_Operator", delegation_id="d2",
                                        delegation_name="Mythic_Operator", status="finished",
                                        content="Task 29 has not reached a terminal state.", complete=True)
        return "⏸️ Checkpoint — awaiting continuation"


class _DriverChat(HeadlessSageChat):
    def __init__(self, model):
        super().__init__()
        self._model = model

    async def _get_or_create_model(self, request):
        return self._model, False


def _first_seen(emissions):
    order = []
    for emission in emissions:
        if emission["response_key"] not in order:
            order.append(emission["response_key"])
    return order


def test_assembled_request_terminal_is_created_after_every_other_row():
    chat = _DriverChat(_RequestFourModel())
    asyncio.run(chat.chat(build_chat_request("dcsync", channel_id=5, request_id=4)))
    terminal = chat.terminal_emissions[-1]
    assert terminal["response_key"] == "assistant:4:turn"
    assert terminal["content"].startswith("⏸️ Checkpoint")
    assert _first_seen(chat.emissions)[-1] == "assistant:4:turn"
    waiting = [e for e in chat.emissions if e["response_key"] == "assistant:4:1"]
    assert [e["content"] for e in waiting] == ["**Waiting for propagation**"]


class _FinalBlockModel(_RequestFourModel):
    async def invoke(self, prompt, is_interactive=False):
        emit = self._response_emitter
        await emit.emit_tool_use(tool_call_id="c1", tool_name="list_callbacks", tool_source="mythic",
                                 status="finished", content="done", complete=True)
        await emit("Final report.")
        return ""


def test_assembled_request_still_finalizes_a_last_block_in_place():
    chat = _DriverChat(_FinalBlockModel())
    asyncio.run(chat.chat(build_chat_request("list", channel_id=5, request_id=12)))
    terminal = chat.terminal_emissions[-1]
    assert terminal["response_key"] == "assistant:12:1"
    assert terminal["content"] == "Final report."
    assert not [e for e in chat.emissions if e["response_key"] == "assistant:12:turn"]


# A delegation still open at the terminal is closed by the real Model close path. When its close content is
# new text (a refusal appended to what the specialist streamed), Mythic stores it as a new sub-agent final-output
# row, so the terminal must be created after that close, not chosen before it.

from ai.langgraph.model import Model
from sage_chat import session as chat_session

_STREAMED = "Callback 7 is the target; preparing the task."
_REFUSAL = "[task preparation] Approval was not requested for this step."


class _OpenDelegationModel(Model):
    """A Model without a provider. The scripted turn only stands in for the graph; every lifecycle hook the
    service calls on the terminal path (close, reconcile, final record, projection) is the real Model's."""

    provider = "test"
    model = "test"

    def __init__(self, close_content):
        self._response_emitter = None
        self._thread_id_override = None
        self._active_delegations = {}
        self._delegation_seq = 0
        self.verbose = False
        self._close_content = close_content

    async def _hitl_interrupt_pending(self, thread_id):
        return False

    async def invoke(self, prompt, is_interactive=False):
        # The service has already opened the request ledger and installed the contract on this Model.
        await self._open_delegation("Mythic_Operator", "Run step 3", 1, title="Run step 3")
        delegation_id = self.current_delegation_id("Mythic_Operator")
        await self._emit_agent_text(content=_STREAMED, delegation_id=delegation_id,
                                    delegation_name="Mythic_Operator")
        self._record_delegation_final_summary("Mythic_Operator", self._close_content)
        # The request ends on a main-window block while the specialist's delegation is still open.
        await self._response_emitter("**Blocked** — step 3 was refused.")
        return ""


def _mythic_rows(emissions):
    """Rows in Mythic's creation order: a key's first send creates its row, and a sub-agent card that closes
    with content also creates its final-output row."""
    rows = []
    for emission in emissions:
        key = emission["response_key"]
        if key not in rows:
            rows.append(key)
        metadata = emission.get("metadata") or {}
        if (metadata.get("special_type") == "subagent" and emission.get("complete")
                and emission.get("content") and f"subagent_final:{key}" not in rows):
            rows.append(f"subagent_final:{key}")
    return rows


def _run_open_delegation_request(monkeypatch, tmp_path, request_id, close_content):
    monkeypatch.setattr(chat_session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "markers.db"))
    model = _OpenDelegationModel(close_content)
    chat = _DriverChat(model)
    asyncio.run(chat.chat(build_chat_request("run step 3", channel_id=5, request_id=request_id)))
    return chat, model


def test_terminal_is_created_after_a_delegation_closed_at_the_terminal(monkeypatch, tmp_path):
    chat, model = _run_open_delegation_request(
        monkeypatch, tmp_path, 21, f"{_STREAMED}\n{_REFUSAL}")

    closes = [e for e in chat.emissions
              if (e.get("metadata") or {}).get("special_type") == "subagent" and e.get("complete")]
    assert [e["content"] for e in closes] == [_REFUSAL]
    assert closes[0]["metadata"]["subagent"]["status"] == "stopped"
    assert not model._active_delegations

    terminal = chat.terminal_emissions[-1]
    assert terminal["response_key"] == "assistant:21:turn"
    assert terminal["content"] == "**Blocked** — step 3 was refused."
    rows = _mythic_rows(chat.emissions)
    assert rows[-1] == "assistant:21:turn", rows
    assert rows.index(f"subagent_final:{closes[0]['response_key']}") < rows.index("assistant:21:turn")
    block = [e for e in chat.emissions if e["response_key"] == "assistant:21:1"]
    assert [e["content"] for e in block] == ["**Blocked** — step 3 was refused."]
    assert not any(e.get("complete_request") for e in block)


def test_delegation_closed_without_new_text_keeps_the_last_block_as_terminal(monkeypatch, tmp_path):
    chat, model = _run_open_delegation_request(monkeypatch, tmp_path, 22, _STREAMED)

    closes = [e for e in chat.emissions
              if (e.get("metadata") or {}).get("special_type") == "subagent" and e.get("complete")]
    assert [e["content"] for e in closes] == [""]
    assert not model._active_delegations

    terminal = chat.terminal_emissions[-1]
    assert terminal["response_key"] == "assistant:22:1"
    assert terminal["content"] == "**Blocked** — step 3 was refused."
    assert _mythic_rows(chat.emissions)[-1] == "assistant:22:1"
    assert not [e for e in chat.emissions if e["response_key"] == "assistant:22:turn"]
