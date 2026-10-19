"""A read-only native /state can inspect a retained ledger after Sage restarts."""

import asyncio

import pytest
from mythic_container.ChatBase import ChatMessageContext, ChatSlashCommandInvocation

from sage_chat.headless import HeadlessSageChat, build_chat_request
import sage_chat.service as service
import sage_chat.slash as slash
from ai.langgraph import engagement_ledger


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


@pytest.mark.parametrize("argument", ["", "  \t"])
@pytest.mark.parametrize("operation_id", [7, 8])
def test_read_only_state_on_restarted_channel_uses_exact_operation_ledger(
    monkeypatch, restarted_channel, argument, operation_id,
):
    async def resolve(_model, request):
        assert _model is None
        return f"Operation_{request.OperationID}_retained"

    monkeypatch.setattr(slash, "_resolve_chat_engagement_id", resolve)
    monkeypatch.setattr(engagement_ledger, "load_runtime", lambda engagement_id: {
        "objective": f"objective for {engagement_id}", "hops": [],
    })
    chat = _NoModelChat()
    asyncio.run(chat.chat(restarted_channel(name="state", argument=argument,
                                           operation_id=operation_id)))

    assert len(chat.terminal_emissions) == 1
    text = chat.terminal_emissions[0]["content"]
    assert f"Operation_{operation_id}_retained" in text
    assert f"Operation_{15 - operation_id}_retained" not in text
    assert f"objective for Operation_{operation_id}_retained" in text
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
