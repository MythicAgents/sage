"""BloodHound MCP credential resolution and forwarding.

Regression cover for the container defect found 2026-07-30: a Mythic-installed Sage could not
authenticate BloodHound, because the MCP server reads credentials from a `.env` in its own
directory and the container's freshly-cloned `/opt/bloodhound_mcp` has none. Sage forwarded
nothing — `bloodhound_mcp_config` passed `env={}` unconditionally.

The load-bearing premise is exercised here rather than assumed: the MCP stdio client inherits only
a safe subset of the parent environment, so setting `BLOODHOUND_DOMAIN` on the Sage container does
NOT reach the server subprocess on its own. If that ever stops being true, the last test fails and
the forwarding becomes redundant-but-harmless rather than silently load-bearing.

No live Mythic, no network, no MCP subprocess — pure resolution and config construction. Mirrors
the repo's no-pytest-asyncio convention.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.bloodhound_config import bloodhound_mcp_config  # noqa: E402
from sage_chat.config import BLOODHOUND_ENV_KEYS, build_bloodhound_env  # noqa: E402


def _request(config: dict | None = None, secrets: dict | None = None) -> SimpleNamespace:
    """Minimal ChatRequest stand-in: the views read only .Config and .Secrets."""
    return SimpleNamespace(Config=config or {}, Secrets=secrets or {})


def test_config_beats_secret_beats_env(monkeypatch):
    """Per-chat Config → Mythic user Secret → container env, first non-empty wins."""
    monkeypatch.setenv("BLOODHOUND_DOMAIN", "from-env")
    monkeypatch.setenv("BLOODHOUND_TOKEN_ID", "env-token-id")
    monkeypatch.setenv("BLOODHOUND_SCHEME", "http")

    resolved = build_bloodhound_env(
        _request(
            config={"BLOODHOUND_DOMAIN": "from-config"},
            secrets={"BLOODHOUND_DOMAIN": "from-secret", "BLOODHOUND_TOKEN_ID": "secret-token-id"},
        )
    )

    assert resolved["BLOODHOUND_DOMAIN"] == "from-config", "per-chat Config must win"
    assert resolved["BLOODHOUND_TOKEN_ID"] == "secret-token-id", "Secret must beat env"
    assert resolved["BLOODHOUND_SCHEME"] == "http", "env is the last resort, not ignored"


def test_unset_keys_are_omitted_so_dotenv_fallback_survives(monkeypatch):
    """An unset key must stay absent, or it would shadow the MCP's own .env with an empty string."""
    for key in BLOODHOUND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    resolved = build_bloodhound_env(_request(config={"BLOODHOUND_DOMAIN": "bh.example.local"}))

    assert resolved == {"BLOODHOUND_DOMAIN": "bh.example.local"}
    for key in BLOODHOUND_ENV_KEYS:
        if key != "BLOODHOUND_DOMAIN":
            assert key not in resolved


def test_empty_values_do_not_shadow_lower_layers(monkeypatch):
    """An empty Config value must fall through, not resolve to ''."""
    monkeypatch.setenv("BLOODHOUND_DOMAIN", "from-env")
    resolved = build_bloodhound_env(_request(config={"BLOODHOUND_DOMAIN": ""}))
    assert resolved["BLOODHOUND_DOMAIN"] == "from-env"


def test_mcp_config_forwards_credentials_to_the_subprocess():
    """The resolved dict must land on the stdio config Sage hands the MCP client."""
    cfg = bloodhound_mcp_config(
        "/opt/bloodhound_mcp",
        {"BLOODHOUND_DOMAIN": "bh.example.local", "BLOODHOUND_TOKEN_ID": "abc"},
    )
    assert cfg is not None
    assert cfg.env == {"BLOODHOUND_DOMAIN": "bh.example.local", "BLOODHOUND_TOKEN_ID": "abc"}


def test_mcp_config_without_credentials_is_unchanged_legacy_behaviour():
    """No credentials supplied → empty env, i.e. the MCP reads its own directory .env as before."""
    assert bloodhound_mcp_config("/opt/bloodhound_mcp").env == {}
    assert bloodhound_mcp_config("/opt/bloodhound_mcp", {}).env == {}
    assert bloodhound_mcp_config("/opt/bloodhound_mcp", None).env == {}


def test_caller_dict_is_copied_not_aliased():
    """Mutating the caller's dict afterwards must not retroactively change a built config."""
    supplied = {"BLOODHOUND_DOMAIN": "bh.example.local"}
    cfg = bloodhound_mcp_config("/opt/bloodhound_mcp", supplied)
    supplied["BLOODHOUND_TOKEN_KEY"] = "leaked-after-the-fact"
    assert cfg.env == {"BLOODHOUND_DOMAIN": "bh.example.local"}


def test_stdio_client_does_not_inherit_bloodhound_vars_by_default():
    """The premise the forwarding exists for.

    If the MCP SDK inherited the full parent environment, container-level BLOODHOUND_* would reach
    the server unaided and this whole path would be unnecessary. It does not: it inherits a named
    safe subset. This test pins that, so the reason for the forwarding is executable rather than
    a comment that can rot.
    """
    from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS, get_default_environment

    for key in BLOODHOUND_ENV_KEYS:
        assert key not in DEFAULT_INHERITED_ENV_VARS

    import os

    os.environ["BLOODHOUND_DOMAIN"] = "should-not-be-inherited"
    try:
        assert "BLOODHOUND_DOMAIN" not in get_default_environment()
    finally:
        os.environ.pop("BLOODHOUND_DOMAIN", None)
