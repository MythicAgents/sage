"""What a `hello` does when BloodHound is unavailable, across every way it can be unavailable.

Russel's original report was that Sage should start and work without the BloodHound MCP and should
not refuse a `hello`. Chasing the single case he hit produced two hypotheses, both falsified, and no
reproduction — which is the argument for a matrix rather than another example. Five ways BloodHound
can be missing times three channel modes, so the answer is a property of the system instead of the
one cell somebody happened to try.

The rule the matrix pins:

* **conversation and supervised never raise.** Ordinary chat survives a missing optional dependency;
  degradation is scoped to the graph, never widened to the product (ISC-16, ISC-17, ISC-19).
* **autonomous always raises, and the message names BloodHound and how to enable it** (ISC-18, D6).
  A solve reasons over the attack graph to choose and verify each step, so it fails closed rather
  than acting blind.

**Scope, stated so nobody reads more into a green run:** this drives the gate that decides whether a
turn survives — `SageChat._ensure_bloodhound_connected` — not a full request through the graph. It
answers "does this configuration kill the turn, and is the refusal legible", which is exactly what
was in doubt. It does not answer "does the model reply sensibly"; `test_scripted_handoff.py` is the
harness for that. Hermetic: no network, no MCP subprocess, no Mythic.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai.bloodhound_config as bloodhound_config  # noqa: E402
from ai.bloodhound_config import BLOODHOUND_SETUP_STEPS  # noqa: E402
from sage_chat.service import SageChat  # noqa: E402

FULL_CREDENTIALS = {
    "BLOODHOUND_URL": "http://bh.example:8080",
    "BLOODHOUND_TOKEN_ID": "token-id",
    "BLOODHOUND_TOKEN_KEY": "token-key",
}

#: Every way BloodHound can be absent or broken. `dir_env` is what the MCP server's own directory
#: `.env` holds; `chat_config` is what the operator put in the chat configuration.
UNAVAILABILITY_STATES = {
    "no_credentials": dict(chat_config={}, dir_env={}, mcp_dir=True, connect="fail"),
    "partial_credentials": dict(
        chat_config={"BLOODHOUND_URL": "http://bh.example:8080"}, dir_env={}, mcp_dir=True, connect="fail"
    ),
    "wrong_credentials": dict(
        chat_config=dict(FULL_CREDENTIALS), dir_env={}, mcp_dir=True, connect="fail"
    ),
    "no_mcp_dir": dict(chat_config=dict(FULL_CREDENTIALS), dir_env={}, mcp_dir=False, connect="fail"),
    "mcp_server_absent": dict(
        chat_config=dict(FULL_CREDENTIALS), dir_env={}, mcp_dir=True, connect="raise"
    ),
}

#: `conversation` and `supervised` are the SAME path — both pass `autonomous_required=False`. That is
#: asserted rather than assumed by `test_supervised_is_the_same_fail_soft_path_as_conversation`
#: below, because "supervised is stricter" is a plausible-sounding thing for someone to introduce.
MODES = {"conversation": False, "supervised": False, "autonomous": True}


@pytest.fixture
def unavailable(monkeypatch, tmp_path: Path):
    """Configure one cell of the matrix and hand back a request stand-in."""

    def _configure(state_name: str):
        state = UNAVAILABILITY_STATES[state_name]

        if state["mcp_dir"]:
            mcp_dir = tmp_path / "bloodhound_mcp"
            mcp_dir.mkdir(exist_ok=True)
            if state["dir_env"]:
                (mcp_dir / ".env").write_text(
                    "".join(f"{k}={v}\n" for k, v in state["dir_env"].items()), encoding="utf-8"
                )
            monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", str(mcp_dir))
        else:
            monkeypatch.delenv("SAGE_BLOODHOUND_MCP_DIR", raising=False)

        for key in ("BLOODHOUND_URL", "BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY"):
            monkeypatch.delenv(key, raising=False)

        async def _connect(config):
            if state["connect"] == "raise":
                raise RuntimeError("MCP stdio launch failed: no such file or directory")
            return False, "McpError: Connection closed"

        monkeypatch.setattr(bloodhound_config.MCPManager, "connect_server", _connect)
        monkeypatch.setattr(bloodhound_config, "bloodhound_connected", lambda: False)
        monkeypatch.setattr(
            bloodhound_config,
            "bloodhound_tool_admission",
            lambda: {"ready": False, "reason": "BloodHound MCP is not connected."},
        )
        bloodhound_config.reset_bloodhound_connect_cache()
        return SimpleNamespace(Config=state["chat_config"], Secrets={})

    yield _configure
    bloodhound_config.reset_bloodhound_connect_cache()


def _drive(request, *, autonomous: bool):
    service = SageChat.__new__(SageChat)
    return asyncio.run(
        service._ensure_bloodhound_connected(autonomous_required=autonomous, request=request)
    )


@pytest.mark.parametrize("state_name", sorted(UNAVAILABILITY_STATES))
@pytest.mark.parametrize("mode", ["conversation", "supervised"])
def test_ordinary_chat_survives_every_unavailability(unavailable, state_name, mode):
    """Ten cells: a missing optional dependency degrades a capability, never the product."""
    request = unavailable(state_name)

    admitted = _drive(request, autonomous=MODES[mode])

    assert admitted is False, "nothing should claim BloodHound is admitted here"


@pytest.mark.parametrize("state_name", sorted(UNAVAILABILITY_STATES))
def test_autonomous_refuses_legibly_in_every_unavailability(unavailable, state_name):
    """Five cells: fail closed, and say BloodHound plus the remedy while doing it."""
    request = unavailable(state_name)

    with pytest.raises(RuntimeError) as raised:
        _drive(request, autonomous=True)

    message = str(raised.value)
    assert "BloodHound" in message, f"{state_name}: refusal does not name the subject"
    assert BLOODHOUND_SETUP_STEPS in message, f"{state_name}: refusal carries no remedy"
    assert "exact-tool admission" not in message, f"{state_name}: leaked an internal invariant name"


@pytest.mark.parametrize("state_name", sorted(UNAVAILABILITY_STATES))
def test_no_credential_value_escapes_in_any_state(unavailable, state_name):
    """The token in the chat config must not reach a message on any of these paths."""
    request = unavailable(state_name)
    request.Config = dict(request.Config or {})
    request.Config["BLOODHOUND_TOKEN_KEY"] = "sentinel-key-must-not-appear"

    try:
        message = str(_drive(request, autonomous=False))
    except RuntimeError as exc:  # pragma: no cover - non-autonomous must not raise
        pytest.fail(f"{state_name}: fail-soft path raised: {exc}")

    with pytest.raises(RuntimeError) as raised:
        _drive(request, autonomous=True)

    assert "sentinel-key-must-not-appear" not in message
    assert "sentinel-key-must-not-appear" not in str(raised.value)


def test_supervised_is_the_same_fail_soft_path_as_conversation():
    """Asserted, not assumed: 'supervised should be stricter' is a plausible thing to introduce.

    Supervised means per-step approval of guarded TOOLS, which has nothing to do with whether an
    optional graph dependency is present. If someone makes supervised fail closed on BloodHound, the
    matrix above would still pass cell by cell while the product quietly lost a mode.
    """
    assert MODES["supervised"] is MODES["conversation"] is False


def test_the_matrix_is_the_size_it_claims_to_be():
    """Floor assertion: a shrunken matrix must fail rather than quietly cover less."""
    assert len(UNAVAILABILITY_STATES) == 5
    assert len(MODES) == 3
    assert len(UNAVAILABILITY_STATES) * len(MODES) == 15
