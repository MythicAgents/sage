"""Slash commands for the chat container.

The server natively parses `/state`, `/list`, ... against the model's declared commands and delivers a
structured `ChatRequest.SlashCommand{Name, Argument, Raw, Source}`. Sage declares its
commands in `ChatModelMetadata.SlashCommands` (see models.py) and dispatches on `Name` here. An unknown
structured slash command terminates locally instead of being reinterpreted as a model prompt.

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

# Declared to Mythic via model metadata. Names have no leading slash. The Watcher role receives
# only its explanation/control surface; ordinary Sage keeps its existing operational commands.
_FINDINGS_COMMAND = ChatSlashCommandDefinition(
    Name="findings",
    Description="Show Sage's operation-scoped findings view: /findings.",
)
_WATCHER_COMMAND = ChatSlashCommandDefinition(
    Name="watcher",
    Description=(
        "Inspect or control the findings watcher: "
        "/watcher status|apply|scan|pause|resume|interval."
    ),
)
_STOP_COMMAND = ChatSlashCommandDefinition(
    Name="stop", Description="Cooperatively stop the running agent on this channel."
)

SAGE_SLASH_COMMANDS = [
    _FINDINGS_COMMAND,
    _WATCHER_COMMAND,
    ChatSlashCommandDefinition(
        Name="report",
        Description="Show retained identity observations from this operation engagement: /report (or /report identity).",
    ),
    ChatSlashCommandDefinition(Name="state", Description="Show or correct the shared engagement state ledger: /state, /state reconcile|remove|restore|invalidate|objective|wipe."),
    ChatSlashCommandDefinition(Name="list", Description="List active Sage chat sessions."),
    ChatSlashCommandDefinition(
        Name="tools",
        Description="List Sage's Mythic tool functions (/tools) or the binaries staged in its tools/ folder (/tools staged).",
    ),
    ChatSlashCommandDefinition(
        Name="mode",
        Description="Show or set the agent mode: /mode [conversation|supervised|auto].",
    ),
    _STOP_COMMAND,
    ChatSlashCommandDefinition(Name="mcp", Description="Manage MCP servers: /mcp list | /mcp tools [server] | /mcp call <server> <tool> <json-object> | /mcp connect <json> | /mcp disconnect <name>."),
    ChatSlashCommandDefinition(Name="bloodhound", Description="Connect the BloodHound MCP: /bloodhound [directory] | /bloodhound force [directory] to rebind an existing connection with current credentials."),
    ChatSlashCommandDefinition(Name="sandbox", Description="Run a local-only isolated snippet: /sandbox [shell|python] <code>."),
]
WATCHER_SLASH_COMMANDS = [_FINDINGS_COMMAND, _WATCHER_COMMAND, _STOP_COMMAND]
SLASH_COMMANDS = SAGE_SLASH_COMMANDS

_MCP_CALL_TIMEOUT_SECONDS = 60


def looks_like_tool_inventory_prompt(prompt: str) -> bool:
    """Accept only a complete request to describe Sage's Mythic tool surface."""
    value = " ".join(str(prompt or "").strip().casefold().split()).rstrip(" ?!.,:;")
    return value in {
        "list your tools", "list your mythic tools", "list sage tools",
        "show your tools", "show your mythic tools", "show sage tools",
        "what tools do you have", "what mythic tools do you have",
        "what tools does sage have", "what mythic tools does sage have",
        "which tools do you have", "which mythic tools do you have",
    }


