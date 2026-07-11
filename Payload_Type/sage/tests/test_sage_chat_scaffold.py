"""Unit tests for the sage_chat scaffold (Phase 1 MVP).

Covers the safety-critical always-terminal invariant, response_key discipline, cancel re-raise,
error path, the config precedence shim, the channel session key, and the streaming emitter — all
with a stubbed Model (no live LLM), per PRD Section 13.
"""

import asyncio

import pytest

from sage_chat.config import build_model_kwargs
from sage_chat.models import SAGE_MODELS
from sage_chat.session import channel_session_key
from sage_chat.streaming import ChatStreamEmitter
from sage_chat.headless import HeadlessSageChat, build_chat_request


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeModel:
    """Stand-in for ai.langgraph.model.Model — records invoke() and drives the emitter."""

    def __init__(self, behavior="ok", stream=("🤖[Agent]> hi",)):
        self._response_emitter = None
        self._thread_id_override = None
        self.behavior = behavior
        self.stream = stream
        self.stop_called = False
        self.invoked_with = None

    def request_stop(self):
        self.stop_called = True

    async def _hitl_interrupt_pending(self, thread_id):
        return False

    async def invoke(self, prompt, is_interactive=False):
        self.invoked_with = (prompt, is_interactive)
        for block in self.stream:
            await self._response_emitter(block)
        if self.behavior == "cancel":
            raise asyncio.CancelledError()
        if self.behavior == "error":
            raise RuntimeError("boom")
        return "done"


class _DriverChat(HeadlessSageChat):
    """HeadlessSageChat with _get_or_create_model stubbed to a _FakeModel."""

    def __init__(self, model, preexisted=False):
        super().__init__()
        self._model = model
        self._preexisted = preexisted

    async def _get_or_create_model(self, request):
        return self._model, self._preexisted


# --------------------------------------------------------------------------------------
# Always-terminal + response_key discipline
# --------------------------------------------------------------------------------------

def test_happy_path_emits_exactly_one_terminal():
    model = _FakeModel(stream=("🤖> one", "🛠️> two"))
    chat = _DriverChat(model)
    req = build_chat_request("do the thing", channel_id=5, request_id=9)
    _run(chat.chat(req))

    terminals = chat.terminal_emissions
    assert len(terminals) == 1, f"expected exactly one terminal, got {chat.emissions}"
    assert terminals[0]["kind"] == "complete"
    # prompt threaded through, first turn → is_interactive False
    assert model.invoked_with == ("do the thing", False)


def test_response_key_discipline():
    model = _FakeModel(stream=("block-a", "block-b"))
    chat = _DriverChat(model)
    req = build_chat_request("hi", channel_id=5, request_id=9)
    _run(chat.chat(req))

    texts = [e for e in chat.emissions if e["kind"] == "text"]
    assert [t["response_key"] for t in texts] == ["assistant:9:1", "assistant:9:2"]
    complete = [e for e in chat.emissions if e["kind"] == "complete"][0]
    assert complete["response_key"] == "assistant:9:turn"
    assert complete["complete_request"] is True


def test_preexisting_session_is_interactive():
    model = _FakeModel()
    chat = _DriverChat(model, preexisted=True)
    _run(chat.chat(build_chat_request("second turn", channel_id=5, request_id=10)))
    assert model.invoked_with[1] is True


def test_native_chat_marks_model_command_name_chat():
    model = _FakeModel()
    chat = _DriverChat(model)
    _run(chat.chat(build_chat_request("first turn", channel_id=5, request_id=10)))
    assert model.command_name == "chat"


# --------------------------------------------------------------------------------------
# Cancel + error paths
# --------------------------------------------------------------------------------------

def test_cancel_reraises_and_cooperatively_stops():
    model = _FakeModel(behavior="cancel")
    chat = _DriverChat(model)
    req = build_chat_request("go", channel_id=5, request_id=11)
    with pytest.raises(asyncio.CancelledError):
        _run(chat.chat(req))
    # cooperative stop fired; the SDK (not us) emits the cancelled terminal, so we must NOT have.
    assert model.stop_called is True
    assert chat.terminal_emissions == []


def test_handler_exception_emits_one_error_terminal():
    model = _FakeModel(behavior="error")
    chat = _DriverChat(model)
    req = build_chat_request("go", channel_id=5, request_id=12)
    _run(chat.chat(req))  # run_chat_turn swallows the exception into send_error
    errors = [e for e in chat.emissions if e["kind"] == "error"]
    assert len(errors) == 1
    assert errors[0]["complete_request"] is True
    assert len(chat.terminal_emissions) == 1


# --------------------------------------------------------------------------------------
# Streaming emitter
# --------------------------------------------------------------------------------------

def test_emitter_skips_empty_and_increments_blocks():
    sent = []

    class _Rec:
        async def send_text(self, request, response_key, content="", metadata=None):
            sent.append((response_key, content))

    req = build_chat_request("x", request_id=3)
    emitter = ChatStreamEmitter(_Rec(), req)
    assert _run(emitter("")) is False       # empty → no send
    assert _run(emitter("a")) is True
    assert _run(emitter("b")) is True
    assert sent == [("assistant:3:1", "a"), ("assistant:3:2", "b")]


def test_emit_tool_use_produces_tool_use_card():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_tool_use(
        tool_call_id="call_1",
        tool_name="search_credentials",
        tool_source="mythic",
        status="started",
        content="Using Mythic tool `search_credentials`...",
        complete=False,
        arguments_present=True,
    )) is True

    assert len(chat.emissions) == 1
    emitted = chat.emissions[0]
    assert emitted["response_key"] == "tool_use:call_1:search_credentials"
    assert emitted["metadata"]["special_type"] == "tool_use"
    tool_use = emitted["metadata"]["tool_use"]
    assert tool_use["status"] == "started"
    assert tool_use["tool_name"] == "search_credentials"
    assert tool_use["tool_source"] == "mythic"
    assert tool_use["arguments_present"] is True
    assert "delegation_id" not in emitted["metadata"]
    assert "delegation_name" not in emitted["metadata"]
    assert "delegation_id" not in tool_use
    assert "delegation_name" not in tool_use


def test_emit_tool_use_ships_full_output_lazily():
    """Large result: result_preview holds a short preview; tool_use.output carries the full raw result
    (Mythic serves that lazily via 'View output', so it never inflates the chat message)."""
    chat = HeadlessSageChat()
    emitter = ChatStreamEmitter(chat, build_chat_request("x"))
    assert _run(emitter.emit_tool_use(
        tool_call_id="call_9", tool_name="get_task_history_for_callback", tool_source="mythic",
        status="completed", content="finished", complete=True,
        result_preview="preview…[View output]", output="FULL RAW RESULT " * 500,
    )) is True
    tool_use = chat.emissions[-1]["metadata"]["tool_use"]
    assert tool_use["result_preview"] == "preview…[View output]"
    assert tool_use["output"].startswith("FULL RAW RESULT")


def test_emit_tool_use_small_result_has_no_lazy_output():
    """Small result stays fully inline (result_preview) with no separate lazy output field."""
    chat = HeadlessSageChat()
    emitter = ChatStreamEmitter(chat, build_chat_request("x"))
    assert _run(emitter.emit_tool_use(
        tool_call_id="call_1", tool_name="whoami", tool_source="mythic",
        status="completed", content="done", complete=True,
        result_preview="CORP\\kevin", output=None,
    )) is True
    tool_use = chat.emissions[-1]["metadata"]["tool_use"]
    assert tool_use["result_preview"] == "CORP\\kevin"
    assert "output" not in tool_use          # nothing lazy for a small result


