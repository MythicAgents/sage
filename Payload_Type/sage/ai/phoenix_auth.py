"""Authentication for the Phoenix that Sage runs in its own process.

Phoenix 22 turns authentication on whenever ``PHOENIX_HOST`` is not a loopback address, and Sage's
``.env`` ships ``PHOENIX_HOST=0.0.0.0`` so the trace UI stays reachable from the operator's browser. Three things
follow, and this module owns them:

* Sage's own span exporters must authenticate. They use Phoenix's admin secret, the system bearer token Phoenix
  accepts on HTTP and gRPC. That secret is valid only beside ``PHOENIX_SECRET``, which also signs Phoenix's
  sessions, so both must stay the same across restarts. Phoenix reads both from the process environment, and reads
  the admin secret again on every request, so they live in Sage's environment for the life of the process.
* Neither value may reach a process that does not need it. ``child_environment`` is the filter for the capture-mode
  Phoenix child; MCP stdio servers already get the MCP SDK's allow-listed environment instead of Sage's.
* The ``.env`` also ships a documented default admin password. Sage tries it once against its own sign-in and,
  while it still works, posts one warning to Mythic's operation event log. The warning never contains the password.

An operator's non-empty ``PHOENIX_SECRET`` or ``PHOENIX_ADMIN_SECRET`` wins. Otherwise Sage generates each value
once and keeps it, owner-only, in ``SECRETS_FILE`` inside Phoenix's working directory, beside Phoenix's database.
Nothing here logs, returns or emits a secret except ``ensure_phoenix_secrets``, whose return value is the admin
secret for Sage's exporters.
"""

from __future__ import annotations

import json
import os
import secrets
import string
from pathlib import Path
from typing import Mapping, MutableMapping

PHOENIX_SECRET = "PHOENIX_SECRET"
PHOENIX_ADMIN_SECRET = "PHOENIX_ADMIN_SECRET"
PHOENIX_HOST = "PHOENIX_HOST"
PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD = "PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD"
# Phoenix 20 listened on every address; Sage keeps that, now behind Phoenix's sign-in.
DEFAULT_HOST = "0.0.0.0"
SECRET_KEYS = (PHOENIX_SECRET, PHOENIX_ADMIN_SECRET)
SECRETS_FILE = "sage-phoenix-secrets.json"

# The value Sage's .env ships for PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD. Phoenix applies it to the default admin
# account while that account still holds Phoenix's own default ("admin"); after that it is changed only in the UI.
SHIPPED_DEFAULT_ADMIN_PASSWORD = "sage-phoenix-change-me"
DEFAULT_ADMIN_EMAIL = "admin@localhost"
DEFAULT_PASSWORD_NOTICE = (
    "Phoenix, Sage's trace viewer, listens on the network and still accepts the default admin password Sage "
    "ships. Sign in to Phoenix as admin@localhost and change the password, or set PHOENIX_HOST=127.0.0.1 in "
    "Sage's .env to keep Phoenix on this host."
)

_UNSPECIFIED_HOSTS = frozenset({"0.0.0.0", "::", "[::]", ""})
_check_enabled = False
_notice_checked = False


