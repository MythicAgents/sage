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

try:  # match the rest of the container's logging
    from mythic_container.logging import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

# Declared to Mythic via model metadata. Names have no leading slash.
SLASH_COMMANDS = [
    ChatSlashCommandDefinition(Name="state", Description="Show/edit Sage's engagement state (hop ledger): /state, /state remove|set|objective|wipe."),
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
        f"Mode set to **{choice}** for this channel. Controller-owned objective turns use the new mode "
        "immediately; the legacy graph guarded-tool middleware is rebuilt on the next fresh session."
    )


async def _handle_state(model: Any, request: ChatRequest, arg: str = "") -> str:
    """Show or edit Sage's durable engagement state — the hop ledger of achieved effects that grounds
    the autonomous solve. Re-homed from the PayloadType `state` command; `ai.langgraph.engagement_ledger`
    is the single source of truth (the running model publishes its active engagement id there).

    Usage: `/state` (show) · `/state remove <row|id[,…]>` · `/state set <row|id> <status>` ·
    `/state objective <text>` · `/state wipe`.
    """
    try:
        from ai.langgraph import engagement_ledger
    except ImportError:  # pragma: no cover
        from ..ai.langgraph import engagement_ledger  # type: ignore

    engagement_id = engagement_ledger.active_engagement_id()
    if not engagement_id:
        # The live process hasn't frozen an engagement key yet (e.g. right after a reboot, before any
        # turn). Resolve it from Mythic now so /state works WITHOUT sending a message first — the durable
        # ledger already exists on disk; only Mythic knows which uuid is current (many historical ones).
        engagement_id = await _resolve_chat_engagement_id(model, request)
    if not engagement_id:
        return ("Couldn't resolve this channel's engagement from Mythic (no operation context yet). "
                "Send one message to start a session, then try `/state` again.")
    parts = (arg or "").strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "wipe":
        engagement_ledger.wipe(engagement_id)
        return _render_ledger_markdown(engagement_ledger.load(engagement_id), engagement_id, model, request,
                                       notice="🧹 Wiped the ledger.")
    if sub == "objective":
        if not rest:
            return "Usage: `/state objective <text>` — set the engagement objective."
        data = engagement_ledger.load(engagement_id)
        data["objective"] = rest
        data["objective_source"] = "operator"
        engagement_ledger.save(data, engagement_id)
        return _render_ledger_markdown(data, engagement_id, model, request, notice="🎯 Objective updated.")
    if sub == "remove":
        if not rest:
            return "Usage: `/state remove <row|id[,…]>` — remove hop(s) by row number (the # column) or id/effect/technique."
        data = engagement_ledger.load(engagement_id)
        selectors = [s.strip() for s in rest.split(",") if s.strip()]
        data, removed = engagement_ledger.remove_hops(data, selectors)
        engagement_ledger.save(data, engagement_id)
        return _render_ledger_markdown(data, engagement_id, model, request, notice=f"🗑️ Removed {removed} hop(s).")
    if sub == "set":
        bits = rest.split(maxsplit=1)
        if len(bits) < 2:
            return "Usage: `/state set <row|id> <status>` — change a hop's status (e.g. `/state set 9 pending`)."
        selector, status = bits[0].strip(), bits[1].strip()
        data = engagement_ledger.load(engagement_id)
        data, changed = engagement_ledger.set_hop_status(data, selector, status)
        engagement_ledger.save(data, engagement_id)
        return _render_ledger_markdown(data, engagement_id, model, request, notice=f"✏️ Set status on {changed} hop(s) → `{status}`.")
    if sub and sub != "show":
        return ("Unknown `/state` subcommand. Usage:\n"
                "- `/state` — show the engagement ledger\n"
                "- `/state remove <row|id[,…]>`\n"
                "- `/state set <row|id> <status>`\n"
                "- `/state objective <text>`\n"
                "- `/state wipe`")

    return _render_ledger_markdown(engagement_ledger.load(engagement_id), engagement_id, model, request)


