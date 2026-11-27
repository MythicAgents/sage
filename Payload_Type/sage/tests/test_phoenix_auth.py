"""Phoenix authentication for Sage's own Phoenix (``ai/phoenix_auth.py``, ``main.py``, ``ai/phoenix_tracing.py``).

The secret tests run the module against a real directory. The Phoenix test runs Sage's real startup in a child
process (``phoenix_auth_child.py``): the installed Phoenix with authentication on, on its own loopback ports so it
cannot reach a Phoenix already running on this host, and checks what reaches the operator: the health check, a
refused unauthenticated query, Sage's own span arriving through the authenticated exporter, the default-password
warning while that password works, and no warning once the operator has changed it.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ai import phoenix_auth
from ai.phoenix_auth import (
    PHOENIX_ADMIN_SECRET,
    PHOENIX_SECRET,
    SECRETS_FILE,
    PhoenixSecretError,
    child_environment,
    ensure_phoenix_secrets,
)

SAGE_ROOT = Path(__file__).resolve().parents[1]
OPERATOR_SECRET = "operator-phoenix-secret-0123456789abcdef"
OPERATOR_ADMIN = "operator-phoenix-admin-0123456789abcdef"


def test_secrets_are_generated_once_stored_owner_only_and_reused(tmp_path):
    first: dict[str, str] = {}
    admin = ensure_phoenix_secrets(tmp_path, first)

    stored = tmp_path / SECRETS_FILE
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert json.loads(stored.read_text()) == first
    assert admin == first[PHOENIX_ADMIN_SECRET] != first[PHOENIX_SECRET]
    for value in first.values():
        assert len(value) >= 32 and any(c.isdigit() for c in value) and any(c.islower() for c in value)

    second: dict[str, str] = {}
    assert ensure_phoenix_secrets(tmp_path, second) == admin
    assert second == first


def test_an_operators_values_win_and_are_never_stored(tmp_path):
    environ = {PHOENIX_SECRET: OPERATOR_SECRET, PHOENIX_ADMIN_SECRET: OPERATOR_ADMIN}

    assert ensure_phoenix_secrets(tmp_path, environ) == OPERATOR_ADMIN
    assert environ == {PHOENIX_SECRET: OPERATOR_SECRET, PHOENIX_ADMIN_SECRET: OPERATOR_ADMIN}
    assert not (tmp_path / SECRETS_FILE).exists()

    # Only the missing one is generated and stored; an empty value counts as missing.
    partial = {PHOENIX_SECRET: OPERATOR_SECRET, PHOENIX_ADMIN_SECRET: ""}
    ensure_phoenix_secrets(tmp_path, partial)
    assert partial[PHOENIX_SECRET] == OPERATOR_SECRET
    assert json.loads((tmp_path / SECRETS_FILE).read_text()) == {PHOENIX_ADMIN_SECRET: partial[PHOENIX_ADMIN_SECRET]}


@pytest.mark.parametrize(
    "environ, named",
    [
        ({PHOENIX_SECRET: "too-short-1"}, PHOENIX_SECRET),
        ({PHOENIX_ADMIN_SECRET: "ALLUPPERCASEANDDIGITS0123456789ABCDEF"}, PHOENIX_ADMIN_SECRET),
        ({PHOENIX_SECRET: OPERATOR_SECRET, PHOENIX_ADMIN_SECRET: OPERATOR_SECRET}, PHOENIX_ADMIN_SECRET),
    ],
)
def test_an_unusable_operator_value_is_refused_by_name_without_its_value(tmp_path, environ, named):
    with pytest.raises(PhoenixSecretError) as raised:
        ensure_phoenix_secrets(tmp_path, dict(environ))
    assert named in str(raised.value)
    for value in environ.values():
        assert value not in str(raised.value)


def test_a_readable_secrets_file_is_tightened_and_a_corrupt_one_is_refused(tmp_path):
    ensure_phoenix_secrets(tmp_path, {})
    stored = tmp_path / SECRETS_FILE
    stored.chmod(0o644)
    ensure_phoenix_secrets(tmp_path, {})
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600

    stored.write_text("not json")
    with pytest.raises(PhoenixSecretError, match="not valid JSON"):
        ensure_phoenix_secrets(tmp_path, {})


def test_sages_phoenix_defaults_fill_only_what_the_operator_left_unset():
    environ: dict[str, str] = {}
    assert phoenix_auth.apply_phoenix_defaults(environ) == ["PHOENIX_HOST", "PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD"]
    assert environ == {"PHOENIX_HOST": "0.0.0.0",
                       "PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD": phoenix_auth.SHIPPED_DEFAULT_ADMIN_PASSWORD}

    operator = {"PHOENIX_HOST": "127.0.0.1", "PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD": "operator-chosen-42"}
    assert phoenix_auth.apply_phoenix_defaults(operator) == []
    assert operator == {"PHOENIX_HOST": "127.0.0.1", "PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD": "operator-chosen-42"}

    empty = {"PHOENIX_HOST": "  ", "PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD": ""}
    assert phoenix_auth.apply_phoenix_defaults(empty) == ["PHOENIX_HOST", "PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD"]
    assert empty["PHOENIX_HOST"] == "0.0.0.0"


def test_child_environment_drops_only_the_phoenix_secrets():
    environ = {PHOENIX_SECRET: OPERATOR_SECRET, PHOENIX_ADMIN_SECRET: OPERATOR_ADMIN, "PATH": "/bin", "HOME": "/h"}
    assert child_environment(environ) == {"PATH": "/bin", "HOME": "/h"}


def test_the_shipped_env_sets_the_default_password_this_module_checks():
    lines = (SAGE_ROOT / ".env").read_text().splitlines()
    active = dict(line.split("=", 1) for line in lines if line and not line.startswith("#") and "=" in line)
    assert active["PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD"] == phoenix_auth.SHIPPED_DEFAULT_ADMIN_PASSWORD
    assert active["PHOENIX_HOST"] == phoenix_auth.DEFAULT_HOST == "0.0.0.0"
    assert phoenix_auth.SHIPPED_DEFAULT_ADMIN_PASSWORD not in phoenix_auth.DEFAULT_PASSWORD_NOTICE


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_sages_phoenix_refuses_anonymous_reads_takes_sages_spans_and_warns_about_the_default_password(tmp_path):
    environ = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("PHOENIX_", "OTEL_", "SAGE_"))
    }
    environ.update({
        "PHOENIX_HOST": "127.0.0.1",
        "PHOENIX_ENABLE_AUTH": "true",
        "PHOENIX_PORT": str(_free_port()),
        "PHOENIX_GRPC_PORT": str(_free_port()),
        "PHOENIX_WORKING_DIR": str(tmp_path / ".phoenix"),
        "PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD": phoenix_auth.SHIPPED_DEFAULT_ADMIN_PASSWORD,
        "SAGE_DATABASE_PATH": str(tmp_path / "sage.db"),
        # Offline: a fresh working directory would otherwise make Phoenix fetch its WASM sandbox binary from the
        # internet during startup, inside Phoenix's five-second in-thread start window.
        "PHOENIX_ALLOW_EXTERNAL_RESOURCES": "false",
    })
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("phoenix_auth_child.py")), str(SAGE_ROOT)],
        env=environ, cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    marker = "PHOENIX_AUTH_OBSERVATIONS "
    lines = [line for line in completed.stdout.splitlines() if line.startswith(marker)]
    assert lines, completed.stdout[-2000:] + completed.stderr[-4000:]
    observed = json.loads(lines[-1][len(marker):])

    assert observed["healthz"] == 200
    assert observed["graphql_unauthenticated"] == 401
    assert observed["graphql_admin_secret"] == 200
    assert observed["sage_span_arrived"] is True
    assert observed["notice_first"] == phoenix_auth.DEFAULT_PASSWORD_NOTICE
    assert observed["notice_second"] is None
    assert observed["operator_login"] == 204
    assert (observed["password_change"], observed["password_change_errors"]) == (200, False)
    assert observed["accepted_after_change"] is False

    stored = json.loads((tmp_path / ".phoenix" / SECRETS_FILE).read_text())
    assert set(stored) == {PHOENIX_SECRET, PHOENIX_ADMIN_SECRET}
    for value in stored.values():
        assert value not in completed.stdout and value not in completed.stderr


def test_sages_on_start_response_carries_the_warning_once_as_mythics_warning_field(monkeypatch):
    """The wiring from the check to Mythic: the on-start response's EventLogErrorMessage, which Mythic posts as a
    warning without a token. The sign-in itself is a stand-in here (it returns "accepted"); the real Phoenix sign-in
    is exercised by the test above."""
    import asyncio

    from mythic_container.SharedClasses import ContainerOnStartMessage
    from sage_chat.service import SageChat

    monkeypatch.setattr(phoenix_auth, "_check_enabled", True)
    monkeypatch.setattr(phoenix_auth, "_notice_checked", False)
    attempts = []

    async def accepted(**_kwargs):
        attempts.append(1)
        return True

    monkeypatch.setattr(phoenix_auth, "default_password_accepted", accepted)

    class Watcher:
        def __init__(self, error=None):
            self.error = error

        async def restore_operation(self, operation, *, server_name="", bootstrap_token=None):
            if self.error:
                raise self.error

    async def start(chat, operation):
        return await chat.on_container_start(ContainerOnStartMessage(
            container_name="sage", operation_id=operation, server_name="mythic", apitoken="short-lived"))

    async def scenario():
        chat = SageChat()
        monkeypatch.setattr(chat, "_findings_watcher", lambda: Watcher(RuntimeError("watcher down")))
        first = await start(chat, 7)
        monkeypatch.setattr(chat, "_findings_watcher", lambda: Watcher())
        second = await start(chat, 8)
        return first, second

    first, second = asyncio.run(scenario())
    assert phoenix_auth.DEFAULT_PASSWORD_NOTICE in first.EventLogErrorMessage
    assert "Sage findings watcher startup failed" in first.EventLogErrorMessage
    assert phoenix_auth.SHIPPED_DEFAULT_ADMIN_PASSWORD not in first.EventLogErrorMessage
    assert second.EventLogErrorMessage == "" and "/watcher status" in second.EventLogInfoMessage
    assert attempts == [1]


def test_no_warning_and_no_sign_in_attempt_until_sages_own_phoenix_is_up(monkeypatch):
    import asyncio

    monkeypatch.setattr(phoenix_auth, "_check_enabled", False)
    monkeypatch.setattr(phoenix_auth, "_notice_checked", False)

    async def must_not_run(**_kwargs):
        raise AssertionError("no sign-in attempt before main.py enables the check")

    monkeypatch.setattr(phoenix_auth, "default_password_accepted", must_not_run)
    assert asyncio.run(phoenix_auth.default_password_notice()) is None
