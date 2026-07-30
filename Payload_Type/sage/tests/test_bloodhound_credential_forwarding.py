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


def test_every_resolved_key_is_declared_to_mythic():
    """Resolution without declaration is invisible to the operator.

    The 2026-07-30 miss: `build_bloodhound_env` read all five keys correctly, but nothing declared
    them in `ChatModelMetadata`, so Mythic rendered no fields and never populated `request.Config`.
    The credential path worked and could not be reached. This ties the two sides together — a key
    added to the resolver but not to the UI declaration (or vice versa) fails here.
    """
    from sage_chat.models import SAGE_MODELS, _CONFIG_OPTIONS

    declared_options = {opt.Name for opt in _CONFIG_OPTIONS}
    declared_secrets = set(SAGE_MODELS[0].Metadata.OptionalUserSecrets)

    for key in BLOODHOUND_ENV_KEYS:
        assert key in declared_options, (
            f"{key} is resolved by build_bloodhound_env but not declared as a chat configuration "
            "option — the operator would have no field to fill in"
        )
        assert key in declared_secrets, (
            f"{key} is resolved by build_bloodhound_env but not declared as an optional user "
            "secret — the secret-store layer of the documented resolution order would be dead"
        )


def test_slash_bloodhound_forwards_resolved_credentials(monkeypatch):
    """The `/bloodhound` command is what an operator reaches for when BloodHound is not connected.

    It previously called `ensure_bloodhound_connected(directory)` with no credentials at all, so it
    could never fix the condition it exists to fix — the auto-connect path had been wired but this
    one had not.
    """
    import asyncio

    from ai import bloodhound_config
    from sage_chat import slash

    captured: dict = {}

    async def _fake_connect(directory=None, env=None, force=False):
        captured["directory"] = directory
        captured["env"] = env
        return False, "stub"

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _fake_connect)

    req = _request(
        config={"BLOODHOUND_DOMAIN": "bh.range.local"},
        secrets={"BLOODHOUND_TOKEN_ID": "tid", "BLOODHOUND_TOKEN_KEY": "tkey"},
    )
    asyncio.run(slash._handle_bloodhound(req, ""))

    assert captured["env"] is not None, "slash path must forward credentials, not None"
    assert captured["env"]["BLOODHOUND_DOMAIN"] == "bh.range.local"
    assert captured["env"]["BLOODHOUND_TOKEN_ID"] == "tid"
    assert captured["env"]["BLOODHOUND_TOKEN_KEY"] == "tkey"


def test_slash_bloodhound_still_honours_an_explicit_directory(monkeypatch):
    """`/bloodhound <dir>` must keep overriding the directory."""
    import asyncio

    from ai import bloodhound_config
    from sage_chat import slash

    captured: dict = {}

    async def _fake_connect(directory=None, env=None, force=False):
        captured["directory"] = directory
        return True, "ok"

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _fake_connect)
    asyncio.run(slash._handle_bloodhound(_request(), "  /Mythic/bloodhound_mcp  "))
    assert captured["directory"] == "/Mythic/bloodhound_mcp"


def test_credential_diagnostic_names_gaps_without_leaking_values():
    """The diagnostic must be actionable and must never print a token."""
    from ai.bloodhound_config import credential_diagnostic

    text = credential_diagnostic({"BLOODHOUND_DOMAIN": "bh.range.local", "BLOODHOUND_TOKEN_ID": "SUPERSECRET"})
    assert "BLOODHOUND_TOKEN_KEY" in text, "must name the missing required key"
    assert "SUPERSECRET" not in text, "must never echo a credential value"
    assert "bh.range.local" not in text, "must never echo a credential value"

    none_supplied = credential_diagnostic({})
    assert "NONE" in none_supplied
    for key in ("BLOODHOUND_DOMAIN", "BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY"):
        assert key in none_supplied

    complete = credential_diagnostic(
        {k: "x" for k in ("BLOODHOUND_DOMAIN", "BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY")}
    )
    assert "Missing" not in complete, "no gap to report when all required keys are present"
    assert "upstream of configuration" in complete


