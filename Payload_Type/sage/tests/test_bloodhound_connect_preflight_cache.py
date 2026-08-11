"""Sage must not spawn an MCP server it already knows will exit, or respawn it once per request.

Two behaviours, one test file because they share every fixture:

* **Pre-flight (F2).** The BloodHound MCP server refuses to start without `BLOODHOUND_DOMAIN`,
  `BLOODHOUND_TOKEN_ID` and `BLOODHOUND_TOKEN_KEY`. When none of them resolve, spawning
  `uv run main.py` to rediscover that costs a subprocess, a stdio timeout on the operator's turn,
  and two tracebacks — to learn what was already knowable.
* **Negative cache (F3).** The missing configuration is static, so three chat requests in ninety
  seconds produced three identical doomed attempts. One is enough until something changes.

The trap this file exists to guard is the *safe* half. Credentials do not only arrive through Sage:
the MCP server reads a `.env` from its own directory, which is precisely how a working local install
is configured. A pre-flight that consulted Sage's `env` dict alone would refuse to connect a
BloodHound that works perfectly, and the failure would look like the bug it was meant to fix.
`test_directory_env_alone_is_sufficient` is that case, and it fails against any such shortcut.

No subprocess, no network: `MCPManager.connect_server` is replaced by a counter, so "did it try?"
is answered by observation rather than by inference from a log line.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai.bloodhound_config as bloodhound_config  # noqa: E402

FULL_CREDENTIALS = {
    "BLOODHOUND_DOMAIN": "bh.example",
    "BLOODHOUND_TOKEN_ID": "token-id",
    "BLOODHOUND_TOKEN_KEY": "token-key",
}


class _ProbeTool:
    """Answers the reachability read so a scripted connect success stays a success."""

    name = "domain_info"

    async def ainvoke(self, args):
        return '{"domains": []}'


class ConnectSpy:
    """Counts real connect attempts and scripts their outcome."""

    def __init__(self, succeed: bool = False):
        self.calls = 0
        self.succeed = succeed

    async def __call__(self, config):
        self.calls += 1
        return self.succeed, "" if self.succeed else "McpError: Connection closed"


def _reset() -> None:
    getattr(bloodhound_config, "reset_bloodhound_connect_cache", lambda: None)()


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> ConnectSpy:
    connect_spy = ConnectSpy()
    monkeypatch.setattr(bloodhound_config.MCPManager, "connect_server", connect_spy)
    # A `domain_info` that answers, so a scripted SUCCESS also passes the ISC-27 reachability read.
    # Without it a "successful" connect is correctly reported as unusable, since the handshake alone
    # no longer earns the claim.
    monkeypatch.setattr(
        bloodhound_config.MCPManager, "get_tools_by_server", lambda _name: [_ProbeTool()]
    )
    monkeypatch.setattr(bloodhound_config, "bloodhound_connected", lambda: False)
    # Tolerated rather than called directly so this file still RUNS against a build without the
    # cache, and fails on the behaviour it is asserting instead of erroring on a missing attribute.
    # An AttributeError is a weaker control: it proves the symbol is gone, not that the guard works.
    _reset()
    yield connect_spy
    _reset()


def _connect(directory: Path, env: dict | None = None, force: bool = False):
    return asyncio.run(
        bloodhound_config.ensure_bloodhound_connected(
            directory=str(directory), env=env, force=force
        )
    )


def _write_dir_env(directory: Path, values: dict[str, str]) -> None:
    directory.joinpath(".env").write_text(
        "# BloodHound MCP server's own configuration\n"
        + "".join(f"{k}={v}\n" for k, v in values.items()),
        encoding="utf-8",
    )


def test_no_credentials_anywhere_skips_the_spawn(tmp_path: Path, spy: ConnectSpy) -> None:
    """ISC-4: nothing resolved means no subprocess, and a message saying which keys are missing."""
    connected, message = _connect(tmp_path)

    assert connected is False
    assert spy.calls == 0, "spawned a server that could only have exited during startup"
    # Names the OPERATOR key, not the internal one: the resolver expands one BLOODHOUND_URL into
    # the address triple, so reporting a missing BLOODHOUND_DOMAIN would send a reader looking for
    # a field that exists in no UI, no .env and no document.
    for key in bloodhound_config.BLOODHOUND_OPERATOR_CONFIG_KEYS:
        assert key in message


def test_directory_env_alone_is_sufficient(tmp_path: Path, spy: ConnectSpy) -> None:
    """The server reads its own `.env`, so this configuration is complete and must be attempted.

    This is the local-development shape. A pre-flight that looked only at Sage's `env` would skip
    here and break a working install.
    """
    _write_dir_env(tmp_path, FULL_CREDENTIALS)

    _connect(tmp_path)

    assert spy.calls == 1, "refused to connect a fully-configured BloodHound"


def test_empty_value_in_directory_env_does_not_count_as_configured(
    tmp_path: Path, spy: ConnectSpy
) -> None:
    """`KEY=` sets nothing, matching dotenv semantics; a half-filled file must not defeat the check."""
    _write_dir_env(tmp_path, {**FULL_CREDENTIALS, "BLOODHOUND_TOKEN_KEY": ""})

    connected, message = _connect(tmp_path)

    assert connected is False
    assert spy.calls == 0
    assert "BLOODHOUND_TOKEN_KEY" in message


@pytest.mark.parametrize("attempts", [2, 3, 5, 12])
def test_unchanged_failing_credentials_attempt_once(
    tmp_path: Path, spy: ConnectSpy, attempts: int
) -> None:
    """ISC-5, over a range of call counts rather than one hand-picked pair."""
    _write_dir_env(tmp_path, FULL_CREDENTIALS)

    for _ in range(attempts):
        connected, _ = _connect(tmp_path)
        assert connected is False

    assert spy.calls == 1, f"{attempts} requests produced {spy.calls} doomed connect attempts"


def test_changed_credential_value_invalidates_the_cache(tmp_path: Path, spy: ConnectSpy) -> None:
    """ISC-6: fixing a typo'd token changes no key NAME, and must still trigger a fresh attempt."""
    _write_dir_env(tmp_path, FULL_CREDENTIALS)
    _connect(tmp_path)
    _connect(tmp_path)
    assert spy.calls == 1

    _write_dir_env(tmp_path, {**FULL_CREDENTIALS, "BLOODHOUND_TOKEN_KEY": "corrected-key"})
    _connect(tmp_path)

    assert spy.calls == 2, "a corrected credential was never retried"


