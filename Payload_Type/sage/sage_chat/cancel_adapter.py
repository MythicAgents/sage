"""Bridge SDK cancellation of a released native HITL request to its Sage session.

The SDK cancels only running chat tasks. A confirmation card releases that task while
keeping its model and request ledger in Sage, so a later cancel needs this narrow seam.
"""

from __future__ import annotations

import asyncio
import ujson

from mythic_container import ChatBase, chat_utils
from mythic_container.logging import logger

from .session import drop_channel_session, get_channel_session
from .streaming import ChatStreamEmitter


def install_paused_cancel_handler() -> None:
    """Wrap the SDK dispatch once before Mythic binds its cancel queue handler."""
    previous = chat_utils.ChatCancelHandler
    if getattr(previous, "_sage_paused_cancel_wrapper", False):
        return

    async def sage_cancel_handler(msg: bytes) -> None:
        # Preserve the SDK's task cancel / tombstone behavior for every message.
        try:
            parsed = ujson.loads(msg)
            request = ChatBase.ChatCancelRequest(**parsed)
            task = ChatBase.chatRequestTasks.get(request.RequestID)
            inactive = task is None or task.done()
            candidate = (
                inactive
                and request.ContainerName == "sage"
                and request.RequestID > 0
                and request.OperationID > 0
                and request.ChannelID > 0
            )
        except Exception:
            candidate = False
            request = None
        await previous(msg)
        if not candidate or request is None:
            return
        try:
            await _close_paused_request(request)
        except Exception:
            logger.exception("Sage paused request cancellation cleanup failed")

    sage_cancel_handler._sage_paused_cancel_wrapper = True  # type: ignore[attr-defined]
    chat_utils.ChatCancelHandler = sage_cancel_handler


async def _close_paused_request(cancel: ChatBase.ChatCancelRequest) -> None:
    # Import here to avoid service/package registration cycles at startup.
    from .service import SageChat, _CHANNEL_TURN_LOCKS
    from .session import channel_session_key

    chat = ChatBase.chatServices.get(cancel.ContainerName)
    if not isinstance(chat, SageChat):
        return
    lock = _CHANNEL_TURN_LOCKS.setdefault(channel_session_key(cancel), asyncio.Lock())
    async with lock:
        active = ChatBase.chatRequestTasks.get(cancel.RequestID)
        if active is not None and not active.done():
            return
        model = await get_channel_session(cancel)
        if model is None:
            return
        emitter = getattr(model, "_response_emitter", None)
        installed = getattr(emitter, "_request", None) if isinstance(emitter, ChatStreamEmitter) else None
        contract = getattr(model, "_request_contract", None)
        context = getattr(model, "_pending_approval_context", None)
        if not (
            installed is not None
            and installed.ContainerName == cancel.ContainerName
            and installed.OperationID == cancel.OperationID
            and installed.ChannelID == cancel.ChannelID
            and installed.RequestID == cancel.RequestID
            and str(getattr(model, "operation_id", "")) == str(cancel.OperationID)
            and str(getattr(model, "channel_id", "")) == str(cancel.ChannelID)
            and str(getattr(model, "apitoken_id", "")) == str(installed.APITokenID)
            and int(installed.APITokenID) > 0
            and getattr(model, "_hitl_card_pending", False) is True
            and isinstance(context, dict)
            and contract is not None
            and context.get("request_id") == contract.request_id
            and context.get("request_contract_digest") == contract.digest
            and context.get("operation_id") == str(cancel.OperationID)
            and context.get("apitoken_id") == str(installed.APITokenID)
        ):
            return
        # Revoke the exact outstanding card before any await; no resume can claim it.
        model._pending_approval_context = None
        model._hitl_card_pending = False
        model._controller_hitl_pending = None
        await chat._stop_and_close_request_lifecycles(
            model, status="cancelled", reason="operator",
        )
        reconciler = getattr(model, "finalize_visibility_turn", None)
        if callable(reconciler):
            result = await reconciler(require_final=True)
            if not result.get("ok", False):
                logger.error("Paused cancelled request lifecycle reconciliation failed: %s", result)
        await drop_channel_session(cancel, expected_model=model)
