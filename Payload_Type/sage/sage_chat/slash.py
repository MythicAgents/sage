"""Phase 3 slash commands for the chat container.

The server natively parses `/state`, `/list`, ... against the model's declared commands and delivers a
structured `ChatRequest.SlashCommand{Name, Argument, Raw, Source}` (PRD Section 8). Sage declares its
commands in `ChatModelMetadata.SlashCommands` (see models.py) and dispatches on `Name` here. Undeclared
input falls through to normal chat.

These are lightweight chat-native re-homes of the old PayloadType `state`/`list`/`stop` commands — enough
to be useful without porting the full task-bound implementations. They grow later; `mcp_*` /
`bloodhound_connect` are follow-ups.
"""

from __future__ import annotations

from typing import Any

from mythic_container.ChatBase import ChatRequest, ChatSlashCommandDefinition

# Declared to Mythic via model metadata. Names have no leading slash.
SLASH_COMMANDS = [
    ChatSlashCommandDefinition(Name="state", Description="Show this channel's Sage session (mode, provider/model, turns)."),
    ChatSlashCommandDefinition(Name="list", Description="List active Sage chat sessions."),
    ChatSlashCommandDefinition(Name="mode", Description="Show or set the agent mode: /mode [supervised|auto]."),
    ChatSlashCommandDefinition(Name="stop", Description="Cooperatively stop the running agent on this channel."),
    ChatSlashCommandDefinition(Name="mcp", Description="Manage MCP servers: /mcp list | /mcp tools [server] | /mcp connect <json> | /mcp disconnect <name>."),
    ChatSlashCommandDefinition(Name="bloodhound", Description="Connect the baked-in BloodHound MCP: /bloodhound [directory]."),
]


def _handle_mode(model: Any, arg: str) -> str:
    current = getattr(model, "mode", "supervised") if model is not None else "(no session)"
    choice = arg.strip().lower()
    if not choice:
        return f"Current mode: **{current}**. Set with `/mode supervised` or `/mode auto`."
    if choice not in ("supervised", "auto"):
        return f"Unknown mode `{choice}`. Valid: `supervised`, `auto`."
    if model is None:
        return f"No active session yet — send a message first, then `/mode {choice}`."
    model.mode = choice
    return (
        f"Mode set to **{choice}** for this channel. Note: the guarded-tool interrupt is wired at graph "
        "build, so a supervised↔auto switch fully applies on the next fresh session."
    )


def _handle_state(model: Any, request: ChatRequest) -> str:
    if model is None:
        return "No active Sage session on this channel yet — send a message to start one."
    turns = len(getattr(model, "messages", []) or [])
    # Mythic chat renders markdown — present the session state as a table.
    return (
        "**Sage session — this channel**\n\n"
        "| Field | Value |\n"
        "|---|---|\n"
        f"| Channel | `{request.ChannelID}` |\n"
        f"| Mode | `{getattr(model, 'mode', 'supervised')}` |\n"
        f"| Provider | `{getattr(model, 'provider', '?')}` |\n"
        f"| Model | `{getattr(model, 'model', '?')}` |\n"
        f"| Captured messages | `{turns}` |"
    )


async def _handle_list() -> str:
    try:
        from ai.langgraph.model import list_sessions
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.model import list_sessions  # type: ignore
    sessions = await list_sessions()
    if not sessions:
        return "No active Sage chat sessions."
    lines = ["**Active Sage sessions**", "", "| Channel | Provider | Model | Mode |", "|---|---|---|---|"]
    for key, m in sessions.items():
        lines.append(
            f"| `{key}` | `{getattr(m, 'provider', '?')}` | `{getattr(m, 'model', '?')}` | `{getattr(m, 'mode', '?')}` |"
        )
    return "\n".join(lines)


async def _mcp_connect(spec: str) -> str:
    import json as _json
    try:
        from ai.mcp import MCPManager, create_stdio_config, create_sse_config, create_streamable_http_config
    except ImportError:  # pragma: no cover
        from ..ai.mcp import MCPManager, create_stdio_config, create_sse_config, create_streamable_http_config  # type: ignore
    if not spec:
        return 'Usage: `/mcp connect {"type":"stdio","name":"x","command":"uv","args":["run","main.py"]}`'
    try:
        cfg = _json.loads(spec)
    except Exception as e:
        return f"Invalid JSON for `/mcp connect`: {e}"
    name = cfg.get("name")
    if not name:
        return "`/mcp connect` requires a `name` field."
    ctype = str(cfg.get("type", "stdio")).lower()
    try:
        if ctype == "stdio":
            conf = create_stdio_config(
                name=name, command=cfg.get("command", ""), args=cfg.get("args") or [],
                env=cfg.get("env"), cwd=cfg.get("cwd"), encoding=None,
                encoding_error_handler=None, session_kwargs=None,
            )
        elif ctype == "sse":
            conf = create_sse_config(name=name, url=cfg.get("url", ""), headers=cfg.get("headers"))
        elif ctype in ("http", "streamable_http", "streamable-http"):
            conf = create_streamable_http_config(name=name, url=cfg.get("url", ""), headers=cfg.get("headers"))
        else:
            return f"Unknown MCP type `{ctype}` — use `stdio`, `sse`, or `http`."
        ok, err = await MCPManager.connect_server(conf)
        return f"Connected MCP server `{name}`." if ok else f"Failed to connect `{name}`: {err}"
    except Exception as e:
        return f"Error connecting MCP server `{name}`: {e}"


