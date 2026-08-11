"""An autonomous refusal must name BloodHound and say how to fix it.

D6, Russel's call 2026-08-11: an autonomous channel KEEPS its hard refusal when BloodHound is
unavailable, because a solve reasons over the attack graph to choose and verify each step and a
graph-blind autonomous session would act with no way to do either. The behaviour is deliberate; the
message was not. It named an internal invariant — "exact-tool admission" — which tells an operator
neither that BloodHound is the subject nor what to do next.

Also the dead-constant guard for `BLOODHOUND_SETUP_STEPS`. It had NO consumers repo-wide while the
module docstring claimed it fed "the BloodHound agent's not-connected EventFeed notice", so the text
could drift arbitrarily far from reality without anything failing. Asserting it has a live consumer
generalises: the same shape catches the next constant that rots.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai.bloodhound_config as bloodhound_config  # noqa: E402
import sage_chat.service as service_module  # noqa: E402
from ai.bloodhound_config import BLOODHOUND_SETUP_STEPS  # noqa: E402
from sage_chat.service import SageChat  # noqa: E402


def _composer(module, name: str):
    """Resolve a message composer, or fail the test that needs it.

    Deliberately not a module-level `from ... import`: that turns a build without these helpers into
    a COLLECTION ERROR, which proves only that a symbol is absent. Resolving here keeps the file
    runnable against such a build so the end-to-end refusal test below fails on what the operator
    actually sees, which is the property under guard.
    """
    resolved = getattr(module, name, None)
    assert resolved is not None, f"{module.__name__}.{name} is missing; the refusal has no composer"
    return resolved


def autonomous_unavailable_message(reason: str | None = None) -> str:
    return _composer(bloodhound_config, "autonomous_unavailable_message")(reason)


def _bloodhound_unavailable_message(reason: str | None = None) -> str:
    return _composer(service_module, "_bloodhound_unavailable_message")(reason)


def test_message_names_bloodhound_and_carries_the_remedy():
    message = autonomous_unavailable_message("BloodHound MCP is not connected.")

    assert "BloodHound" in message
    assert "autonomous" in message.lower()
    assert BLOODHOUND_SETUP_STEPS in message, "the operator gets the reason but not the remedy"
    assert "exact-tool admission" not in message, (
        "an internal invariant name is not an operator-facing explanation"
    )


def test_a_redundant_reason_is_not_repeated():
    """The commonest admission reason restates the lead; echoing it reads as generated text."""
    message = autonomous_unavailable_message("BloodHound MCP is not connected.")

    assert message.count("is not connected.") == 1


def test_a_distinct_reason_is_preserved():
    message = autonomous_unavailable_message("BloodHound MCP admission requires exactly one matching server.")

    assert "exactly one matching server" in message


def test_setup_steps_warn_that_the_baked_directory_is_not_durable():
    """ISC-9: offering the image's baked `.env` as a peer option without this is offering a trap."""
    assert "/opt/bloodhound_mcp" in BLOODHOUND_SETUP_STEPS
    lost_on_rebuild = "lost on the next rebuild" in BLOODHOUND_SETUP_STEPS
    assert lost_on_rebuild, "the baked directory is not on the bind mount and the text must say so"


def test_setup_steps_have_a_live_consumer():
    """The dead-constant guard. Deleting the wiring must fail here, not go unnoticed for weeks."""
    assert BLOODHOUND_SETUP_STEPS in autonomous_unavailable_message("any reason")


def test_autonomous_connect_failure_raises_the_operator_facing_message(monkeypatch):
    """The real refusal path: `autonomous_required=True` with a connect that cannot succeed."""

    async def _connect(env=None, **kwargs):
        return False, "Connection closed"

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _connect)
    monkeypatch.setattr(
        bloodhound_config,
        "bloodhound_tool_admission",
        lambda: {"ready": False, "reason": "BloodHound MCP is not connected."},
    )

    service = SageChat.__new__(SageChat)
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(service._ensure_bloodhound_connected(autonomous_required=True))

    message = str(raised.value)
    assert "BloodHound" in message
    assert BLOODHOUND_SETUP_STEPS in message


def test_service_helper_falls_back_without_naming_an_internal_invariant():
    """Even if the lazy import fails, the refusal must still be about BloodHound."""
    message = _bloodhound_unavailable_message("some reason")

    assert "BloodHound" in message
    assert "exact-tool admission" not in message
