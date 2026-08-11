""""Connected" must mean BloodHound answered, and a failure must name the variable to fix.

ISC-27. The MCP handshake proves a subprocess started and lists 13 statically-declared `@mcp.tool`
functions; `BloodhoundAPI.__init__` validates only that credentials are PRESENT and makes no network
call. So an unreachable host, a wrong port and a revoked token were all invisible — Sage logged
"Connected to BloodHound MCP (13 tools)" for every one of them, and the operator found out on their
first real question.

Two properties:

* A read actually happens before the claim is made, and its failure withholds the claim.
* The failure says WHICH part of the configuration is wrong. Russel's requirement when he asked for
  this: "Connection closed" and "HTTPError" are technically accurate and operationally useless, which
  is the same defect this whole ISA is about.

The probe goes through the MCP session rather than Sage calling BloodHound CE directly, because when
the server reads credentials from its own directory `.env` Sage never sees them — a direct probe
would return green exactly where it knows least.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai.bloodhound_config as bloodhound_config  # noqa: E402
from ai.bloodhound_config import classify_probe_failure, probe_bloodhound_reachable  # noqa: E402


class _Tool:
    """A stand-in for the LangChain tool the MCP session exposes."""

    def __init__(self, name="domain_info", result=None, raises=None, calls=None):
        self.name = name
        self._result = result
        self._raises = raises
        self.calls = calls if calls is not None else []

    async def ainvoke(self, args):
        self.calls.append(args)
        if self._raises is not None:
            raise self._raises
        return self._result


def _install(monkeypatch, tool):
    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "get_tools_by_server",
        staticmethod(lambda _s: [tool] if tool is not None else []),
    )
    return tool


def test_a_successful_read_confirms_the_connection(monkeypatch):
    tool = _install(monkeypatch, _Tool(result='{"domains": [{"name": "NORTH.SEVENKINGDOMS.LOCAL"}]}'))

    ok, _ = asyncio.run(probe_bloodhound_reachable())

    assert ok is True
    assert tool.calls == [{"info_type": "list"}], "the probe must be a real read, with list semantics"


def test_an_error_returned_as_TEXT_is_not_treated_as_success(monkeypatch):
    """The server returns failures as strings rather than raising, so a 401 would read as healthy."""
    _install(monkeypatch, _Tool(result="Error: 401 Unauthorized"))

    ok, detail = asyncio.run(probe_bloodhound_reachable())

    assert ok is False
    assert "TOKEN_ID" in detail


@pytest.mark.parametrize(
    "raw,expected_fragment",
    [
        ("HTTPSConnectionPool: Max retries exceeded (Connection refused)", "HOST or PORT"),
        ("401 Client Error: Unauthorized for url", "TOKEN_ID"),
        ("403 Forbidden", "TOKEN_ID"),
        ("SSLError: [SSL] record layer failure", "SCHEME"),
        ("wrong version number", "SCHEME"),
        ("Failed to resolve: Name or service not known", "did not resolve"),
        ("Read timed out", "HOST or PORT"),
        ("404 Not Found", "wrong service"),
        ("something nobody has seen before", "did not match a known class"),
    ],
)
def test_every_failure_class_names_what_to_fix(raw, expected_fragment):
    """The requirement in one test: an error an operator can act on, per class."""
    assert expected_fragment in classify_probe_failure(raw)


def test_a_tls_failure_is_not_misread_as_a_connection_failure():
    """Ordering matters: both texts mention connecting, but only one is fixed by the scheme."""
    tls = classify_probe_failure("SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number')")

    assert "SCHEME" in tls
    assert "HOST or PORT" not in tls


def test_a_transient_failure_is_retried_once_and_a_rejection_is_not(monkeypatch):
    """One retry buys resilience to a blip; retrying an auth rejection only delays the answer."""
    calls: list = []
    _install(monkeypatch, _Tool(raises=asyncio.TimeoutError(), calls=calls))
    asyncio.run(probe_bloodhound_reachable(timeout=0.01))
    assert len(calls) == 2, "a transient failure must be retried exactly once"

    calls.clear()
    _install(monkeypatch, _Tool(result="Error: 403 Forbidden", calls=calls))
    asyncio.run(probe_bloodhound_reachable(timeout=0.01))
    assert len(calls) == 1, "a deterministic rejection must not be retried"


def test_a_missing_probe_tool_is_reported_rather_than_assumed_healthy(monkeypatch):
    _install(monkeypatch, None)

    ok, detail = asyncio.run(probe_bloodhound_reachable())

    assert ok is False
    assert "domain_info" in detail


def test_a_hanging_bloodhound_degrades_the_claim_not_the_turn(monkeypatch):
    """The probe is bounded. Without a timeout it would hold the operator's turn open."""

    class _Hanging(_Tool):
        async def ainvoke(self, args):
            self.calls.append(args)
            await asyncio.sleep(5)

    _install(monkeypatch, _Hanging())

    ok, detail = asyncio.run(probe_bloodhound_reachable(timeout=0.05))

    assert ok is False
    assert "HOST or PORT" in detail
