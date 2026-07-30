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

import asyncio
from datetime import datetime, timezone
import json
import math
from typing import Any

from mythic_container.ChatBase import ChatRequest, ChatSlashCommandDefinition

try:  # match the rest of the container's logging
    from mythic_container.logging import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

# Declared to Mythic via model metadata. Names have no leading slash.
SLASH_COMMANDS = [
    ChatSlashCommandDefinition(Name="state", Description="Show/edit Sage's engagement state (hop ledger): /state, /state reconcile|remove|set|objective|wipe."),
    ChatSlashCommandDefinition(Name="list", Description="List active Sage chat sessions."),
    ChatSlashCommandDefinition(
        Name="mode",
        Description="Show or set the agent mode: /mode [conversation|supervised|auto].",
    ),
    ChatSlashCommandDefinition(Name="stop", Description="Cooperatively stop the running agent on this channel."),
    ChatSlashCommandDefinition(Name="mcp", Description="Manage MCP servers: /mcp list | /mcp tools [server] | /mcp call <server> <tool> <json-object> | /mcp connect <json> | /mcp disconnect <name>."),
    ChatSlashCommandDefinition(Name="bloodhound", Description="Connect the BloodHound MCP: /bloodhound [directory] | /bloodhound force [directory] to rebind an existing connection with current credentials."),
    ChatSlashCommandDefinition(Name="sandbox", Description="Run a local-only isolated snippet: /sandbox [shell|python] <code>."),
]

_MCP_CALL_TIMEOUT_SECONDS = 60


def _handle_mode(model: Any, arg: str) -> str:
    current = getattr(model, "mode", "conversation") if model is not None else "(no session)"
    choice = arg.strip().lower()
    if not choice:
        return (
            f"Current mode: **{current}**. Set with `/mode conversation`, "
            "`/mode supervised`, or `/mode auto`."
        )
    if choice not in ("conversation", "supervised", "auto"):
        return f"Unknown mode `{choice}`. Valid: `conversation`, `supervised`, `auto`."
    if model is None:
        return f"No active session yet — send a message first, then `/mode {choice}`."
    base_autonomy = getattr(model, "_chat_request_base_autonomous_solve", None)
    if base_autonomy is None:
        prior_bound = getattr(model, "_chat_mode_override_base_autonomous_solve", None)
        base_autonomy = (
            bool(prior_bound)
            if prior_bound is not None
            else bool(getattr(model, "_autonomous_solve", False))
        )
        model._chat_request_base_autonomous_solve = bool(base_autonomy)
    model.mode = choice
    model._autonomous_solve = (
        True if choice == "auto" else False if choice == "conversation" else bool(base_autonomy)
    )
    model._chat_mode_override = choice
    model._chat_mode_override_base_signature = str(
        getattr(model, "_chat_request_config_signature", "") or ""
    )
    model._chat_mode_override_base_autonomous_solve = bool(base_autonomy)
    return (
        f"Mode set to **{choice}** for this channel. Controller-owned objective turns use the new mode "
        "immediately; guarded-tool middleware is rebuilt on the next turn."
    )


