"""Operator tool discovery goes through deterministic native chat, without a model turn."""

import asyncio
from types import SimpleNamespace

import pytest
from mythic_container.ChatBase import ChatSlashCommandInvocation

from ai.langgraph.model import _looks_like_scoped_callback_inventory_prompt
from ai.langgraph import model as graph_model
from sage_chat.headless import HeadlessSageChat, build_chat_request
import sage_chat.service as service
from sage_chat.slash import WATCHER_SLASH_COMMANDS, looks_like_tool_inventory_prompt, render_tool_inventory


class _NoModelChat(HeadlessSageChat):
    async def _get_or_create_model(self, request):
        raise AssertionError("tool discovery constructed a model")


@pytest.fixture
def fresh_channel(monkeypatch):
    async def no_session(_request):
        return None

    async def first_claim(_request):
        return True

    monkeypatch.setattr(service, "get_channel_session", no_session)
    monkeypatch.setattr(service, "claim_channel_marker", first_claim)


@pytest.mark.parametrize("prompt", ["list your tools", "What Mythic tools do you have?"])
def test_natural_inventory_uses_public_tool_data_without_model(fresh_channel, prompt):
    chat = _NoModelChat()
    request = build_chat_request(prompt, operation_id=9, channel_id=901, request_id=51)
    asyncio.run(chat.chat(request))
    assert len(chat.terminal_emissions) == 1
    content = chat.terminal_emissions[0]["content"]
    assert "`list_callbacks`" in content
    assert "`issue_task_and_waitfor_task_output`" in content
    assert "read-only" in content and "guarded" in content
    assert "commands installed on a payload callback" in content
    assert "No active Sage session" in content


def test_structured_tools_dispatch_and_scope_from_existing_session(monkeypatch):
    model = SimpleNamespace(mode="supervised", mythic_client=SimpleNamespace(
        disabled_tools={"issue_task_and_waitfor_task_output"}))

    async def current_session(_request):
        return model

    async def existing_claim(_request):
        return False

    monkeypatch.setattr(service, "get_channel_session", current_session)
    monkeypatch.setattr(service, "claim_channel_marker", existing_claim)
    async def keep_session(_self, _request, existing):
        return existing

    async def refresh(_self, _model, _request):
        return None

    monkeypatch.setattr(service.SageChat, "_rotate_auth_changed_session", keep_session)
    monkeypatch.setattr(service.SageChat, "_refresh_auth_context", refresh)
    chat = _NoModelChat()
    request = build_chat_request("", operation_id=9, channel_id=902, request_id=52)
    request.SlashCommand = ChatSlashCommandInvocation(name="tools", argument="")
    asyncio.run(chat.chat(request))
    content = chat.terminal_emissions[0]["content"]
    assert "`issue_task_and_waitfor_task_output` | guarded; unavailable to this token" in content
    assert "`execute_capability` | guarded; native approval required" in content


def test_tool_discovery_is_available_after_restart_without_recovered_scope(monkeypatch):
    async def no_session(_request):
        return None

    async def old_channel(_request):
        return False

    monkeypatch.setattr(service, "get_channel_session", no_session)
    monkeypatch.setattr(service, "claim_channel_marker", old_channel)
    chat = _NoModelChat()
    request = build_chat_request("list your tools", operation_id=9, channel_id=903, request_id=53)
    asyncio.run(chat.chat(request))
    assert "No active Sage session" in chat.terminal_emissions[0]["content"]