def test_bloodhound_arg_parser_splits_force_from_directory():
    from sage_chat.slash import _parse_bloodhound_arg as parse

    assert parse("") == (False, None)
    assert parse("   ") == (False, None)
    assert parse("force") == (True, None)
    assert parse("--force") == (True, None)
    assert parse("RECONNECT") == (True, None)
    assert parse("/Mythic/bh") == (False, "/Mythic/bh")
    assert parse("force /Mythic/bh") == (True, "/Mythic/bh")
    # only the first token is a flag, so a directory named "force" stays reachable
    assert parse("./force") == (False, "./force")
    assert parse("force ./force") == (True, "./force")


def _patch_connected(monkeypatch, *, connected: bool, sink: list):
    """Pretend BloodHound is/isn't connected and record any connect attempt."""
    from ai import bloodhound_config as bh

    monkeypatch.setattr(bh, "bloodhound_connected", lambda: connected)

    class _FakeManager:
        @staticmethod
        async def connect_server(config):
            sink.append(config)
            return True, ""

        @staticmethod
        def get_tools_by_server(_name):
            return ["file_upload", "domain_info", "cypher_query"]

    monkeypatch.setattr(bh, "MCPManager", _FakeManager)


def test_default_call_is_idempotent_when_already_connected(monkeypatch):
    """A new chat must never tear down a working session — this is the property force overrides."""
    import asyncio

    from ai.bloodhound_config import ensure_bloodhound_connected

    attempts: list = []
    _patch_connected(monkeypatch, connected=True, sink=attempts)

    ok, msg = asyncio.run(ensure_bloodhound_connected("/opt/bloodhound_mcp", {"BLOODHOUND_DOMAIN": "x"}))

    assert ok is True
    assert attempts == [], "must short-circuit, not reconnect"
    assert "already connected" in msg


def test_force_rebinds_an_existing_connection(monkeypatch):
    """The override: reconnect without a container restart, carrying the new credentials."""
    import asyncio

    from ai.bloodhound_config import ensure_bloodhound_connected

    attempts: list = []
    _patch_connected(monkeypatch, connected=True, sink=attempts)

    ok, msg = asyncio.run(
        ensure_bloodhound_connected(
            "/Mythic/bloodhound_mcp", {"BLOODHOUND_DOMAIN": "new.range.local"}, force=True
        )
    )

    assert ok is True
    assert len(attempts) == 1, "force must actually reconnect"
    assert attempts[0].env == {"BLOODHOUND_DOMAIN": "new.range.local"}
    assert attempts[0].args == ["--directory", "/Mythic/bloodhound_mcp", "run", "main.py"]
    assert "Reconnected" in msg, "message must distinguish a rebind from a first connect"


def test_forced_failure_warns_the_previous_connection_is_gone(monkeypatch):
    """connect_server disconnects before connecting, so a failed rebind leaves nothing behind."""
    import asyncio

    from ai import bloodhound_config as bh

    monkeypatch.setattr(bh, "bloodhound_connected", lambda: True)

    class _FailingManager:
        @staticmethod
        async def connect_server(_config):
            return False, "boom"

        @staticmethod
        def get_tools_by_server(_name):
            return []

    monkeypatch.setattr(bh, "MCPManager", _FailingManager)

    ok, msg = asyncio.run(bh.ensure_bloodhound_connected("/opt/bloodhound_mcp", {"BLOODHOUND_DOMAIN": "x"}, force=True))
    assert ok is False
    assert "previous BloodHound connection was replaced" in msg


def test_slash_force_reaches_the_connect_call(monkeypatch):
    import asyncio

    from ai import bloodhound_config
    from sage_chat import slash

    seen: dict = {}

    async def _fake(directory=None, env=None, force=False):
        seen["directory"] = directory
        seen["force"] = force
        return True, "ok"

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _fake)

    asyncio.run(slash._handle_bloodhound(_request(), "force /Mythic/bh"))
    assert seen == {"directory": "/Mythic/bh", "force": True}

    asyncio.run(slash._handle_bloodhound(_request(), ""))
    assert seen["force"] is False, "plain /bloodhound must stay idempotent"


