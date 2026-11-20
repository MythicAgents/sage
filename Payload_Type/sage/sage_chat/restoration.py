"""Restore an existing native chat after a Sage process restart.

The LangGraph checkpoint owns the conversation; ``session.py`` owns the per-channel record that
names its generation and binding. This module holds the read-only Mythic lookups and the pure
shaping used by ``SageChat`` when a channel's Model is missing from this process:

* the stored approval card row and its Continue/Stop control, to tell a pending card from a decided one;
* the stored request rows, to prove which generation a pre-record (legacy) channel used;
* this container's requests that a restart left open, for the on-start reconciliation;
* exact task records for a receipt left by an interrupted approved action.

Nothing here grants authority. Startup never applies a decision Mythic stored on a card: it offers a
Continue/Stop control in the same request, and only the original decision's own delivery or Continue
(which re-reads and revalidates that stored decision) can claim it, once.
"""

from __future__ import annotations

import asyncio
import copy
import sqlite3
from typing import Any, Mapping

import aiosqlite
from mythic import mythic
from mythic_container.ChatBase import ChatInputResponse


_NO_TOOLS = "No tools were used for this request."
RETIRED_REFUSAL = (
    "Sage restarted or this channel's session was replaced. Its prior "
    "scope and evidence are not restored. Start a new Sage chat and "
    "state the current scope and evidence before continuing. No tools "
    "were used for this request."
)


def changed_refusal(detail: str) -> str:
    return (
        "Sage restarted, and this channel's saved session no longer matches its current "
        f"{detail}, so its prior scope and evidence are not restored. Start a new Sage chat and "
        f"state the current scope and evidence before continuing. {_NO_TOOLS}"
    )


def unreadable_refusal(detail: str) -> str:
    return (
        f"Sage restarted, but this channel's saved session could not be restored: {detail}. Its "
        "prior scope and evidence are not restored. Start a new Sage chat and state the current "
        f"scope and evidence before continuing. {_NO_TOOLS}"
    )


def unproven_refusal(detail: str) -> str:
    # With no saved record Sage cannot tell a restart from a replaced session, so the reply keeps
    # the original sentence and adds why the earlier conversation was not adopted.
    return (
        "Sage restarted or this channel's session was replaced, and this channel's earlier "
        f"conversation could not be matched with certainty ({detail}). Its prior scope and evidence "
        "are not restored. Start a new Sage chat and state the current scope and evidence before "
        f"continuing. {_NO_TOOLS}"
    )


RESTORED_NOTICE = "Sage restarted; this channel's conversation was restored from its saved session."
LEGACY_MODE_NOTICE = (
    "This chat was saved before Sage recorded `/mode` changes, so it continues in its configured "
    "`{mode}` mode. Run `/mode {previous}` again if you still want `{previous}`."
)
LEGACY_PENDING_NOTICE = (
    "The action waiting for approval before the restart was saved without its request state, so "
    "it was not restored. It was not executed and will not be resent."
)
RETIRED_CARD_CLOSURE = (
    "[Not executed: Sage restarted while this action was waiting for approval, and the pending "
    "approval could not be restored. Sage did not run it and will not resend it.]"
)
REJECTED_CLAIM_CLOSURE = (
    "[Not executed: the operator rejected this action. Sage restarted before the rejection was "
    "saved, so it is recorded here; the action will not be resent.]"
)
UNKNOWN_OUTCOME_CLOSURE = (
    "[Outcome unknown: Sage restarted after this action was approved and before its result was "
    "saved. Sage will not resend it. {detail}]"
)
CLAIM_CLOSED_NOTICE = (
    "Sage restarted while an approved action from this request was running, so this request could "
    "not finish. Sage will not resend the action. Send your next message to see what Mythic recorded "
    "for it."
)
TURN_CLOSED_NOTICE = (
    "Sage restarted while this request was being handled, so it could not finish. Nothing from it "
    "will be resent. Send your message again if it is still needed."
)


