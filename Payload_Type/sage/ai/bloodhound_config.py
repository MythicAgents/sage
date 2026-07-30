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
        + (", ".join(supplied) if supplied else "NONE"),
    ]
    if missing:
        lines.append("Missing (required): " + ", ".join(missing))
        lines.append(
            "Set them in the chat configuration when creating the chat, or as Mythic user secrets, "
            "or as environment variables on the Sage container. Resolution order is chat config → "
            "user secret → container env."
        )
        lines.append(
            "If you would rather keep credentials in the BloodHound MCP server's own .env, that "
            "file must live in the directory SAGE_BLOODHOUND_MCP_DIR points at — note the image's "
            "baked /opt/bloodhound_mcp is not on the Mythic bind mount, so a .env written there is "
            "lost on rebuild. See README 'Using a .env file instead, under a Mythic install'."
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
    "2. Supply BLOODHOUND_DOMAIN, BLOODHOUND_TOKEN_ID and BLOODHOUND_TOKEN_KEY (plus BLOODHOUND_PORT "
    "and BLOODHOUND_SCHEME if they are not 443/https). Set them in the chat configuration when you "
    "create the chat, as Mythic user secrets, or as environment variables on the Sage container — "
    "resolution order is chat config → user secret → container env. Alternatively put them in the "
    "BloodHound MCP server's own .env, in the directory SAGE_BLOODHOUND_MCP_DIR points at.\n"
    "3. SAGE_BLOODHOUND_MCP_DIR must locate the MCP server; the container image bakes "
    "/opt/bloodhound_mcp by default.\n"
    "4. Then run the `/bloodhound` command to connect, or start a new chat to auto-connect. The "
    "connection is process-global: once it succeeds, every later chat in this container reuses it."
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
    try:
        success, err = await MCPManager.connect_server(config)
    except Exception as e:
        return False, f"BloodHound MCP connect raised: {e}\n\n{credential_diagnostic(env)}{lost}"
    if success:
        n = len(MCPManager.get_tools_by_server(BLOODHOUND_SERVER_NAME))
        verb = "Reconnected to" if replacing else "Connected to"
        return True, f"{verb} BloodHound MCP ({n} tools)."
    return False, f"Failed to connect to BloodHound MCP: {err}\n\n{credential_diagnostic(env)}{lost}"
