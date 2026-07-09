"""Native-card HITL (Option C) for the chat container — Phase 2 scope.

Reuses sage's existing LangGraph interrupt/resume core (`model.py`): a guarded tool trips
`HumanInTheLoopMiddleware`, the graph pauses on disk under `thread_id = str(ChannelID)`, and
`handle_hitl_resume(decision, thread_id)` resumes it with `Command(resume={"decisions":[...]})`.
This module supplies only the chat-specific *surface* and *policy*, over the generic **input-request**
model introduced in `mythic-container==0.7.0rc2` (server commit `updating mcp to generic user input
requests`):

- **policy** — `should_confirm`: which tool calls need approval (static `GUARDED_TOOLS`; can grow into
  per-arg/risk rules later, sage-side, zero Mythic changes).
- **surface** — `build_approval_request` + `make_card_emitter`: emit the same normalized `input_requested`
  block as `send_approval_request`, with `complete_request=False`. That release (NOT a terminal `complete`)
  frees the channel while the graph waits on disk — so the chat handler returns None and adds no terminal.
- **decision** — `resume_decision_for_request` (Phase 2): reads the operator's explicit
  `request.InputResponse.Action`: `accept` → `"approve"`; **everything else (`reject`/`respond`/`select`)
  → `"deny"`** (default-deny). `respond`/`select` steering is Phase 3. Reject now sends a real response
  (server `Action="reject"`), so the resume is immediate — no next-message guessing.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from mythic_container.ChatBase import CHAT_INPUT_REQUESTED_SPECIAL_TYPE, ChatRequest
from mythic_container.logging import logger


def should_confirm(tool_name: str, mode: str = "supervised") -> bool:
    """True when a tool call must raise an approval request. Auto mode never arms the interrupt."""
    if mode != "supervised":
        return False
    try:
        from ai.langgraph.mythic_tools import GUARDED_TOOLS
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.mythic_tools import GUARDED_TOOLS  # type: ignore
    return tool_name in GUARDED_TOOLS


def build_approval_request(action_requests: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the `send_approval_request` kwargs (title/prompt/description/data) for a guarded tool.

    `data` is echoed back verbatim in `InputResponse.InputRequest` for reference. The single-in-flight-
    per-channel guarantee means at most one pending interrupt per channel, so `thread_id = ChannelID`
    disambiguates the resume — no correlation key needed. If several guarded calls interrupt at once, the
    card names the first and notes the count.

    Note on formatting: the Mythic input-request card renders `title`/`prompt`/`description` as PLAIN TEXT
    (not markdown — see `ChatInputRequestedEvent` in the React UI), so we format the args as a readable
    aligned key/value list rather than a markdown table or raw JSON braces. A true markdown/table approval
    card would need a Mythic UI change (render `description` via markdown), which is Cody's side.
    """
    import json
    first = action_requests[0] if action_requests else {}
    tool_name = str(first.get("name") or "guarded_tool")
    arguments = first.get("args") if isinstance(first.get("args"), dict) else {}
    count = len(action_requests)
    extra = count - 1
    suffix = f"  (+{extra} more guarded action{'s' if extra != 1 else ''} queued behind it)" if extra > 0 else ""

    # Surface a blast-radius hint if the args name a target — lets the operator judge scope at a glance.
    target = ""
    for key in ("callback", "callback_id", "callback_display_id", "host", "hostname", "computer", "target", "agent"):
        val = arguments.get(key)
        if val not in (None, "", 0):
            target = f" against {key}=`{val}`"
            break

    # Plain-text, readable arg list (one per line). Scalars inline; dict/list values compact-JSON'd.
    def _fmt(v: Any) -> str:
        if isinstance(v, (dict, list)):
            try:
                return json.dumps(v, default=str)
            except Exception:
                return str(v)
        return str(v)
    if arguments:
        args_block = "\n".join(f"  • {k}: {_fmt(v)}" for k, v in arguments.items())
    else:
        args_block = "  • (no arguments)"

    return {
        "title": f"Approve: {tool_name}",
        "prompt": (
            f"Sage wants to run the guarded action {tool_name}{target}.{suffix}\n"
            "Accept to execute · Reject to skip (Sage replans without it)."
        ),
        "description": f"Arguments\n{args_block}",
        "data": {
            "tool_name": tool_name,
            "arguments": arguments,
            "guarded_action_count": count,
        },
    }


def make_card_emitter(
    chat: Any,
    request: ChatRequest,
    delegation_lookup: Callable[[], tuple[str, str] | None] | None = None,
) -> Callable[[list[dict[str, Any]]], Awaitable[None]]:
    """Return an async `(action_requests) -> None` that raises the approval request for this turn.

    Bound per turn (like the stream emitter). This emits the same normalized `input_requested` block
    as `send_approval_request`, but uses a per-channel unique response key so repeated approvals for
    one tool append at the current timeline position. `complete_request=False` releases the channel;
    it is NOT a terminal `complete`, so the chat handler must return None.
    """
    async def _emit(action_requests: list[dict[str, Any]]) -> None:
        try:
            kwargs = build_approval_request(action_requests)
            input_request = {
                "status": "pending",
                "input_type": "approval",
                "title": kwargs["title"],
                "prompt": kwargs["prompt"],
                "description": kwargs["description"],
                "data": kwargs["data"],
            }
            normalized = chat.normalize_input_request(input_request)

            seqs = getattr(chat, "_approval_request_seqs", None)
            if seqs is None:
                seqs = {}
                setattr(chat, "_approval_request_seqs", seqs)
            channel = getattr(request, "ChannelID", 0)
            n = seqs.get(channel, 0) + 1
            seqs[channel] = n
            response_key = f"input_requested:approval:{getattr(request, 'RequestID', 0)}:{n}"

            metadata = {
                "special_type": CHAT_INPUT_REQUESTED_SPECIAL_TYPE,
                "input_requested": normalized,
            }
            if delegation_lookup is not None:
                try:
                    active = delegation_lookup()
                    if active:
                        metadata["delegation_id"] = active[0]
                        metadata["delegation_name"] = active[1]
                except Exception as e:
                    logger.debug(f"approval delegation lookup failed (non-fatal): {e}")

            content = normalized.get("prompt") or normalized.get("description") or "Input is required before continuing."
            await chat.send_complete(
                request,
                response_key,
                content=content,
                complete_request=False,
                metadata=metadata,
            )
        except Exception as e:
            logger.debug(f"approval card emit failed (non-fatal): {e}")
    return _emit


def resume_decision_for_request(request: ChatRequest) -> str:
    """Phase 2 decision from the operator's input response: `accept` → approve; everything else → deny.

    `handle_hitl_resume` classifies this string with `_hitl_is_approved` (default-deny), so mapping only
    an explicit `accept` to "approve" is exactly the safe behavior. `respond`/`select` (Phase 3 steer)
    default-deny for now.
    """
    ir = getattr(request, "InputResponse", None)
    action = str(getattr(ir, "Action", "") or "").strip().lower() if ir is not None else ""
    return "approve" if action == "accept" else "deny"