CONTINUATION_STOPPED_NOTICE = (
    "Stopped: the decision Mythic saved while Sage was restarting was discarded. The action was not "
    "run and will not be resent. Send your request again if it is still needed."
)
SAVED_DECISION_UNAVAILABLE_NOTICE = (
    "Sage could not read the decision Mythic saved while it was restarting, or it changed, so it was "
    "not applied. The action was not run and will not be resent. Send your request again if it is "
    "still needed."
)
CONTROL_UNCONFIRMED_NOTICE = (
    "Sage could not confirm whether the Continue/Stop card below was answered, so it did not apply "
    "the saved decision. Nothing ran. Answer that card to continue or stop."
)
STALE_RESPONSE_NOTICE = (
    "That answer arrived after this request's decision was already handled. No action was executed."
)
STOPPED_CLOSURE = (
    "[Not executed: the operator chose Stop for the decision saved while Sage was restarting. Sage "
    "did not run it and will not resend it.]"
)


def card_closed_notice(reason: str) -> str:
    return (
        "Sage restarted while this request was waiting for approval, and the approval could not be "
        f"asked again ({reason}). The action was not run and will not be resent. Send your request "
        "again if it is still needed."
    )


_CARD_QUERY = (
    "query sageRestoredCard($op: Int!, $ch: Int!, $ctx: jsonb!) { "
    "chat_message(where: {operation_id: {_eq: $op}, channel_id: {_eq: $ch}, "
    "metadata: {_contains: $ctx}}, limit: 2) { id channel_id operation_id metadata } }"
)
_REQUEST_QUERY = (
    "query sageRestoredRequest($op: Int!, $ch: Int!, $id: Int!) { "
    "chat_request(where: {id: {_eq: $id}, operation_id: {_eq: $op}, channel_id: {_eq: $ch}}, "
    "limit: 2) { id channel_id operation_id request_message { id message } } }"
)
_OPEN_REQUESTS_QUERY = (
    "query sageOpenRequests($op: Int!, $container: String!) { "
    "chat_request(where: {operation_id: {_eq: $op}, status: {_in: [\"pending\", \"streaming\"]}, "
    "chat_container: {name: {_eq: $container}}}, order_by: {id: asc}) { "
    "id channel_id operation_id status } }"
)
_MYTHIC_TIMEOUT_SECONDS = 5.0


