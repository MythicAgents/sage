"""Bare and explicit native identity reports share the assisted slash route."""

import asyncio
from types import SimpleNamespace

import pytest
from mythic_container.ChatBase import ChatSlashCommandInvocation

from sage_chat.headless import HeadlessSageChat, build_chat_request
from sage_chat.models import SAGE_MODELS
import sage_chat.service as service
from sage_chat.slash import handle_slash


class _Chat(HeadlessSageChat):
    async def _get_or_create_model(self, request):
        raise AssertionError("report slash constructed a model")

    async def handle_identity_report(self, request, model):
        return await model.identity_report() if model is not None else "Retained task observations are unavailable."


def _request(argument="identity", *, mode="Sage"):
    request = build_chat_request("", model=mode, operation_id=72, channel_id=48, request_id=31)
    request.SlashCommand = ChatSlashCommandInvocation(name="report", argument=argument)
    return request


def _session(request, *, mode="conversation"):
    calls = []

    async def identity_report():
        calls.append("identity_report")
        return "**Identity report**\n\n| Principal | Evidence |\n| --- | --- |\n| Alice | task 1 |"

    return SimpleNamespace(
        mode=mode,
        operation_id=request.OperationID,
        apitoken_id=request.APITokenID,
        identity_report=identity_report,
    ), calls


@pytest.mark.parametrize("mode", ["conversation", "supervised"])
@pytest.mark.parametrize("argument", ["", "  ", "identity"])
def test_native_chat_report_uses_existing_session_once(monkeypatch, mode, argument):
    request = _request(argument)
    model, calls = _session(request, mode=mode)

    async def get_session(_request):
        return model

    async def first_claim(_request):
        return True

    monkeypatch.setattr(service, "get_channel_session", get_session)
    monkeypatch.setattr(service, "claim_channel_marker", first_claim)
    monkeypatch.setenv("SAGE_ASSISTED_REPORTING", "0")
    chat = _Chat()
    asyncio.run(chat.chat(request))

    assert calls == ["identity_report"]
    assert len(chat.terminal_emissions) == 1
    assert chat.terminal_emissions[0]["content"].startswith("**Identity report**")
    assert chat.terminal_emissions[0]["complete_request"] is True


@pytest.mark.parametrize("argument", ["Identity", "identity access", "identities", "access"])
def test_report_rejects_unknown_argument(argument):
    request = _request(argument)
    model, calls = _session(request)
    chat = _Chat()

    assert asyncio.run(handle_slash(chat, request, model, "report:31")) is True
    assert calls == []
    assert chat.terminal_emissions[0]["content"].startswith("Usage: `/report` or `/report identity`")


@pytest.mark.parametrize("argument", ["", "identity"])
def test_report_without_session_gives_actionable_guidance(argument):
    chat = _Chat()
    assert asyncio.run(handle_slash(chat, _request(argument), None, "report:31")) is True
    assert "unavailable" in chat.terminal_emissions[0]["content"]


@pytest.mark.parametrize("argument", ["", "identity"])
def test_report_refuses_auto_without_invoking_adapter(argument):
    request = _request(argument)
    model, calls = _session(request, mode="auto")
    chat = _Chat()
    assert asyncio.run(handle_slash(chat, request, model, "report:31")) is True
    assert calls == []
    assert "conversation or supervised" in chat.terminal_emissions[0]["content"]


def test_report_is_sage_only_and_ordinary_text_is_not_slash():
    ordinary, watcher = (model.Metadata for model in SAGE_MODELS)
    assert "report" in {command.Name for command in ordinary.SlashCommands}
    assert "report" not in {command.Name for command in watcher.SlashCommands}

    request = build_chat_request("Please report on identity access", operation_id=72, channel_id=48)
    model, calls = _session(request)
    chat = _Chat()
    assert asyncio.run(handle_slash(chat, request, model, "plain:31")) is False
    assert calls == []
    assert chat.emissions == []
