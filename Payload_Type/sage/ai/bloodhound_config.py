"""Shared BloodHound MCP connection config + connect helper.

BloodHound is central to Sage, but its MCP connection params are ENVIRONMENT-SPECIFIC (Sage cannot
guess where an arbitrary user's BloodHound MCP server lives). So they are configured via env:
  - SAGE_BLOODHOUND_MCP_DIR      — path to the BloodHound MCP server directory (REQUIRED to auto-connect)
  - SAGE_BLOODHOUND_MCP_COMMAND  — launcher command (default: "uv")

Used by:
  - the `bloodhound-connect` command (operator-facing one-shot connect)
  - the lazy startup auto-connect on the first `query` (in the serving event loop)
  - the BloodHound agent's not-connected EventFeed notice (the steps text)
"""
import hashlib
import os
from collections import Counter
from typing import Any, Optional

from ai.mcp import (
    BLOODHOUND_CREDENTIAL_ENV_KEYS,
    MCPManager,
    MCPConnectionConfig,
    MCP_EXECUTION_CLASS_BLOODHOUND_CONTROL_PLANE,
    create_stdio_config,
)

BLOODHOUND_SERVER_NAME = "BloodHound"
REQUIRED_BLOODHOUND_TOOLS = frozenset({"file_upload", "domain_info", "cypher_query"})

# Single definition, re-exported. It lives in ai/mcp.py because the pre-connect canonical-config
# guard there needs it to allowlist what may enter the MCP subprocess, and this module imports from
# that one — defining it here would be a circular import. Re-exported under the name the resolver
# (sage_chat/config.py), the UI declaration (sage_chat/models.py) and the diagnostic below already
# use, so all four stay bound to one list.
BLOODHOUND_CREDENTIAL_KEYS = BLOODHOUND_CREDENTIAL_ENV_KEYS
# The MCP server refuses to start without these three; PORT and SCHEME have defaults.
BLOODHOUND_REQUIRED_CREDENTIAL_KEYS = (
    "BLOODHOUND_DOMAIN",
    "BLOODHOUND_TOKEN_ID",
    "BLOODHOUND_TOKEN_KEY",
)

#: The one field an operator sets to say WHERE BloodHound is. Expanded back into the three keys the
#: MCP server reads, at the subprocess boundary and nowhere else.
BLOODHOUND_URL_KEY = "BLOODHOUND_URL"


#: Internal key → the key an operator can actually set. The resolver expands one `BLOODHOUND_URL`
#: into the address triple, so reporting a missing `BLOODHOUND_DOMAIN` would send someone looking for
#: a field that no longer exists in the chat configuration, the `.env`, or any document. Found by the
#: documentation guard rather than by review: the sample diagnostic copied into README named a
#: retired key, which was true of the code and wrong for the reader.
_OPERATOR_KEY_ALIASES = {
    "BLOODHOUND_DOMAIN": BLOODHOUND_URL_KEY,
    "BLOODHOUND_PORT": BLOODHOUND_URL_KEY,
    "BLOODHOUND_SCHEME": BLOODHOUND_URL_KEY,
}


def _operator_key(key: str) -> str:
    return _OPERATOR_KEY_ALIASES.get(key, key)