def test_emit_subagent_status_produces_subagent_card():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_subagent_status(
        title="List all domains",
        delegation_id="bloodhound:1",
        delegation_name="BloodHound",
        status="running",
        tool_count=0,
        icon="BH",
    )) is True

    assert len(chat.emissions) == 1
    emitted = chat.emissions[0]
    assert emitted["kind"] == "response"
    assert emitted["metadata"]["special_type"] == "subagent"
    assert emitted["metadata"]["delegation_id"] == "bloodhound:1"
    assert emitted["metadata"]["delegation_name"] == "BloodHound"
    subagent = emitted["metadata"]["subagent"]
    assert subagent["title"] == "List all domains"
    assert subagent["status"] == "running"
    assert subagent["tool_count"] == 0
    assert subagent["icon"] == "BH"


def test_emit_subagent_status_forwards_icon_color():
    """A provided icon_color reaches the subagent card metadata (fixes per-card random colors)."""
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_subagent_status(
        title="List all domains",
        delegation_id="bloodhound:1",
        delegation_name="BloodHound",
        status="running",
        tool_count=0,
        icon="BH",
        icon_color="#E5484D",
    )) is True

    subagent = chat.emissions[0]["metadata"]["subagent"]
    assert subagent["icon_color"] == "#E5484D"


def test_delegation_color_is_deterministic_and_frontmatter_driven():
    """Model._delegation_color pins one stable color per agent; BloodHound resolves to red
    from its prompt frontmatter, and an unknown agent yields '' (Mythic auto-derives)."""
    from ai.langgraph.model import Model

    assert Model._delegation_color("BloodHound") == "#E5484D"   # from prompts/bloodhound.md
    assert Model._delegation_color("Mythic_Operator") == "#3B82F6"
    # Same agent → same color every call (the whole point — no per-card drift).
    assert Model._delegation_color("BloodHound") == Model._delegation_color("BloodHound")
    # Unknown agent with no prompt file / no frontmatter color → empty (UI derives).
    assert Model._delegation_color("Nonexistent_Agent") == ""


def test_emit_agent_text_is_delegation_tagged_not_main_text():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_agent_text(
        content="I'll inspect callback state",
        delegation_id="mythic_operator:1",
        delegation_name="Mythic_Operator",
    )) is True

    assert len(chat.emissions) == 1
    emitted = chat.emissions[0]
    assert emitted["response_key"].startswith("agent_text:mythic_operator:1:")
    assert not emitted["response_key"].startswith("assistant:")
    assert emitted["metadata"]["delegation_id"] == "mythic_operator:1"
    assert emitted["metadata"]["delegation_name"] == "Mythic_Operator"


def test_emit_tool_use_includes_delegation_metadata_when_provided():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_tool_use(
        tool_call_id="call_2",
        tool_name="list_domains",
        tool_source="mcp",
        status="started",
        content="Using MCP tool `list_domains`...",
        complete=False,
        delegation_id="bloodhound:1",
        delegation_name="BloodHound",
    )) is True

    emitted = chat.emissions[0]
    assert emitted["metadata"]["delegation_id"] == "bloodhound:1"
    assert emitted["metadata"]["delegation_name"] == "BloodHound"
    tool_use = emitted["metadata"]["tool_use"]
    assert tool_use["delegation_id"] == "bloodhound:1"
    assert tool_use["delegation_name"] == "BloodHound"


def test_emit_tool_use_started_then_finished_reuse_key():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_tool_use(
        tool_call_id="call_1",
        tool_name="search_credentials",
        tool_source="mythic",
        status="started",
        content="Using Mythic tool `search_credentials`...",
        complete=False,
        arguments_present=True,
    )) is True
    assert _run(emitter.emit_tool_use(
        tool_call_id="call_1",
        tool_name="search_credentials",
        tool_source="mythic",
        status="completed",
        content="Tool `search_credentials` finished.",
        complete=True,
        result_preview="ok done",
    )) is True

    assert len(chat.emissions) == 2
    started, finished = chat.emissions
    assert started["response_key"] == "tool_use:call_1:search_credentials"
    assert finished["response_key"] == "tool_use:call_1:search_credentials"
    assert started["status"] == "streaming"
    assert finished["status"] == "complete"
    assert finished["metadata"]["tool_use"]["result_preview"] == "ok done"
    assert finished["metadata"]["tool_use"]["status"] == "completed"


def test_subagent_lifecycle_reuses_key_and_tags_tool_card():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)
    delegation_id = "bloodhound:1"

    assert _run(emitter.emit_subagent_status(
        title="List all domains",
        delegation_id=delegation_id,
        delegation_name="BloodHound",
        status="running",
        tool_count=0,
        icon="BH",
    )) is True
    assert _run(emitter.emit_tool_use(
        tool_call_id="call_3",
        tool_name="list_domains",
        tool_source="mcp",
        status="started",
        content="Using MCP tool `list_domains`...",
        complete=False,
        delegation_id=delegation_id,
        delegation_name="BloodHound",
    )) is True
    assert _run(emitter.emit_subagent_status(
        title="List all domains",
        delegation_id=delegation_id,
        delegation_name="BloodHound",
        status="running",
        tool_count=1,
        icon="BH",
    )) is True
    assert _run(emitter.emit_subagent_status(
        title="List all domains",
        delegation_id=delegation_id,
        delegation_name="BloodHound",
        status="finished",
        tool_count=1,
        icon="BH",
        content="Found one domain.",
        complete=True,
    )) is True

    subagent_emissions = [
        emitted for emitted in chat.emissions
        if emitted["metadata"]["special_type"] == "subagent"
    ]
    tool_emissions = [
        emitted for emitted in chat.emissions
        if emitted["metadata"]["special_type"] == "tool_use"
    ]
    assert len(subagent_emissions) == 3
    assert {emitted["response_key"] for emitted in subagent_emissions} == {"subagent:bloodhound_1"}
    assert subagent_emissions[0]["metadata"]["subagent"]["tool_count"] == 0
    assert subagent_emissions[1]["metadata"]["subagent"]["tool_count"] == 1
    assert subagent_emissions[2]["metadata"]["subagent"]["status"] == "finished"
    assert subagent_emissions[2]["complete"] is True
    assert subagent_emissions[2]["content"] == "Found one domain."
    assert len(tool_emissions) == 1
    assert tool_emissions[0]["metadata"]["delegation_id"] == delegation_id
    assert tool_emissions[0]["metadata"]["tool_use"]["delegation_id"] == delegation_id


def test_control_tools_do_not_render_operator_cards():
    from ai.langgraph.model import _is_control_tool

    assert _is_control_tool("handback_to_supervisor") is True
    assert _is_control_tool("summarize_and_handback") is True
    assert _is_control_tool("transfer_to_Mythic_Operator") is True
    assert _is_control_tool("respond_to_user") is True
    assert _is_control_tool("request_continuation") is True
    assert _is_control_tool("execute_capability") is False
    assert _is_control_tool("list_callbacks") is False
    assert _is_control_tool("") is False


class _SubagentStatusRecorder:
    def __init__(self):
        self.calls = []

    async def emit_subagent_status(self, **kwargs):
        self.calls.append(kwargs)
        return True