def render_tool_inventory(model: Any = None) -> str:
    """Describe public shipped Mythic functions without building agents or contacting Mythic."""
    try:
        from ai.comparison_tool_surface import SHIPPING_MYTHIC_OPERATOR_TOOL_NAMES, SHIPPING_MYTHIC_PAYLOAD_TOOL_NAMES
        from ai.langgraph.mythic_tools import MythicTools, TOOL_SAFETY_READ_ONLY, tool_safety_of
        from ai.langgraph.prompt_loader import get_prompt_tools
    except ImportError:  # pragma: no cover
        from ..ai.comparison_tool_surface import SHIPPING_MYTHIC_OPERATOR_TOOL_NAMES, SHIPPING_MYTHIC_PAYLOAD_TOOL_NAMES  # type: ignore
        from ..ai.langgraph.mythic_tools import MythicTools, TOOL_SAFETY_READ_ONLY, tool_safety_of  # type: ignore
        from ..ai.langgraph.prompt_loader import get_prompt_tools  # type: ignore

    client = getattr(model, "mythic_client", None) if model is not None else None
    disabled = getattr(client, "disabled_tools", None) if client is not None else None
    mode = str(getattr(model, "mode", "") or "") if model is not None else ""
    rows = ["**Sage Mythic tools**", "", "These are Sage tool functions, not commands installed on a payload callback."]
    if model is None:
        rows.append("No active Sage session: token scope and live tool availability are unverified.")
    elif disabled is None:
        rows.append("Token scope availability has not been verified in this session.")
    rows.extend(["", "| Agent | Tool | Access | Description |", "| --- | --- | --- | --- |"])
    for agent, prompt_name, candidates in (
        ("Mythic Operator", "mythic_operator", SHIPPING_MYTHIC_OPERATOR_TOOL_NAMES),
        ("Mythic Payload", "mythic_payload", SHIPPING_MYTHIC_PAYLOAD_TOOL_NAMES),
    ):
        declared = set(get_prompt_tools(prompt_name))
        for name in candidates:
            if name not in declared:
                continue
            method = getattr(MythicTools, name, None)
            if method is None:
                continue
            safety = tool_safety_of(name, MythicTools)
            access = "read-only" if safety == TOOL_SAFETY_READ_ONLY else "guarded"
            if disabled is not None and name in disabled:
                access += "; unavailable to this token"
            elif safety != TOOL_SAFETY_READ_ONLY and mode == "conversation":
                access += "; unavailable in conversation mode"
            elif safety != TOOL_SAFETY_READ_ONLY and mode == "supervised":
                access += "; native approval required"
            description = " ".join((method.__doc__ or "").strip().split()).split(". ", 1)[0].rstrip(".")
            description = description.replace("|", "\\|")
            rows.append(f"| {agent} | `{name}` | {access} | {description} |")
    rows.extend(["", "For connected MCP tools, use `/mcp tools [server]`. "
                 "For commands installed on a payload callback, ask Sage to query that payload type."])
    return "\n".join(rows)



_IN_MYTHIC_LABELS = {
    "yes": "yes",
    "same_bytes_other_name": "yes, as {mythic_filename}",
    "different_bytes": "other bytes under this name",
    "no": "no",
    "unknown": "not checked",
}


async def render_staged_tools(model: Any = None) -> str:
    """Show the tools/ drop zone inventory without a model call; Mythic presence when a session can read it."""
    try:
        from ai.langgraph import staged_tools, ttp_library
    except ImportError:  # pragma: no cover
        from ..ai.langgraph import staged_tools, ttp_library  # type: ignore

    inventory = await asyncio.to_thread(staged_tools.scan, ttp_library.TOOLS_DIR, ttp_library.iter_ttps())
    client = getattr(model, "mythic_client", None) if model is not None else None
    rows = None
    if client is not None and hasattr(client, "_uploaded_file_rows"):
        try:
            rows = await client._uploaded_file_rows()
        except Exception:
            rows = None
    staged_tools.annotate_mythic(inventory["files"], rows)
    if not inventory["tools_dir_present"]:
        return "**Staged tools**\n\nSage's `tools/` folder does not exist, so nothing is staged."
    files = [entry for entry in inventory["files"] if "error" not in entry]
    if not files:
        return "**Staged tools**\n\nSage's `tools/` folder is empty. Drop tool binaries there for Sage to use."
    lines = [
        "**Staged tools**",
        "",
        "Binaries in Sage's `tools/` folder. Staging a file does not authorize running it.",
    ]
    if rows is None:
        lines.append("Mythic's file store was not checked in this session.")
    lines.extend(["", "| Family | File | Kind | Arch | Size | TTP | In Mythic |", "| --- | --- | --- | --- | --- | --- | --- |"])
    for entry in sorted(files, key=lambda e: (e["family"], e["filename"].casefold())):
        matches = entry.get("ttp_matches") or []
        if matches:
            total = entry.get("ttp_match_count", len(matches))
            ttp = f"`{matches[0]['ttp']}`" + (f" +{total - 1}" if total > 1 else "")
            if matches[0]["match"] != "exact":
                ttp += f" ({matches[0]['match'].replace('_', ' ')})"
        else:
            ttp = "none — ask before use"
        in_mythic = _IN_MYTHIC_LABELS.get(entry.get("in_mythic", "unknown"), "not checked").format(
            mythic_filename=entry.get("mythic_filename") or "another name"
        )
        cells = [
            entry["family"], f"`{entry['filename']}`", entry["kind"], entry["arch"] or "—",
            _format_size(entry["size"]), ttp, in_mythic,
        ]
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
    if inventory["truncated"]:
        lines.append(f"\nShowing the first {staged_tools.MAX_FILES} files.")
    errors = [entry for entry in inventory["files"] if "error" in entry]
    if errors:
        lines.append("\nUnreadable: " + ", ".join(f"`{entry['filename']}`" for entry in errors))
    return "\n".join(lines)


