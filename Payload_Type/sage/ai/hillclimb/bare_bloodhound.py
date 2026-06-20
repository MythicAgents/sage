"""Dynamic BloodHound MCP tools for the BARE model — ALL tools the MCP server exposes, discovered at
runtime (not hardcoded).

This gives the bare model the SAME external attack-graph tool Sage uses (BloodHound is a standard
offensive tool, not Sage's invention). What stays EXCLUDED is Sage's BloodHound *agent* — the
`ingest_collection` reconciliation, collect-once gate, graph-fact injection, dedicated-agent
orchestration — i.e. the harness value, not the tool.

Connection reuses Sage's config contract (`ai/bloodhound_config.py`): the stdio MCP server started via
`uv --directory $SAGE_BLOODHOUND_MCP_DIR run main.py`. We connect with `MultiServerMCPClient` in its
stateless mode — each tool call opens its OWN stdio session (its docstring: "A new session will be
created for each tool call"), which is loop-safe with the bare runner's asyncio.run-per-call dispatcher
(a persistent session would be bound to one event loop and break across calls).

The MCP directory defaults to the known lab path (`_DEFAULT_BLOODHOUND_MCP_DIR`) so a bare run gets
BloodHound with no env setup; `SAGE_BLOODHOUND_MCP_DIR` overrides it. Graceful: if the resolved dir
doesn't exist or the connect/list fails, returns NO tools and the bare model simply runs without
BloodHound (it still has the Mythic toolset to collect/enumerate by hand).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable

BLOODHOUND_SERVER = "BloodHound"


# Hardcoded default so a bare run lights up BloodHound with NO env setup; the env var is just an override.
_DEFAULT_BLOODHOUND_MCP_DIR = "/home/john/dev/bloodhound_mcp"


def _connections() -> dict | None:
    """Stdio connection dict for MultiServerMCPClient. Resolves the MCP dir as SAGE_BLOODHOUND_MCP_DIR
    (override) else the hardcoded default; returns None only if the resolved dir doesn't exist on this
    host (graceful no-op for non-lab machines)."""
    d = os.environ.get("SAGE_BLOODHOUND_MCP_DIR") or _DEFAULT_BLOODHOUND_MCP_DIR
    if not d or not os.path.isdir(d):
        return None
    command = os.environ.get("SAGE_BLOODHOUND_MCP_COMMAND", "uv")
    return {BLOODHOUND_SERVER: {"transport": "stdio", "command": command,
                                "args": ["--directory", d, "run", "main.py"], "cwd": d}}


def _params_from_tool(tool: Any) -> dict:
    """Build an OpenAI-function JSON-schema `parameters` object from a LangChain MCP tool."""
    schema = getattr(tool, "args_schema", None)
    try:
        if schema is not None and hasattr(schema, "model_json_schema"):
            js = schema.model_json_schema()
            return {"type": "object", "properties": js.get("properties", {}),
                    "required": js.get("required", [])}
        if isinstance(schema, dict):
            return {"type": "object", "properties": schema.get("properties", schema),
                    "required": schema.get("required", [])}
    except Exception:
        pass
    try:
        args = tool.args
        if isinstance(args, dict):
            return {"type": "object", "properties": args, "required": list(args.keys())}
    except Exception:
        pass
    return {"type": "object", "properties": {}}


def tool_spec(tool: Any) -> dict:
    """One discovered MCP tool -> OpenAI-function schema (pure; unit-testable with a stub tool)."""
    return {"type": "function",
            "function": {"name": tool.name,
                         "description": (getattr(tool, "description", "") or tool.name)[:1000],
                         "parameters": _params_from_tool(tool)}}


def load_bloodhound_mcp_tools() -> tuple[list[dict], dict]:
    """Discover EVERY BloodHound MCP tool. Returns (specs, {name: langchain_tool}).
    Empty (and a loud note) if not configured / connect fails — bare then runs without BloodHound."""
    conns = _connections()
    if conns is None:
        return [], {}
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        client = MultiServerMCPClient(conns)
        tools = asyncio.run(client.get_tools(server_name=BLOODHOUND_SERVER))
    except Exception as e:
        print(f"[bare] BloodHound MCP unavailable ({type(e).__name__}: {e}) — running without it", flush=True)
        return [], {}
    return [tool_spec(t) for t in tools], {t.name: t for t in tools}


def make_bloodhound_dispatcher(registry: dict) -> Callable[[dict], str | None]:
    """Executor for the discovered BloodHound tools. Returns None if the call isn't a BloodHound tool
    (so a combined dispatcher can fall through). Each call opens its own stdio session (loop-safe)."""
    def dispatch(call: dict) -> str | None:
        t = registry.get(call.get("tool", ""))
        if t is None:
            return None
        try:
            res = asyncio.run(t.ainvoke(call.get("args", {}) or {}))
            return res if isinstance(res, str) else json.dumps(res, default=str)
        except Exception as e:
            return f"[bloodhound tool error] {call.get('tool')}: {type(e).__name__}: {e}"

    return dispatch