def test_delegation_reentry_same_source_seq_keeps_one_card():
    from ai.langgraph.model import Model

    emitter = _SubagentStatusRecorder()
    model = Model.__new__(Model)
    model._active_delegations = {}
    model._delegation_seq = 0
    model._response_emitter = emitter

    _run(model._open_delegation("Mythic_Operator", "do X", 7))
    _run(model._open_delegation("Mythic_Operator", "do X", 7))

    assert len(emitter.calls) == 1
    assert emitter.calls[0]["delegation_id"] == "mythic_operator:1"
    assert model._active_delegations["Mythic_Operator"]["source_seq"] == 7

    _run(model._open_delegation("Mythic_Operator", "do Y", 9))

    running_calls = [call for call in emitter.calls if call["status"] == "running"]
    assert [call["delegation_id"] for call in running_calls] == [
        "mythic_operator:1",
        "mythic_operator:2",
    ]
    assert model._active_delegations["Mythic_Operator"]["source_seq"] == 9


def test_delegation_card_uses_short_title_but_retains_full_instruction():
    from ai.langgraph.model import Model

    emitter = _SubagentStatusRecorder()
    model = Model.__new__(Model)
    model._active_delegations = {}
    model._delegation_seq = 0
    model._response_emitter = emitter

    instruction = "List all active Mythic callbacks and report each host, user, and integrity level."
    _run(model._open_delegation("Mythic_Operator", instruction, 7, title="List active callbacks"))

    assert emitter.calls[0]["title"] == "List active callbacks"
    assert model._active_delegations["Mythic_Operator"]["title"] == "List active callbacks"
    assert model._active_delegations["Mythic_Operator"]["instruction"] == instruction


def test_delegation_safety_close_falls_back_to_last_text():
    from ai.langgraph.model import Model

    emitter = _SubagentStatusRecorder()
    model = Model.__new__(Model)
    model._response_emitter = emitter
    model._active_delegations = {
        "Mythic_Operator": {
            "id": "mythic_operator:1",
            "name": "Mythic_Operator",
            "title": "do X",
            "tool_count": 2,
            "icon": "MO",
            "source_seq": 7,
            "last_text": "Callback 1 history shows only failed tasks.",
        }
    }

    _run(model._close_delegation("Mythic_Operator"))

    assert len(emitter.calls) == 1
    finished = emitter.calls[0]
    assert finished["content"] == "Callback 1 history shows only failed tasks."
    assert finished["status"] == "finished"
    assert finished["complete"] is True
    assert finished["tool_count"] == 2


def test_delegation_close_prefers_final_summary_then_explicit_content():
    from ai.langgraph.model import Model

    emitter = _SubagentStatusRecorder()
    model = Model.__new__(Model)
    model._response_emitter = emitter
    delegation = {
        "id": "mythic_operator:1",
        "name": "Mythic_Operator",
        "title": "do X",
        "tool_count": 3,
        "icon": "MO",
        "source_seq": 7,
        "last_text": "mid-run reasoning line",
        "final_summary": "",
    }

    model._active_delegations = {"Mythic_Operator": dict(delegation)}
    _run(model._capture_delegation_final_summary("Mythic_Operator", "DONE: dumped 4 hashes from DC01"))
    _run(model._close_delegation("Mythic_Operator"))
    assert emitter.calls[-1]["content"] == "DONE: dumped 4 hashes from DC01"

    model._active_delegations = {"Mythic_Operator": dict(delegation)}
    _run(model._capture_delegation_final_summary("Mythic_Operator", "DONE: dumped 4 hashes from DC01"))
    _run(model._close_delegation("Mythic_Operator", content="explicit copy-back summary"))
    assert emitter.calls[-1]["content"] == "explicit copy-back summary"


# --------------------------------------------------------------------------------------
# Config precedence + session key
# --------------------------------------------------------------------------------------

def test_config_precedence_config_over_secret_over_env(monkeypatch):
    monkeypatch.setenv("provider", "ollama")
    monkeypatch.setenv("model", "env-model")
    req = build_chat_request(
        "hi",
        config={"provider": "anthropic"},           # Config wins over Secret + env
        secrets={"model": "secret-model", "API_KEY": "sk-abc"},  # Secret wins over env
    )
    kwargs = build_model_kwargs(req)
    assert kwargs["provider"] == "anthropic"
    assert kwargs["model"] == "secret-model"
    assert kwargs["config"]["configurable"]["api_key"] == "sk-abc"
    # neutral placeholders — chat has no task
    assert kwargs["task_id"] == 0 and kwargs["agent_task_id"] == ""
    assert kwargs["operation_id"] == req.OperationID


def test_mode_auto_sets_autonomous_solve():
    req = build_chat_request("hi", config={"mode": "auto"})
    kwargs = build_model_kwargs(req)
    assert kwargs["mode"] == "auto"
    assert kwargs["autonomous_solve"] is True


def test_channel_session_key_is_channel_id():
    req = build_chat_request("hi", channel_id=77)
    assert channel_session_key(req) == "77"


# --------------------------------------------------------------------------------------
# Phase 3 — slash commands
# --------------------------------------------------------------------------------------

from mythic_container.ChatBase import ChatSlashCommandInvocation
from sage_chat.slash import handle_slash, SLASH_COMMANDS


def _slash_req(name, argument="", channel_id=5, request_id=1):
    r = build_chat_request("", channel_id=channel_id, request_id=request_id)
    r.SlashCommand = ChatSlashCommandInvocation(name=name, argument=argument)
    return r


def test_slash_commands_declared():
    assert {c.Name for c in SLASH_COMMANDS} == {"state", "list", "mode", "stop", "mcp", "bloodhound"}


def test_slash_state_no_session_is_handled_with_one_terminal(monkeypatch):
    from sage_chat import slash

    async def _no_resolve(model, request):  # no Mythic in unit tests — simulate an unresolvable operation
        return ""

    monkeypatch.setattr(slash, "_resolve_chat_engagement_id", _no_resolve)
    chat = HeadlessSageChat()
    handled = _run(handle_slash(chat, _slash_req("state"), None, "slash:1"))
    assert handled is True
    assert len(chat.terminal_emissions) == 1
    assert "start a session" in chat.emissions[-1]["content"]


def test_slash_mode_show_then_set():
    chat = HeadlessSageChat()

    class _M:
        mode = "supervised"

    m = _M()
    _run(handle_slash(chat, _slash_req("mode"), m, "slash:1"))
    assert "supervised" in chat.emissions[-1]["content"]
    _run(handle_slash(chat, _slash_req("mode", "auto"), m, "slash:2"))
    assert m.mode == "auto"


def test_slash_unknown_falls_through_without_emitting():
    chat = HeadlessSageChat()
    handled = _run(handle_slash(chat, _slash_req("frobnicate"), None, "slash:1"))
    assert handled is False
    assert chat.emissions == []


def test_slash_mcp_and_bloodhound_declared():
    assert {c.Name for c in SLASH_COMMANDS} >= {"mcp", "bloodhound"}


def test_slash_mcp_list_empty():
    from ai import mcp as mcpmod
    import sage_chat.slash as slashmod
    orig = mcpmod.MCPManager.get_connected_servers
    mcpmod.MCPManager.get_connected_servers = lambda: []
    try:
        chat = HeadlessSageChat()
        _run(handle_slash(chat, _slash_req("mcp", "list"), None, "slash:1"))
        assert "No MCP servers connected" in chat.emissions[-1]["content"]
    finally:
        mcpmod.MCPManager.get_connected_servers = orig


def test_slash_mcp_connect_invalid_json():
    chat = HeadlessSageChat()
    _run(handle_slash(chat, _slash_req("mcp", "connect {not valid}"), None, "slash:1"))
    assert "Invalid JSON" in chat.emissions[-1]["content"]


