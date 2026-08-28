"""Dynamic BloodHound MCP tools for the BARE model, discovered at runtime.

This gives the bare model the SAME LLM-visible external attack-graph surface shipping Sage uses.
Server tools outside that shared allowlist are withheld from both arms. What stays EXCLUDED is
Sage's BloodHound *agent* — the
`ingest_collection` reconciliation, collect-once gate, graph-fact injection, dedicated-agent
orchestration — i.e. the harness value, not the tool.

Connection reuses Sage's config contract (`ai/bloodhound_config.py`): the stdio MCP server started via
`uv --directory $SAGE_BLOODHOUND_MCP_DIR run main.py`. We connect with `MultiServerMCPClient` in its
stateless mode — each tool call opens its OWN stdio session (its docstring: "A new session will be
created for each tool call"), which is loop-safe with the bare runner's asyncio.run-per-call dispatcher
(a persistent session would be bound to one event loop and break across calls).

The MCP directory is supplied explicitly through `SAGE_BLOODHOUND_MCP_DIR`. Discovery returns an empty
toolset when unavailable so offline callers can inspect the seam; the Gate 1P live runner rejects that
empty result rather than silently relabeling a Mythic-only baseline as Mythic/BloodHound.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable

try:
    from ..comparison_tool_surface import SHIPPING_BLOODHOUND_TOOL_NAMES
except ImportError:  # direct module import in focused tests
    from ai.comparison_tool_surface import SHIPPING_BLOODHOUND_TOOL_NAMES  # type: ignore

BLOODHOUND_SERVER = "BloodHound"


def _connections() -> dict | None:
    """Build the stdio connection from the explicit external-dependency contract."""
    d = str(os.environ.get("SAGE_BLOODHOUND_MCP_DIR") or "").strip()
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


def admitted_bloodhound_tools(tools: list[Any]) -> list[Any]:
    """Apply the same exact-name, unique-name allowlist as shipping Sage."""
    name_counts: dict[str, int] = {}
    for tool in tools:
        name = getattr(tool, "name", None)
        if isinstance(name, str) and name and name == name.strip():
            name_counts[name] = name_counts.get(name, 0) + 1
    return [
        tool
        for tool in tools
        if (
            isinstance(getattr(tool, "name", None), str)
            and name_counts.get(tool.name) == 1
            and tool.name in SHIPPING_BLOODHOUND_TOOL_NAMES
        )
    ]


def load_bloodhound_mcp_tools() -> tuple[list[dict], dict]:
    """Discover and admit the exact shipping-Sage BloodHound surface.

    Duplicate, malformed, and non-shipping names fail closed by being withheld. Empty discovery is
    returned when unavailable; the live Gate 1P builder rejects that state.
    """
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
    admitted = admitted_bloodhound_tools(list(tools))
    return [tool_spec(t) for t in admitted], {t.name: t for t in admitted}


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