def _format_size(size: int) -> str:
    for unit in ("B", "KiB", "MiB"):
        if size < 1024 or unit == "MiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


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


async def _handle_report(chat: Any, request: ChatRequest, model: Any, arg: str) -> str:
    if arg.strip() not in {"", "identity"}:
        return "Usage: `/report` or `/report identity` — show this operation engagement's retained identity report."
    if model is not None and getattr(model, "mode", None) not in {"conversation", "supervised"}:
        return "`/report` is available only in conversation or supervised mode."
    return await chat.handle_identity_report(request, model)


_STATE_USAGE = (
    "Unknown `/state` subcommand. Usage:\n"
    "- `/state` — show the engagement ledger\n"
    "- `/state reconcile [<id>[,<id>…]] [apply]` — record verified effects from the operation's task history, or "
    "from the cited task IDs, `cred:<id>` and `file:<id>` entries. Verified ledger updates are saved immediately, "
    "including without `apply`. `apply` only permits importing missing credential material into Mythic's "
    "credential store; without it, those imports are previewed.\n"
    "- `/state set <#|id> achieved task=<id> type=<type> account=<account>@<realm> line=<n> after=<text>` — promote a "
    "pending account-secret row to credential material by citing where the secret is in a task's output\n"
    "- `/state remove <#|id[,…]> [--reason <text>]` — retract rows (kept, numbered, undoable)\n"
    "- `/state restore <#|id[,…]> [--reason <text>]` — undo retractions\n"
    "- `/state invalidate <#|id[,…]> [--reason <text>]` — mark rows as no longer holding\n"
    "- `/state objective <text>` — set the operation's stored compatibility objective (an operation setting, not a "
    "channel goal)\n"
    "- `/state wipe` — retract every row"
)


_SET_USAGE = (
    "`/state set` promotes one pending account-secret row to **credential material** by citing where the secret is "
    "in a Mythic task's output; it never takes the secret itself. Usage: `/state set <#|id> achieved task=<task "
    "display id> type=<plaintext|cookie|ntlm|rc4|aes128|aes256> account=<account>@<realm> line=<output line> "
    "after=<the text just before the value on that line>`. Only a task whose completion function failed after the "
    "agent answered can be promoted, and a working credential still needs its own verified proof. Otherwise hop "
    "status comes only from verified evidence: use `/state reconcile <id>` to record what a Mythic task (or "
    "`cred:<id>`) proved, `/state remove` to retract a row, or `/state invalidate` to mark it no longer holding."
)
_PROMOTION_FIELDS = ("task", "type", "account", "line")