def test_slash_mcp_disconnect(monkeypatch):
    from ai import mcp as mcpmod

    async def _disc(name):
        return True

    monkeypatch.setattr(mcpmod.MCPManager, "disconnect_server", _disc)
    chat = HeadlessSageChat()
    _run(handle_slash(chat, _slash_req("mcp", "disconnect srv1"), None, "slash:1"))
    assert "Disconnected MCP server `srv1`" in chat.emissions[-1]["content"]


def test_slash_bloodhound(monkeypatch):
    from ai import bloodhound_config as bh

    async def _ensure(directory=None):
        return (True, "BloodHound MCP connected.")

    monkeypatch.setattr(bh, "ensure_bloodhound_connected", _ensure)
    chat = HeadlessSageChat()
    _run(handle_slash(chat, _slash_req("bloodhound"), None, "slash:1"))
    assert "BloodHound MCP connected" in chat.emissions[-1]["content"]


def test_slash_dispatched_via_chat_without_creating_model():
    class _NoModelChat(HeadlessSageChat):
        async def _get_or_create_model(self, request):  # must not be called for a handled slash
            raise AssertionError("slash command should not construct a Model")

    chat = _NoModelChat()
    _run(chat.chat(_slash_req("state", channel_id=9, request_id=3)))
    assert len(chat.terminal_emissions) == 1


# --------------------------------------------------------------------------------------
# Phase 2 P0 — MythicTools chat-token auth rewrite
# --------------------------------------------------------------------------------------

def test_build_model_kwargs_threads_channel_auth():
    req = build_chat_request("hi", channel_id=42, operation_id=7)
    req.APITokenID = 99
    kwargs = build_model_kwargs(req)
    assert kwargs["channel_id"] == 42
    assert kwargs["apitoken_id"] == 99
    assert kwargs["operation_id"] == 7


def test_login_chat_branch_degrades_when_mint_fails(monkeypatch):
    """channel_id set → chat branch; if the token mint fails (offline), client stays None (fail closed).

    Patches ``create`` on the real ``ChatAPITokenProvider`` class (not the module attribute) so the fake
    is used no matter how ``login()`` imports the symbol — otherwise the real RPC blocks on absent RabbitMQ.
    """
    from ai.langgraph import mythic_tools as mt
    from mythic_container.ChatBase import ChatAPITokenProvider

    async def _boom(cls, *a, **k):
        raise RuntimeError("no mythic reachable")

    monkeypatch.setattr(ChatAPITokenProvider, "create", classmethod(_boom))
    client = mt.MythicTools(channel_id=5, operation_id=1, apitoken_id=2)
    _run(client.login())
    assert client.client is None  # degraded, did not raise or hang


def test_login_chat_branch_mints_channel_token(monkeypatch):
    """Happy chat path: token minted via ChatAPITokenProvider(ChatChannelID) and passed to mythic.login."""
    from ai.langgraph import mythic_tools as mt
    from mythic_container.ChatBase import ChatAPITokenProvider

    class _Prov:
        async def get_token(self):
            return "chat-token"

    async def _create(cls, op, ch, tok):
        assert (op, ch, tok) == (1, 5, 2)
        return _Prov()

    seen = {}

    async def _fake_login(apitoken, server_ip, server_port, ssl):
        seen["apitoken"] = apitoken
        return "CLIENT"

    monkeypatch.setattr(ChatAPITokenProvider, "create", classmethod(_create))
    monkeypatch.setattr(mt.mythic, "login", _fake_login)
    client = mt.MythicTools(channel_id=5, operation_id=1, apitoken_id=2)
    _run(client.login())
    assert client.client == "CLIENT"
    assert seen["apitoken"] == "chat-token"


def test_login_task_branch_unchanged(monkeypatch):
    """No channel_id but agent_task_id set → legacy task path mints from AgentTaskID."""
    from ai.langgraph import mythic_tools as mt

    seen = {}

    class _Resp:
        Success = True
        APIToken = "task-token"

    async def _fake_rpc(msg):
        return _Resp()

    async def _fake_login(apitoken, server_ip, server_port, ssl):
        seen["apitoken"] = apitoken
        return "TASKCLIENT"

    monkeypatch.setattr(mt, "SendMythicRPCAPITokenCreate", _fake_rpc)
    monkeypatch.setattr(mt.mythic, "login", _fake_login)
    client = mt.MythicTools(agent_task_id="task-123")
    _run(client.login())
    assert client.client == "TASKCLIENT"
    assert seen["apitoken"] == "task-token"


# --------------------------------------------------------------------------------------
# Phase 2 — native-card HITL (Option C)
# --------------------------------------------------------------------------------------

from sage_chat.hitl import build_approval_request, make_card_emitter, resume_decision_for_request, should_confirm
from mythic_container.ChatBase import ChatInputResponse


class _HitlModel:
    """Stub Model that raises a card on the first turn and resumes on the next."""

    def __init__(self):
        self._response_emitter = None
        self._hitl_card_emitter = None
        self._hitl_card_pending = False
        self._thread_id_override = None
        self._pending = False
        self.resumed_with = None

    async def _hitl_interrupt_pending(self, thread_id):
        return self._pending

    async def invoke(self, prompt, is_interactive=False):
        # Simulate hitting a guarded tool: emit the card (which finishes request N) and pause.
        await self._hitl_card_emitter([{"name": "execute_capability", "args": {"target": "DC01"}}])
        self._hitl_card_pending = True
        self._pending = True

    async def handle_hitl_resume(self, decision, thread_id, operator_message=""):
        self.resumed_with = decision
        self.steered_with = operator_message
        self._pending = False
        await self._response_emitter(f"🤖> resume:{decision}")
        return ""

    def request_stop(self):
        pass


class _HitlDriverChat(HeadlessSageChat):
    def __init__(self, model):
        super().__init__()
        self._model = model

    async def _get_or_create_model(self, request):
        return self._model, self._model._pending  # preexisted-ish; routing uses interrupt_pending anyway


class _ControllerHitlModel(_HitlModel):
    """Stub controller-owned approval that is pending outside LangGraph checkpoint state."""

    def __init__(self):
        super().__init__()
        self._controller_hitl_pending = {"tool": "execute_capability", "args": {"target": "DC01"}}

    async def handle_controller_hitl_resume(self, decision):
        self.resumed_with = decision
        self._controller_hitl_pending = None
        await self._response_emitter(f"🤖> controller-resume:{decision}")
        return ""


def test_should_confirm_policy():
    assert should_confirm("execute_capability") is True
    assert should_confirm("list_callbacks") is False          # not guarded
    assert should_confirm("execute_capability", "auto") is False  # auto never arms


def test_approval_request_shape():
    req = build_approval_request([{"name": "create_payload", "args": {"os": "windows"}}])
    assert set(req) == {"title", "prompt", "description", "data"}
    assert "create_payload" in req["title"]
    assert req["data"]["tool_name"] == "create_payload"
    assert req["data"]["display_name"] == "create_payload"
    assert req["data"]["arguments"] == {"os": "windows"}
    assert req["data"]["guarded_action_count"] == 1


def test_approval_request_uses_capability_display_name_without_changing_guarded_tool():
    req = build_approval_request([{
        "name": "execute_capability",
        "args": {"action": {"name": "forge-golden-ticket"}, "inputs": {"callback_id": "3"}},
    }])
    assert req["title"] == "Approve: forge-golden-ticket"
    assert "forge-golden-ticket" in req["prompt"]
    assert req["data"]["tool_name"] == "execute_capability"
    assert req["data"]["display_name"] == "forge-golden-ticket"


