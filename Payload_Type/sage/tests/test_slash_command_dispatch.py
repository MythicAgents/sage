"""Native slash routing and operation-scoped state commands without a model call."""

import asyncio
from types import SimpleNamespace

import pytest
from mythic_container.ChatBase import ChatSlashCommandInvocation

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


def _bound_session(monkeypatch, namespace):
    from ai.langgraph import mythic_tools

    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", namespace)
    tools = mythic_tools.MythicTools(agent_task_id="state")
    tools._require_ledger_binding()
    return tools


def test_state_wipe_retracts_only_the_selected_operations_rows(monkeypatch, tmp_path, fresh_channel):
    from tests.esl_fixtures import seed_ledger

    sessions = {7: _bound_session(monkeypatch, "Operation_7_selected"),
                8: _bound_session(monkeypatch, "Operation_8_other")}
    for tools in sessions.values():
        seed_ledger(tools, hops=[{"technique": "t", "target": "a", "effect": "e", "status": "pending"}])

    async def resolve(_model, request):
        assert _model is None
        return sessions[request.OperationID]

    monkeypatch.setattr(slash, "_resolve_chat_ledger", resolve)
    chat = _NoModelChat()
    asyncio.run(chat.chat(_request("state", "wipe", operation_id=7)))

    assert len(chat.terminal_emissions) == 1
    text = chat.terminal_emissions[0]["content"]
    assert "Retracted 1 hop(s)" in text and "Nothing was deleted" in text
    assert sessions[7]._engagement_hops == [] and len(sessions[7]._ledger_snapshot().hop_rows()) == 1
    assert [hop.target for hop in sessions[8]._engagement_hops] == ["a"]


def test_tools_staged_renders_the_drop_zone_without_model(monkeypatch, fresh_channel, tmp_path):
    from ai.langgraph import ttp_library

    (tmp_path / "SharpHound.exe").write_bytes(b"not a pe")
    (tmp_path / "Mystery.exe").write_bytes(b"also not a pe")
    monkeypatch.setattr(ttp_library, "TOOLS_DIR", tmp_path)
    monkeypatch.setattr(ttp_library, "iter_ttps", lambda: iter([
        ("sharphound", {"name": "SharpHound", "binary_filename": "SharpHound.exe"}, ""),
    ]))
    chat = _NoModelChat()
    asyncio.run(chat.chat(_request("tools", "staged")))

    text = chat.terminal_emissions[0]["content"]
    assert "| sharphound | `SharpHound.exe` | unknown | — |" in text
    assert "`sharphound` | not checked |" in text
    assert "| mystery | `Mystery.exe` |" in text and "none — ask before use" in text
    assert "Mythic's file store was not checked" in text


def test_tools_rejects_unknown_subcommand(fresh_channel):
    chat = _NoModelChat()
    asyncio.run(chat.chat(_request("tools", "bogus")))
    assert "`/tools staged`" in chat.terminal_emissions[0]["content"]


_COMPATIBILITY_LABEL = "Stored compatibility objective (operation setting, not a channel goal):"


def test_new_channel_state_shows_its_operations_labelled_objective_without_model(monkeypatch, fresh_channel):
    """A channel with no live session: `/state` reads the request operation's own snapshot, labels its compatibility
    objective, hides a placeholder, and never builds a model (so no provider call). The retained-channel case after a
    restart is in test_state_after_restart.py."""
    sessions = {7: _bound_session(monkeypatch, "Operation_7_selected"),
                8: _bound_session(monkeypatch, "Operation_8_other")}
    sessions[7]._ledger_store().update_objective(sessions[7]._require_ledger_binding(), "collect graph", "operator")
    sessions[8]._ledger_store().update_objective(sessions[8]._require_ledger_binding(), "sage-engagement:solve-8",
                                                 "autonomous")
    resolved = []

    async def resolve(_model, request):
        assert _model is None
        resolved.append(request.OperationID)
        return sessions[request.OperationID]

    monkeypatch.setattr(slash, "_resolve_chat_ledger", resolve)
    chat = _NoModelChat()
    for operation_id in (7, 8):
        asyncio.run(chat.chat(_request("state", "", operation_id=operation_id)))
    asyncio.run(chat.chat(_request("state", "objective", operation_id=7)))
    asyncio.run(chat.chat(_request("state", "help", operation_id=7)))

    assert resolved == [7, 8, 7]  # valid help answers before resolving any ledger
    assert [emission["complete_request"] for emission in chat.terminal_emissions] == [True] * 4
    selected, other, usage, help_text = (emission["content"] for emission in chat.terminal_emissions)
    assert f"**{_COMPATIBILITY_LABEL}** collect graph" in selected.splitlines()
    assert "`Operation_7_selected`" in selected
    assert _COMPATIBILITY_LABEL not in other and "solve-8" not in other and "collect graph" not in other
    assert usage == ("Usage: `/state objective <text>` — set the operation's stored compatibility objective "
                     "(an operation setting, not a channel goal).")
    assert help_text.startswith("Usage:\n") and "Unknown" not in help_text
    assert "Verified ledger updates are saved immediately, including without `apply`." in help_text
    assert sessions[7]._ledger_snapshot().objective_setting() == ("collect graph", "operator")