def credential_diagnostic(env: Optional[dict] = None) -> str:
    """Explain a connect failure in terms an operator can act on. Never emits a credential value.

    The raw failure is `McpError: Connection closed` — the MCP server exits during startup and the
    real reason (a missing BLOODHOUND_* variable) is only visible in the container log. This turns
    that into a statement of which credentials arrived, which did not, and where to set them.
    """
    supplied = sorted(k for k in (env or {}) if k in BLOODHOUND_CREDENTIAL_KEYS)
    missing = [k for k in BLOODHOUND_REQUIRED_CREDENTIAL_KEYS if k not in supplied]
    lines = [
        "Credentials Sage resolved for this attempt: "
        + (", ".join(_operator_key(k) for k in supplied) if supplied else "NONE"),
    ]
    if missing:
        lines.append("Missing (required): " + ", ".join(_operator_key(k) for k in missing))
        lines.append(
            "Two supported places, both editable without a shell: the chat configuration when you "
            "create the chat (per-chat), or Sage's own .env file, which you can open and edit from "
            "the Mythic UI on the installed-services page (shared by every chat in this container). "
            "Mythic user secrets work too. Full order, highest first: chat config → user secret → "
            "container env → .env.local → .env."
        )
        lines.append(
            "A third option exists and is the least durable: the BloodHound MCP server also reads "
            "its own .env from the directory SAGE_BLOODHOUND_MCP_DIR points at. In a Mythic install "
            "that is the image's baked /opt/bloodhound_mcp, which is NOT on the bind mount, so a "
            "file written there is lost on the next rebuild and cannot be edited from the UI."
        )
    else:
        lines.append(
            "All required credentials were supplied, so the failure is upstream of configuration: "
            "check that BloodHound CE is reachable from the Sage container at that host/port and "
            "that the API token is still valid. The container log has the server's own traceback."
        )
    return "\n".join(lines)

BLOODHOUND_SETUP_STEPS = (
    "BloodHound is NOT connected, so attack-graph ingest and analysis are unavailable — and BloodHound "
    "is central to Sage. To enable it:\n"
    "1. Ensure BloodHound CE is running (web/API + neo4j) and reachable from the Sage container.\n"
    "2. Supply BLOODHOUND_URL, BLOODHOUND_TOKEN_ID and BLOODHOUND_TOKEN_KEY. BLOODHOUND_URL is one "
    "address covering scheme, host and port, e.g. http://localhost:8080 for a stock CE. Set them "
    "either in the chat configuration when you "
    "create the chat, or in Sage's own .env — which you can open and edit straight from the Mythic "
    "UI on the installed-services page, then restart the container. No shell required for either. "
    "Mythic user secrets also work. Full order, highest first: chat config → user secret → "
    "container env → .env.local → .env.\n"
    "3. SAGE_BLOODHOUND_MCP_DIR must locate the MCP server; the container image bakes "
    "/opt/bloodhound_mcp by default. That directory also holds the MCP server's OWN .env, which is "
    "a third place credentials can live — but it is the least durable one: it is not on the Mythic "
    "bind mount, so anything written there is lost on the next rebuild and cannot be edited from "
    "the UI. Prefer step 2.\n"
    "4. Then run the `/bloodhound` command to connect, or start a new chat to auto-connect. The "
    "connection is process-global: once it succeeds, every later chat in this container reuses it."
)


def degraded_chat_notice(env: Optional[dict] = None) -> str:
    """The short, chat-surface version of "BloodHound is unavailable".

    Deliberately NOT `BLOODHOUND_SETUP_STEPS`. That text is right for an autonomous refusal, which is
    a wall the operator has just hit and must clear before anything proceeds. This fires on an
    ordinary conversational turn that WORKED, so it has to inform without hijacking the chat: what is
    unavailable, what still works, and where to fix it, in a few lines.

    Shown once per session (D5, Russel 2026-08-11): the operator saw it on the first degraded turn and
    does not need telling again, and Mythic already renders a live BloodHound-connected chip at the
    top of the chat, so repetition would be a third copy of a fact that is already on screen.
    """
    return (
        "⚠️ **BloodHound is not connected**, so attack-graph analysis and ingest are unavailable "
        "for this chat. Everything else works normally; only graph-dependent work is blocked, and an "
        "autonomous solve will refuse until it is connected.\n\n"
        f"{credential_diagnostic(env)}"
    )