def test_approval_cards_use_unique_keys_and_optional_delegation_tags():
    chat = HeadlessSageChat()
    req = build_chat_request("x", channel_id=5, request_id=9)
    emitter = make_card_emitter(chat, req)

    _run(emitter([{"name": "execute_capability", "args": {"callback": 2}}]))
    _run(emitter([{"name": "execute_capability", "args": {"callback": 2}}]))

    input_reqs = [
        emitted
        for emitted in chat.emissions
        if emitted["kind"] == "complete"
        and emitted.get("metadata", {}).get("special_type") == "input_requested"
    ]
    assert len(input_reqs) == 2
    assert input_reqs[0]["response_key"] != input_reqs[1]["response_key"]   # each approval is a fresh card
    assert all(
        emitted["response_key"].startswith("input_requested:")             # SDK's unique input_requested:{uuid}
        for emitted in input_reqs
    )
    assert all(emitted["complete_request"] is False for emitted in input_reqs)

    tagged_chat = HeadlessSageChat()
    tagged_emitter = make_card_emitter(
        tagged_chat,
        req,
        delegation_lookup=lambda: ("mythic_operator:1", "Mythic_Operator"),
    )
    _run(tagged_emitter([{"name": "execute_capability", "args": {"callback": 2}}]))
    tagged = tagged_chat.emissions[0]
    assert tagged["metadata"]["delegation_id"] == "mythic_operator:1"
    assert tagged["metadata"]["delegation_name"] == "Mythic_Operator"


def test_hitl_confirm_flow_input_request_then_resume():
    model = _HitlModel()
    chat = _HitlDriverChat(model)

    # Request N: guarded tool → approval request. This posts an `input_requested` block with
    # complete_request=False (the channel-release), NOT a terminal. Handler returns None.
    _run(chat.chat(build_chat_request("do risky thing", channel_id=5, request_id=1)))
    assert chat.terminal_emissions == []  # released by input_requested, not a complete-terminal
    input_reqs = [e for e in chat.emissions if e.get("metadata", {}).get("special_type") == "input_requested"]
    assert len(input_reqs) == 1
    assert model._pending is True

    # Request N+1: operator ACCEPTS → InputResponse(action="accept") → resume APPROVE, one terminal.
    chat.emissions.clear()
    reqN1 = build_chat_request("", channel_id=5, request_id=2)
    reqN1.InputResponse = ChatInputResponse(action="accept")
    _run(chat.chat(reqN1))
    assert model.resumed_with == "approve"
    assert model._pending is False
    assert len(chat.terminal_emissions) == 1


def test_hitl_reject_resumes_deny():
    """Operator REJECT → InputResponse(action='reject') → resume deny (server now sends a real response)."""
    model = _HitlModel()
    model._pending = True  # an interrupt is already pending on this channel
    chat = _HitlDriverChat(model)
    reqR = build_chat_request("", channel_id=5, request_id=2)
    reqR.InputResponse = ChatInputResponse(action="reject")
    _run(chat.chat(reqR))
    assert model.resumed_with == "deny"
    assert len(chat.terminal_emissions) == 1


def test_controller_hitl_card_response_resumes_controller_pending_move():
    model = _ControllerHitlModel()
    chat = _HitlDriverChat(model)
    req = build_chat_request("", channel_id=5, request_id=3)
    req.InputResponse = ChatInputResponse(action="accept")
    _run(chat.chat(req))
    assert model.resumed_with == "approve"
    assert model._controller_hitl_pending is None
    assert len(chat.terminal_emissions) == 1


# --------------------------------------------------------------------------------------
# Phase 2 P1 — scope preflight gating
# --------------------------------------------------------------------------------------

def test_tools_missing_scope_unknown_gates_nothing():
    from ai.langgraph.mythic_tools import tools_missing_scope
    assert tools_missing_scope(None) == set()  # unknown scopes → don't gate (login fail-closed covers it)


def test_tools_missing_scope_partial_grant():
    from ai.langgraph.mythic_tools import tools_missing_scope
    disabled = tools_missing_scope({"callback.write"})
    assert "execute_capability" not in disabled       # callback.write granted → kept
    assert "create_payload" in disabled                # payload.write not granted → gated
    assert "add_credential" in disabled                # credential.write not granted → gated


def test_tools_missing_scope_full_grant_gates_nothing():
    from ai.langgraph.mythic_tools import tools_missing_scope, SCOPE_REQUIREMENTS
    assert tools_missing_scope(set(SCOPE_REQUIREMENTS.values())) == set()


def test_get_tools_skips_scope_gated():
    from ai.langgraph.mythic_tools import MythicTools
    mt = MythicTools(channel_id=5)
    mt.apply_scope_gating({"callback.write"})
    names = {t.name for t in mt.get_tools(["execute_capability", "create_payload", "list_callbacks"])}
    assert "create_payload" not in names               # gated (no payload.write)
    assert {"execute_capability", "list_callbacks"} <= names


def test_tools_missing_scope_star_gates_nothing():
    from ai.langgraph.mythic_tools import tools_missing_scope
    assert tools_missing_scope({"*"}) == set()  # SCOPE_ALL grants everything


def test_tools_missing_scope_resource_wildcard():
    from ai.langgraph.mythic_tools import tools_missing_scope
    disabled = tools_missing_scope({"callback.*"})   # wildcard grants all callback.* tools
    assert "execute_capability" not in disabled       # callback.write covered by callback.*
    assert "create_payload" in disabled                # payload.write not covered


def test_whoami_scopes_offline_returns_none():
    """No client (offline/degraded) → None → tools_missing_scope gates nothing (fail-closed still applies)."""
    from ai.langgraph.mythic_tools import MythicTools
    assert _run(MythicTools(channel_id=5).whoami_scopes()) is None


def test_whoami_scopes_parses_effective_scopes(monkeypatch):
    """Query the whoami action; prefer effective_scopes (server-expanded). Source: actions.graphql whoamiOutput."""
    from ai.langgraph import mythic_tools as mt

    async def _q(mythic, query):
        assert "whoami" in query and "effective_scopes" in query
        return {"whoami": {"effective_scopes": ["callback.write", "callback.read"], "scopes": ["callback.*"]}}

    monkeypatch.setattr(mt.mythic, "execute_custom_query", _q)
    client = mt.MythicTools(channel_id=5)
    client.client = object()  # non-None → runs the query
    assert _run(client.whoami_scopes()) == {"callback.write", "callback.read"}


def test_whoami_scopes_query_failure_returns_none(monkeypatch):
    from ai.langgraph import mythic_tools as mt

    async def _boom(mythic, query):
        raise RuntimeError("graphql down")

    monkeypatch.setattr(mt.mythic, "execute_custom_query", _boom)
    client = mt.MythicTools(channel_id=5)
    client.client = object()
    assert _run(client.whoami_scopes()) is None  # failure → gate nothing


def test_refresh_auth_context_relogins_only_on_token_change():
    chat = HeadlessSageChat()

    class _Client:
        def __init__(self):
            self.logins = 0
            self.apitoken_id = None

        async def login(self):
            self.logins += 1

    class _M:
        def __init__(self):
            self.apitoken_id = 1
            self.operation_id = 1
            self.mythic_client = _Client()

    m = _M()
    req = build_chat_request("hi", channel_id=5, operation_id=2)
    req.APITokenID = 99
    _run(chat._refresh_auth_context(m, req))
    assert m.apitoken_id == 99 and m.mythic_client.logins == 1  # token changed → re-login
    _run(chat._refresh_auth_context(m, req))
    assert m.mythic_client.logins == 1  # unchanged → no re-login