def _parse_promotion(rest: str):
    """Parse `<#|id> achieved task=… type=… account=…@… line=… after=<text>` into (selector, task, citation), or
    return the refusal. Nothing the operator typed is echoed, so a mistyped secret never reaches the reply."""
    try:
        from ai.langgraph import credential_review
    except ImportError:  # pragma: no cover
        from ..ai.langgraph import credential_review  # type: ignore

    def refused(reason: str):
        return None, f"⚠️ {reason}.\n\n{_SET_USAGE}"

    head, sep, after = (rest or "").partition(" after=")
    tokens = head.split()
    if len(tokens) < 2 or tokens[1].casefold() != "achieved":
        return refused("Only `achieved` can be set, on one row, with a citation")
    fields: dict[str, str] = {}
    for token in tokens[2:]:
        key, eq, value = token.partition("=")
        key = key.casefold()
        if not eq or key not in _PROMOTION_FIELDS or key in fields:
            return refused("Only one each of `task=`, `type=`, `account=`, `line=` and a final `after=` are accepted")
        fields[key] = value
    if set(fields) != set(_PROMOTION_FIELDS) or not sep or not after.strip():
        return refused("The citation needs `task=`, `type=`, `account=`, `line=` and `after=`")
    account, at, realm = fields["account"].rpartition("@")
    if not fields["task"].isdigit() or not fields["line"].isdigit() or int(fields["line"]) < 1 or not (
            at and account and realm):
        return refused("`task=` and `line=` are numbers and `account=` is `<account>@<realm>`")
    citation = credential_review.Citation(fields["type"].casefold(), account, realm, int(fields["line"]),
                                          after.strip())
    return (tokens[0], int(fields["task"]), citation), ""


def _split_reason(rest: str) -> tuple[str, str]:
    head, sep, reason = (rest or "").partition("--reason")
    return head.strip(), reason.strip() if sep else ""


def _state_actor(request: ChatRequest):
    """The authenticated context of a `/state` request. Chat requests carry no operator identifier, so the actor
    id is recorded as unavailable; a name typed in the command is never treated as identity."""
    try:
        from ai.langgraph import engagement_store
    except ImportError:  # pragma: no cover
        from ..ai.langgraph import engagement_store  # type: ignore
    return engagement_store.Actor(
        kind="operator",
        name="operator (identity unavailable in chat request)",
        actor_id="",
        channel_id=str(getattr(request, "ChannelID", "") or ""),
        request_id=(f"request:{getattr(request, 'RequestID', '')};message:{getattr(request, 'RequestMessageID', '')};"
                    f"apitoken:{getattr(request, 'APITokenID', '')}"),
    )