def test_signature_without_bloodhound_is_byte_identical_to_the_legacy_form():
    """No BloodHound configuration must hash exactly as before, so upgrading rotates nobody."""
    import sage_chat.service as svc

    kwargs = {"provider": "openai", "model": "x", "mode": "conversation"}
    legacy = svc._model_config_signature(kwargs)
    assert legacy == svc._model_config_signature(kwargs, None)
    assert legacy == svc._model_config_signature(kwargs, {})


def test_changing_bloodhound_config_changes_the_session_signature():
    """The fix for 'configured it and nothing happened'.

    Model.initialize() resolves the BloodHound tool list at build time, so a session cannot pick up
    a later connection. Changing BloodHound configuration therefore has to rotate the session — and
    rotation is driven entirely by this signature.
    """
    import sage_chat.service as svc

    kwargs = {"provider": "openai", "model": "x", "mode": "conversation"}
    none = svc._model_config_signature(kwargs)
    one = svc._model_config_signature(kwargs, {"BLOODHOUND_DOMAIN": "127.0.0.1"})
    two = svc._model_config_signature(kwargs, {"BLOODHOUND_DOMAIN": "10.0.0.5"})

    assert one != none, "adding BloodHound config must rotate"
    assert one != two, "changing a BloodHound value must rotate"


def test_signature_is_order_stable_and_does_not_mutate_caller_kwargs():
    import sage_chat.service as svc

    kwargs = {"provider": "openai", "model": "x"}
    snapshot = dict(kwargs)
    a = svc._model_config_signature(kwargs, {"BLOODHOUND_PORT": "8083", "BLOODHOUND_DOMAIN": "x"})
    b = svc._model_config_signature(kwargs, {"BLOODHOUND_DOMAIN": "x", "BLOODHOUND_PORT": "8083"})

    assert a == b, "dict ordering must not change the signature"
    assert kwargs == snapshot, "kwargs are passed to Model(**kwargs) and must not be mutated"


def test_get_or_create_model_actually_feeds_bloodhound_into_the_signature():
    """Testing `_model_config_signature` proves nothing if the call site never passes the env.

    Caught by a green→red→green control: reverting the call site to the one-argument form left every
    other signature test passing, because they all call the helper directly. This asserts the wiring.
    """
    import inspect

    import sage_chat.service as svc

    src = inspect.getsource(svc.SageChat._get_or_create_model)
    assert "build_bloodhound_env(request)" in src, (
        "_get_or_create_model must resolve BloodHound config for the signature"
    )
    assert "_model_config_signature(kwargs, bloodhound_env)" in src, (
        "the resolved BloodHound config must be fed into the session signature, or changing "
        "credentials will not rotate the session and the new values never reach the graph"
    )


def test_reused_non_autonomous_session_still_attempts_bloodhound():
    """Guard for the branch scoping, not just the behaviour.

    Two failure modes are covered. First the reported one: only the autonomous branch attempted a
    connect, so a reused conversation session never tried. Second the one I introduced fixing it —
    inserting the `else` swallowed the autonomous branch's per-turn admission check, which is a
    safety guard, and made `admitted` unbound. Both live in the same if/else, so both are asserted
    here against the source.
    """
    import inspect
    import re

    import sage_chat.service as svc

    src = inspect.getsource(svc.SageChat._get_or_create_model)
    branch = src[src.index("autonomous_now = bool(") :]
    branch = branch[: branch.index("return existing, True")]

    assert re.search(r"\n\s+else:", branch), "reused non-autonomous sessions must attempt a connect"
    assert branch.count("_ensure_bloodhound_connected") == 2, "both branches must attempt a connect"

    autonomous_part, else_part = branch.split("\n            else:", 1)
    assert "if not admitted" in autonomous_part, (
        "the per-turn admission guard must stay inside the autonomous branch — it was swallowed by "
        "the else once already, which unbound `admitted` and dropped a safety check"
    )
    assert "if not admitted" not in else_part
    assert "autonomous_required=True" in autonomous_part
    assert "autonomous_required" not in else_part, "the reuse keep-warm must stay fail-soft"


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
