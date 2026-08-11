"""The operator learns BloodHound is unavailable in the chat, once, without the turn breaking.

The diagnostic this ISA raised to WARNING lands in the container log, which an operator has to go
looking for. F4 puts it where they already are. D5 (Russel, 2026-08-11) sets the cadence at once per
session: they saw it on the first degraded turn, and Mythic already renders a live BloodHound chip at
the top of the chat, so repeating it per turn would be a third copy of a fact that is on screen.

Three properties, and the third is the one that matters most: a notice about a degraded OPTIONAL
dependency must never be the thing that breaks a working turn. Sage went fully down on 2026-08-10
because two individually-correct features were fatally incompatible in combination, with 3955 tests
green — so the failure mode this file guards is "the helpful message took the product out".
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai.bloodhound_config as bloodhound_config  # noqa: E402
from sage_chat.service import SageChat  # noqa: E402


class _Session:
    """Stands in for the session model, which is where the once-per-session flag lives."""


@pytest.fixture
def service(monkeypatch):
    """A real SageChat with `send_response` captured rather than sent to Mythic."""
    chat = SageChat.__new__(SageChat)
    sent: list[dict] = []

    async def _send_response(request, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(chat, "send_response", _send_response, raising=False)
    chat._sent = sent  # type: ignore[attr-defined]
    return chat


@pytest.fixture
def unavailable(monkeypatch):
    monkeypatch.setattr(
        bloodhound_config,
        "bloodhound_tool_admission",
        lambda: {"ready": False, "reason": "BloodHound MCP is not connected."},
    )


@pytest.fixture
def available(monkeypatch):
    monkeypatch.setattr(bloodhound_config, "bloodhound_tool_admission", lambda: {"ready": True})


def _request():
    return SimpleNamespace(ChannelID=7, Config={}, Secrets={})


def _notify(service, session, request=None):
    return asyncio.run(service._notify_bloodhound_degraded_once(session, request or _request()))


def test_a_degraded_session_is_told_in_the_chat(service, unavailable):
    _notify(service, _Session())

    assert len(service._sent) == 1
    message = service._sent[0]["content"]
    assert "BloodHound is not connected" in message
    assert "Everything else works normally" in message, "must say what still works, not just what broke"
    assert bloodhound_config.BLOODHOUND_URL_KEY in message, "must say where to fix it"


def test_it_is_said_once_per_session_not_once_per_turn(service, unavailable):
    """D5. Four turns in the same session produce one notice."""
    session = _Session()
    for _ in range(4):
        _notify(service, session)

    assert len(service._sent) == 1, f"repeated the notice {len(service._sent)} times in one session"


def test_a_new_session_is_told_again(service, unavailable):
    """Right, not a bug: a new chat is a new operator context, and the answer may have changed."""
    _notify(service, _Session())
    _notify(service, _Session())

    assert len(service._sent) == 2


def test_a_working_bloodhound_says_nothing(service, available):
    _notify(service, _Session())

    assert service._sent == [], "a connected BloodHound must not announce itself every session"


def test_the_notice_never_breaks_the_turn(service, unavailable, monkeypatch):
    """The property that matters: an exception here must not propagate into the request path."""

    async def _explode(request, **kwargs):
        raise RuntimeError("Mythic rejected the response")

    monkeypatch.setattr(service, "send_response", _explode, raising=False)

    _notify(service, _Session())  # must not raise


def test_admission_failing_does_not_break_the_turn(service, monkeypatch):
    """Same, for the other thing that can throw: inspecting the MCP connection."""

    def _explode():
        raise RuntimeError("MCP inspection blew up")

    monkeypatch.setattr(bloodhound_config, "bloodhound_tool_admission", _explode)

    _notify(service, _Session())  # must not raise
    assert service._sent == []


def test_no_credential_value_reaches_the_chat(service, unavailable):
    """The chat surface is stored by Mythic, so it is a leak destination like the log."""
    request = SimpleNamespace(
        ChannelID=9,
        Config={"BLOODHOUND_TOKEN_KEY": "chat-sentinel-must-not-appear", "BLOODHOUND_URL": "http://bh:8080"},
        Secrets={},
    )

    _notify(service, _Session(), request)

    message = service._sent[0]["content"]
    assert "chat-sentinel-must-not-appear" not in message
    assert "BLOODHOUND_TOKEN_KEY" in message, "key names are still the actionable part"