async def _handle_state(model: Any, request: ChatRequest, arg: str = "") -> str:
    """Show or correct the operation's shared Engagement State Ledger.

    Usage: `/state` (show) · `/state reconcile [<id>[,<id>…]] [apply]` · `/state remove <#|id[,…]> [--reason …]` ·
    `/state restore <#|id[,…]>` · `/state invalidate <#|id[,…]>` · `/state objective <text>` · `/state wipe`.
    Corrections are recorded events in sage.db: rows, numbers and history are kept, and running sessions see
    them on their next ledger read.
    """
    tools = await _resolve_chat_ledger(model, request)
    if tools is None:
        return ("Couldn't resolve this channel's engagement ledger from Mythic (no operation context yet). "
                "Send one message to start a session, then try `/state` again.")
    parts = (arg or "").strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    store = tools._ledger_store()
    try:
        binding = tools._require_ledger_binding()
    except Exception as exc:
        return f"⚠️ The engagement ledger is unavailable: {exc}"
    actor = _state_actor(request)

    try:
        if sub == "wipe":
            result = store.wipe(binding, actor=actor)
            return _render_ledger_markdown(
                tools, request, model,
                notice=(f"🧹 Retracted {len(result.changed)} hop(s) and marked current graph facts as no longer "
                        "current. Nothing was deleted: rows keep their numbers and history, and `/state restore` "
                        "undoes a retraction."),
            )
        if sub == "objective":
            if not rest:
                return ("Usage: `/state objective <text>` — set the operation's stored compatibility objective "
                        "(an operation setting, not a channel goal).")
            store.update_objective(binding, rest, "operator")
            state = getattr(model, "state", None)
            if isinstance(state, dict):
                state["_pending_objective_refinement"] = None
            return _render_ledger_markdown(tools, request, model, notice=(
                "🎯 Stored compatibility objective updated (operation setting, not a channel goal)."))
        if sub in {"remove", "restore", "invalidate"}:
            selector_text, reason = _split_reason(rest)
            selectors = [item.strip() for item in selector_text.split(",") if item.strip()]
            if not selectors:
                return (f"Usage: `/state {sub} <#|id[,…]> [--reason <text>]` — select hop(s) by hop number "
                        "(the # column) or id/effect/technique.")
            if sub == "remove":
                result = store.retract(binding, selectors, reason=reason, actor=actor)
                notice = f"🗑️ Retracted {len(result.changed)} hop(s); they stay listed with their numbers."
            elif sub == "restore":
                result = store.restore(binding, selectors, reason=reason, actor=actor)
                notice = (f"↩️ Restored {len(result.changed)} hop(s). A restored row counts again only if its "
                          "proof still passes; a separate \"no longer holds\" marker stays.")
            else:
                result = store.invalidate(binding, selectors, reason=reason, actor=actor)
                notice = f"⏹️ Marked {len(result.changed)} hop(s) as no longer holding."
            unchanged = len(result.matched) - len(result.changed)
            if unchanged:
                notice += f" ({unchanged} matched row(s) already had that state.)"
            return _render_ledger_markdown(tools, request, model, notice=notice)
        if sub == "set":
            parsed, refusal = _parse_promotion(rest)
            if parsed is None:
                return refusal
            if getattr(tools, "client", None) is None:
                return ("`/state set` needs an active Mythic session — send one message on this channel first, then "
                        "retry the promotion.")
            selector, task_display_id, citation = parsed
            notice = await tools._operator_promote_material(
                selector, citation, task_display_id=task_display_id, actor=actor, command_text=rest,
            )
            return _render_ledger_markdown(tools, request, model, notice=notice)
        if sub == "reconcile":
            if getattr(tools, "client", None) is None:
                return ("`/state reconcile` needs an active Mythic session — send one message on this channel "
                        "first, then retry `/state reconcile [<id>[,<id>…]] [apply]`.")
            cited, apply, invalid = _reconcile_citations(rest)
            if invalid:
                return (f"`/state reconcile` takes Mythic task display IDs, `cred:<credential id>` or "
                        f"`file:<file id>`, separated by commas, then an optional `apply`; not "
                        f"{', '.join(f'`{item}`' for item in invalid)}.")
            notes = await tools._reconcile_operation(cited, apply=apply)
            return _render_ledger_markdown(tools, request, model, notice="\n".join(notes))
    except Exception as exc:
        return f"⚠️ The engagement ledger change was not recorded: {exc}"
    if sub and sub != "show":
        return _STATE_USAGE
    return _render_ledger_markdown(tools, request, model)


def _reconcile_citations(text: str) -> tuple[list[str], bool, list[str]]:
    """Parse `[<id>[,<id>…]] [apply]`: task display IDs, `cred:<id>` and `file:<id>`. Returns (cited, apply, invalid).

    A `cred:<id>` or `file:<id>` is operator-entered material: alone it is not proof of use, and the reply says so;
    cited together with the proving task's ID it requests verification that the task's action used that exact
    material."""
    cited: list[str] = []
    invalid: list[str] = []
    apply = False
    for token in text.replace(",", " ").split():
        lowered = token.casefold()
        if lowered == "apply":
            apply = True
        elif token.isdigit():
            cited.append(str(int(token)))
        elif lowered.startswith(("cred:", "file:")) and lowered.split(":", 1)[1].isdigit():
            prefix, _, number = lowered.partition(":")
            cited.append(f"{prefix}:{int(number)}")
        else:
            invalid.append(token)
    return list(dict.fromkeys(cited)), apply, invalid