def pending_record(
    context: Mapping[str, Any],
    contract: Any,
    lifecycle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Durable form of a raised card: its context without actions, the exact contract, and the
    paused request's lifecycle (``Model.pending_lifecycle_snapshot``)."""
    stored = {
        key: copy.deepcopy(value)
        for key, value in dict(context).items()
        if key != "actions"
    }
    return {
        "context": stored,
        "contract": contract.to_payload() if contract is not None else None,
        "lifecycle": copy.deepcopy(dict(lifecycle)) if lifecycle is not None else None,
    }


def restored_pending_context(
    pending: Mapping[str, Any] | None,
    action_requests: list[Any],
    thread_id: str,
) -> tuple[dict[str, Any] | None, Any, str]:
    """Rebuild the in-memory pending context from the record and the checkpoint interrupt.

    Returns ``(context, contract, reason)``; ``context`` is ``None`` with a reason on any mismatch.
    Actions come only from the checkpoint and must reproduce the digest the card carried.
    """
    try:
        from ai.langgraph.request_contract import RequestContract
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.request_contract import RequestContract  # type: ignore
    from .hitl import approval_action_digest, approval_claim_actions, approval_proposal_digest

    if not isinstance(pending, Mapping) or not isinstance(pending.get("context"), Mapping):
        return None, None, "no approval card was saved for the paused action"
    context = dict(pending["context"])
    if not action_requests:
        return None, None, "the saved checkpoint has no paused action"
    try:
        actions = approval_claim_actions(list(action_requests))
        action_digest = approval_action_digest(list(action_requests))
        contract = RequestContract.from_payload(pending.get("contract"))
    except (TypeError, ValueError) as exc:
        return None, None, f"the saved approval is unreadable ({type(exc).__name__})"
    if (
        str(context.get("thread_id") or "") != thread_id
        or str(context.get("action_digest") or "") != action_digest
        or str(context.get("request_id") or "") != contract.request_id
        or str(context.get("request_contract_digest") or "") != contract.digest
        or str(context.get("proposal_digest") or "")
        != approval_proposal_digest(contract.digest, action_digest)
    ):
        return None, None, "the saved approval does not match the paused action"
    context["actions"] = actions
    return context, contract, ""


async def read_card_row(
    client: Any,
    operation_id: int,
    channel_id: int,
    approval_id: str,
) -> dict[str, Any] | None:
    """Return the one stored card carrying this approval id in this channel, else ``None``."""
    return await _read_input_card(
        client, operation_id, channel_id, "sage_approval_context", "approval_id", approval_id
    )


async def read_control_row(
    client: Any,
    operation_id: int,
    channel_id: int,
    control_id: str,
) -> dict[str, Any] | None:
    """Return the one stored saved-decision control with this id in this channel, else ``None``."""
    return await _read_input_card(
        client, operation_id, channel_id, "sage_continuation", "control_id", control_id
    )


async def _read_input_card(
    client: Any,
    operation_id: int,
    channel_id: int,
    data_key: str,
    id_key: str,
    value: str,
) -> dict[str, Any] | None:
    if client is None or not value:
        return None
    contains = {
        "special_type": "input_requested",
        "input_requested": {"data": {data_key: {id_key: value}}},
    }
    result = await asyncio.wait_for(
        mythic.execute_custom_query(
            client,
            _CARD_QUERY,
            variables={"op": int(operation_id), "ch": int(channel_id), "ctx": contains},
        ),
        timeout=_MYTHIC_TIMEOUT_SECONDS,
    )
    rows = result.get("chat_message") if isinstance(result, Mapping) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return None
    row = dict(rows[0])
    if row.get("operation_id") != int(operation_id) or row.get("channel_id") != int(channel_id):
        return None
    return row


def input_response_from_card_row(row: Mapping[str, Any] | None) -> ChatInputResponse | None:
    """Rebuild Mythic's own input-response envelope from a resolved stored card.

    Mythic dispatches exactly this envelope (``chat_update_webhook.go``) after writing the
    decision; when Sage was down the write happened and the dispatch did not. A still-pending or
    malformed card yields ``None``.
    """
    if not isinstance(row, Mapping):
        return None
    metadata = row.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("special_type") != "input_requested":
        return None
    card = metadata.get("input_requested")
    if not isinstance(card, Mapping):
        return None
    response = card.get("response")
    if not isinstance(response, Mapping) or str(card.get("status") or "") in {"", "pending"}:
        return None
    message_id = row.get("id")
    if type(message_id) is not int or response.get("input_request_message_id") != message_id:
        return None
    action = response.get("action")
    if not isinstance(action, str):
        return None
    return ChatInputResponse(
        action=action,
        response=str(response.get("response") or ""),
        choice=dict(response.get("choice") or {}),
        input_request_message_id=message_id,
        input_request=copy.deepcopy(dict(card)),
        resolved_by_operator_id=response.get("resolved_by_operator_id") or 0,
        resolved_by=str(response.get("resolved_by") or ""),
        resolved_at=str(response.get("resolved_at") or ""),
    )


def card_row_state(row: Mapping[str, Any] | None) -> str:
    """``pending``, ``decided`` (Mythic stored an answer) or ``unreadable`` for one card row."""
    if input_response_from_card_row(row) is not None:
        return "decided"
    metadata = row.get("metadata") if isinstance(row, Mapping) else None
    card = metadata.get("input_requested") if isinstance(metadata, Mapping) else None
    if (
        isinstance(card, Mapping)
        and metadata.get("special_type") == "input_requested"
        and type(row.get("id")) is int
        and card.get("status") == "pending"
    ):
        return "pending"
    return "unreadable"


def saved_decision_digest(envelope: ChatInputResponse) -> str:
    """Identity of one stored operator decision: what was decided, by whom, on which card, when."""
    import hashlib
    import json

    payload = {
        "action": envelope.Action,
        "response": envelope.Response,
        "choice": envelope.Choice,
        "input_request_message_id": envelope.InputRequestMessageID,
        "resolved_by_operator_id": envelope.ResolvedByOperatorID,
        "resolved_at": envelope.ResolvedAt,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def control_row_choice(row: Mapping[str, Any] | None) -> str:
    """``pending``, ``continue``, ``stop`` (any other stored answer, cancel included) or ``unreadable``.

    Used only to decide what startup or the original response may do; revoking needs no proof, and
    a stored Continue that was never delivered only earns a new control, never an effect.
    """
    state = card_row_state(row)
    if state != "decided":
        return state
    envelope = input_response_from_card_row(row)
    choice = envelope.Choice if envelope is not None else None
    if envelope is not None and envelope.Action == "select" and isinstance(choice, dict) \
            and choice.get("id") == "continue":
        return "continue"
    return "stop"


async def open_container_requests(
    client: Any,
    operation_id: int,
    container_name: str,
) -> list[dict[str, Any]]:
    """This container's ``pending``/``streaming`` requests in exactly this operation."""
    if client is None or not container_name:
        return []
    result = await asyncio.wait_for(
        mythic.execute_custom_query(
            client,
            _OPEN_REQUESTS_QUERY,
            variables={"op": int(operation_id), "container": str(container_name)},
        ),
        timeout=_MYTHIC_TIMEOUT_SECONDS,
    )
    rows = result.get("chat_request") if isinstance(result, Mapping) else None
    if not isinstance(rows, list):
        return []
    return [
        dict(row) for row in rows
        if isinstance(row, Mapping)
        and type(row.get("id")) is int and row["id"] > 0
        and type(row.get("channel_id")) is int and row["channel_id"] > 0
        and row.get("operation_id") == int(operation_id)
        and row.get("status") in {"pending", "streaming"}
    ]


async def paused_action_requests(db_path: str, thread_id: str) -> list[Any]:
    """The HITL action requests paused in a generation's latest checkpoint, read without a Model.

    The parent checkpoint's pending ``__interrupt__`` writes are what ``graph.aget_state`` reports
    as the snapshot's interrupts; the same collector counts them once.
    """
    try:
        from ai.langgraph.model import _collect_hitl_action_requests
        from ai.langgraph.sqlite_checkpointer import SageAsyncSqliteSaver
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.model import _collect_hitl_action_requests  # type: ignore
        from ..ai.langgraph.sqlite_checkpointer import SageAsyncSqliteSaver  # type: ignore
    from types import SimpleNamespace

    async with aiosqlite.connect(db_path, timeout=5) as conn:
        saved = await SageAsyncSqliteSaver(conn).aget_tuple(
            {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
        )
    if saved is None:
        return []
    interrupts: list[Any] = []
    for _task_id, channel, value in saved.pending_writes or ():
        if channel == "__interrupt__":
            interrupts.extend(value if isinstance(value, (list, tuple)) else [value])
    return _collect_hitl_action_requests(SimpleNamespace(interrupts=interrupts))


async def stored_request_prompt(
    client: Any,
    operation_id: int,
    channel_id: int,
    request_id: int,
) -> str | None:
    """Return the stored prompt of one request in exactly this channel and operation."""
    if client is None:
        return None
    result = await asyncio.wait_for(
        mythic.execute_custom_query(
            client,
            _REQUEST_QUERY,
            variables={"op": int(operation_id), "ch": int(channel_id), "id": int(request_id)},
        ),
        timeout=_MYTHIC_TIMEOUT_SECONDS,
    )
    rows = result.get("chat_request") if isinstance(result, Mapping) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return None
    row = rows[0]
    if (
        row.get("id") != int(request_id)
        or row.get("operation_id") != int(operation_id)
        or row.get("channel_id") != int(channel_id)
    ):
        return None
    message = row.get("request_message")
    text = message.get("message") if isinstance(message, Mapping) else None
    return text if isinstance(text, str) else None


async def channel_generations(db_path: str, channel_id: int) -> list[str]:
    """List this channel's checkpoint generations from the runtime file, oldest name first."""
    prefix = f"{int(channel_id)}:generation:"
    try:
        async with aiosqlite.connect(db_path, timeout=5) as db:
            cursor = await db.execute(
                "SELECT DISTINCT thread_id FROM checkpoints "
                "WHERE checkpoint_ns='' AND substr(thread_id, 1, ?)=?",
                (len(prefix), prefix),
            )
            rows = await cursor.fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    generations = []
    for (thread_id,) in rows:
        suffix = str(thread_id)[len(prefix):]
        if len(suffix) == 32 and all(ch in "0123456789abcdef" for ch in suffix):
            generations.append(str(thread_id))
    return sorted(generations)


def request_number(request_id: Any, channel_id: int) -> int | None:
    """Parse ``chat:<channel>:request:<n>`` for exactly this channel."""
    prefix = f"chat:{int(channel_id)}:request:"
    text = str(request_id or "")
    if not text.startswith(prefix):
        return None
    tail = text[len(prefix):]
    if not tail.isascii() or not tail.isdigit() or tail.startswith("0"):
        return None
    return int(tail)


def operator_prompt_for_request(values: Mapping[str, Any], request_id: str) -> str | None:
    """The opening operator message the checkpoint recorded for one request id."""
    for message in values.get("supervisor_messages") or ():
        kwargs = getattr(message, "additional_kwargs", None) or {}
        if (
            kwargs.get("_request_id") == request_id
            and kwargs.get("_operator_input") is True
            and kwargs.get("_operator_input_revision", 0) == 0
        ):
            content = getattr(message, "content", None)
            return content if isinstance(content, str) else None
    return None


async def describe_receipts(mythic_client: Any, receipts: list[Any]) -> str:
    """State what exact task records show for an interrupted approved action."""
    exact = [
        receipt for receipt in receipts
        if isinstance(receipt, Mapping) and type(receipt.get("task_display_id")) is int
    ]
    if not exact:
        return (
            "No Mythic task was recorded for it, so whether it reached the callback is unknown; "
            "check the callback's task list before repeating it."
        )
    reader = getattr(mythic_client, "_step_task_records", None)
    records: Mapping[int, Any] = {}
    if callable(reader):
        try:
            records = await asyncio.wait_for(
                reader([receipt["task_display_id"] for receipt in exact]),
                timeout=_MYTHIC_TIMEOUT_SECONDS,
            )
        except Exception:
            records = {}
    lines = []
    for receipt in exact:
        task_id = receipt["task_display_id"]
        record = records.get(task_id) if isinstance(records, Mapping) else None
        if (
            isinstance(record, Mapping)
            and record.get("callback_display_id") == receipt.get("callback_display_id")
            and str(record.get("command") or "") == str(receipt.get("command") or "")
        ):
            lines.append(
                f"Mythic task {task_id} on callback {receipt.get('callback_display_id')} "
                f"(`{receipt.get('command')}`) is {record.get('status') or 'unknown'}."
            )
        else:
            lines.append(
                f"Mythic task {task_id} was issued for it, but its current record could not be "
                "read; check it in Mythic."
            )
    return " ".join(lines)
