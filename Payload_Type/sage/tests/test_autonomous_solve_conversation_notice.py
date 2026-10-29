"""Conversation mode stays read-only when Autonomous Solve is on, and the operator is told once.

The toggle used to coerce a conversation channel to `auto`, so a read-only channel silently ran the
autonomous kernel with no per-action approval. Auto authority now comes only from Mode=Autonomous.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sage_chat.config import autonomous_solve_ignored, build_model_kwargs  # noqa: E402
from sage_chat.headless import build_chat_request  # noqa: E402
from sage_chat.service import SageChat  # noqa: E402


@pytest.fixture
def service(monkeypatch):
    chat = SageChat.__new__(SageChat)
    sent: list[dict] = []

    async def _send_response(request, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(chat, "send_response", _send_response, raising=False)
    chat._sent = sent  # type: ignore[attr-defined]
    return chat


def _request(config):
    return build_chat_request("hi", config=config)


def _notify(service, session, request):
    asyncio.run(service._notify_autonomous_solve_ignored_once(session, request))


@pytest.mark.parametrize(
    ("config", "ignored"),
    (
        ({"mode": "conversation", "autonomous_solve": "true"}, True),
        ({"autonomous_solve": "true"}, True),
        ({"mode": "invalid", "autonomous_solve": "true"}, True),
        ({"mode": "conversation", "autonomous_solve": "false"}, False),
        ({"mode": "supervised", "autonomous_solve": "true"}, False),
        ({"mode": "auto", "autonomous_solve": "true"}, False),
    ),
)
def test_toggle_is_ignored_only_in_conversation(config, ignored):
    request = _request(config)
    assert autonomous_solve_ignored(request) is ignored
    if ignored:
        kwargs = build_model_kwargs(request)
        assert kwargs["mode"] == "conversation"
        assert kwargs["autonomous_solve"] is False


def test_conversation_with_toggle_is_told_once(service):
    session = SimpleNamespace(mode="conversation")
    request = _request({"mode": "conversation", "autonomous_solve": "true"})

    _notify(service, session, request)
    _notify(service, session, request)

    assert len(service._sent) == 1
    assert "read-only" in service._sent[0]["content"]
    assert service._sent[0]["complete"] is False


@pytest.mark.parametrize(
    ("session_mode", "config"),
    (
        ("conversation", {"mode": "conversation", "autonomous_solve": "false"}),
        ("supervised", {"mode": "supervised", "autonomous_solve": "true"}),
        ("auto", {"mode": "auto", "autonomous_solve": "true"}),
        # `/mode supervised` moved the session out of conversation; the toggle now applies.
        ("supervised", {"mode": "conversation", "autonomous_solve": "true"}),
    ),
)
def test_no_notice_when_the_toggle_is_not_ignored(service, session_mode, config):
    _notify(service, SimpleNamespace(mode=session_mode), _request(config))
    assert service._sent == []


def test_a_failed_notice_never_breaks_the_turn(service, monkeypatch):
    async def _boom(request, **kwargs):
        raise RuntimeError("mythic unavailable")

    monkeypatch.setattr(service, "send_response", _boom, raising=False)
    session = SimpleNamespace(mode="conversation")
    _notify(service, session, _request({"mode": "conversation", "autonomous_solve": "true"}))
    assert not getattr(session, "_autonomous_solve_ignored_notice_sent", False)