# --------------------------------------------------------------------------------------
# Full legacy config-option parity (restored: verbose, autonomous_solve, API_KEY,
# API_ENDPOINT, AWS quad) — see sage_chat/models.py
# --------------------------------------------------------------------------------------

# The complete set the operator saw when "creating Sage" as an agent, minus the per-turn `prompt`
# (which is ChatRequest.Prompt, not config), plus the chat-era `system_prompt`.
# `verbose` is deliberately NOT here — the chat container always runs full-detail (cards ARE the
# verbose view), so there is no operator verbose toggle.
_EXPECTED_CONFIG_OPTIONS = {
    "provider", "model", "mode", "autonomous_solve", "max_steps",
    "system_prompt", "API_ENDPOINT", "API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION",
}


def test_all_legacy_config_options_restored():
    meta = SAGE_MODELS[0].Metadata
    names = {opt.Name for opt in meta.ConfigurationOptions}
    assert names == _EXPECTED_CONFIG_OPTIONS
    # round-trips through to_json (what actually syncs to Mythic)
    json_names = {o["name"] for o in SAGE_MODELS[0].to_json()["metadata"]["configuration_options"]}
    assert json_names == _EXPECTED_CONFIG_OPTIONS


def test_secrets_are_optional_not_required():
    # Config-first order means nothing is a *required* secret; sensitive keys stay optional fallbacks.
    meta = SAGE_MODELS[0].Metadata
    assert meta.RequiredUserSecrets == []
    assert "API_KEY" in meta.OptionalUserSecrets
    assert "API_ENDPOINT" in meta.OptionalUserSecrets


def test_autonomous_is_boolean_option_and_verbose_removed():
    opts = {o.Name: o for o in SAGE_MODELS[0].Metadata.ConfigurationOptions}
    assert str(opts["autonomous_solve"].Type) == "boolean"
    assert "verbose" not in opts  # removed — chat container is always full-detail


def test_provider_is_choice_dropdown_not_freeform_string():
    """Restored the Mythic-v3 provider dropdown: `provider` is a Choice, not a freeform String. A freeform box
    lets a typo'd provider name through to init_chat_model, which then fails at model init. Values must be the
    ones config.py / _get_base_chat_model actually handle (bedrock special-cased; the rest via init_chat_model)."""
    opts = {o.Name: o for o in SAGE_MODELS[0].Metadata.ConfigurationOptions}
    prov = opts["provider"]
    assert str(prov.Type) == "choice"
    assert prov.DefaultValue == "openai"
    values = {c.Value for c in prov.Choices}
    assert values == {"openai", "bedrock", "anthropic", "ollama"}
    # "bedrock" must be exactly this string so config.py's `if provider == "bedrock"` AWS-quad branch fires
    assert "bedrock" in values
    # round-trips through to_json (what actually syncs to Mythic) with choices intact
    pj = [o for o in SAGE_MODELS[0].to_json()["metadata"]["configuration_options"] if o["name"] == "provider"][0]
    assert pj["type"] == "choice"
    assert {c["value"] for c in pj["choices"]} == {"openai", "bedrock", "anthropic", "ollama"}


def test_autonomous_solve_toggle_independent_of_mode():
    # the explicit toggle enables autonomy even when mode stays supervised
    kwargs = build_model_kwargs(build_chat_request("hi", config={"autonomous_solve": "true"}))
    assert kwargs["mode"] == "supervised" and kwargs["autonomous_solve"] is True


def test_api_key_and_endpoint_resolve_from_config(monkeypatch):
    # Config field wins over both the user secret and the container env for the sensitive keys.
    monkeypatch.setenv("API_KEY", "env-key")
    monkeypatch.setenv("API_ENDPOINT", "http://env-endpoint")
    req = build_chat_request(
        "hi",
        config={"API_KEY": "cfg-key", "API_ENDPOINT": "http://cfg-endpoint"},
        secrets={"API_KEY": "sec-key"},
    )
    configurable = build_model_kwargs(req)["config"]["configurable"]
    assert configurable["api_key"] == "cfg-key"
    assert configurable["base_url"] == "http://cfg-endpoint"


def test_bedrock_aws_quad_resolves_from_config():
    req = build_chat_request(
        "hi",
        config={
            "provider": "bedrock",
            "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "secret",
            "AWS_SESSION_TOKEN": "token", "AWS_DEFAULT_REGION": "us-west-2",
        },
    )
    configurable = build_model_kwargs(req)["config"]["configurable"]
    assert configurable["aws_access_key_id"] == "AKIA"
    assert configurable["aws_secret_access_key"] == "secret"
    assert configurable["aws_session_token"] == "token"
    assert configurable["region"] == "us-west-2"


# --------------------------------------------------------------------------------------
# Sub-agent card close: operator-stop badge + handback-summary echo into the drill-down
# --------------------------------------------------------------------------------------

class _RecEmitter:
    """Minimal response_emitter double: callable (text egress) + the two card-emit coroutines."""

    def __init__(self):
        self.subagent_calls = []
        self.agent_text_calls = []
        self.tool_use_calls = []
        self.text_sends = []

    async def __call__(self, formatted_message):
        self.text_sends.append(formatted_message)
        return True

    async def emit_subagent_status(self, **kw):
        self.subagent_calls.append(kw)
        return True

    async def emit_agent_text(self, **kw):
        self.agent_text_calls.append(kw)
        return True

    async def emit_tool_use(self, **kw):
        self.tool_use_calls.append(kw)
        return True


def _bare_model_with(emitter, delegations):
    """A Model with just the attributes the close-path touches (no heavy __init__)."""
    from ai.langgraph.model import Model
    m = Model.__new__(Model)
    m._response_emitter = emitter
    m._active_delegations = delegations
    m.verbose = False
    return m


def test_close_all_delegations_marks_open_cards_stopped():
    """Operator stop closes every still-open card with status 'stopped' (was: stuck 'running')."""
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {
        "Mythic_Operator": {"id": "mythic_operator:1", "name": "Mythic_Operator", "title": "t",
                            "tool_count": 3, "icon": "MO", "icon_color": "#3B82F6",
                            "final_summary": "", "last_text": ""},
    })

    _run(m._close_all_delegations(status="stopped"))

    assert m._active_delegations == {}                     # all closed, none left running
    assert len(emitter.subagent_calls) == 1
    call = emitter.subagent_calls[0]
    assert call["status"] == "stopped"
    assert call["complete"] is True
    assert call["icon_color"] == "#3B82F6"                 # color still applied on the stopped card


def test_close_delegation_echoes_handback_summary_to_drilldown():
    """The captured handback summary lands in BOTH the card content and the Open drill-down."""
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {
        "BloodHound": {"id": "bloodhound:1", "name": "BloodHound", "title": "t", "tool_count": 1,
                       "icon": "BH", "icon_color": "#E5484D",
                       "final_summary": "DONE — ingested job 228.", "last_text": "streamed reasoning"},
    })

    _run(m._close_delegation("BloodHound"))

    assert emitter.subagent_calls[0]["content"] == "DONE — ingested job 228."   # card summary
    assert len(emitter.agent_text_calls) == 1                                    # echoed to drill-down
    assert emitter.agent_text_calls[0]["content"] == "DONE — ingested job 228."
    assert emitter.agent_text_calls[0]["delegation_id"] == "bloodhound:1"


