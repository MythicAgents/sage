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
from ai.bloodhound_config import BLOODHOUND_OPERATOR_CONFIG_KEYS  # noqa: E402
from sage_chat.config import BLOODHOUND_ENV_KEYS, build_bloodhound_env  # noqa: E402


def _request(config: dict | None = None, secrets: dict | None = None) -> SimpleNamespace:
    """Minimal ChatRequest stand-in: the views read only .Config and .Secrets."""
    return SimpleNamespace(Config=config or {}, Secrets=secrets or {})


def test_config_beats_secret_beats_env(monkeypatch):
    """Per-chat Config → Mythic user Secret → container env, first non-empty wins.

    Precedence is asserted on the OPERATOR key (`BLOODHOUND_URL`) and observed through the expanded
    output, because the collapse means the thing a human sets and the thing the MCP server reads are
    no longer the same key.
    """
    monkeypatch.setenv("BLOODHOUND_URL", "http://from-env:9999")
    monkeypatch.setenv("BLOODHOUND_TOKEN_ID", "env-token-id")

    resolved = build_bloodhound_env(
        _request(
            config={"BLOODHOUND_URL": "http://from-config:8080"},
            secrets={"BLOODHOUND_URL": "http://from-secret:7070", "BLOODHOUND_TOKEN_ID": "secret-token-id"},
        )
    )

    assert resolved["BLOODHOUND_DOMAIN"] == "from-config", "per-chat Config must win"
    assert resolved["BLOODHOUND_PORT"] == "8080", "the winning URL supplies the whole address"
    assert resolved["BLOODHOUND_TOKEN_ID"] == "secret-token-id", "Secret must beat env"


def test_env_url_is_the_last_resort_not_ignored(monkeypatch):
    """The container env layer still works, which is what Sage's UI-editable .env feeds."""
    monkeypatch.setenv("BLOODHOUND_URL", "https://from-env.example")

    resolved = build_bloodhound_env(_request())

    assert resolved["BLOODHOUND_DOMAIN"] == "from-env.example"
    assert resolved["BLOODHOUND_SCHEME"] == "https"
    assert resolved["BLOODHOUND_PORT"] == "443", "https default when the URL omits a port"


def test_an_unparseable_url_does_not_masquerade_as_unset(monkeypatch, caplog):
    """A bad URL is a different problem from a missing one and must not be reported as missing."""
    import logging

    monkeypatch.delenv("BLOODHOUND_URL", raising=False)
    caplog.set_level(logging.DEBUG)

    resolved = build_bloodhound_env(_request(config={"BLOODHOUND_URL": "http://host:8080/ui/login"}))

    assert "BLOODHOUND_DOMAIN" not in resolved
    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert "BLOODHOUND_URL" in emitted and "a path" in emitted


def test_unset_keys_are_omitted_so_dotenv_fallback_survives(monkeypatch):
    """An unset key must stay absent, or it would shadow the MCP's own .env with an empty string."""
    for key in BLOODHOUND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.delenv("BLOODHOUND_URL", raising=False)

    resolved = build_bloodhound_env(_request(config={"BLOODHOUND_URL": "http://bh.example.local"}))

    # The URL expands to exactly the address triple and nothing else; unset tokens stay absent so
    # the MCP server's own .env can still supply them.
    assert resolved == {
        "BLOODHOUND_DOMAIN": "bh.example.local",
        "BLOODHOUND_PORT": "80",
        "BLOODHOUND_SCHEME": "http",
    }
    for key in ("BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY"):
        assert key not in resolved