async def _resolve_chat_ledger(model: Any, request: ChatRequest):
    """A MythicTools bound to exactly this channel's operation, or None.

    Reuse the live session's client only when it belongs to the request's operation; otherwise build a
    short-lived one from the chat request's API token. The binding comes from that client's own operation id
    and Mythic engagement marker, never a process-global key.
    """
    wanted = getattr(request, "OperationID", None)
    client = getattr(model, "mythic_client", None) if model is not None else None
    same_operation = str(getattr(client, "operation_id", None) or 0) == str(wanted or 0)
    if client is not None and hasattr(client, "_ledger_store") and same_operation:
        try:
            await client._ensure_engagement_key()
            if getattr(client, "_ledger_binding", None) is not None:
                return client
        except Exception as e:
            logger.debug(f"/state: session-client ledger binding failed: {e}")
    try:
        try:
            from ai.langgraph.mythic_tools import MythicTools
        except ImportError:  # pragma: no cover
            from ..ai.langgraph.mythic_tools import MythicTools  # type: ignore
        tools = MythicTools(
            operation_id=wanted,
            channel_id=getattr(request, "ChannelID", None),
            apitoken_id=getattr(request, "APITokenID", 0),
        )
        await tools.login()
        await tools._ensure_engagement_key()
        if getattr(tools, "_ledger_binding", None) is not None:
            return tools
    except Exception as e:
        logger.debug(f"/state: short-lived-client ledger binding failed: {e}")
    return None