def test_close_delegation_does_not_reecho_streamed_last_text():
    """With no handback summary (only streamed last_text), nothing is re-echoed — last_text was
    already streamed to the drill-down live, so re-emitting it would duplicate the block."""
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {
        "Generalist": {"id": "generalist:1", "name": "Generalist", "title": "t", "tool_count": 0,
                       "icon": "GN", "icon_color": "#10B981",
                       "final_summary": "", "last_text": "already streamed"},
    })

    _run(m._close_delegation("Generalist"))

    assert emitter.agent_text_calls == []                                        # no re-echo
    assert emitter.subagent_calls[0]["content"] == "already streamed"            # still the card content


def test_run_operator_stop_shielded_streams_notice_and_stops_cards():
    """The shielded operator-stop cleanup streams the stop notice AND flips every open card to
    'stopped' — the fix for a mid-run card left stuck on 'running' after the operator hits stop."""
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {
        "BloodHound": {"id": "bloodhound:1", "name": "BloodHound", "title": "t", "tool_count": 1,
                       "icon": "BH", "icon_color": "#E5484D", "final_summary": "", "last_text": ""},
    })

    _run(m._run_operator_stop_shielded("\n🛑> Session stopped by operator.\n"))

    assert any("stopped by operator" in t for t in emitter.text_sends)   # notice reached egress
    assert m._active_delegations == {}                                    # card closed
    assert emitter.subagent_calls[-1]["status"] == "stopped"              # ...as stopped


def test_execute_capability_tool_card_uses_semantic_capability_header():
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {})
    m._classify_tool_source = lambda _tool_name: "mythic"

    _run(m._emit_tool_use_card(
        tool_call_id="call-capability",
        tool_name="execute_capability",
        status="started",
        complete=False,
        arguments={"action": {"name": "forge-golden-ticket"}, "inputs": {"callback_id": "3"}},
    ))

    assert len(emitter.tool_use_calls) == 1
    call = emitter.tool_use_calls[0]
    assert call["tool_name"] == "forge-golden-ticket"
    assert "Request: execute_capability(" in call["content"]


def test_capability_command_observer_surfaces_real_callback_command_name():
    from ai.langgraph.mythic_tools import MythicTools

    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {})
    m._classify_tool_source = lambda _tool_name: "mythic"
    mt = MythicTools(agent_task_id="test")
    mt.set_capability_command_observer(m._emit_capability_command_card)

    async def _binding(command_obj, _callback_id):
        return {
            "ok": True,
            "command": command_obj["command"],
            "parameters": command_obj["parameters"],
        }

    async def _issue(_command, _parameters, _callback_id, token_id=None, timeout=None):
        mt._last_issued_task_display_id = 42
        return "Ticket cache purged."

    mt._prepare_capability_command_binding = _binding
    mt.issue_task_and_waitfor_task_output = _issue

    item = _run(mt._execute_capability_command(
        {"command": "ticket_cache_purge", "parameters": "", "purpose": "clear stale tickets"},
        3,
        timeout=5,
        capability_name="forge-golden-ticket",
    ))

    assert item["command"] == "ticket_cache_purge"
    assert [call["status"] for call in emitter.tool_use_calls] == ["started", "completed"]
    assert [call["tool_name"] for call in emitter.tool_use_calls] == ["ticket_cache_purge", "ticket_cache_purge"]
    assert emitter.tool_use_calls[0]["tool_call_id"] == emitter.tool_use_calls[1]["tool_call_id"]
    assert "forge-golden-ticket" in emitter.tool_use_calls[0]["arguments"]
    assert emitter.tool_use_calls[1]["result_preview"] == "Ticket cache purged."


def test_capability_wait_observer_emits_operator_progress_messages():
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {})

    base = {
        "trace_id": "capability_command:1",
        "command": "wait_for_seconds",
        "parameters": {
            "seconds": 300,
            "reason": "wait for Group Policy refresh after GPO task write",
        },
    }
    _run(m._emit_capability_command_card({**base, "status": "started"}))
    _run(m._emit_capability_command_card({
        **base,
        "status": "progress",
        "result_preview": "1 minute elapsed; 4 minutes remaining",
    }))
    _run(m._emit_capability_command_card({**base, "status": "completed"}))

    assert "Waiting for propagation" in emitter.text_sends[0]
    assert "sleeping for 5 minutes" in emitter.text_sends[0]
    assert "No operator action is required" in emitter.text_sends[0]
    assert "4 minutes remaining" in emitter.text_sends[1]
    assert "No operator action is required" in emitter.text_sends[1]
    assert "Propagation wait complete" in emitter.text_sends[2]
    assert emitter.tool_use_calls == []


def test_tool_result_is_error_ignores_nested_errors_in_a_listing():
    """A data-listing result (e.g. get_task_history_for_callback) whose records include a historical
    'error' status must NOT tag the call Failed — only the TOP-LEVEL shape signals tool failure."""
    from ai.langgraph.model import _tool_result_is_error
    import json as _json

    listing = _json.dumps([
        {"command_name": "whoami", "status": "success"},
        {"command_name": "net_dclist", "status": "error"},    # a historical task, not this call
        {"command_name": "ls", "status": "completed"},
    ])
    assert _tool_result_is_error(listing) is False                        # the reported false-positive
    # Genuine failures are still detected:
    assert _tool_result_is_error(_json.dumps({"status": "error", "error": "boom"})) is True
    assert _tool_result_is_error("Error: something blew up") is True
    # Top-level success payloads unaffected:
    assert _tool_result_is_error(_json.dumps({"status": "success"})) is False


# --------------------------------------------------------------------------------------
# /state slash command — re-homed engagement-ledger show + edit
# --------------------------------------------------------------------------------------

def test_handle_state_shows_engagement_ledger(monkeypatch):
    """/state (no arg) renders the engagement hop ledger, not just session info."""
    from sage_chat import slash
    from ai.langgraph import engagement_ledger

    ledger = {
        "objective": "compromise CORP",
        "hops": [
            {"id": "da:corp.local", "effect": "da:corp.local", "status": "achieved",
             "evidence": {"mythic_task_id": 31, "callback_id": 1, "result_preview": "dcsync ok"}},
        ],
    }
    monkeypatch.setattr(engagement_ledger, "active_engagement_id", lambda: "Operation_Chimera_1")
    monkeypatch.setattr(engagement_ledger, "load", lambda eid=None: {"objective": ledger["objective"],
                                                                     "hops": [dict(h) for h in ledger["hops"]]})
    text = _run(slash._handle_state(None, build_chat_request("x"), ""))
    assert "Operation_Chimera_1" in text          # the engagement, not the channel
    assert "compromise CORP" in text              # objective surfaced
    assert "da:corp.local" in text                # a hop row
    assert "Achieved hops:** 1" in text


def test_handle_state_remove_mutates_and_saves(monkeypatch):
    """/state remove <row> drops the hop and persists via engagement_ledger.save."""
    from sage_chat import slash
    from ai.langgraph import engagement_ledger

    hops = [{"id": "a", "effect": "e1", "status": "x"}, {"id": "b", "effect": "e2", "status": "y"}]
    saved = {}
    monkeypatch.setattr(engagement_ledger, "active_engagement_id", lambda: "op")
    monkeypatch.setattr(engagement_ledger, "load", lambda eid=None: {"hops": [dict(h) for h in hops]})
    monkeypatch.setattr(engagement_ledger, "save", lambda d, eid=None: saved.update(data=d) or "path")

    text = _run(slash._handle_state(None, build_chat_request("x"), "remove 1"))
    assert "Removed 1 hop" in text
    assert [h["id"] for h in saved["data"]["hops"]] == ["b"]   # row 1 (1-based) removed


