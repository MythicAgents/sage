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
from typing import Any, Optional

from ai.mcp import (
    MCPManager,
    MCPConnectionConfig,
    MCP_EXECUTION_CLASS_BLOODHOUND_CONTROL_PLANE,
    create_stdio_config,
)

BLOODHOUND_SERVER_NAME = "BloodHound"
REQUIRED_BLOODHOUND_TOOLS = frozenset({"file_upload", "domain_info", "cypher_query"})

BLOODHOUND_SETUP_STEPS = (
    "BloodHound is NOT connected, so attack-graph ingest and analysis are unavailable — and BloodHound "
    "is central to Sage. To enable it:\n"
    "1. Ensure BloodHound CE is running (web/API + neo4j) and reachable from the Sage host.\n"
    "2. Put your BloodHound API token (id + key) in the BloodHound MCP server's .env.\n"
    "3. Set SAGE_BLOODHOUND_MCP_DIR to the BloodHound MCP directory so Sage auto-connects on startup, OR "
    "run the `bloodhound-connect` command (optionally `-directory <path>`), OR connect manually with "
    "`mcp-connect`.\n"
    "Full steps: Sage payload documentation -> \"Connecting BloodHound to Sage\"."
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


def bloodhound_mcp_config(directory: Optional[str] = None) -> Optional[MCPConnectionConfig]:
    """Build the BloodHound MCP stdio config from an explicit directory or SAGE_BLOODHOUND_MCP_DIR.
    Returns None when no directory is configured (auto-connect then no-ops, gracefully)."""
    d = directory or os.environ.get("SAGE_BLOODHOUND_MCP_DIR")
    if not d:
        return None
    command = os.environ.get("SAGE_BLOODHOUND_MCP_COMMAND", "uv")
    return create_stdio_config(
        name=BLOODHOUND_SERVER_NAME,
        command=command,
        args=["--directory", d, "run", "main.py"],
        env={},
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
            "reason": "BloodHound MCP admission requires exactly one matching server.",
        }
    server = connected_servers[0]
    try:
        names = sorted({
            str(getattr(tool, "name", "") or "").strip()
            for tool in MCPManager.get_tools_by_server(server)
            if str(getattr(tool, "name", "") or "").strip()
        })
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
            "reason": f"BloodHound MCP tool inspection failed: {exc}",
        }
    missing = sorted(REQUIRED_BLOODHOUND_TOOLS.difference(names))
    return {
        "ready": not missing,
        "connected": True,
        "server": _safe_server_identity(server),
        "matching_server_count": 1,
        "matching_servers": matching_servers,
        "required_tools": sorted(REQUIRED_BLOODHOUND_TOOLS),
        "tool_names": names,
        "missing_tools": missing,
        "reason": (
            "BloodHound MCP exposes the required exact tools."
            if not missing
            else f"BloodHound MCP missing exact tools: {', '.join(missing)}."
        ),
    }


async def ensure_bloodhound_connected(directory: Optional[str] = None) -> tuple[bool, str]:
    """Connect the BloodHound MCP if not already connected. Idempotent. Returns (connected, message).

    MUST be awaited inside the serving event loop (the MCP stdio session is bound to the loop that
    creates it) — i.e. from a task handler or the agent run, NOT a throwaway loop at import time.
    """
    if bloodhound_connected():
        return True, "BloodHound MCP already connected."
    config = bloodhound_mcp_config(directory)
    if config is None:
        return False, ("BloodHound MCP not connected and no connection params configured "
                       "(set SAGE_BLOODHOUND_MCP_DIR or pass a directory).")
    try:
        success, err = await MCPManager.connect_server(config)
    except Exception as e:
        return False, f"BloodHound MCP connect raised: {e}"
    if success:
        n = len(MCPManager.get_tools_by_server(BLOODHOUND_SERVER_NAME))
        return True, f"Connected to BloodHound MCP ({n} tools)."
    return False, f"Failed to connect to BloodHound MCP: {err}"