async def _handle_mcp(arg: str) -> str:
    try:
        from ai.mcp import MCPManager
    except ImportError:  # pragma: no cover
        from ..ai.mcp import MCPManager  # type: ignore
    parts = (arg or "").strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    rest = parts[1].strip() if len(parts) > 1 else ""
    if sub in ("", "list"):
        servers = MCPManager.get_connected_servers()
        if not servers:
            return "No MCP servers connected."
        summary = MCPManager.get_tools_summary()
        total = summary.get("total_tools") if isinstance(summary, dict) else None
        body = "\n".join(f"- `{s}` ({len(MCPManager.get_tools_by_server(s))} tools)" for s in servers)
        return (f"**Connected MCP servers**\n{body}"
                + (f"\n_total tools: {total}_" if total is not None else "")
                + "\n\n_See tool names with_ `/mcp tools [server]`.")
    if sub == "tools":
        servers = MCPManager.get_connected_servers()
        if not servers:
            return "No MCP servers connected."
        if rest:
            target = [s for s in servers if s.lower() == rest.lower()]
            if not target:
                joined = ", ".join(f"`{s}`" for s in servers)
                return f"No connected MCP server named `{rest}`. Connected: {joined}."
        else:
            target = servers
        lines: list[str] = []
        for s in target:
            tools = MCPManager.get_tools_by_server(s)
            lines.append(f"**{s}** — {len(tools)} tool(s)")
            for t in tools:
                name = getattr(t, "name", "?")
                desc = (getattr(t, "description", "") or "").strip().splitlines()[0] if getattr(t, "description", "") else ""
                desc = (desc[:100] + "…") if len(desc) > 100 else desc
                lines.append(f"- `{name}`" + (f" — {desc}" if desc else ""))
        return "\n".join(lines)
    if sub == "disconnect":
        if not rest:
            return "Usage: `/mcp disconnect <server-name>`"
        ok = await MCPManager.disconnect_server(rest)
        return f"Disconnected MCP server `{rest}`." if ok else f"Failed to disconnect `{rest}` (not connected?)."
    if sub == "connect":
        return await _mcp_connect(rest)
    return "Usage: `/mcp list` · `/mcp tools [server]` · `/mcp connect <json>` · `/mcp disconnect <name>`"


async def _handle_bloodhound(arg: str) -> str:
    try:
        from ai.bloodhound_config import ensure_bloodhound_connected
    except ImportError:  # pragma: no cover
        from ..ai.bloodhound_config import ensure_bloodhound_connected  # type: ignore
    directory = (arg or "").strip() or None
    _connected, msg = await ensure_bloodhound_connected(directory)
    return msg


async def _handle_stop(request: ChatRequest) -> str:
    try:
        from ai.langgraph.model import request_stop_for_sessions
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.model import request_stop_for_sessions  # type: ignore
    stopped = await request_stop_for_sessions(str(request.ChannelID))
    return f"Stop requested for {len(stopped)} session(s) on this channel." if stopped else "No running session to stop on this channel."


async def handle_slash(chat: Any, request: ChatRequest, model: Any, response_key: str) -> bool:
    """Dispatch a declared slash command. Returns True (and sends the terminal) if handled.

    Returns False for a non-sage command so `chat()` falls back to normal prompt handling.
    """
    sc = getattr(request, "SlashCommand", None)
    if sc is None:
        return False
    name = (getattr(sc, "Name", "") or "").lower().lstrip("/")
    arg = getattr(sc, "Argument", "") or ""
    if name == "mode":
        text = _handle_mode(model, arg)
    elif name == "state":
        text = _handle_state(model, request)
    elif name == "list":
        text = await _handle_list()
    elif name == "stop":
        text = await _handle_stop(request)
    elif name == "mcp":
        text = await _handle_mcp(arg)
    elif name == "bloodhound":
        text = await _handle_bloodhound(arg)
    else:
        return False
    await chat.send_complete(request, response_key, content=text, complete_request=True)
    return True