@pytest.mark.parametrize("structured", [False, True])
def test_changed_token_inventory_uses_unverified_scope(monkeypatch, structured):
    old = SimpleNamespace(apitoken_id=-1, operation_id=9, mode="supervised",
                          mythic_client=SimpleNamespace(disabled_tools=set()))
    dropped = []

    async def old_session(_request):
        return old

    async def previous_claim(_request):
        return False

    async def stop(_self, _model, **_kwargs):
        dropped.append("stop")

    async def drop(_request, **_kwargs):
        dropped.append("drop")

    monkeypatch.setattr(service, "get_channel_session", old_session)
    monkeypatch.setattr(service, "claim_channel_marker", previous_claim)
    monkeypatch.setattr(service, "drop_channel_session", drop)
    monkeypatch.setattr(service.SageChat, "_stop_and_close_request_lifecycles", stop)
    request = build_chat_request("list your tools", operation_id=9, channel_id=904,
                                 request_id=54)
    assert request.APITokenID != old.apitoken_id
    if structured:
        request.SlashCommand = ChatSlashCommandInvocation(name="tools", argument="")
    chat = _NoModelChat()
    asyncio.run(chat.chat(request))
    assert "No active Sage session" in chat.terminal_emissions[0]["content"]
    assert dropped == ["stop", "drop"]


def test_conversation_inventory_labels_guarded_tools_unavailable():
    model = SimpleNamespace(mode="conversation", mythic_client=SimpleNamespace(disabled_tools=set()))
    text = render_tool_inventory(model)
    assert "`list_callbacks` | read-only" in text
    assert "`issue_task_and_waitfor_task_output` | guarded; unavailable in conversation mode" in text
    assert "tools" not in {command.Name for command in WATCHER_SLASH_COMMANDS}


def test_exact_callback_literal_invokes_existing_one_read_table_path():
    m = object.__new__(graph_model.Model)
    m.mode = "supervised"
    m.command_name = "chat"
    m._autonomous_solve = False
    m._supervised_objective_active = False
    m._controller_hitl_pending = None
    m._thread_id_override = "channel-tool-discovery"
    m._running_tasks = set()
    m._message_seq = 1
    m.graph = object()
    m.state = {"messages": [], "_message_seq": 1}
    m.provider = "test"
    m.model = "test"
    m.is_interactive = True
    reads = []
    streamed = []

    class FakeMythic:
        async def list_callbacks(self):
            reads.append("list_callbacks")
            return [{"id": 1, "agent": "apollo", "host": "TEST-HOST", "user": "operator",
                     "integrity": 2, "status": "alive", "secs_since_checkin": 1}]

    async def no_interrupt(_thread_id):
        return False

    async def stream(text):
        streamed.append(text)
        return True

    m.mythic_client = FakeMythic()
    m._hitl_interrupt_pending = no_interrupt
    m._seed_autonomous_objective = lambda _prompt: None
    m._format_message_for_streaming = lambda msg, agent_name=None: msg.content
    m._stream_message_to_mythic = stream
    assert asyncio.run(m.invoke("list_callbacks", is_interactive=True)) == ""
    assert reads == ["list_callbacks"]
    assert any("| 1 | apollo | TEST-HOST | operator |" in text for text in streamed)


def test_discovery_matchers_do_not_swallow_actions_or_examples():
    for prompt in (
        "list your tools and run whoami", "don't list your tools",
        'the user said "list your tools"', "list your tools for callback 1",
    ):
        assert not looks_like_tool_inventory_prompt(prompt)
    assert _looks_like_scoped_callback_inventory_prompt("list_callbacks")
    for prompt in (
        "list_callbacks and run whoami", "do not list_callbacks", "'list_callbacks'",
        "list_callbacks for callback 1",
    ):
        assert not _looks_like_scoped_callback_inventory_prompt(prompt)


def test_compound_operator_request_reaches_normal_chat_path(fresh_channel):
    reached = []

    class ProbeChat(HeadlessSageChat):
        async def _get_or_create_model(self, request):
            reached.append(request.Prompt)
            raise RuntimeError("normal chat path reached")

    request = build_chat_request("list your tools and run whoami", operation_id=9,
                                 channel_id=905, request_id=55)
    asyncio.run(ProbeChat().chat(request))
    assert reached == ["list your tools and run whoami"]
