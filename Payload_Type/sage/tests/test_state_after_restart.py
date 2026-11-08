"""A read-only native /state can inspect a retained ledger after Sage restarts."""

import asyncio

import pytest
from mythic_container.ChatBase import ChatMessageContext, ChatSlashCommandInvocation

from sage_chat.headless import HeadlessSageChat, build_chat_request
import sage_chat.service as service
import sage_chat.slash as slash


class _NoModelChat(HeadlessSageChat):
    async def _get_or_create_model(self, request):
        raise AssertionError("a restarted channel must not construct a model")


@pytest.fixture
def restarted_channel(monkeypatch):
    async def no_session(_request):
        return None

    async def seen_channel(_request):
        return False

    monkeypatch.setattr(service, "get_channel_session", no_session)
    monkeypatch.setattr(service, "claim_channel_marker", seen_channel)

    def request(prompt="", *, name=None, argument="", operation_id=7, request_id=23):
        req = build_chat_request(prompt, channel_id=990024, operation_id=operation_id,
                                 request_id=request_id)
        if name is not None:
            req.SlashCommand = ChatSlashCommandInvocation(name=name, argument=argument)
        req.RequestMessageID = 1535
        req.Context = [
            ChatMessageContext(id=1534, author_type="operator", message="prior scope"),
            ChatMessageContext(id=1535, author_type="operator", message=prompt or "/state"),
        ]
        return req

    return request


_RETAINED_OBJECTIVES = {
    7: "objective for Operation_7_retained",
    8: "objective for Operation_8_retained",
    9: "sage-engagement:solve-9",  # an internal identifier, never shown as an objective
}
_COMPATIBILITY_LABEL = "Stored compatibility objective (operation setting, not a channel goal):"


@pytest.mark.parametrize("argument", ["", "  \t"])
@pytest.mark.parametrize("operation_id", sorted(_RETAINED_OBJECTIVES))
def test_read_only_state_on_restarted_channel_uses_exact_operation_ledger(
    monkeypatch, restarted_channel, argument, operation_id,
):
    from ai.langgraph import mythic_tools

    sessions = {}
    for op, objective in _RETAINED_OBJECTIVES.items():
        key = f"Operation_{op}_retained"
        monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", key)
        tools = mythic_tools.MythicTools(agent_task_id="state")
        tools._ledger_store().update_objective(tools._require_ledger_binding(), objective, "operator")
        sessions[op] = tools

    async def resolve(_model, request):
        assert _model is None
        return sessions[request.OperationID]

    monkeypatch.setattr(slash, "_resolve_chat_ledger", resolve)
    chat = _NoModelChat()
    asyncio.run(chat.chat(restarted_channel(name="state", argument=argument,
                                           operation_id=operation_id)))

    assert len(chat.terminal_emissions) == 1
    text = chat.terminal_emissions[0]["content"]
    assert f"`Operation_{operation_id}_retained`" in text
    assert not any(f"Operation_{op}_retained" in text for op in _RETAINED_OBJECTIVES if op != operation_id)
    objective_lines = [line for line in text.splitlines() if _COMPATIBILITY_LABEL in line]
    if operation_id == 9:
        assert objective_lines == [] and "solve-9" not in text
    else:
        assert objective_lines == [f"**{_COMPATIBILITY_LABEL}** {_RETAINED_OBJECTIVES[operation_id]}"]
    assert "prior scope and evidence are not restored" not in text


@pytest.mark.parametrize("name,argument,prompt", [
    (None, "", "continue the investigation"),
    (None, "", "/state"),
    ("state", "wipe", ""),
    ("state", "objective changed", ""),
    ("mode", "", ""),
])
def test_restarted_channel_still_blocks_prompts_and_other_slash_inputs(
    monkeypatch, restarted_channel, name, argument, prompt,
):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("guarded input reached slash dispatch")

    monkeypatch.setattr(service, "handle_slash", forbidden)
    chat = _NoModelChat()
    asyncio.run(chat.chat(restarted_channel(prompt, name=name, argument=argument)))

    assert len(chat.terminal_emissions) == 1
    assert "prior scope and evidence are not restored" in chat.terminal_emissions[0]["content"]