async def _handle_state(model: Any, request: ChatRequest, arg: str = "") -> str:
    """Show or edit Sage's durable engagement state — the hop ledger of achieved effects that grounds
    the autonomous solve. Re-homed from the PayloadType `state` command; `ai.langgraph.engagement_ledger`
    is the single source of truth (the running model publishes its active engagement id there).

    Usage: `/state` (show) · `/state reconcile [task_id] [apply]` · `/state remove <row|id[,…]>` ·
    `/state set <row|id> <status>` · `/state objective <text>` · `/state wipe`.
    """
    try:
        from ai.langgraph import engagement_ledger
    except ImportError:  # pragma: no cover
        from ..ai.langgraph import engagement_ledger  # type: ignore

    # Resolve the exact current operation on every call. A process-global active key may belong to a
    # different operation or channel, so it is never authoritative for `/state`.
    engagement_id = await _resolve_chat_engagement_id(model, request)
    if not engagement_id:
        return ("Couldn't resolve this channel's engagement from Mythic (no operation context yet). "
                "Send one message to start a session, then try `/state` again.")
    parts = (arg or "").strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "wipe":
        engagement_ledger.wipe(engagement_id)
        return _render_ledger_markdown(engagement_ledger.load_runtime(engagement_id), engagement_id, model, request,
                                       notice="🧹 Wiped the ledger.")
    if sub == "objective":
        if not rest:
            return "Usage: `/state objective <text>` — set the engagement objective."
        data = engagement_ledger.load_runtime(engagement_id)
        data["objective"] = rest
        data["objective_source"] = "operator"
        engagement_ledger.save_runtime(data, engagement_id)
        state = getattr(model, "state", None)
        if isinstance(state, dict):
            state["_pending_objective_refinement"] = None
        return _render_ledger_markdown(data, engagement_id, model, request, notice="🎯 Objective updated.")
    if sub == "remove":
        if not rest:
            return "Usage: `/state remove <row|id[,…]>` — remove hop(s) by row number (the # column) or id/effect/technique."
        data = engagement_ledger.load_runtime(engagement_id)
        selectors = [s.strip() for s in rest.split(",") if s.strip()]
        data, removed = engagement_ledger.remove_hops(data, selectors)
        engagement_ledger.save_runtime(data, engagement_id)
        return _render_ledger_markdown(data, engagement_id, model, request, notice=f"🗑️ Removed {removed} hop(s).")
    if sub == "set":
        bits = rest.split(maxsplit=1)
        if len(bits) < 2:
            return "Usage: `/state set <row|id> <status>` — change a hop's status (e.g. `/state set 9 pending`)."
        selector, status = bits[0].strip(), bits[1].strip()
        if status.casefold() == "achieved":
            return (
                "`/state set` cannot promote a hop to `achieved`. Runtime achievements require an "
                "admissible Mythic/BloodHound proof envelope; use `/state reconcile` for verified task history."
            )
        data = engagement_ledger.load_runtime(engagement_id)
        data, changed = engagement_ledger.set_hop_status(data, selector, status)
        engagement_ledger.save_runtime(data, engagement_id)
        return _render_ledger_markdown(data, engagement_id, model, request, notice=f"✏️ Set status on {changed} hop(s) → `{status}`.")
    if sub == "reconcile":
        # Re-homed operator action: inspect completed Mythic task history, record achieved effects into
        # the ledger, and import discovered credential material. DRY-RUN unless `apply` is given (task
        # output is attacker-influenceable). `/state reconcile [task_id] [apply]`.
        tools = getattr(model, "mythic_client", None)
        client = getattr(tools, "client", None) if tools is not None else None
        if client is None:
            return ("`/state reconcile` needs an active Mythic session — send one message on this channel "
                    "first, then retry `/state reconcile [task_id] [apply]`.")
        try:
            from ai.langgraph import state_reconcile
        except ImportError:  # pragma: no cover
            from ..ai.langgraph import state_reconcile  # type: ignore
        tokens = rest.split()
        apply = any(t.lower() == "apply" for t in tokens)
        task_id = next((t for t in tokens if t.isdigit()), "")
        data = engagement_ledger.load_runtime(engagement_id)
        now = datetime.now(timezone.utc).isoformat()
        data, notes = await state_reconcile.reconcile_task_history(
            client, data, task_id, "", 25, now, apply=apply,
        )
        engagement_ledger.save_runtime(data, engagement_id)
        return _render_ledger_markdown(data, engagement_id, model, request, notice="\n".join(notes))
    if sub and sub != "show":
        return ("Unknown `/state` subcommand. Usage:\n"
                "- `/state` — show the engagement ledger\n"
                "- `/state reconcile [task_id] [apply]` — import verified effects/creds from task history (dry-run unless `apply`)\n"
                "- `/state remove <row|id[,…]>`\n"
                "- `/state set <row|id> <status>`\n"
                "- `/state objective <text>`\n"
                "- `/state wipe`")

    return _render_ledger_markdown(engagement_ledger.load_runtime(engagement_id), engagement_id, model, request)