def test_credentials_arriving_through_chat_config_invalidate_the_cache(
    tmp_path: Path, spy: ConnectSpy
) -> None:
    """The other direction: nothing on disk, then the operator fills in the chat fields."""
    connected, _ = _connect(tmp_path)
    assert (connected, spy.calls) == (False, 0)

    _connect(tmp_path, env=dict(FULL_CREDENTIALS))

    assert spy.calls == 1, "newly-supplied chat-config credentials were treated as the failed set"


def test_success_clears_the_cache(tmp_path: Path, spy: ConnectSpy, monkeypatch) -> None:
    """A later failure after a success must not be suppressed by a stale negative entry.

    The credentials are corrected between the failure and the success on purpose. Reaching the
    success without changing them would require the cache NOT to hold, which is the opposite of
    what the test above pins.
    """
    _write_dir_env(tmp_path, FULL_CREDENTIALS)
    _connect(tmp_path)
    assert spy.calls == 1

    spy.succeed = True
    _write_dir_env(tmp_path, {**FULL_CREDENTIALS, "BLOODHOUND_TOKEN_KEY": "corrected-key"})
    connected, _ = _connect(tmp_path)
    assert connected is True
    assert spy.calls == 2

    spy.succeed = False
    _connect(tmp_path)
    assert spy.calls == 3, "the cache survived a successful connect"


def test_force_bypasses_both_the_cache_and_the_preflight(tmp_path: Path, spy: ConnectSpy) -> None:
    """An explicit operator reconnect always gets a real attempt.

    The pre-flight models two credential sources and the server may read others, so refusing is a
    guess. That guess is worth making on the automatic path, which pays its cost every turn; it is
    not worth making against an operator who deliberately asked to reconnect and would be left
    unable to. `test_force_rebinds_an_existing_connection` in the credential-forwarding suite pins
    the same boundary from the other side.
    """
    _write_dir_env(tmp_path, FULL_CREDENTIALS)
    _connect(tmp_path)
    _connect(tmp_path, force=True)
    assert spy.calls == 2, "force did not bypass the negative cache"

    (tmp_path / ".env").unlink()
    _connect(tmp_path, force=True)
    assert spy.calls == 3, "force must still attempt when the pre-flight would have refused"

    _reset()
    connected, message = _connect(tmp_path)
    assert connected is False
    assert spy.calls == 3, "the automatic path still must not spawn a doomed server"
    assert "BLOODHOUND_URL" in message
