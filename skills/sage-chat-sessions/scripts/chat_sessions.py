#!/usr/bin/env python3
"""Create and drive conversation / supervised Sage native-chat channels from the CLI.

The one-shot `native_chat.py` runner (sibling skill `sage-live-runner`) can create a supervised
channel and post its FIRST prompt, but it has no command to post a SUBSEQUENT prompt into an
existing channel, and no conversation-mode creator. A real supervised or conversation engagement is
multi-turn in one channel so the engagement ledger accumulates. This wrapper adds exactly those
missing verbs, reusing `native_chat.py`'s own Mythic primitives (no duplicated mutation logic):

  create   --mode {conversation,supervised} [--name N] [--max-steps K] [--policy-mode hybrid]
  send     --channel-id ID --prompt TEXT [--force]        # refuses if the channel is still processing
  inspect  --channel-id ID [--request-id R]               # latest (or given) request: status + messages
  list                                                    # every active Sage AI channel + its latest status

Auto mode is intentionally NOT offered here — use `native_chat.py run --autonomous` for that.
Approvals for supervised guarded actions stay in `native_chat.py approve-pending/reject-pending`
or the Mythic UI; this tool neither approves nor bypasses HITL.

Credentials resolve exactly as native_chat.py does (MYTHIC_ADMIN_PASSWORD, then MYTHIC_ENV_PATH).
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

# --- load the sibling sage-live-runner native_chat module as the single source of primitives ---
_NATIVE_CHAT = (
    Path(__file__).resolve().parents[2]
    / "sage-live-runner"
    / "scripts"
    / "native_chat.py"
)
if not _NATIVE_CHAT.is_file():
    raise SystemExit(f"cannot find native_chat.py at {_NATIVE_CHAT}")
_spec = importlib.util.spec_from_file_location("sage_native_chat_for_sessions", _NATIVE_CHAT)
nc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = nc
_spec.loader.exec_module(nc)  # type: ignore[union-attr]

# Request statuses that mean "this request is finished and the channel is free for the next turn".
# Anything else (streaming, submitted, an unknown value) is treated as still-processing so `send`
# fails safe and never double-posts into an in-flight turn.
_TERMINAL_STATUSES = {
    "complete", "completed", "error", "errored", "failed",
    "cancelled", "canceled", "halted", "stopped", "terminated",
}

_CHANNEL_CONFIG_QUERY = """
query SageChannelConfig($id: Int!) {
  chat_channel(where: {id: {_eq: $id}}, limit: 1) {
    id
    name
    locked
    archived
    ai_metadata
  }
}
"""

_ACTIVE_CHANNELS_QUERY = """
query SageActiveChannels {
  chat_channel(
    where: {
      channel_type: {_eq: "ai"}
      archived: {_eq: false}
      chat_container: {name: {_eq: "sage"}, type: {_eq: "chat"}, deleted: {_eq: false}}
    }
    order_by: {id: desc}
  ) {
    id
    name
    ai_metadata
  }
}
"""


async def _login(args: argparse.Namespace) -> Any:
    return await nc.login(
        server=args.server, user=args.user, password=args.password, env_path=args.env_path
    )


async def _latest_request(client: Any, channel_id: int) -> dict[str, Any] | None:
    result = await nc.mythic.execute_custom_query(
        client, nc.LATEST_REQUEST_QUERY, variables={"channelIds": [int(channel_id)]}
    )
    rows = result.get("chat_request") or []
    return rows[0] if rows else None


def _is_processing(request: dict[str, Any] | None) -> bool:
    if not request:
        return False
    return str(request.get("status") or "").strip().casefold() not in _TERMINAL_STATUSES


def _mode_of(ai_metadata: Any) -> str:
    config = ai_metadata.get("config") if isinstance(ai_metadata, dict) else None
    return str(config.get("mode")) if isinstance(config, dict) else "?"


def _build_metadata(mode: str, max_steps: int, policy_mode: str | None) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    config_extra: dict[str, Any] = {}
    if mode == "supervised" and policy_mode:
        config_extra["policy_mode"] = policy_mode
    if config_extra:
        extra["config"] = config_extra
    display = getattr(nc, "DEFAULT_CHANNEL_METADATA_DISPLAY", None)
    if display:
        extra["channel_metadata_display"] = {"display": display}
    return nc.canary_ai_metadata(max_steps=max_steps, session_mode=mode, extra=extra or None)


async def _cmd_create(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    mode = args.mode
    max_steps = args.max_steps if args.max_steps is not None else (200 if mode == "supervised" else 50)
    policy_mode = args.policy_mode if mode == "supervised" else None
    metadata = _build_metadata(mode, max_steps, policy_mode)
    created = await nc.create_locked_channel(
        client,
        name=args.name,
        description=f"Sage {mode} session (chat_sessions.py)",
        metadata=metadata,
    )
    # Read the SERVER-stored config back so we report the mode the kernel will actually use, not
    # what we asked for.
    readback = await nc.mythic.execute_custom_query(
        client, _CHANNEL_CONFIG_QUERY, variables={"id": int(created["chat_channel_id"])}
    )
    rows = readback.get("chat_channel") or []
    stored = rows[0].get("ai_metadata") if rows else None
    stored_mode = _mode_of(stored)
    if stored_mode != mode:
        raise RuntimeError(
            f"channel {created['chat_channel_id']} stored mode={stored_mode!r}, expected {mode!r}"
        )
    return {
        "action": "create",
        "mode": stored_mode,
        "max_steps": max_steps,
        "policy_mode": policy_mode,
        **created,
    }


async def _cmd_send(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    channel_id = int(args.channel_id)
    latest = await _latest_request(client, channel_id)
    processing = _is_processing(latest)
    if processing and not args.force:
        return {
            "action": "send",
            "posted": False,
            "reason": "channel is still processing its latest request; pass --force to post anyway",
            "channel_id": channel_id,
            "latest_request_id": (latest or {}).get("id"),
            "latest_request_status": (latest or {}).get("status"),
        }
    message = await nc.create_message(client, channel_id, args.prompt)
    return {
        "action": "send",
        "posted": True,
        "forced_over_processing": bool(processing and args.force),
        "channel_id": channel_id,
        "chat_request_id": message["chat_request_id"],
        "chat_message_id": message["chat_message_id"],
        "previous_request_id": (latest or {}).get("id"),
    }


def _short(value: Any, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


async def _cmd_inspect(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    if args.request_id is not None:
        request_id = int(args.request_id)
        channel_id = None
    else:
        channel_id = int(args.channel_id)
        latest = await _latest_request(client, channel_id)
        if not latest:
            return {"action": "inspect", "channel_id": channel_id, "requests": 0,
                    "note": "channel has no requests yet"}
        request_id = int(latest["id"])
    snapshot = await nc.fetch_request_snapshot(client, request_id)
    request = snapshot.get("request") or {}
    messages = snapshot.get("messages") or []
    compact: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        text = m.get("message") or m.get("text") or m.get("content") or ""
        row: dict[str, Any] = {
            "role": m.get("author_type") or m.get("sender_display_name"),
            "text": _short(text),
        }
        meta = m.get("metadata")
        tcs = (meta.get("tool_calls") if isinstance(meta, dict) else None) or m.get("tool_calls") or []
        if isinstance(tcs, list) and tcs:
            row["tools"] = [tc.get("name") if isinstance(tc, dict) else str(tc) for tc in tcs]
        compact.append(row)
    return {
        "action": "inspect",
        "channel_id": channel_id,
        "request_id": request_id,
        "status": request.get("status"),
        "processing": _is_processing(request),
        "error": request.get("error"),
        "message_count": len(messages),
        "messages": compact,
    }


async def _cmd_list(client: Any, _args: argparse.Namespace) -> dict[str, Any]:
    result = await nc.mythic.execute_custom_query(client, _ACTIVE_CHANNELS_QUERY)
    out: list[dict[str, Any]] = []
    for ch in result.get("chat_channel") or []:
        latest = await _latest_request(client, int(ch["id"]))
        out.append({
            "channel_id": ch.get("id"),
            "name": ch.get("name"),
            "mode": _mode_of(ch.get("ai_metadata")),
            "latest_request_id": (latest or {}).get("id"),
            "latest_status": (latest or {}).get("status"),
            "processing": _is_processing(latest),
        })
    return {"action": "list", "channels": out}


_DISPATCH = {
    "create": _cmd_create,
    "send": _cmd_send,
    "inspect": _cmd_inspect,
    "list": _cmd_list,
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default=getattr(nc, "DEFAULT_SERVER", "127.0.0.1"))
    p.add_argument("--user", default=getattr(nc, "DEFAULT_USER", "mythic_admin"))
    p.add_argument("--password", default=None)
    p.add_argument("--env-path", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("create", help="create a conversation or supervised channel")
    c.add_argument("--mode", required=True, choices=["conversation", "supervised"])
    c.add_argument("--name", default=None, help="visible channel name (auto-generated if omitted)")
    c.add_argument("--max-steps", type=int, default=None,
                   help="supervised step cap (default 200); conversation default 50")
    c.add_argument("--policy-mode", default="hybrid", help="supervised policy mode (default hybrid)")

    s = sub.add_parser("send", help="post a prompt into an existing channel")
    s.add_argument("--channel-id", type=int, required=True)
    s.add_argument("--prompt", required=True)
    s.add_argument("--force", action="store_true",
                   help="post even if the channel's latest request is still processing")

    i = sub.add_parser("inspect", help="show a channel's latest (or given) request contents")
    i.add_argument("--channel-id", type=int, default=None)
    i.add_argument("--request-id", type=int, default=None)

    sub.add_parser("list", help="list active Sage AI channels and their latest request status")
    return p


async def _main_async(args: argparse.Namespace) -> int:
    if args.command == "inspect" and args.channel_id is None and args.request_id is None:
        print("inspect requires --channel-id or --request-id", file=sys.stderr)
        return 2
    client = await _login(args)
    result = await _DISPATCH[args.command](client, args)
    print(json.dumps(result, indent=2, default=str))
    return 0


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
