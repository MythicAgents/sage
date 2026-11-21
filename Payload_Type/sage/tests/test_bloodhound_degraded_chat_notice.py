"""BloodHound availability is not announced in every new transcript; a lost connection is, once per channel.

Until 2026-11, this file asserted a "BloodHound is not connected" notice once per session. Every settings change,
model switch or restore builds a new session, so the notice repeated in ordinary chat that never needed
BloodHound. Availability now lives on the BloodHound chip, the BloodHound agent explains a missing BloodHound
when it is asked to use it, and the only notice is the one a channel gets when a connection it saw working is
lost. These cases exercise ``_observe_bloodhound_turn`` with admission and the MCP registry stood in (wiring);
``test_bloodhound_turn_state.py`` drives the same path through the real chat handler and registry.

The property this file has always guarded still holds: observing an optional dependency must never be the thing
that breaks a working turn.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai.bloodhound_config as bloodhound_config  # noqa: E402
from ai.mcp import MCPManager  # noqa: E402
from sage_chat.service import SageChat  # noqa: E402
from tests.test_bloodhound_ingest_contract import FakeIngestTool, install_bloodhound  # noqa: E402


class _Session:
    """Stands in for the session model, which carries the turn's observed state."""


class _DomainInfo:
    """`domain_info` answering a read (the server's list envelope)."""

    name = "domain_info"

    async def ainvoke(self, args):
        return [{"type": "text", "text": json.dumps({"info_type": "list", "data": [{"name": "OFFLINE.LOCAL"}]})}]


@pytest.fixture(autouse=True)
def _no_host_bloodhound(monkeypatch):
    monkeypatch.delenv("SAGE_BLOODHOUND_MCP_DIR", raising=False)
    for key in ("BLOODHOUND_URL", "BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY", "BLOODHOUND_DOMAIN"):
        monkeypatch.delenv(key, raising=False)


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
def not_configured(monkeypatch):
    monkeypatch.setattr(MCPManager, "get_connected_servers", lambda: [])


def _admitted(monkeypatch):
    monkeypatch.setattr(
        bloodhound_config, "bloodhound_tool_admission", lambda: {"ready": True, "server": "BloodHound"}
    )


@pytest.fixture
def available(monkeypatch):
    """Connected with the exact tools, and serving the image's MCP (status patch present)."""
    _admitted(monkeypatch)
    tool = FakeIngestTool()
    install_bloodhound(monkeypatch, tool, extra_tools=(_DomainInfo(),))
    return tool


@pytest.fixture
def connected_without_status(monkeypatch):
    """Connected with the exact tools and answering reads, but serving the pinned six-handler base MCP."""
    _admitted(monkeypatch)
    tool = FakeIngestTool(patched=False)
    install_bloodhound(monkeypatch, tool, extra_tools=(_DomainInfo(),))
    return tool


def _request():
    return SimpleNamespace(ChannelID=7, Config={}, Secrets={})


def _observe(service, session, request=None):
    return asyncio.run(service._observe_bloodhound_turn(session, request or _request()))


def test_an_unconfigured_chat_is_not_told_in_the_chat(service, not_configured):
    """What to fix moved from a per-session notice to the BloodHound agent's guard."""
    session = _Session()
    _observe(service, session)

    assert service._sent == []
    state = session._bloodhound_turn_state
    assert state.state == bloodhound_config.BLOODHOUND_STATE_NOT_CONFIGURED
    assert bloodhound_config.BLOODHOUND_URL_KEY in state.guard_text, "the guard must say where to fix it"
    assert "did not read any graph data" in state.guard_text


def test_no_turn_in_a_session_announces_it(service, not_configured):
    session = _Session()
    for _ in range(4):
        _observe(service, session)

    assert service._sent == [], f"announced {len(service._sent)} times in one session"


def test_a_new_session_is_not_told_either(service, not_configured):
    """A new session is what a settings change, model switch or restore builds; none of them repeat it."""
    _observe(service, _Session())
    _observe(service, _Session())

    assert service._sent == []


def test_a_working_bloodhound_says_nothing(service, available):
    _observe(service, _Session())

    assert service._sent == [], "a connected BloodHound must not announce itself every session"
    assert available.calls == [{"info_type": "status", "job_id": 0}], "working means the status read answered"


def test_a_connected_bloodhound_that_cannot_report_ingest_status_reads_degraded_without_a_notice(
    service, connected_without_status
):
    session = _Session()
    for _ in range(3):
        _observe(service, session)

    assert service._sent == []
    state = session._bloodhound_turn_state
    assert state.state == bloodhound_config.BLOODHOUND_STATE_DEGRADED
    assert "Unknown info_type 'status'" in state.reason
    assert state.guard_text == "", "reads work, so the BloodHound agent keeps its tools"


def test_a_healthy_server_is_re_read_each_turn_and_a_later_breakage_is_reported(service, available, monkeypatch):
    session = _Session()
    _observe(service, session)
    _observe(service, session)
    assert service._sent == [] and len(available.calls) == 2, "healthy is observed, never remembered"
    install_bloodhound(monkeypatch, FakeIngestTool(patched=False), extra_tools=(_DomainInfo(),))
    _observe(service, session)
    _observe(service, session)

    assert [sent["response_key"] for sent in service._sent] == ["bloodhound_lost:7"], "once, not per turn"
    assert "Graph reads still work" in service._sent[0]["content"]


def test_the_observation_never_breaks_the_turn(service, available, monkeypatch):
    """The property that matters: an exception here must not propagate into the request path."""
    _observe(service, _Session())
    install_bloodhound(monkeypatch, FakeIngestTool(patched=False), extra_tools=(_DomainInfo(),))

    async def _explode(request, **kwargs):
        raise RuntimeError("Mythic rejected the response")

    monkeypatch.setattr(service, "send_response", _explode, raising=False)

    _observe(service, _Session())  # must not raise


def test_admission_failing_does_not_break_the_turn(service, monkeypatch):
    """Same, for the other thing that can throw: inspecting the MCP connection."""

    def _explode():
        raise RuntimeError("MCP inspection blew up")

    monkeypatch.setattr(bloodhound_config, "bloodhound_tool_admission", _explode)
    session = _Session()

    _observe(service, session)  # must not raise
    assert service._sent == []
    assert session._bloodhound_turn_state is None, "no state: the chip and the guard fall back"


def test_no_credential_value_reaches_the_chat(service, not_configured):
    """The guard's text is stored by Mythic, so it is a leak destination like the log."""
    request = SimpleNamespace(
        ChannelID=9,
        Config={"BLOODHOUND_TOKEN_KEY": "chat-sentinel-must-not-appear", "BLOODHOUND_URL": "http://bh:8080"},
        Secrets={},
    )
    session = _Session()

    _observe(service, session, request)

    state = session._bloodhound_turn_state
    assert state.state == bloodhound_config.BLOODHOUND_STATE_UNAVAILABLE
    for surface in (state.guard_text, state.reason):
        assert "chat-sentinel-must-not-appear" not in surface
    assert "BLOODHOUND_TOKEN_KEY" in state.guard_text, "key names are still the actionable part"