async def _resolve_chat_engagement_id(model: Any, request: ChatRequest) -> str:
    """Resolve this channel's durable engagement key from Mythic WITHOUT needing a chat turn first.

    The key is `<Operation>_<id>_<uuid>`; the uuid is a per-operation durable marker only Mythic holds
    (there can be many historical ledgers for one operation), so resolution needs a Mythic client.
    Prefer the live session's already-logged-in client; otherwise build a short-lived one from the chat
    request's API token (the same MythicTools init the model uses). Read the resolved key back from that exact
    client instance after `_ensure_engagement_key`; the process-global published key is not operation-scoped.
    Returns "" on failure.
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
            key = str(getattr(client, "_engagement_key", "") or "").strip()
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
        return str(getattr(tools, "_engagement_key", "") or "").strip()
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
    read_only_tools = cfg.get("read_only_tools")
    if read_only_tools is not None:
        if not isinstance(read_only_tools, list) or not all(
            isinstance(item, str) and item and item == item.strip()
            for item in read_only_tools
        ):
            return (
                "`read_only_tools` must be a JSON list of exact, non-empty MCP tool names "
                "without surrounding whitespace."
            )
        read_only_tools = list(dict.fromkeys(read_only_tools))
    ctype = str(cfg.get("type", "stdio")).lower()
    try:
        if ctype == "stdio":
            conf = create_stdio_config(
                name=name, command=cfg.get("command", ""), args=cfg.get("args") or [],
                env=cfg.get("env"), cwd=cfg.get("cwd"), encoding=None,
                encoding_error_handler=None, session_kwargs=None,
                sage_execution_class=cfg.get("sage_execution_class"),
            )
        elif ctype == "sse":
            conf = create_sse_config(
                name=name,
                url=cfg.get("url", ""),
                headers=cfg.get("headers"),
                timeout=cfg.get("timeout"),
                sse_read_timeout=cfg.get("sse_read_timeout"),
                ssl_verify=cfg.get("ssl_verify", True),
                session_kwargs=cfg.get("session_kwargs"),
                sage_execution_class=cfg.get("sage_execution_class"),
            )
        elif ctype in ("http", "streamable_http", "streamable-http"):
            conf = create_streamable_http_config(
                name=name,
                url=cfg.get("url", ""),
                headers=cfg.get("headers"),
                timeout=cfg.get("timeout"),
                sse_read_timeout=cfg.get("sse_read_timeout"),
                terminate_on_close=cfg.get("terminate_on_close"),
                ssl_verify=cfg.get("ssl_verify", True),
                session_kwargs=cfg.get("session_kwargs"),
                sage_execution_class=cfg.get("sage_execution_class"),
            )
        else:
            return f"Unknown MCP type `{ctype}` — use `stdio`, `sse`, or `http`."
        if read_only_tools is not None:
            conf.extra_params = dict(conf.extra_params or {})
            conf.extra_params["read_only_tools"] = read_only_tools
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
    if sub == "call":
        return await _mcp_call(rest)
    return (
        "Usage: `/mcp list` · `/mcp tools [server]` · "
        "`/mcp call <server> <tool> <json-object>` · "
        "`/mcp connect <json>` · `/mcp disconnect <name>`"
    )


def _mcp_call_usage() -> str:
    return "Usage: `/mcp call <server> <tool> <json-object>`"


def _mcp_result_fence(result: Any) -> str:
    if isinstance(result, str):
        body = result
        language = "text"
    else:
        body = json.dumps(result, indent=2, sort_keys=True, default=str)
        language = "json"
    fence = "````" if "```" in body else "```"
    return f"{fence}{language}\n{body}\n{fence}"


def _mcp_tool_has_explicit_write_contradiction(tool: Any) -> bool:
    """Return True when server-supplied hints explicitly contradict read-only use."""
    metadata = getattr(tool, "metadata", None)
    sources: list[dict[str, Any]] = []
    if isinstance(metadata, dict):
        sources.append(metadata)
        nested = metadata.get("annotations")
        if isinstance(nested, dict):
            sources.append(nested)
    annotations = getattr(tool, "annotations", None)
    if isinstance(annotations, dict):
        sources.append(annotations)
    elif annotations is not None:
        model_dump = getattr(annotations, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict):
                sources.append(dumped)
        attr_view = {
            key: getattr(annotations, key)
            for key in ("readOnlyHint", "destructiveHint")
            if hasattr(annotations, key)
        }
        if attr_view:
            sources.append(attr_view)
    return any(
        source.get("readOnlyHint") is False
        or source.get("destructiveHint") is True
        for source in sources
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _reject_nonfinite_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _validate_finite_json_numbers(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON number overflowed to a non-finite value")
    if isinstance(value, dict):
        for item in value.values():
            _validate_finite_json_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _validate_finite_json_numbers(item)


def _strict_json_object(raw: str) -> dict[str, Any]:
    value = json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_nonfinite_json_constant,
    )
    _validate_finite_json_numbers(value)
    if not isinstance(value, dict):
        raise TypeError("argument payload must be a JSON object")
    return value


def _consume_late_mcp_task(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _mcp_call(spec: str) -> str:
    """Invoke exactly one locally-allowlisted non-BloodHound MCP tool."""
    try:
        from ai.mcp import MCPManager
    except ImportError:  # pragma: no cover
        from ..ai.mcp import MCPManager  # type: ignore

    parts = (spec or "").strip().split(maxsplit=2)
    if len(parts) != 3:
        return _mcp_call_usage()
    server_name, tool_name, raw_args = parts
    if not server_name or server_name != server_name.strip():
        return "MCP call denied: server name must be an exact non-empty name."
    if not tool_name or tool_name != tool_name.strip():
        return "MCP call denied: tool name must be an exact non-empty name."
    try:
        arguments = _strict_json_object(raw_args)
    except TypeError:
        return "`/mcp call` requires a JSON object argument payload."
    except Exception as exc:
        return f"Invalid JSON for `/mcp call`: {exc}"

    connected = list(MCPManager.get_connected_servers() or [])
    if server_name not in connected:
        return f"MCP call denied: no connected server named exactly `{server_name}`."
    if (
        server_name.casefold() == "bloodhound"
        or MCPManager.is_bloodhound_server(server_name)
    ):
        return "MCP call denied: direct `/mcp call` excludes the canonical BloodHound server."

    config = getattr(MCPManager, "configs", {}).get(server_name)
    extra = getattr(config, "extra_params", None)
    configured = extra.get("read_only_tools") if isinstance(extra, dict) else None
    if not isinstance(configured, (list, tuple, set, frozenset)):
        return (
            f"MCP call denied: server `{server_name}` has no local `read_only_tools` allowlist."
        )
    allowlisted = {
        item
        for item in configured
        if isinstance(item, str) and item and item == item.strip()
    }
    if tool_name not in allowlisted:
        return (
            f"MCP call denied: tool `{tool_name}` is not locally allowlisted for server `{server_name}`."
        )

    tools = list(MCPManager.get_tools_by_server(server_name) or [])
    malformed_names = [
        getattr(tool, "name", None)
        for tool in tools
        if not isinstance(getattr(tool, "name", None), str)
        or not getattr(tool, "name", None)
        or getattr(tool, "name", None) != getattr(tool, "name", None).strip()
    ]
    if malformed_names:
        return f"MCP call denied: server `{server_name}` exposes malformed tool names."
    tool_names = [getattr(tool, "name") for tool in tools]
    if len(tool_names) != len(set(tool_names)):
        if tool_names.count(tool_name) > 1:
            return (
                f"MCP call denied: server `{server_name}` exposes duplicate tools named `{tool_name}`."
            )
        return f"MCP call denied: server `{server_name}` exposes duplicate tool names."
    matches = [tool for tool in tools if getattr(tool, "name", None) == tool_name]
    if len(matches) != 1:
        return f"MCP call denied: server `{server_name}` has no tool named `{tool_name}`."
    if _mcp_tool_has_explicit_write_contradiction(matches[0]):
        return (
            f"MCP call denied: tool `{tool_name}` on server `{server_name}` "
            "has an explicit non-read-only annotation."
        )

    task: asyncio.Future[Any] | None = None
    try:
        task = asyncio.ensure_future(matches[0].ainvoke(arguments))
        done, _ = await asyncio.wait(
            {task},
            timeout=_MCP_CALL_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task not in done:
            task.add_done_callback(_consume_late_mcp_task)
            task.cancel()
            return (
                f"MCP call `{server_name}.{tool_name}` timed out after "
                f"{_MCP_CALL_TIMEOUT_SECONDS} seconds."
            )
        result = task.result()
    except asyncio.CancelledError:
        if task is not None and not task.done():
            task.add_done_callback(_consume_late_mcp_task)
            task.cancel()
        raise
    except Exception as exc:
        return f"MCP call `{server_name}.{tool_name}` failed: {exc}"
    return (
        f"**MCP result — `{server_name}.{tool_name}`**\n\n"
        f"{_mcp_result_fence(result)}"
    )


_BLOODHOUND_FORCE_TOKENS = frozenset({"force", "-force", "--force", "reconnect"})


def _parse_bloodhound_arg(arg: str) -> tuple[bool, str | None]:
    """Split `/bloodhound [force] [directory]` into (force, directory).

    Only the FIRST token is treated as a force flag, so a directory that happens to be named
    `force` is still reachable as `/bloodhound ./force`. Plain `/bloodhound` stays idempotent —
    it is also the cheap way to ask whether BloodHound is connected, and must not tear down a
    working session to answer that.
    """
    tokens = (arg or "").split()
    if not tokens:
        return False, None
    if tokens[0].lower() in _BLOODHOUND_FORCE_TOKENS:
        remainder = " ".join(tokens[1:]).strip()
        return True, remainder or None
    return False, " ".join(tokens).strip() or None


async def _handle_bloodhound(request: ChatRequest, arg: str) -> str:
    """Operator-facing one-shot connect.

    Takes the request so credentials resolve through the same Config → Secret → env chain the
    auto-connect path uses. Without it this command connected with no credentials at all, which
    is the exact failure an operator reaches for it to fix.
    """
    try:
        from ai.bloodhound_config import ensure_bloodhound_connected
    except ImportError:  # pragma: no cover
        from ..ai.bloodhound_config import ensure_bloodhound_connected  # type: ignore
    try:
        from .config import build_bloodhound_env
    except ImportError:  # pragma: no cover
        from config import build_bloodhound_env  # type: ignore
    force, directory = _parse_bloodhound_arg(arg)
    env = build_bloodhound_env(request) if request is not None else {}
    _connected, msg = await ensure_bloodhound_connected(directory, env=env or None, force=force)
    return msg


def _sandbox_usage() -> str:
    return (
        "Usage: `/sandbox [shell|python] <code>`\n\n"
        "Examples:\n"
        "```text\n"
        "/sandbox shell printf 'hello\\n'\n"
        "/sandbox python print(2 + 2)\n"
        "```"
    )


def _parse_sandbox_arg(arg: str) -> tuple[str, str] | None:
    text = (arg or "").strip()
    if not text:
        return None
    first, _, rest = text.partition(" ")
    mode = first.casefold()
    if mode in {"shell", "sh"}:
        return "shell", rest.strip()
    if mode in {"python", "py"}:
        return "python", rest.strip()
    return "shell", text


async def _sandbox_tools_for_request(model: Any, request: ChatRequest):
    tools = getattr(model, "mythic_client", None) if model is not None else None
    if tools is not None:
        return tools
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
    try:
        tools.apply_scope_gating(await tools.whoami_scopes())
    except Exception as e:
        logger.debug(f"/sandbox scope preflight skipped: {e}")
    return tools


def _sandbox_fence(label: str, text: str) -> str:
    body = str(text or "")
    fence = "````" if "```" in body else "```"
    return f"{fence}{label}\n{body}\n{fence}"


def _render_sandbox_result(language: str, raw: str) -> str:
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"status": "error", "error": raw}
    if not isinstance(payload, dict):
        payload = {"status": "error", "error": str(payload)}

    status = str(payload.get("status") or "error")
    if status != "ok":
        return (
            "**Sandbox result**\n\n"
            f"| Field | Value |\n|---|---|\n| Status | `{status}` |\n| Language | `{language}` |\n\n"
            f"{_sandbox_fence('text', str(payload.get('error') or 'sandbox execution failed'))}"
        )

    lines = [
        "**Sandbox result**",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Status | `{status}` |",
        f"| Language | `{language}` |",
        f"| Exit code | `{payload.get('exit_code')}` |",
        f"| Timed out | `{bool(payload.get('timed_out'))}` |",
        f"| Truncated | `{bool(payload.get('truncated'))}` |",
    ]
    lines.extend([
        "",
        _sandbox_fence("stdout", str(payload.get("stdout") or "")),
        "",
        _sandbox_fence("stderr", str(payload.get("stderr") or "")),
    ])
    return "\n".join(lines)


async def _handle_sandbox(model: Any, request: ChatRequest, arg: str) -> str:
    parsed = _parse_sandbox_arg(arg)
    if parsed is None:
        return _sandbox_usage()
    language, code = parsed
    if not code:
        return _sandbox_usage()
    tools = await _sandbox_tools_for_request(model, request)
    if "sandbox_exec" in (getattr(tools, "disabled_tools", set()) or set()):
        return "`/sandbox` is unavailable for this channel because the chat token lacks `callback.write` scope."
    raw = await tools.sandbox_exec(code_or_command=code, language=language)
    return _render_sandbox_result(language, raw)


async def _handle_stop(request: ChatRequest, model: Any = None) -> str:
    try:
        from ai.langgraph.model import request_stop_for_sessions
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.model import request_stop_for_sessions  # type: ignore
    try:
        from .session import drop_channel_session
    except ImportError:  # pragma: no cover
        from sage_chat.session import drop_channel_session  # type: ignore
    stopped = await request_stop_for_sessions(str(request.ChannelID))
    await drop_channel_session(request, expected_model=model if model is not None else None)
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
        text = await _handle_stop(request, model)
    elif name == "mcp":
        text = await _handle_mcp(arg)
    elif name == "bloodhound":
        text = await _handle_bloodhound(request, arg)
    elif name == "sandbox":
        text = await _handle_sandbox(model, request, arg)
    else:
        return False
    await chat.send_complete(request, response_key, content=text, complete_request=True)
    return True