def test_handle_state_set_status_mutates_and_saves(monkeypatch):
    """/state set <row> <status> flips a hop's status and persists it."""
    from sage_chat import slash
    from ai.langgraph import engagement_ledger

    saved = {}
    monkeypatch.setattr(engagement_ledger, "active_engagement_id", lambda: "op")
    monkeypatch.setattr(engagement_ledger, "load",
                        lambda eid=None: {"hops": [{"id": "a", "effect": "e1", "status": "achieved"}]})
    monkeypatch.setattr(engagement_ledger, "save", lambda d, eid=None: saved.update(data=d) or "path")

    text = _run(slash._handle_state(None, build_chat_request("x"), "set 1 pending"))
    assert saved["data"]["hops"][0]["status"] == "pending"
    assert "pending" in text


# --------------------------------------------------------------------------------------
# HITL Respond/Select steering (Phase 3)
# --------------------------------------------------------------------------------------

def test_hitl_respond_select_steer_deny_and_carry_text():
    """Respond/Select must NEVER approve the guarded action (safety) — they map to deny AND surface the
    operator's free-text as the steering message. Accept → approve; Reject → deny with no steer text."""
    from sage_chat.hitl import resume_decision_for_request, resume_steer_message_for_request

    class _IR:
        def __init__(self, action, response=""):
            self.Action, self.Response = action, response

    class _Req:
        def __init__(self, ir):
            self.InputResponse = ir

    for act in ("respond", "select"):
        req = _Req(_IR(act, "use aes256, not rc4"))
        assert resume_decision_for_request(req) == "deny"                       # never blind-run
        assert resume_steer_message_for_request(req) == "use aes256, not rc4"   # guidance carried

    accept = _Req(_IR("accept", "ignored"))
    assert resume_decision_for_request(accept) == "approve"
    assert resume_steer_message_for_request(accept) == ""

    reject = _Req(_IR("reject"))
    assert resume_decision_for_request(reject) == "deny"
    assert resume_steer_message_for_request(reject) == ""

    class _Bare:
        InputResponse = None
    assert resume_decision_for_request(_Bare()) == "deny"                        # default-deny, safe
    assert resume_steer_message_for_request(_Bare()) == ""


# --------------------------------------------------------------------------------------
# Phase 2 header chips — live channel metadata
# --------------------------------------------------------------------------------------

def test_mythic_tools_preauth_client_adopts_without_mint():
    """Headless/eval auth (Option A): a preauth_client is adopted by login() directly — no channel/task
    context, no token mint — so the in-process gauge solve authenticates from the harness's admin client."""
    from ai.langgraph.mythic_tools import MythicTools

    sentinel = object()
    mt = MythicTools(preauth_client=sentinel)   # no channel_id, no agent_task_id
    _run(mt.login())
    assert mt.client is sentinel                # adopted as-is; the mint branches were skipped


def test_headless_solver_imports_and_wraps():
    """The in-process solve seam imports and exposes the harness-facing entry points."""
    from ai.hillclimb import headless_solver
    assert callable(headless_solver.run_headless_solve)
    assert callable(headless_solver.solve_headless)


def test_make_headless_solver_routes_to_in_process_solve(monkeypatch):
    """The alongside headless harness builder returns the same solve(objective)->status contract and
    routes into run_headless_solve with the injected client + pinned engagement id."""
    from ai.hillclimb import live_seams, headless_solver

    calls = {}

    async def _fake_run(objective, **kw):
        calls.update(objective=objective, **kw)
        return "completed"

    monkeypatch.setattr(headless_solver, "run_headless_solve", _fake_run)
    solve = live_seams.make_headless_solver("CLIENT", engagement_id="Operation_Chimera_1",
                                            operation_id=7, timeout=99, max_steps=0)
    assert solve("compromise CORP") == "completed"
    assert calls["objective"] == "compromise CORP"
    assert calls["client"] == "CLIENT"
    assert calls["engagement_id"] == "Operation_Chimera_1"
    assert calls["operation_id"] == 7


def test_build_channel_metadata_live_counts(monkeypatch):
    """The live header chips reflect MCP tool/server counts, session rounds, and BloodHound state."""
    from sage_chat.metadata import build_channel_metadata
    from ai import mcp

    monkeypatch.setattr(mcp.MCPManager, "get_tools_summary",
                        lambda: {"total_tools": 13, "connected_servers": 1}, raising=False)
    monkeypatch.setattr(mcp.MCPManager, "get_connected_servers", lambda: ["BloodHound"], raising=False)

    class _M:
        _global_step_count = 7
        model = "claude-sonnet-5"
        mode = "auto"
        _autonomous_solve = True

    items = {i["key"]: i for i in build_channel_metadata(_M())["items"]}
    assert items["mcp_tools"]["value"] == 13
    assert items["mcp_servers"]["value"] == 1
    assert items["rounds"]["value"] == 7
    assert items["bloodhound"]["value"] is True
    assert items["bloodhound"]["display_value"] == "connected"
    assert "mythic_tools" in items                       # scope-usable Mythic tool count present
    # Model / Mode / Autonomous render as accented ("info") chips so they stand out from the neutral ones.
    assert items["cfg_model"]["value"] == "claude-sonnet-5" and items["cfg_model"]["color"] == "info"
    assert items["cfg_mode"]["value"] == "auto" and items["cfg_mode"]["color"] == "info"
    assert items["cfg_autonomous"]["display_value"] == "on" and items["cfg_autonomous"]["color"] == "info"


def test_scope_usable_mythic_tools_reflects_disabled():
    """Scope-usable Mythic-tools = the declared universe minus the token's scope-gated tools."""
    from sage_chat.metadata import scope_usable_mythic_tools, _mythic_tool_universe

    universe = _mythic_tool_universe()
    assert len(universe) > 0                             # real frontmatter has Mythic tasking tools

    one = next(iter(universe))
    class _Client:
        disabled_tools = {one}
    class _M:
        mythic_client = _Client()
    assert scope_usable_mythic_tools(_M()) == len(universe) - 1          # a gated Mythic tool drops the count

    class _Client2:
        disabled_tools = {"not_a_real_mythic_tool_xyz"}
    class _M2:
        mythic_client = _Client2()
    assert scope_usable_mythic_tools(_M2()) == len(universe)             # gating a non-Mythic name is a no-op

    assert scope_usable_mythic_tools(object()) == len(universe)          # no client → full universe


def test_build_channel_metadata_degrades_safely(monkeypatch):
    """A header must never break a turn: MCP lookups failing → counts fall back to 0/off, no raise."""
    from sage_chat.metadata import build_channel_metadata
    from ai import mcp

    def _boom(*a, **k):
        raise RuntimeError("mcp down")

    monkeypatch.setattr(mcp.MCPManager, "get_tools_summary", _boom, raising=False)
    monkeypatch.setattr(mcp.MCPManager, "get_connected_servers", _boom, raising=False)

    items = {i["key"]: i for i in build_channel_metadata(object())["items"]}
    assert items["mcp_tools"]["value"] == 0
    assert items["mcp_servers"]["value"] == 0
    assert items["rounds"]["value"] == 0
    assert items["bloodhound"]["value"] is False
