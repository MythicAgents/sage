"""MCP tool safety policy: deny-by-default gating for MCP server tools.

Loads a JSON policy file that classifies MCP tools as ``guarded`` (HITL in supervised, denied in
conversational) or ``read_only`` (freely available). Unclassified tools default to guarded.

Policy file format (``mcp_tool_policy.json``):

    {
        "default": "guarded",
        "servers": {
            "bloodhound-ce": {
                "default": "read_only",
                "tools": {
                    "file_upload": "guarded"
                }
            }
        }
    }

Lookup order: tool-level override > server default > global default > GUARDED (hardcoded fallback).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from .mythic_tools import TOOL_SAFETY_GUARDED, TOOL_SAFETY_READ_ONLY, _TOOL_SAFETY_VALUES
except ImportError:
    from mythic_tools import TOOL_SAFETY_GUARDED, TOOL_SAFETY_READ_ONLY, _TOOL_SAFETY_VALUES

try:
    from mythic_container.logging import logger
except ImportError:
    import logging
    logger = logging.getLogger("mythic")

_DEFAULT_POLICY_FILENAME = "mcp_tool_policy.json"
_ENV_VAR = "SAGE_MCP_TOOL_POLICY"

_loaded_policy: dict[str, Any] | None = None
_policy_path: str = ""


def _validate_classification(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in _TOOL_SAFETY_VALUES:
        return normalized
    return TOOL_SAFETY_GUARDED


def _find_policy_path() -> str:
    env_path = os.environ.get(_ENV_VAR, "").strip()
    if env_path:
        return env_path
    here = Path(__file__).resolve().parent
    for candidate in [
        here.parent / _DEFAULT_POLICY_FILENAME,
        here.parent.parent / _DEFAULT_POLICY_FILENAME,
    ]:
        if candidate.is_file():
            return str(candidate)
    return ""


def load_policy(path: str = "") -> dict[str, Any]:
    """Load and validate the MCP tool policy. Returns the parsed policy or an empty dict."""
    global _loaded_policy, _policy_path
    resolved = path or _find_policy_path()
    if not resolved:
        _loaded_policy = {}
        _policy_path = ""
        return _loaded_policy
    try:
        raw = Path(resolved).read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning(f"MCP tool policy at {resolved} is not a JSON object, using defaults")
            data = {}
        _loaded_policy = data
        _policy_path = resolved
        server_count = len(data.get("servers", {}))
        logger.info(f"MCP tool policy loaded from {resolved} ({server_count} server(s) configured)")
        return _loaded_policy
    except FileNotFoundError:
        logger.info(f"MCP tool policy file not found at {resolved}, all MCP tools default to guarded")
        _loaded_policy = {}
        _policy_path = ""
        return _loaded_policy
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"MCP tool policy at {resolved} failed to parse ({e}), all MCP tools default to guarded")
        _loaded_policy = {}
        _policy_path = ""
        return _loaded_policy


def get_policy() -> dict[str, Any]:
    """Return the currently loaded policy, loading lazily if needed."""
    global _loaded_policy
    if _loaded_policy is None:
        load_policy()
    return _loaded_policy or {}


def policy_path() -> str:
    """Return the path the policy was loaded from, or empty string."""
    if _loaded_policy is None:
        load_policy()
    return _policy_path


def classify_mcp_tool(server_name: str, tool_name: str) -> str:
    """Return the safety classification for an MCP tool per the loaded policy.

    Lookup order:
    1. Server-specific tool override
    2. Server default
    3. Global default
    4. TOOL_SAFETY_GUARDED (hardcoded fallback)
    """
    policy = get_policy()
    servers = policy.get("servers")
    if isinstance(servers, dict):
        server_entry = servers.get(server_name)
        if isinstance(server_entry, dict):
            tools = server_entry.get("tools")
            if isinstance(tools, dict) and tool_name in tools:
                return _validate_classification(tools[tool_name])
            server_default = server_entry.get("default")
            if server_default is not None:
                return _validate_classification(server_default)
    global_default = policy.get("default")
    if global_default is not None:
        return _validate_classification(global_default)
    return TOOL_SAFETY_GUARDED


def is_mcp_tool_guarded(server_name: str, tool_name: str) -> bool:
    """True if the MCP tool should be HITL-gated in supervised mode / denied in conversational."""
    return classify_mcp_tool(server_name, tool_name) == TOOL_SAFETY_GUARDED


def effective_policy_summary() -> list[dict[str, Any]]:
    """Return a display-ready summary of the effective policy for /mcp policy."""
    policy = get_policy()
    result = []
    global_default = _validate_classification(policy.get("default", TOOL_SAFETY_GUARDED))
    result.append({"scope": "global", "default": global_default})
    servers = policy.get("servers")
    if isinstance(servers, dict):
        for name, entry in sorted(servers.items()):
            if not isinstance(entry, dict):
                continue
            server_default = _validate_classification(entry.get("default", global_default))
            tools = entry.get("tools", {})
            overrides = {}
            if isinstance(tools, dict):
                overrides = {t: _validate_classification(v) for t, v in tools.items()}
            result.append({
                "scope": f"server:{name}",
                "default": server_default,
                "tools": overrides,
            })
    return result