def autonomous_unavailable_message(reason: Optional[str] = None) -> str:
    """Why an autonomous request was refused, and how to make it possible.

    D6, Russel's call 2026-08-11: an autonomous channel keeps its hard refusal when BloodHound is
    unavailable, because a solve reasons over the attack graph and a graph-blind autonomous session
    would act without any way to choose or verify a step. What changes is that the refusal now says
    so. The previous text named an internal invariant ("exact-tool admission") and left an operator
    with no idea that BloodHound was the subject or what to do about it.

    This is also the first live consumer of BLOODHOUND_SETUP_STEPS, which had none repo-wide while
    the module docstring claimed it fed a not-connected notice. A constant with no consumers cannot
    rot loudly: it drifts from reality and nobody finds out.
    """
    lead = "Autonomous execution is unavailable because BloodHound is not connected."
    # Callers pass an admission `reason`, and the commonest one is literally "BloodHound MCP is not
    # connected." Repeating the lead back at the operator makes the message look generated rather
    # than written, so a reason that adds nothing is dropped.
    cleaned = (reason or "").strip()
    detail = "" if not cleaned or "not connected" in cleaned.lower() else f" {cleaned}"
    return (
        f"{lead}{detail}\n\n"
        "An autonomous solve reasons over the BloodHound attack graph to choose and verify each "
        "step, so it fails closed rather than acting blind. Ordinary chat is unaffected.\n\n"
        f"{BLOODHOUND_SETUP_STEPS}"
    )


def _safe_server_identity(server: Any) -> str:
    if isinstance(server, str):
        return server
    if isinstance(server, (int, float, bool)):
        return str(server)
    if isinstance(server, dict):
        name = server.get("name")
        if isinstance(name, (str, int, float, bool)):
            return str(name)
    name = getattr(server, "name", None)
    if isinstance(name, (str, int, float, bool)):
        return str(name)
    return type(server).__name__