def test_empty_values_do_not_shadow_lower_layers(monkeypatch):
    """An empty Config value must fall through, not resolve to ''."""
    monkeypatch.setenv("BLOODHOUND_URL", "http://from-env")
    resolved = build_bloodhound_env(_request(config={"BLOODHOUND_URL": ""}))
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

    Iterates the OPERATOR-settable keys rather than the subprocess allowlist, because as of the
    URL collapse those are no longer the same list. `BLOODHOUND_CREDENTIAL_KEYS` is what reaches the
    MCP server (DOMAIN/PORT/SCHEME plus the tokens); `BLOODHOUND_OPERATOR_CONFIG_KEYS` is what a
    human types (URL plus the tokens). Asserting the old list here after the collapse would demand a
    UI field for `BLOODHOUND_DOMAIN`, which is exactly the three-fields-for-one-address problem the
    collapse removed. The invariant is unchanged: whatever an operator can set must be reachable.
    """
    from sage_chat.models import SAGE_MODELS, _CONFIG_OPTIONS

    declared_options = {opt.Name for opt in _CONFIG_OPTIONS}
    declared_secrets = set(SAGE_MODELS[0].Metadata.OptionalUserSecrets)

    for key in BLOODHOUND_OPERATOR_CONFIG_KEYS:
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
        config={"BLOODHOUND_URL": "http://bh.range.local:8080"},
        secrets={"BLOODHOUND_TOKEN_ID": "tid", "BLOODHOUND_TOKEN_KEY": "tkey"},
    )
    asyncio.run(slash._handle_bloodhound(req, ""))

    assert captured["env"] is not None, "slash path must forward credentials, not None"
    assert captured["env"]["BLOODHOUND_DOMAIN"] == "bh.range.local"
    assert captured["env"]["BLOODHOUND_PORT"] == "8080", "the slash path must expand the URL too"
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
    # Names the OPERATOR key, not the internal one: the resolver expands one BLOODHOUND_URL into
    # the address triple, so reporting a missing BLOODHOUND_DOMAIN would send a reader looking for
    # a field that exists in no UI, no .env and no document.
    for key in ("BLOODHOUND_URL", "BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY"):
        assert key in none_supplied
    assert "BLOODHOUND_DOMAIN" not in none_supplied

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


class _ProbeTool:
    """A `domain_info` that answers, so a mocked connect also passes the ISC-27 reachability read.

    Since 2026-08-11 a successful handshake is not on its own a successful connect: Sage calls one
    real read before claiming BloodHound is usable. A fixture that returns bare tool NAMES therefore
    describes a server that connected and cannot answer, which is now correctly reported as not
    connected.
    """

    name = "domain_info"

    async def ainvoke(self, args):
        return '{"domains": [{"name": "TEST.LOCAL"}]}'


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
            return [_ProbeTool()]

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


def _canonical_with_env(tmp_path, env):
    """Build the real stdio config against a real directory and run the real pre-connect guard."""
    import os

    from ai.mcp import MCPManager
    from ai.bloodhound_config import bloodhound_mcp_config

    d = str(tmp_path)
    prior = os.environ.get("SAGE_BLOODHOUND_MCP_DIR")
    os.environ["SAGE_BLOODHOUND_MCP_DIR"] = d
    try:
        return MCPManager._is_canonical_bloodhound_config(bloodhound_mcp_config(d, env))
    finally:
        if prior is None:
            os.environ.pop("SAGE_BLOODHOUND_MCP_DIR", None)
        else:
            os.environ["SAGE_BLOODHOUND_MCP_DIR"] = prior


def test_canonical_guard_admits_credentials_and_empty_env(tmp_path):
    """The guard required `env == {}`, which refused the credentials the MCP server needs.

    Empty must stay admissible so the pre-credential shape is unchanged, and the five credential
    keys must now pass — that is the whole point of the relaxation.
    """
    from ai.mcp import BLOODHOUND_CREDENTIAL_ENV_KEYS

    assert _canonical_with_env(tmp_path, None) is True
    assert _canonical_with_env(tmp_path, {}) is True
    assert _canonical_with_env(tmp_path, {"BLOODHOUND_DOMAIN": "127.0.0.1"}) is True
    assert _canonical_with_env(tmp_path, {k: "v" for k in BLOODHOUND_CREDENTIAL_ENV_KEYS}) is True


def test_canonical_guard_still_refuses_execution_redirecting_env(tmp_path):
    """The property the guard defends: nothing may point the trusted launcher at other code.

    These are the variables that make injected environment dangerous. If any of them ever passes,
    the allowlist has been widened into a hole — the canonical name, directory and execution class
    would all still look correct while a different binary runs.
    """
    for hostile in (
        {"PATH": "/tmp/evil"},
        {"LD_PRELOAD": "/tmp/x.so"},
        {"PYTHONPATH": "/tmp/evil"},
        {"VIRTUAL_ENV": "/tmp/evil"},
        {"BLOODHOUND_DOMAIN": "127.0.0.1", "PATH": "/tmp/evil"},
    ):
        assert _canonical_with_env(tmp_path, hostile) is False, f"must refuse {sorted(hostile)}"


def test_canonical_guard_matches_keys_exactly_not_by_prefix(tmp_path):
    """A `BLOODHOUND_`-prefix rule would admit any future variable the server may interpret."""
    assert _canonical_with_env(tmp_path, {"BLOODHOUND_EXTRA": "x"}) is False
    assert _canonical_with_env(tmp_path, {"BLOODHOUND_": "x"}) is False
    assert _canonical_with_env(tmp_path, {"bloodhound_domain": "x"}) is False, "case-exact"


def test_canonical_guard_requires_string_values_and_a_dict(tmp_path):
    """Non-strings get coerced somewhere downstream, and coercion is where surprises live."""
    assert _canonical_with_env(tmp_path, {"BLOODHOUND_PORT": 8083}) is False
    assert _canonical_with_env(tmp_path, {"BLOODHOUND_DOMAIN": None}) is False

    from ai.mcp import _bloodhound_env_admissible

    assert _bloodhound_env_admissible(["BLOODHOUND_DOMAIN=x"]) is False
    assert _bloodhound_env_admissible("BLOODHOUND_DOMAIN=x") is False


def test_credential_allowlist_is_the_same_list_the_resolver_uses():
    """A key resolvable but not admissible would fail at connect with a confusing guard error."""
    from ai.mcp import BLOODHOUND_CREDENTIAL_ENV_KEYS

    assert tuple(BLOODHOUND_ENV_KEYS) == tuple(BLOODHOUND_CREDENTIAL_ENV_KEYS)


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
