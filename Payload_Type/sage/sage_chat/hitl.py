"""Native-card HITL (Option C) for the chat container — Phase 2 scope.

Reuses sage's existing LangGraph interrupt/resume core (`model.py`): a guarded tool trips
`HumanInTheLoopMiddleware`, the graph pauses on disk under a generation-bound channel thread id, and
`handle_hitl_resume(decision, thread_id)` resumes it with `Command(resume={"decisions":[...]})`.
This module supplies only the chat-specific *surface* and *policy*, over the generic **input-request**
model introduced in `mythic-container==0.7.0rc2` (server commit `updating mcp to generic user input
requests`):

- **policy** — `should_confirm`: which tool calls need approval (static `GUARDED_TOOLS`; can grow into
  per-arg/risk rules later, sage-side, zero Mythic changes).
- **surface** — `build_approval_request` + `make_card_emitter`: emit the same normalized `input_requested`
  block as `send_approval_request`, with `complete_request=False`. That release (NOT a terminal `complete`)
  frees the channel while the graph waits on disk — so the chat handler returns None and adds no terminal.
- **decision** — Accept approves all guarded actions in the batch; Reject denies all.
  Default-deny: malformed and unknown responses are treated as reject.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Awaitable, Callable
from uuid import uuid4

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


def _guarded_tool_names() -> set[str]:
    try:
        from ai.langgraph.mythic_tools import GUARDED_TOOLS
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.mythic_tools import GUARDED_TOOLS  # type: ignore
    names = set(GUARDED_TOOLS)
    try:
        from ai.langgraph.mcp_tool_policy import is_mcp_tool_guarded
        from ai.mcp import MCPManager
    except ImportError:  # pragma: no cover
        try:
            from ..ai.langgraph.mcp_tool_policy import is_mcp_tool_guarded  # type: ignore
            from ..ai.mcp import MCPManager  # type: ignore
        except ImportError:
            return names
    try:
        for server in MCPManager.get_connected_servers():
            for tool in MCPManager.get_tools_by_server(server):
                tool_name = getattr(tool, "name", None)
                if tool_name and is_mcp_tool_guarded(str(server), str(tool_name)):
                    names.add(str(tool_name))
    except Exception:
        pass
    return names


def _canonical_json_native(value: Any, path: str = "value") -> Any:
    try:
        from ai.langgraph.request_contract import canonical_action_arguments
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.request_contract import canonical_action_arguments  # type: ignore
    return canonical_action_arguments(value, path)


def approval_action_fingerprint(action: dict[str, Any]) -> str:
    if not isinstance(action, dict):
        raise ValueError("guarded action is not an object")
    tool_name = action.get("name")
    if not isinstance(tool_name, str) or not tool_name or tool_name != tool_name.strip():
        raise ValueError("guarded action has no exact tool name")
    if tool_name not in _guarded_tool_names():
        raise ValueError(f"guarded action names unknown tool {tool_name!r}")
    arguments = action.get("args")
    if not isinstance(arguments, dict):
        raise ValueError("guarded action arguments are not an object")
    try:
        from ai.langgraph.request_contract import action_fingerprint
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.request_contract import action_fingerprint  # type: ignore
    return action_fingerprint(
        tool_name,
        _canonical_json_native(arguments, "guarded action arguments"),
    )


def _normalized_approval_actions(action_requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and canonicalize the exact guarded batch shown to the operator.

    This is an authorization boundary, not display cleanup. Never synthesize a name/args pair for a
    malformed checkpoint: doing so can make two different invalid requests share a digest and become
    approvable. Tool-call arguments are required to be JSON values because Mythic and LangGraph persist
    them as JSON across the approval pause.
    """
    if not isinstance(action_requests, list) or not action_requests:
        raise ValueError("approval request contained no guarded actions")

    def _display_name(action: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> str:
        raw_display_name = action.get("display_name")
        display_name = raw_display_name.strip() if isinstance(raw_display_name, str) else ""
        if not display_name and tool_name == "execute_capability":
            for key in ("action", "capability"):
                candidate = arguments.get(key)
                if isinstance(candidate, dict):
                    display_name = str(candidate.get("name") or candidate.get("capability") or "").strip()
                elif isinstance(candidate, str):
                    display_name = candidate.strip()
                if display_name:
                    break
        return display_name or tool_name

    actions = []
    guarded_tools = _guarded_tool_names()
    for index, action in enumerate(action_requests):
        if not isinstance(action, dict):
            raise ValueError(f"guarded action {index + 1} is not an object")
        tool_name = action.get("name")
        if not isinstance(tool_name, str) or not tool_name or tool_name != tool_name.strip():
            raise ValueError(f"guarded action {index + 1} has no exact tool name")
        if tool_name not in guarded_tools:
            raise ValueError(f"guarded action {index + 1} names unknown tool {tool_name!r}")
        arguments = action.get("args")
        if not isinstance(arguments, dict):
            raise ValueError(f"guarded action {index + 1} arguments are not an object")
        try:
            arguments = json.loads(json.dumps(
                _canonical_json_native(arguments, f"guarded action {index + 1} arguments"),
                sort_keys=True,
                allow_nan=False,
            ))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"guarded action {index + 1} arguments are not JSON-safe") from exc
        actions.append({
            "tool_name": tool_name,
            "display_name": _display_name(action, tool_name, arguments),
            "arguments": arguments,
        })
    identities = [
        approval_action_fingerprint({
            "name": action["tool_name"],
            "args": action["arguments"],
        })
        for action in actions
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("approval request contains duplicate guarded action identities")
    return actions


def approval_action_digest(action_requests: list[dict[str, Any]]) -> str:
    """Stable digest of the exact guarded-action batch shown on an approval card."""
    _normalized_approval_actions(action_requests)
    return hashlib.sha256(
        json.dumps(
            [approval_action_fingerprint(action) for action in action_requests],
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def approval_claim_actions(action_requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical action objects persisted in the approval claim and echoed by Mythic."""
    return [
        {"name": action["tool_name"], "args": action["arguments"]}
        for action in _normalized_approval_actions(action_requests)
    ]


def approval_proposal_digest(contract_digest: str, action_digest: str) -> str:
    """Bind a model-proposed guarded batch to one immutable request contract."""
    if not isinstance(contract_digest, str) or len(contract_digest) != 64:
        raise ValueError("request contract digest must be lowercase SHA-256 hex")
    if not isinstance(action_digest, str) or len(action_digest) != 64:
        raise ValueError("action digest must be lowercase SHA-256 hex")
    if any(character not in "0123456789abcdef" for character in contract_digest + action_digest):
        raise ValueError("approval digests must be lowercase SHA-256 hex")
    return hashlib.sha256(
        json.dumps(
            {
                "action_digest": action_digest,
                "request_contract_digest": contract_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def approval_selection_digest(
    contract_digest: str,
    action_digest: str,
    approved_action_ids: list[str] | tuple[str, ...],
) -> str:
    """Bind an exact operator-selected subset to the narrowed contract revision."""
    normalized = tuple(approved_action_ids)
    if (
        not normalized
        or any(not isinstance(value, str) for value in normalized)
        or len(normalized) != len(set(normalized))
        or any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in normalized
        )
    ):
        raise ValueError("approved action IDs must be unique lowercase SHA-256 hex")
    return hashlib.sha256(
        json.dumps(
            {
                "action_digest": action_digest,
                "approved_action_ids": list(normalized),
                "request_contract_digest": contract_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_approval_request(action_requests: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the `send_approval_request` kwargs (title/prompt/description/data) for a guarded tool.

    `data` is echoed back verbatim in `InputResponse.InputRequest`. ``make_card_emitter`` augments it with
    a generation/turn/action correlation key so a delayed response to an older card cannot approve the
    current pending action. If several guarded calls interrupt at once, the card exposes a typed exact-one
    action choice rather than a batch-wide approval.

    Note on formatting: the Mythic input-request card renders `title`/`prompt`/`description` as PLAIN TEXT
    (not markdown — see `ChatInputRequestedEvent` in the React UI), so we format the args as a readable
    aligned key/value list rather than a markdown table or raw JSON braces. A true markdown/table approval
    card would need a Mythic UI change (render `description` via markdown), which is Cody's side.
    """
    actions = _normalized_approval_actions(action_requests)
    action_digest = approval_action_digest(action_requests)
    first = actions[0]
    tool_name = first["tool_name"]
    arguments = first["arguments"]
    display_name = first["display_name"]
    count = len(action_requests)

    # Plain-text, readable arg list (one per line). Scalars inline; dict/list values compact-JSON'd.
    def _fmt(v: Any) -> str:
        if isinstance(v, (dict, list)):
            try:
                return json.dumps(v, default=str)
            except Exception:
                return str(v)
        return str(v)
    description_blocks = []
    for index, action in enumerate(actions, 1):
        action_args = action["arguments"]
        args_block = (
            "\n".join(f"  • {key}: {_fmt(value)}" for key, value in action_args.items())
            if action_args
            else "  • (no arguments)"
        )
        description_blocks.append(
            f"Action {index}: {action['display_name']} (tool: {action['tool_name']})\n{args_block}"
        )

    result = {
        "title": (
            f"Approve: {display_name}"
            if count == 1
            else f"Approve {count} guarded actions"
        ),
        "prompt": (
            (
                f"Sage wants to run the guarded action {display_name}. "
                if count == 1
                else (
                    f"Sage proposed {count} guarded actions. "
                )
            )
            + "Accept approves; Reject denies."
        ),
        "description": "\n\n".join(description_blocks),
        "data": {
            "tool_name": tool_name,
            "display_name": display_name,
            "arguments": arguments,
            "guarded_action_count": count,
            "actions": actions,
            "action_digest": action_digest,
        },
    }
    if count > 1:
        result["choices"] = [
            {
                "id": approval_action_fingerprint(action_request),
                "label": action["display_name"],
                "description": f"Approve only {action['tool_name']}",
                "data": {"action_id": approval_action_fingerprint(action_request)},
            }
            for action, action_request in zip(actions, action_requests)
        ]
    return result


def make_card_emitter(
    chat: Any,
    request: ChatRequest,
    delegation_lookup: Callable[[], tuple[str, str] | None] | None = None,
    approval_context_lookup: Callable[[], dict[str, Any]] | None = None,
    approval_context_store: Callable[[dict[str, Any]], None] | None = None,
) -> Callable[[list[dict[str, Any]]], Awaitable[None]]:
    """Return an async `(action_requests) -> None` that raises the approval request for this turn.

    Bound per turn (like the stream emitter). Delegates to the SDK's `send_approval_request`, which owns
    the `input_requested` assembly and generates a unique default `response_key` (`input_requested:{uuid}`)
    — so repeated approvals never collide and each appends at the current timeline position. It sends with
    `complete_request=False` (channel-release, not a terminal `complete`), so the chat handler returns None
    while the graph waits on the operator.
    """
    async def _emit(action_requests: list[dict[str, Any]]) -> None:
        kwargs = build_approval_request(action_requests)
        approval_context: dict[str, Any] = {}
        if approval_context_lookup is not None:
            try:
                approval_context.update(approval_context_lookup() or {})
            except Exception as e:
                logger.debug(f"approval context lookup failed (non-fatal): {e}")
        approval_context.update({
            "approval_id": uuid4().hex,
            "tool_name": kwargs["data"].get("tool_name", ""),
            "actions": approval_claim_actions(action_requests),
            "selection_mode": "single",
            "action_digest": kwargs["data"]["action_digest"],
        })
        contract_digest = str(
            approval_context.get("request_contract_digest") or ""
        )
        if contract_digest:
            approval_context["proposal_digest"] = approval_proposal_digest(
                contract_digest,
                kwargs["data"]["action_digest"],
            )
        kwargs["data"]["sage_approval_context"] = approval_context
        metadata: dict[str, Any] = {}
        if delegation_lookup is not None:
            try:
                active = delegation_lookup()
                if active:
                    metadata["delegation_id"] = active[0]
                    metadata["delegation_name"] = active[1]
            except Exception as e:
                logger.debug(f"approval delegation lookup failed (non-fatal): {e}")
        # Store the resumable context only after Mythic confirms that it surfaced the card. A failed send
        # must propagate so the caller can rotate the inaccessible paused checkpoint.
        await chat.send_approval_request(
            request,
            title=kwargs["title"],
            prompt=kwargs["prompt"],
            description=kwargs["description"],
            data=kwargs["data"],
            metadata=metadata or None,
        )
        if approval_context_store is not None:
            approval_context_store(dict(approval_context))
    return _emit


def approval_response_matches(
    request: ChatRequest,
    expected_context: dict[str, Any] | None,
) -> bool:
    """Return whether an input-card response belongs to the exact pending Sage approval.

    Mythic echoes the original input request in ``InputResponse.InputRequest``. The random approval id
    prevents a delayed card from generation N from approving a different action in generation N+1;
    thread/turn/tool fields make the binding inspectable and fail closed if any layer drops context.
    """
    if not isinstance(expected_context, dict) or not expected_context:
        return False
    response = getattr(request, "InputResponse", None)
    input_request = getattr(response, "InputRequest", None) if response is not None else None
    if not isinstance(input_request, dict):
        return False
    if getattr(response, "AdditionalItems", None):
        return False
    data = input_request.get("data") or input_request.get("Data") or {}
    if not isinstance(data, dict):
        return False
    actual = data.get("sage_approval_context") or data.get("SageApprovalContext") or {}
    if not isinstance(actual, dict):
        return False
    for key in (
        "approval_id",
        "thread_id",
        "turn_id",
        "tool_name",
        "selection_mode",
        "action_digest",
        "proposal_digest",
        "request_id",
        "request_contract_digest",
        "operation_id",
        "apitoken_id",
    ):
        expected = str(expected_context.get(key) or "")
        observed = str(actual.get(key) or "")
        if not expected or observed != expected:
            return False
    expected_actions = expected_context.get("actions")
    actual_actions = actual.get("actions")
    if (
        not isinstance(expected_actions, list)
        or not expected_actions
        or not isinstance(actual_actions, list)
        or actual_actions != expected_actions
    ):
        return False
    try:
        actual_action_digest = approval_action_digest(actual_actions)
        actual_proposal_digest = approval_proposal_digest(
            str(actual.get("request_contract_digest") or ""),
            actual_action_digest,
        )
    except (TypeError, ValueError):
        return False
    if actual_action_digest != str(actual.get("action_digest") or ""):
        return False
    if actual_proposal_digest != str(actual.get("proposal_digest") or ""):
        return False

    # Validate the complete server-echoed typed request, not only its nested Sage context. Mythic resolves
    # a card by copying the original request, changing its status, and appending response/resolver fields.
    # Anything else is a malformed protocol envelope and cannot grant authority.
    try:
        card = build_approval_request(expected_actions)
    except (TypeError, ValueError):
        return False
    selection_mode = str(expected_context.get("selection_mode") or "")
    expected_input_type = "single_choice" if selection_mode == "exact_one" else "approval"
    if selection_mode not in {"single", "exact_one"}:
        return False
    expected_base: dict[str, Any] = {
        "status": "",
        "input_type": expected_input_type,
        "title": card["title"],
        "prompt": card["prompt"],
        "description": card["description"],
        # Mythic v4 persists a canonical empty catalog for approval cards and
        # echoes the stored request in ChatContainerInputResponse.InputRequest.
        "choices": card.get("choices", []),
        "data": {
            **card["data"],
            "sage_approval_context": dict(expected_context),
        },
    }

    outer_action = getattr(response, "Action", None)
    if not isinstance(outer_action, str):
        return False
    status_by_action = {
        "accept": "accepted",
        "reject": "rejected",
        "respond": "responded",
        "select": "selected",
    }
    expected_status = status_by_action.get(outer_action)
    if expected_status is None:
        return False
    expected_base["status"] = expected_status
    outer_choice = getattr(response, "Choice", None)
    if not isinstance(outer_choice, dict):
        return False
    if outer_action == "select":
        choices = expected_base.get("choices")
        if not isinstance(choices, list) or outer_choice not in choices:
            return False
    elif outer_choice:
        return False
    input_request_message_id = getattr(response, "InputRequestMessageID", 0)
    resolved_by_operator_id = getattr(response, "ResolvedByOperatorID", 0)
    resolved_by = getattr(response, "ResolvedBy", "")
    resolved_at = getattr(response, "ResolvedAt", "")
    if (
        isinstance(input_request_message_id, bool)
        or not isinstance(input_request_message_id, int)
        or input_request_message_id <= 0
        or isinstance(resolved_by_operator_id, bool)
        or not isinstance(resolved_by_operator_id, int)
        or resolved_by_operator_id <= 0
        or not isinstance(resolved_by, str)
        or not resolved_by
        or not isinstance(resolved_at, str)
        or not resolved_at
    ):
        return False

    response_payload: dict[str, Any] = {
        "action": outer_action,
        "input_request_message_id": input_request_message_id,
        "resolved_by_operator_id": resolved_by_operator_id,
        "resolved_by": resolved_by,
        "resolved_at": resolved_at,
    }
    outer_text = getattr(response, "Response", "")
    if outer_text:
        response_payload["response"] = outer_text
    if outer_action == "select":
        response_payload["choice"] = outer_choice
    expected_base.update({
        "response": response_payload,
        "resolved_by_operator_id": response_payload["resolved_by_operator_id"],
        "resolved_by": response_payload["resolved_by"],
        "resolved_at": response_payload["resolved_at"],
    })
    if input_request != expected_base:
        return False
    return True


def approved_action_ids_for_request(
    request: ChatRequest,
    expected_context: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Resolve the exact typed approval selection; every malformed shape denies all."""
    if not approval_response_matches(request, expected_context):
        return ()
    actions = expected_context.get("actions") if isinstance(expected_context, dict) else None
    if not isinstance(actions, list) or not actions:
        return ()
    try:
        action_ids = tuple(approval_action_fingerprint(action) for action in actions)
    except (TypeError, ValueError):
        return ()
    if len(action_ids) != len(set(action_ids)):
        return ()
    response = getattr(request, "InputResponse", None)
    action = getattr(response, "Action", "")
    if not isinstance(action, str):
        return ()
    selection_mode = str(expected_context.get("selection_mode") or "")
    if selection_mode == "single":
        return action_ids if action == "accept" else ()
    if selection_mode != "exact_one" or len(action_ids) < 2 or action != "select":
        return ()
    choice = getattr(response, "Choice", None)
    if not isinstance(choice, dict):
        return ()
    if set(choice) != {"id", "label", "description", "data"}:
        return ()
    selected_id = choice.get("id")
    if not isinstance(selected_id, str):
        return ()
    return (selected_id,) if selected_id in action_ids else ()


def resume_decision_for_request(request: ChatRequest) -> str:
    """Decision from the operator's input response: `accept` → approve; everything else → deny.

    `handle_hitl_resume` classifies this string with `_hitl_is_approved` (default-deny), so mapping only
    an explicit `accept` to "approve" is exactly the safe behavior. `respond`/`select` also map to deny
    HERE (the guarded action is never blind-run) — their steering text is delivered separately via
    `resume_steer_message_for_request`.
    """
    ir = getattr(request, "InputResponse", None)
    action = str(getattr(ir, "Action", "") or "").strip().lower() if ir is not None else ""
    return "approve" if action == "accept" else "deny"


def resume_steer_message_for_request(request: ChatRequest) -> str:
    """The operator's free-text steering message from a `respond`/`select` input response (Phase 3).

    On `respond`/`select`, `InputResponse.Response` carries what the operator typed or chose. We hand it
    to the agent as the guarded action's REJECTION message, so the agent replans WITH the guidance — the
    guarded action itself is never executed (steering is "deny this, but here's what to do instead", not a
    blind run). Returns "" for `accept`/`reject` and when there is no response text, so those paths keep
    the plain default-deny message.
    """
    ir = getattr(request, "InputResponse", None)
    action = str(getattr(ir, "Action", "") or "").strip().lower() if ir is not None else ""
    if action in ("respond", "select"):
        return str(getattr(ir, "Response", "") or "").strip()
    return ""
