"""A failed BloodHound auto-connect must be logged where the container can actually emit it.

Mythic runs the Sage container at `DEBUG_LEVEL=warning` and owns that setting, so anything Sage
writes at INFO or DEBUG is discarded before an operator sees it. `credential_diagnostic()` exists
precisely to turn `McpError: Connection closed` into a statement of which credentials arrived and
which did not — and it was emitted at INFO, so the container log carried four connect failures and
zero explanations. The message was correct, written, and invisible.

This is the regression that would have caught it. It asserts the LEVEL of the emitted record, not
the presence of a line in captured output at a level the test itself configured, because the latter
passes just as happily against the defect. Lower any of these emissions back to INFO or DEBUG and
these tests go red.

No network, no MCP subprocess, no Mythic: the connect helper is replaced and the real method under
test is driven directly.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai.bloodhound_config as bloodhound_config  # noqa: E402
from sage_chat.service import SageChat  # noqa: E402

#: A value that must never be logged. Long and distinctive so a partial leak is still caught.
SENTINEL_TOKEN = "sentinel-token-value-must-never-appear-in-a-log-line"


def _service() -> SageChat:
    """The real class, without running Mythic's `Chat.__init__`.

    `__new__` rather than a hand-built stand-in: a stand-in only tests the parts someone remembered
    to copy, and stops matching the real object silently when it changes.
    """
    return SageChat.__new__(SageChat)


def _drive(service: SageChat, **kwargs):
    return asyncio.run(service._ensure_bloodhound_connected(**kwargs))


@pytest.fixture
def failing_connect(monkeypatch: pytest.MonkeyPatch):
    """Make the connect fail the way an unconfigured container does."""

    async def _connect(env=None):
        return False, "Connection closed"

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _connect)
    monkeypatch.setattr(
        bloodhound_config,
        "bloodhound_tool_admission",
        lambda: {"ready": False, "reason": "not connected"},
    )


@pytest.fixture
def raising_connect(monkeypatch: pytest.MonkeyPatch):
    """The other invisible path: the helper raises and fail-soft chat swallows it."""

    async def _connect(env=None):
        raise RuntimeError("MCP stdio launch failed")

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _connect)
    return _connect


def _bloodhound_records(caplog: pytest.LogCaptureFixture):
    return [r for r in caplog.records if "BloodHound auto-connect" in r.getMessage()]


def test_failed_connect_is_logged_at_warning_or_above(failing_connect, caplog):
    caplog.set_level(logging.DEBUG)
    assert _drive(_service()) is False

    records = _bloodhound_records(caplog)
    assert records, "a failed connect must say something; silence is the original defect"
    assert all(r.levelno >= logging.WARNING for r in records), (
        "emitted below WARNING, which the container discards: "
        + repr([(r.levelname, r.getMessage()[:60]) for r in records])
    )


def test_failed_connect_names_every_missing_required_key(failing_connect, caplog):
    caplog.set_level(logging.DEBUG)
    _drive(_service())

    emitted = "\n".join(r.getMessage() for r in _bloodhound_records(caplog))
    # Names the OPERATOR key, not the internal one: the resolver expands one BLOODHOUND_URL into
    # the address triple, so reporting a missing BLOODHOUND_DOMAIN would send a reader looking for
    # a field that exists in no UI, no .env and no document.
    for key in bloodhound_config.BLOODHOUND_OPERATOR_CONFIG_KEYS:
        assert key in emitted, f"{key} resolved nowhere and was not named"


def test_swallowed_exception_path_also_reaches_the_operator(raising_connect, caplog):
    """Fail-soft must mean the turn survives, never that the reason disappears."""
    caplog.set_level(logging.DEBUG)
    assert _drive(_service()) is False

    records = [r for r in caplog.records if "BloodHound auto-connect" in r.getMessage()]
    assert records, "the swallowed-exception path emitted nothing at all"
    assert all(r.levelno >= logging.WARNING for r in records)


def test_no_credential_value_is_ever_emitted(failing_connect, caplog, monkeypatch):
    """Key NAMES are the contract; a value in a log line is a leak into Mythic's stored output."""
    caplog.set_level(logging.DEBUG)

    request = SimpleNamespace(
        Config={"BLOODHOUND_TOKEN_KEY": SENTINEL_TOKEN, "BLOODHOUND_DOMAIN": "bh.example"},
        Secrets={},
    )
    _drive(_service(), request=request)

    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert SENTINEL_TOKEN not in emitted, "a credential value reached the log"
    assert "bh.example" not in emitted, "a resolved host value reached the log"
    assert "BLOODHOUND_TOKEN_KEY" in emitted, "key names must still be reported"