def _render_ledger_markdown(tools: Any, request: ChatRequest, model: Any, notice: str = "") -> str:
    """Render the operation ledger (every row, corrections marked) from one fresh snapshot.

    The model needs no copy of this table: every Supervisor and Mythic_Operator call carries its own fresh view of
    the same rows and permanent numbers.
    """
    try:
        from ai.langgraph import engagement_ledger
    except ImportError:  # pragma: no cover
        from ..ai.langgraph import engagement_ledger  # type: ignore

    out: list[str] = []
    if notice:
        out.append(notice + "\n")
    try:
        snapshot = tools._ledger_snapshot()
    except Exception as exc:
        out.append(f"⚠️ The engagement ledger is unavailable: {exc}")
        return "\n".join(out)
    hops = snapshot.display_hops()
    objective = tools._human_engagement_objective(snapshot.objective_setting()[0])
    out.append(f"**Engagement state — `{snapshot.engagement_key}`**")
    if objective:
        out.append(f"\n**Stored compatibility objective (operation setting, not a channel goal):** {objective}")
    out.append(f"\n**Hops:** {len(hops)}\n")
    if not hops:
        out.append("_(empty — no hops recorded yet)_")
    else:
        out.extend(engagement_ledger.render_hop_table(hops))
        evidence = engagement_ledger.evidence_notes(hops)
        if evidence:
            out.append("\n**Evidence**")
            out.extend(evidence)
        notes = engagement_ledger.correction_notes(hops)
        if notes:
            out.append("\n**Corrections**")
            out.extend(notes)
    out.append(
        "\n**Edit:** `/state remove <#|id[,…]> [--reason …]` · `/state restore <#|id[,…]>` · "
        "`/state invalidate <#|id[,…]>` · `/state objective <text>` · `/state wipe`  "
        "_(# = the permanent hop number, or a hop id/effect/technique)_"
    )
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
    if sub == "policy":
        try:
            from ai.langgraph.mcp_tool_policy import effective_policy_summary, policy_path
        except ImportError:
            from ..ai.langgraph.mcp_tool_policy import effective_policy_summary, policy_path
        path = policy_path()
        summary = effective_policy_summary()
        lines_out: list[str] = []
        lines_out.append(f"**MCP Tool Policy**" + (f" (`{path}`)" if path else " (no file, all guarded)"))
        for entry in summary:
            scope = entry.get("scope", "?")
            default = entry.get("default", "guarded")
            if scope == "global":
                lines_out.append(f"\nDefault: **{default}**")
            else:
                server = scope.replace("server:", "")
                lines_out.append(f"\n**{server}** — default: {default}")
                tools = entry.get("tools", {})
                for tname, tclass in sorted(tools.items()):
                    lines_out.append(f"- `{tname}`: {tclass}")
        return "\n".join(lines_out)
    return (
        "Usage: `/mcp list` · `/mcp tools [server]` · "
        "`/mcp call <server> <tool> <json-object>` · "
        "`/mcp connect <json>` · `/mcp disconnect <name>` · `/mcp policy`"
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
        from ai.bloodhound_config import ensure_bloodhound_connected, probe_ingest_contract
    except ImportError:  # pragma: no cover
        from ..ai.bloodhound_config import ensure_bloodhound_connected, probe_ingest_contract  # type: ignore
    try:
        from .config import build_bloodhound_env
    except ImportError:  # pragma: no cover
        from config import build_bloodhound_env  # type: ignore
    force, directory = _parse_bloodhound_arg(arg)
    env = build_bloodhound_env(request) if request is not None else {}
    connected, msg = await ensure_bloodhound_connected(directory, env=env or None, force=force)
    # Connection and ingest readiness are separate facts. An already-connected server is probed too: being
    # connected says nothing about whether it can report ingest status, which is what verifies a collection.
    if not connected:
        ingest = "Unavailable until BloodHound is connected."
    else:
        contract = await probe_ingest_contract()
        ingest = (
            f"Ready. {contract.reason}"
            if contract.ready
            else f"NOT ready — Sage will refuse graph collection and ingest. {contract.operator_text()}"
        )
    return f"**Connection:** {msg}\n\n**Ingest:** {ingest}"


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
    raw = await tools.sandbox_exec(code_or_command=code, language=language)
    return _render_sandbox_result(language, raw)


async def _handle_stop(request: ChatRequest, model: Any = None) -> str:
    try:
        from ai.langgraph.controller_recovery import ControllerRecoveryStore, enabled as recovery_enabled
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.controller_recovery import ControllerRecoveryStore, enabled as recovery_enabled
    if recovery_enabled():
        ControllerRecoveryStore(request.OperationID, request.ChannelID).revoke()
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

    Returns False only when the request has no structured slash invocation. Once Mythic marks input as
    a slash command, an unknown name remains control input and must never fall through to a model turn.
    """
    sc = getattr(request, "SlashCommand", None)
    if sc is None:
        return False
    name = (getattr(sc, "Name", "") or "").lower().lstrip("/")
    arg = getattr(sc, "Argument", "") or ""
    if name == "mode":
        text = _handle_mode(model, arg)
    elif name == "report":
        text = await _handle_report(chat, request, model, arg)
    elif name in {"finding", "findings"}:
        if arg:
            text = "Usage: `/findings` — show this operation's current evidence-backed findings."
        else:
            text = await chat.handle_finding_command(request, model)
    elif name == "watcher":
        text = await chat.handle_watcher_command(request, arg)
    elif name == "state":
        text = await _handle_state(model, request, arg)
    elif name == "list":
        text = await _handle_list()
    elif name == "tools":
        sub = arg.strip().lower()
        if not sub:
            text = render_tool_inventory(model)
        elif sub == "staged":
            text = await render_staged_tools(model)
        else:
            text = "Usage: `/tools` — list Sage Mythic tool functions · `/tools staged` — list staged tool binaries."
    elif name == "stop":
        text = await _handle_stop(request, model)
    elif name == "mcp":
        text = await _handle_mcp(arg)
    elif name == "bloodhound":
        text = await _handle_bloodhound(request, arg)
    elif name == "sandbox":
        text = await _handle_sandbox(model, request, arg)
    else:
        text = (
            "Unknown Sage slash command. Available commands: `/findings`, `/watcher`, `/state`, "
            "`/report [identity]`, `/list`, `/tools`, `/mode`, `/stop`, `/mcp`, `/bloodhound`, and `/sandbox`."
        )
    await chat.send_complete(request, response_key, content=text, complete_request=True)
    return True
