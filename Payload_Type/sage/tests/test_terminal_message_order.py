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