def bloodhound_mcp_config(
    directory: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> Optional[MCPConnectionConfig]:
    """Build the BloodHound MCP stdio config from an explicit directory or SAGE_BLOODHOUND_MCP_DIR.
    Returns None when no directory is configured (auto-connect then no-ops, gracefully).

    ``env`` carries BloodHound connection credentials into the server subprocess. It must be passed
    explicitly: the MCP stdio client inherits only a safe subset of the parent environment (POSIX:
    HOME/LOGNAME/PATH/SHELL/TERM/USER), so ``BLOODHOUND_*`` set on the Sage process does NOT reach
    the server by itself. The SDK merges this dict over that safe set rather than replacing it, so a
    partial dict is fine. Empty/None leaves the server to read its own directory ``.env`` as before.
    """
    d = directory or os.environ.get("SAGE_BLOODHOUND_MCP_DIR")
    if not d:
        return None
    command = os.environ.get("SAGE_BLOODHOUND_MCP_COMMAND", "uv")
    return create_stdio_config(
        name=BLOODHOUND_SERVER_NAME,
        command=command,
        args=["--directory", d, "run", "main.py"],
        env=dict(env) if env else {},
        cwd=d,
        encoding=None,
        encoding_error_handler=None,
        session_kwargs=None,
        sage_execution_class=MCP_EXECUTION_CLASS_BLOODHOUND_CONTROL_PLANE,
    )


def bloodhound_connected() -> bool:
    """True if a BloodHound MCP server is currently connected."""
    try:
        return any(MCPManager.is_bloodhound_server(s) for s in MCPManager.get_connected_servers())
    except Exception:
        return False


def bloodhound_tool_admission() -> dict[str, Any]:
    """Return an exact-name admission record for the canonical BloodHound MCP server.

    Autonomous native chat may only build its graph when the canonical server exposes
    the exact tools the runtime depends on. Matching is by full tool name, never by
    substring or near-match alias.
    """
    try:
        connected_servers = [
            server
            for server in MCPManager.get_connected_servers()
            if MCPManager.is_bloodhound_server(server)
        ]
    except Exception as exc:
        return {
            "ready": False,
            "connected": False,
            "server": None,
            "matching_server_count": 0,
            "matching_servers": [],
            "required_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
            "tool_names": [],
            "missing_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
            "duplicate_tool_names": [],
            "invalid_tool_name_count": 0,
            "reason": f"BloodHound MCP inspection failed: {exc}",
        }
    if not connected_servers:
        return {
            "ready": False,
            "connected": False,
            "server": None,
            "matching_server_count": 0,
            "matching_servers": [],
            "required_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
            "tool_names": [],
            "missing_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
            "duplicate_tool_names": [],
            "invalid_tool_name_count": 0,
            "reason": "BloodHound MCP is not connected.",
        }
    matching_servers = [_safe_server_identity(server) for server in connected_servers]
    if len(connected_servers) != 1:
        return {
            "ready": False,
            "connected": True,
            "server": None,
            "matching_server_count": len(connected_servers),
            "matching_servers": matching_servers,
            "required_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
            "tool_names": [],
            "missing_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
            "duplicate_tool_names": [],
            "invalid_tool_name_count": 0,
            "reason": "BloodHound MCP admission requires exactly one matching server.",
        }
    server = connected_servers[0]
    try:
        raw_names = [getattr(tool, "name", None) for tool in MCPManager.get_tools_by_server(server)]
    except Exception as exc:
        return {
            "ready": False,
            "connected": True,
            "server": _safe_server_identity(server),
            "matching_server_count": 1,
            "matching_servers": matching_servers,
            "required_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
            "tool_names": [],
            "missing_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
            "duplicate_tool_names": [],
            "invalid_tool_name_count": 0,
            "reason": f"BloodHound MCP tool inspection failed: {exc}",
        }
    valid_names = [
        name
        for name in raw_names
        if isinstance(name, str) and name and name == name.strip()
    ]
    name_counts = Counter(valid_names)
    names = sorted(name_counts)
    missing = sorted(REQUIRED_BLOODHOUND_TOOLS.difference(names))
    duplicates = sorted(name for name, count in name_counts.items() if count > 1)
    invalid_tool_name_count = len(raw_names) - len(valid_names)
    problems: list[str] = []
    if missing:
        problems.append(f"missing exact tools: {', '.join(missing)}")
    if duplicates:
        problems.append(f"duplicate exact tools: {', '.join(duplicates)}")
    if invalid_tool_name_count:
        problems.append(f"invalid tool names: {invalid_tool_name_count}")
    return {
        "ready": not problems,
        "connected": True,
        "server": _safe_server_identity(server),
        "matching_server_count": 1,
        "matching_servers": matching_servers,
        "required_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
        "tool_names": names,
        "missing_tools": missing,
        "duplicate_tool_names": duplicates,
        "invalid_tool_name_count": invalid_tool_name_count,
        "reason": (
            "BloodHound MCP exposes the required exact tools."
            if not problems
            else f"BloodHound MCP admission rejected: {'; '.join(problems)}."
        ),
    }


#: What an operator can actually SET, which is no longer the same list as what reaches the MCP
#: server. `BLOODHOUND_CREDENTIAL_KEYS` stays the subprocess allowlist — DOMAIN/PORT/SCHEME plus the
#: two tokens — because that is what the server reads and the pre-connect guard admits. This tuple is
#: the operator-facing half, and the two are bound together deliberately: the resolver
#: (`sage_chat/config.py`), the UI declaration (`sage_chat/models.py`) and the guard test all read
#: THIS list, so a key added to one side and not the other fails rather than silently going
#: unreachable. That exact gap shipped once: all five keys resolved correctly while nothing declared
#: them, so Mythic rendered no fields and the working credential path could not be reached.
BLOODHOUND_OPERATOR_CONFIG_KEYS = (
    BLOODHOUND_URL_KEY,
    "BLOODHOUND_TOKEN_ID",
    "BLOODHOUND_TOKEN_KEY",
)

#: Port assumed when a URL omits one, per scheme. These are the WEB defaults, not BloodHound's: the
#: MCP server's own fallback is 443/https, which matches no BloodHound CE install and is half the
#: reason this collapse is worth doing.
_DEFAULT_PORT_BY_SCHEME = {"http": 80, "https": 443}


class BloodHoundURLError(ValueError):
    """A URL Sage will not guess at. Always names the offending part."""


def parse_bloodhound_url(value: str) -> dict[str, str]:
    """Split one URL-shaped value into the `DOMAIN`/`PORT`/`SCHEME` the MCP server reads.

    `BLOODHOUND_DOMAIN` is a **hostname**, not an Active Directory domain — a genuinely confusing
    thing to call it inside a tool whose whole subject is AD domains, and the reason this collapse
    is worth more than the keystrokes it saves.

    Total over the documented input class: a bare host, an explicit scheme, an explicit port, a
    trailing slash, and an IPv6 literal in brackets. REJECTS anything carrying a path, query,
    fragment, or embedded credentials, naming the part it objected to. Rejecting is the whole point:
    silently discarding `/ui/login` from a pasted browser URL would produce a config that looks like
    what the operator typed and is not.
    """
    from urllib.parse import urlsplit

    raw = (value or "").strip()
    if not raw:
        raise BloodHoundURLError(f"{BLOODHOUND_URL_KEY} is empty.")

    # A bare host has no `//`, and urlsplit would read `host:8083` as scheme `host`. Normalising
    # first keeps one parser rather than a special case that drifts from it.
    candidate = raw if "//" in raw else f"//{raw}"
    parts = urlsplit(candidate, scheme="http")

    for label, present in (
        ("a path", parts.path.strip("/")),
        ("a query string", parts.query),
        ("a fragment", parts.fragment),
        ("embedded credentials", parts.username or parts.password),
    ):
        if present:
            # The offending part is NAMED but the value is never echoed. A URL can carry a password
            # in its userinfo or a token in its query string, and this message goes to the log and
            # the chat surface — the two places this ISA is deliberately putting more detail. A
            # rejection that leaks the secret it rejected is worse than the misconfiguration.
            raise BloodHoundURLError(
                f"{BLOODHOUND_URL_KEY} must be scheme://host:port only, but it carries {label}. "
                "Remove it and try again."
            )

    scheme = (parts.scheme or "http").lower()
    if scheme not in _DEFAULT_PORT_BY_SCHEME:
        raise BloodHoundURLError(
            f"{BLOODHOUND_URL_KEY} has an unsupported scheme {scheme!r}; use http or https."
        )

    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:  # urllib raises on a non-numeric or out-of-range port
        raise BloodHoundURLError(
            f"{BLOODHOUND_URL_KEY} has an invalid port ({exc}); it must be a number from 1 to 65535."
        ) from exc
    if not host:
        raise BloodHoundURLError(f"{BLOODHOUND_URL_KEY} has no host; expected scheme://host:port.")

    return {
        "BLOODHOUND_DOMAIN": host,
        "BLOODHOUND_PORT": str(port if port is not None else _DEFAULT_PORT_BY_SCHEME[scheme]),
        "BLOODHOUND_SCHEME": scheme,
    }


def directory_env_values(directory: Optional[str]) -> dict[str, str]:
    """Read the BloodHound credentials the MCP server will load from its OWN directory `.env`.

    This is not a convenience — it is what makes the pre-flight below safe. The server reads that
    file itself at startup, so a required key can be fully configured while Sage's `env` dict is
    empty. That is exactly how local development is set up here: the host checkout carries a `.env`
    with all five keys, the container image carries none, and only the latter fails. A pre-flight
    that consulted `env` alone would therefore refuse to connect a working BloodHound.

    Deliberately NOT consulted: `os.environ`. The MCP stdio client passes the subprocess only a
    safe subset of the parent environment (see `bloodhound_mcp_config`), so a `BLOODHOUND_*` set on
    the Sage process does not reach the server and must not count as resolved.

    Parsing mirrors `dotenv_bootstrap`: comments and blank lines are skipped, surrounding quotes are
    stripped, and an EMPTY value sets nothing — `KEY=` must not read as configured, or a half-filled
    file would defeat the very check this feeds.
    """
    d = directory or os.environ.get("SAGE_BLOODHOUND_MCP_DIR")
    if not d:
        return {}
    path = os.path.join(d, ".env")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.removeprefix("export ").strip()
        if key not in BLOODHOUND_CREDENTIAL_KEYS:
            continue
        value = value.strip().strip("\"'")
        if value:
            values[key] = value
    return values


def resolvable_credential_keys(
    directory: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> set[str]:
    """Every BloodHound credential key that will actually reach the server subprocess."""
    supplied = {k for k, v in (env or {}).items() if k in BLOODHOUND_CREDENTIAL_KEYS and v}
    return supplied | set(directory_env_values(directory))


#: Signature of the last credential set that FAILED, so an unchanged one is not retried. Values are
#: hashed, never stored: this module must not hold a credential in memory any longer than the connect
#: attempt itself, and a signature is only ever compared for equality.
_failed_credential_signature: Optional[str] = None


def _credential_signature(
    directory: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> str:
    """Fingerprint the exact inputs a connect attempt would use.

    Keys alone are not enough. An operator who fixes a typo'd token changes no key name, and a cache
    keyed on names would refuse to retry the corrected credentials — turning a helpful optimisation
    into a configuration change that appears to do nothing.
    """
    d = directory or os.environ.get("SAGE_BLOODHOUND_MCP_DIR") or ""
    merged = dict(directory_env_values(directory))
    merged.update({k: v for k, v in (env or {}).items() if k in BLOODHOUND_CREDENTIAL_KEYS and v})
    material = "\x00".join([d] + [f"{k}={merged[k]}" for k in sorted(merged)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


#: Seconds the reachability probe may take before it is treated as unreachable. Bounded so a hanging
#: BloodHound degrades the CLAIM rather than the operator's turn.
BLOODHOUND_PROBE_TIMEOUT_SECONDS = 20.0

#: Error-text fragment → which part of the configuration it indicts. Ordered most-specific first:
#: a TLS complaint must not be read as a generic connection failure, since its fix is the scheme.
_PROBE_FAILURE_CLASSES = (
    (
        ("ssl", "certificate verify", "wrong version number", "record layer"),
        "The TLS handshake failed, which usually means the SCHEME in BLOODHOUND_URL is wrong — "
        "https:// pointed at a plain-HTTP port, or http:// at a TLS one.",
    ),
    (
        ("401", "403", "unauthorized", "forbidden", "invalid signature", "authentication"),
        "BloodHound answered but REJECTED the credentials, so the address is right and "
        "BLOODHOUND_TOKEN_ID or BLOODHOUND_TOKEN_KEY is wrong, revoked, or swapped.",
    ),
    (
        ("connection refused", "failed to establish", "newconnectionerror", "max retries"),
        "Nothing accepted a connection at that address, so the HOST or PORT in BLOODHOUND_URL is "
        "wrong, or BloodHound CE is not running.",
    ),
    (
        ("name or service not known", "nodename nor servname", "getaddrinfo", "temporary failure in name"),
        "The hostname in BLOODHOUND_URL did not resolve.",
    ),
    (
        ("timed out", "timeout"),
        "BloodHound did not answer in time. The HOST or PORT in BLOODHOUND_URL may be reachable but "
        "wrong, or BloodHound CE is overloaded.",
    ),
    (
        ("404", "not found"),
        "The address answered but not with a BloodHound API, so BLOODHOUND_URL is probably pointing "
        "at the wrong service.",
    ),
)

#: Failures worth one retry: transient by nature. An auth rejection is deterministic, so retrying it
#: only doubles the delay before the operator is told what is actually wrong.
_TRANSIENT_PROBE_MARKERS = ("timed out", "timeout", "connection reset", "temporarily")


def classify_probe_failure(detail: str) -> str:
    """Turn a raw client error into a statement about which variable to fix.

    Russel's requirement when he asked for this probe: the message must say which part of the
    BloodHound configuration is wrong. "Connection closed" and "HTTPError" do not, and this ISA
    exists because a technically-accurate error that names nothing actionable is the same as silence.
    """
    lowered = (detail or "").lower()
    for markers, explanation in _PROBE_FAILURE_CLASSES:
        if any(marker in lowered for marker in markers):
            return explanation
    return (
        "BloodHound did not return domain data, and the failure did not match a known class. The "
        "raw error is included above; check BLOODHOUND_URL and both token values."
    )


async def probe_bloodhound_reachable(
    timeout: Optional[float] = None,
) -> tuple[bool, str]:
    """Call one read tool so "connected" means BloodHound actually answered.

    The MCP handshake proves a subprocess started and lists 13 statically-declared `@mcp.tool`
    functions; `BloodhoundAPI.__init__` validates only that the credentials are PRESENT and makes no
    network call. So an unreachable host, a wrong port and a revoked token are all invisible until the
    first real query — Sage would log "Connected to BloodHound MCP (13 tools)" for all three.

    It has to go through the MCP session rather than Sage calling BloodHound CE directly: when the
    server reads credentials from its own directory `.env`, Sage never sees those values, so a direct
    probe would return green exactly where it knows least.

    `domain_info(info_type="list")` is the probe because it is already one of the three tools the
    admission contract requires, it is a read, and its failure modes are precisely the three
    conditions that are otherwise invisible.
    """
    import asyncio

    budget = BLOODHOUND_PROBE_TIMEOUT_SECONDS if timeout is None else timeout
    tool = None
    for candidate in MCPManager.get_tools_by_server(BLOODHOUND_SERVER_NAME):
        if getattr(candidate, "name", None) == "domain_info":
            tool = candidate
            break
    if tool is None:
        return False, (
            "The BloodHound MCP server connected but exposes no `domain_info` tool, so its "
            "reachability could not be verified."
        )

    async def _attempt() -> tuple[bool, str]:
        try:
            result = await asyncio.wait_for(tool.ainvoke({"info_type": "list"}), timeout=budget)
        except asyncio.TimeoutError:
            return False, "timed out"
        except Exception as exc:  # noqa: BLE001 - any client failure is a probe failure
            return False, str(exc)
        text = str(result)
        # The server returns errors as TEXT rather than raising, so a successful call is not a
        # successful query. Checked explicitly, or a 401 rendered as a string would read as healthy.
        lowered = text.lower()
        if any(marker in lowered for marker in ("error", "failed", "unauthorized", "forbidden")):
            return False, text[:300]
        return True, text[:200]

    ok, detail = await _attempt()
    if not ok and any(marker in detail.lower() for marker in _TRANSIENT_PROBE_MARKERS):
        ok, detail = await _attempt()
    if ok:
        return True, detail
    return False, f"{detail}\n\n{classify_probe_failure(detail)}"


def reset_bloodhound_connect_cache() -> None:
    """Forget the last failure so the next call attempts a real connect."""
    global _failed_credential_signature
    _failed_credential_signature = None


async def ensure_bloodhound_connected(
    directory: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Connect the BloodHound MCP if not already connected. Idempotent. Returns (connected, message).

    MUST be awaited inside the serving event loop (the MCP stdio session is bound to the loop that
    creates it) — i.e. from a task handler or the agent run, NOT a throwaway loop at import time.

    ``env`` supplies BloodHound credentials to the server subprocess (see ``bloodhound_mcp_config``).
    The connection is process-global: the FIRST caller to connect establishes it for the container,
    and later callers short-circuit on the already-connected check so their ``env`` is not applied.
    That idempotence is deliberate — a new chat must not tear down a working session.

    ``force=True`` skips the short-circuit and rebinds with the supplied directory/credentials.
    ``MCPManager.connect_server`` already disconnects a same-named server before connecting, so no
    separate teardown is needed here. Reserve this for an explicit operator action: if the rebind
    fails, the previous working connection is gone, because the disconnect happens first.
    """
    if bloodhound_connected() and not force:
        return True, "BloodHound MCP already connected."
    replacing = force and bloodhound_connected()
    config = bloodhound_mcp_config(directory, env)
    if config is None:
        return False, ("BloodHound MCP not connected and no connection params configured "
                       "(set SAGE_BLOODHOUND_MCP_DIR or pass a directory).")
    lost = (
        "\n\nThe previous BloodHound connection was replaced before this attempt and is now gone — "
        "a forced reconnect disconnects first. Fix the above and run the command again."
        if replacing
        else ""
    )

    global _failed_credential_signature
    signature = _credential_signature(directory, env)
    resolvable = resolvable_credential_keys(directory, env)
    missing = [k for k in BLOODHOUND_REQUIRED_CREDENTIAL_KEYS if k not in resolvable]

    # F2: a knowably-doomed attempt is not made. The MCP server refuses to start without these three
    # and exits during startup, which Sage can only report as `McpError: Connection closed`. Spawning
    # `uv run main.py` to rediscover a fact already visible here costs a subprocess, a 30s stdio
    # timeout on the turn, and two tracebacks in the log — per request, forever, because the missing
    # configuration is static.
    #
    # `force` bypasses it, and that exemption is deliberate rather than a concession to a failing
    # test. This check models two credential sources; the server may read configuration this module
    # does not know about, so a refusal is a GUESS that the attempt would fail. Guessing is right for
    # the automatic per-request path, where the cost is paid on every turn and the answer is reported
    # either way. It is wrong for an operator who explicitly asked to reconnect: there, a false
    # refusal blocks a connect that might have worked, and the operator has already accepted the cost
    # of one attempt by asking for it.
    if missing and not force:
        _failed_credential_signature = signature
        return False, (
            "BloodHound MCP connect not attempted: required credentials are unset, so the server "
            f"would exit during startup.\n\n{credential_diagnostic(env)}{lost}"
        )

    # F3: an unchanged credential set that already failed is not retried. Keyed on the exact inputs,
    # so correcting a token invalidates it immediately; a successful connect clears it outright.
    if not force and _failed_credential_signature == signature:
        return False, (
            "BloodHound MCP connect skipped: this exact credential set already failed and nothing "
            "about it has changed. Update the configuration to trigger a fresh attempt.\n\n"
            f"{credential_diagnostic(env)}{lost}"
        )

    try:
        success, err = await MCPManager.connect_server(config)
    except Exception as e:
        _failed_credential_signature = signature
        return False, f"BloodHound MCP connect raised: {e}\n\n{credential_diagnostic(env)}{lost}"
    if success:
        # The handshake succeeded, which says the subprocess started. It does NOT say BloodHound
        # answered — so the claim is withheld until one real read proves it (ISC-27).
        reachable, probe_detail = await probe_bloodhound_reachable()
        if not reachable:
            _failed_credential_signature = signature
            return False, (
                "BloodHound MCP started, but BloodHound CE did not answer a read, so the connection "
                f"is NOT usable.\n\n{probe_detail}{lost}"
            )
        _failed_credential_signature = None
        n = len(MCPManager.get_tools_by_server(BLOODHOUND_SERVER_NAME))
        verb = "Reconnected to" if replacing else "Connected to"
        return True, f"{verb} BloodHound MCP ({n} tools), verified by a live domain read."
    _failed_credential_signature = signature
    return False, f"Failed to connect to BloodHound MCP: {err}\n\n{credential_diagnostic(env)}{lost}"
