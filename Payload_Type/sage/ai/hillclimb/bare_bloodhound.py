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
import base64
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


def admitted_collection_upload_tool(tools: list[Any]) -> Any | None:
    """Select one exact `file_upload` adapter without exposing it as a model-visible MCP tool."""
    matches = [tool for tool in tools if getattr(tool, "name", None) == "file_upload"]
    if len(matches) != 1:
        return None
    params = _params_from_tool(matches[0]).get("properties", {})
    required = {"info_type", "job_id", "file_name", "file_bytes_base64"}
    return matches[0] if required <= set(params) else None


def _discover_bloodhound_tools() -> list[Any]:
    conns = _connections()
    if conns is None:
        return []
    from langchain_mcp_adapters.client import MultiServerMCPClient
    client = MultiServerMCPClient(conns)
    return list(asyncio.run(client.get_tools(server_name=BLOODHOUND_SERVER)))


def load_bloodhound_mcp_surface() -> tuple[list[dict], dict, Any | None]:
    """Discover model-visible query tools plus the hidden collection-upload dependency once."""
    try:
        tools = _discover_bloodhound_tools()
    except Exception as e:
        print(f"[bare] BloodHound MCP unavailable ({type(e).__name__}: {e}) — running without it", flush=True)
        return [], {}, None
    admitted = admitted_bloodhound_tools(tools)
    return (
        [tool_spec(tool) for tool in admitted],
        {tool.name: tool for tool in admitted},
        admitted_collection_upload_tool(tools),
    )


def load_bloodhound_mcp_tools() -> tuple[list[dict], dict]:
    """Discover and admit the exact shipping-Sage BloodHound surface.

    Duplicate, malformed, and non-shipping names fail closed by being withheld. Empty discovery is
    returned when unavailable; the live Gate 1P builder rejects that state.
    """
    specs, registry, _ = load_bloodhound_mcp_surface()
    return specs, registry


def make_collection_ingestor(file_upload_tool: Any) -> Callable[[str, bytes], Any]:
    """Build the hidden raw bytes-upload adapter used only by raw `ingest_collection`."""
    async def ingest(file_name: str, file_bytes: bytes) -> dict[str, Any]:
        if file_upload_tool is None:
            raise RuntimeError("BloodHound file_upload tool is unavailable or schema-incompatible")
        upload_raw = await file_upload_tool.ainvoke({
            "info_type": "upload_bytes",
            "file_name": file_name,
            "file_bytes_base64": base64.b64encode(file_bytes).decode("ascii"),
        })
        upload = _json_value(upload_raw)
        job_id = _find_job_id(upload)
        status = None
        if job_id is not None:
            status = _json_value(await file_upload_tool.ainvoke({
                "info_type": "status",
                "job_id": job_id,
            }))
        return {"upload": upload, "job_status": status}
    return ingest


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _find_job_id(value: Any) -> int | str | None:
    if isinstance(value, dict):
        if value.get("job_id") is not None:
            return value["job_id"]
        for nested in value.values():
            found = _find_job_id(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_job_id(nested)
            if found is not None:
                return found
    return None


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