def apply_phoenix_defaults(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Sage's defaults for Phoenix's bind address and first admin password; returns the names it set.

    Each applies only when the environment and Sage's .env leave the setting unset or empty, so an operator's value
    always wins. They live in code as well as in the shipped .env because a deployment keeps its own installed .env
    across upgrades: without them an upgraded install would fall back to Phoenix's loopback default, with sign-in
    off and no default-password warning.
    """
    environ = os.environ if environ is None else environ
    applied = []
    for key, value in ((PHOENIX_HOST, DEFAULT_HOST),
                       (PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD, SHIPPED_DEFAULT_ADMIN_PASSWORD)):
        if not str(environ.get(key) or "").strip():
            environ[key] = value
            applied.append(key)
    return applied


class PhoenixSecretError(RuntimeError):
    """A Phoenix secret setting Sage cannot use. Messages name settings, never values."""


def _usable(value: str) -> bool:
    # Phoenix's own rule: at least 32 characters, with at least one digit and one lowercase letter.
    return (
        len(value) >= 32
        and any(character in string.digits for character in value)
        and any(character in string.ascii_lowercase for character in value)
    )


def _generate() -> str:
    while True:
        value = secrets.token_hex(32)
        if _usable(value):
            return value


def _read_stored(path: Path) -> dict[str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        stored = json.loads(raw)
    except ValueError as exc:
        raise PhoenixSecretError(f"{path} is not valid JSON; move it aside and restart Sage") from exc
    if not isinstance(stored, dict):
        raise PhoenixSecretError(f"{path} is not a JSON object; move it aside and restart Sage")
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)
    return {key: value for key, value in stored.items() if key in SECRET_KEYS and isinstance(value, str)}


def _write_stored(path: Path, values: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(values), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


def ensure_phoenix_secrets(working_dir: Path, environ: MutableMapping[str, str] | None = None) -> str:
    """Put both Phoenix secrets in ``environ`` (the process environment by default); return the admin secret.

    An operator's non-empty value wins and is never stored. A missing one comes from ``SECRETS_FILE`` in
    ``working_dir``, generated and stored there (mode 0600) the first time.
    """
    environ = os.environ if environ is None else environ
    path = Path(working_dir) / SECRETS_FILE
    stored = _read_stored(path)
    values: dict[str, str] = {}
    changed = False
    for key in SECRET_KEYS:
        operator_value = str(environ.get(key) or "").strip()
        if operator_value:
            if not _usable(operator_value):
                raise PhoenixSecretError(
                    f"{key} must be at least 32 characters with a digit and a lowercase letter"
                )
            values[key] = operator_value
            continue
        value = stored.get(key, "")
        if not _usable(value) or value in values.values():
            value = _generate()
            stored[key] = value
            changed = True
        values[key] = value
    if values[PHOENIX_SECRET] == values[PHOENIX_ADMIN_SECRET]:
        raise PhoenixSecretError(f"{PHOENIX_ADMIN_SECRET} must differ from {PHOENIX_SECRET}")
    if changed:
        _write_stored(path, stored)
    for key, value in values.items():
        environ[key] = value
    return values[PHOENIX_ADMIN_SECRET]


def child_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``environ`` (the process environment by default) without either Phoenix secret."""
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items() if key not in SECRET_KEYS}


def enable_default_password_check() -> None:
    """Called by ``main.py`` once Sage's own Phoenix is up; until then the check is a no-op."""
    global _check_enabled
    _check_enabled = True


def _login_url() -> str | None:
    from phoenix.config import (
        auth_enabled_for_host,
        get_env_host,
        get_env_host_root_path,
        get_env_port,
        get_env_tls_enabled_for_http,
    )

    host = get_env_host()
    if not auth_enabled_for_host(host) or get_env_tls_enabled_for_http():
        return None
    if host in _UNSPECIFIED_HOSTS:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    root = get_env_host_root_path().rstrip("/")
    return f"http://{host}:{get_env_port()}{root}/auth/login"


async def default_password_accepted(*, url: str | None = None, timeout: float = 5.0) -> bool | None:
    """Try the shipped default password once. True: Phoenix accepted it. False: refused. None: not checked."""
    import httpx

    url = url or _login_url()
    if url is None:
        return None
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                url, json={"email": DEFAULT_ADMIN_EMAIL, "password": SHIPPED_DEFAULT_ADMIN_PASSWORD}
            )
    except httpx.HTTPError:
        return None
    if 200 <= response.status_code < 300:  # Phoenix answers a good sign-in with 204 and its session cookies
        return True
    if response.status_code == 401:
        return False
    return None


async def default_password_notice() -> str | None:
    """The event-log warning, at most once per process, and only after ``enable_default_password_check``."""
    global _notice_checked
    if not _check_enabled or _notice_checked:
        return None
    _notice_checked = True
    return DEFAULT_PASSWORD_NOTICE if await default_password_accepted() else None
