"""Restore an existing native chat after a Sage process restart.

The LangGraph checkpoint owns the conversation; ``session.py`` owns the per-channel record that
names its generation and binding. This module holds the read-only Mythic lookups and the pure
shaping used by ``SageChat`` when a channel's Model is missing from this process:

* the stored approval card row, to recover a decision made while Sage was down;
* the stored request rows, to prove which generation a pre-record (legacy) channel used;
* exact task records for a receipt left by an interrupted approved action.

Nothing here grants authority. A recovered decision still passes ``approval_response_matches``
against the context Sage stored when it raised the card.
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
OFFLINE_DECISION_NOTICE = (
    "Sage restarted while your decision on the pending approval card was being delivered. That "
    "decision was recovered and applied here; your new message was not processed. Send it again "
    "if it is still needed."
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
_MYTHIC_TIMEOUT_SECONDS = 5.0


def pending_record(context: Mapping[str, Any], contract: Any) -> dict[str, Any]:
    """Durable form of a raised card: its context without actions, and the exact contract."""
    stored = {
        key: copy.deepcopy(value)
        for key, value in dict(context).items()
        if key != "actions"
    }
    return {
        "context": stored,
        "contract": contract.to_payload() if contract is not None else None,
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
    if client is None or not approval_id:
        return None
    contains = {
        "special_type": "input_requested",
        "input_requested": {"data": {"sage_approval_context": {"approval_id": approval_id}}},
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
