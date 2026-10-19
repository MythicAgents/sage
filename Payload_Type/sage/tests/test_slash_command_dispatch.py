"""Native slash routing and operation-scoped state commands without a model call."""

import asyncio
from types import SimpleNamespace

import pytest
from mythic_container.ChatBase import ChatSlashCommandInvocation

from ai.langgraph import engagement_ledger
from sage_chat.headless import HeadlessSageChat, build_chat_request
import sage_chat.service as service
import sage_chat.slash as slash


class _NoModelChat(HeadlessSageChat):
    async def _get_or_create_model(self, request):
        raise AssertionError("structured slash input constructed a model")

    async def handle_finding_command(self, request, model):
        return "routed:findings"

    async def handle_watcher_command(self, request, argument):
        return "routed:watcher"


def _request(name, argument="", *, operation_id=7, channel_id=24, request_id=23):
    request = build_chat_request("", operation_id=operation_id, channel_id=channel_id,
                                 request_id=request_id)
    request.SlashCommand = ChatSlashCommandInvocation(name=name, argument=argument)
    return request


@pytest.fixture
def fresh_channel(monkeypatch):
    async def no_session(_request):
        return None

    async def first_claim(_request):
        return True

    monkeypatch.setattr(service, "get_channel_session", no_session)
    monkeypatch.setattr(service, "claim_channel_marker", first_claim)


_DECLARED_DISPATCH_SCENARIOS = [
    ("findings", None),
    ("watcher", None),
    ("report", "_handle_report"),
    ("state", "_handle_state"),
    ("list", "_handle_list"),
    ("tools", "render_tool_inventory"),
    ("mode", "_handle_mode"),
    ("stop", "_handle_stop"),
    ("mcp", "_handle_mcp"),
    ("bloodhound", "_handle_bloodhound"),
    ("sandbox", "_handle_sandbox"),
]


def test_dispatch_scenarios_cover_every_declared_slash():
    assert {name for name, _handler in _DECLARED_DISPATCH_SCENARIOS} == {
        command.Name for command in slash.SAGE_SLASH_COMMANDS
    }


@pytest.mark.parametrize("name,handler", _DECLARED_DISPATCH_SCENARIOS)
def test_every_declared_slash_routes_once_without_model(
    monkeypatch, fresh_channel, name, handler,
):
    if handler in {"_handle_mode", "render_tool_inventory"}:
        monkeypatch.setattr(slash, handler, lambda *_args: f"routed:{name}")
    elif handler is not None:
        async def routed(*_args):
            return f"routed:{name}"

        monkeypatch.setattr(slash, handler, routed)

    chat = _NoModelChat()
    asyncio.run(chat.chat(_request(name)))

    assert len(chat.terminal_emissions) == 1
    assert chat.terminal_emissions[0]["content"] == f"routed:{name}"
    assert chat.terminal_emissions[0]["complete_request"] is True


def test_list_renders_real_session_inventory_without_model(monkeypatch, fresh_channel):
    from ai.langgraph import model as graph_model

    async def sessions():
        return {
            "24": SimpleNamespace(provider="openai", model="model-a", mode="supervised"),
            "25": SimpleNamespace(provider="bedrock", model="model-b", mode="conversation"),
        }

    monkeypatch.setattr(graph_model, "list_sessions", sessions)
    chat = _NoModelChat()
    asyncio.run(chat.chat(_request("list")))

    assert len(chat.terminal_emissions) == 1
    text = chat.terminal_emissions[0]["content"]
    assert "**Active Sage sessions**" in text
    assert "| `24` | `openai` | `model-a` | `supervised` |" in text
    assert "| `25` | `bedrock` | `model-b` | `conversation` |" in text


def test_state_wipe_removes_only_selected_operation_ledger(
    monkeypatch, tmp_path, fresh_channel,
):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    selected = "Operation_7_selected"
    other = "Operation_8_other"
    engagement_ledger.save_runtime({"engagement_id": selected, "objective": "selected",
                                    "hops": []}, selected)
    engagement_ledger.save_runtime({"engagement_id": other, "objective": "other",
                                    "hops": []}, other)

    async def resolve(_model, request):
        assert _model is None
        return selected if request.OperationID == 7 else other

    monkeypatch.setattr(slash, "_resolve_chat_engagement_id", resolve)
    chat = _NoModelChat()
    asyncio.run(chat.chat(_request("state", "wipe", operation_id=7)))

    assert len(chat.terminal_emissions) == 1
    assert "Wiped the ledger" in chat.terminal_emissions[0]["content"]
    assert not (tmp_path / "state_Operation_7_selected.json").exists()
    assert (tmp_path / "state_Operation_8_other.json").exists()
    assert engagement_ledger.load_runtime(other)["objective"] == "other"
