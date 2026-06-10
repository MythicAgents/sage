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
from typing import Optional

from ai.mcp import MCPManager, MCPConnectionConfig, create_stdio_config

BLOODHOUND_SERVER_NAME = "BloodHound"

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
    )


def bloodhound_connected() -> bool:
    """True if a BloodHound MCP server is currently connected."""
    try:
        return any("bloodhound" in s.lower() for s in MCPManager.get_connected_servers())
    except Exception:
        return False


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