async def _resolve_chat_engagement_id(model: Any, request: ChatRequest) -> str:
    """Resolve this channel's durable engagement key from Mythic WITHOUT needing a chat turn first.

    The key is `<Operation>_<id>_<uuid>`; the uuid is a per-operation durable marker only Mythic holds
    (there can be many historical ledgers for one operation), so resolution needs a Mythic client.
    Prefer the live session's already-logged-in client; otherwise build a short-lived one from the chat
    request's API token (the same MythicTools init the model uses). `_ensure_engagement_key` publishes the
    resolved key via `set_active_engagement_id`, so later `/state` calls are instant. Returns "" on failure.
    """
    try:
        from ai.langgraph import engagement_ledger
    except ImportError:  # pragma: no cover
        from ..ai.langgraph import engagement_ledger  # type: ignore

    # 1) Reuse the live session's client — cheap, already authenticated/scoped.
    client = getattr(model, "mythic_client", None) if model is not None else None
    if client is not None:
        try:
            await client._ensure_engagement_key()
            key = engagement_ledger.active_engagement_id()
            if key:
                return key
        except Exception as e:
            logger.debug(f"/state: session-client engagement resolution failed: {e}")

    # 2) No session yet (e.g. right after a reboot) — build a short-lived client from the request token.
    try:
        try:
            from ai.langgraph.mythic_tools import MythicTools
        except ImportError:  # pragma: no cover
            from ..ai.langgraph.mythic_tools import MythicTools  # type: ignore
        tools = MythicTools(
            operation_id=getattr(request, "OperationID", None),
            channel_id=getattr(request, "ChannelID", None),
            apitoken_id=getattr(request, "APITokenID", 0),
        )
        await tools.login()
        await tools._ensure_engagement_key()
        return engagement_ledger.active_engagement_id()
    except Exception as e:
        logger.debug(f"/state: short-lived-client engagement resolution failed: {e}")
    return ""


def _md_cell(value: Any, limit: int = 40) -> str:
    """Sanitize a value for a SINGLE markdown table cell.

    Ledger evidence is often a bytes-repr (`b'...\\r\\n...'`) or multi-line tool output. A raw CR/LF/tab
    in a cell makes Mythic's markdown renderer split the row into phantom lines (the "[+] Domain Controll…"
    orphan row and the ascii-art-without-a-number row). So: unwrap the bytes-repr, drop its literal escapes,
    collapse EVERY real whitespace run to one space, escape pipes, and hard-truncate.
    """
    s = str(value if value is not None else "")
    if len(s) >= 3 and s[0] == "b" and s[1] in ("'", '"') and s[-1] == s[1]:
        s = s[2:-1]                                                      # b'...' / b"..." → inner text
    s = s.replace("\\r", " ").replace("\\n", " ").replace("\\t", " ")    # literal escapes from the repr
    s = " ".join(s.split())                                             # collapse real CR/LF/tab/space runs
    s = s.replace("|", "\\|")                                            # escape the markdown column pipe
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s or "-"


def _render_ledger_markdown(data: dict, engagement_id: str, model: Any, request: ChatRequest, notice: str = "") -> str:
    """Render the engagement ledger as a Mythic-chat markdown view (+ a compact session footer)."""
    try:
        from ai.langgraph import engagement_ledger
    except ImportError:  # pragma: no cover
        from ..ai.langgraph import engagement_ledger  # type: ignore

    hops = data.get("hops") or []
    out: list[str] = []
    if notice:
        out.append(notice + "\n")
    out.append(f"**Engagement state — `{engagement_id}`**")
    objective = str(data.get("objective") or "").strip()
    if objective:
        out.append(f"\n**Objective:** {objective}")
    out.append(f"\n**Achieved hops:** {len(hops)}\n")
    if not hops:
        out.append("_(empty — no achieved hops recorded yet)_")
    else:
        out.append("| # | Hop | Effect | Status | Task | CB | Evidence |")
        out.append("|---|---|---|---|---|---|---|")
        for i, hop in enumerate(hops, 1):
            ev = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
            task_id = ev.get("mythic_task_id")
            cb_id = ev.get("callback_id")
            evidence = ev.get("result_preview") or ev.get("source") or ""
            out.append(
                f"| {i} | `{_md_cell(engagement_ledger.hop_label(hop), 48)}` | {_md_cell(hop.get('effect'))} | "
                f"`{_md_cell(hop.get('status') or '-', 16)}` | {task_id if task_id is not None else '-'} | "
                f"{cb_id if cb_id is not None else '-'} | {_md_cell(evidence, 60)} |"
            )
    out.append(
        "\n**Edit:** `/state remove <row|id[,…]>` · `/state set <row|id> <status>` · "
        "`/state objective <text>` · `/state wipe`  _(row = the # column, or a hop id/effect/technique)_"
    )
    # Compact session footer so the old /state's session info isn't lost.
    if model is not None:
        out.append(
            f"\n<sub>session: channel `{request.ChannelID}` · mode `{getattr(model, 'mode', 'supervised')}` · "
            f"`{getattr(model, 'provider', '?')}/{getattr(model, 'model', '?')}`</sub>"
        )
    return "\n".join(out)


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
        text = await _handle_state(model, request, arg)
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