# --- `/state help` is neutral usage; unknown subcommands keep their error prefix ---------------------------------------

_UNKNOWN_STATE = "Unknown `/state` subcommand. Usage:\n"
_PHASE6_USAGE_TEXT = (
    "- `/state` — show the engagement ledger\n",
    "from the cited task IDs, `cred:<id>` and `file:<id>` entries. Verified ledger updates are saved immediately, "
    "including without `apply`. `apply` only permits importing missing credential material into Mythic's "
    "credential store; without it, those imports are previewed.\n",
    "- `/state objective <text>` — set the operation's stored compatibility objective (an operation setting, not a "
    "channel goal)\n",
    "- `/state wipe` — retract every row",
)


def _assert_neutral_state_usage(text):
    assert text.startswith("Usage:\n") and "Unknown" not in text, text
    assert "- `/state help` — " in text
    for phrase in _PHASE6_USAGE_TEXT:
        assert phrase in text, phrase


def _ledger_must_not_resolve(monkeypatch):
    async def resolve(_model, _request):
        raise AssertionError("valid /state help resolved the engagement ledger")

    monkeypatch.setattr(slash, "_resolve_chat_ledger", resolve)


@pytest.mark.parametrize("argument", ["help", "HELP", "  Help\t", " hElP "])
def test_state_help_on_a_fresh_channel_is_neutral_usage_without_ledger_or_model(monkeypatch, fresh_channel, argument):
    _ledger_must_not_resolve(monkeypatch)
    chat = _NoModelChat()
    asyncio.run(chat.chat(_request("state", argument)))

    [emission] = chat.terminal_emissions
    assert emission["complete_request"] is True and "content" in emission, emission  # not an error emission
    _assert_neutral_state_usage(emission["content"])


@pytest.mark.parametrize("mode", ["conversation", "supervised"])
def test_state_help_on_an_active_channel_is_neutral_usage_without_ledger_or_model(monkeypatch, mode):
    request = _request("state", " Help ")
    active = SimpleNamespace(mode=mode, operation_id=request.OperationID, apitoken_id=request.APITokenID)

    async def active_session(_request):
        return active

    async def seen_channel(_request):
        return False

    monkeypatch.setattr(service, "get_channel_session", active_session)
    monkeypatch.setattr(service, "claim_channel_marker", seen_channel)
    _ledger_must_not_resolve(monkeypatch)
    chat = _NoModelChat()
    asyncio.run(chat.chat(request))

    [emission] = chat.terminal_emissions
    assert emission["complete_request"] is True and "content" in emission, emission  # not an error emission
    _assert_neutral_state_usage(emission["content"])


@pytest.mark.parametrize("argument", ["unknown-source-check", "help extra", "helpme", "?", "--help"])
def test_unknown_state_subcommands_keep_the_error_prefix(monkeypatch, fresh_channel, argument):
    tools = _bound_session(monkeypatch, "Operation_7_selected")

    async def resolve(_model, _request):
        return tools

    monkeypatch.setattr(slash, "_resolve_chat_ledger", resolve)
    chat = _NoModelChat()
    asyncio.run(chat.chat(_request("state", argument)))

    [emission] = chat.terminal_emissions
    text = emission["content"]
    assert emission["complete_request"] is True
    assert text.startswith(_UNKNOWN_STATE) and text.count("Unknown") == 1, text
    _assert_neutral_state_usage(text[len("Unknown `/state` subcommand. "):])


@pytest.mark.parametrize("argument", ["", "show", " SHOW "])
def test_bare_state_and_show_still_render_the_ledger(monkeypatch, fresh_channel, argument):
    from tests.esl_fixtures import seed_ledger

    tools = _bound_session(monkeypatch, "Operation_7_selected")
    seed_ledger(tools, hops=[{"technique": "t", "target": "a", "effect": "e", "status": "pending"}])

    async def resolve(_model, _request):
        return tools

    monkeypatch.setattr(slash, "_resolve_chat_ledger", resolve)
    chat = _NoModelChat()
    asyncio.run(chat.chat(_request("state", argument)))

    [emission] = chat.terminal_emissions
    text = emission["content"]
    assert "`Operation_7_selected`" in text and "Usage:" not in text and "Unknown" not in text
