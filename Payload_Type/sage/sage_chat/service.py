"""``SageChat`` — the native Mythic v4.0.0 chat container fronting Sage's ``Model`` runtime.

Singleton (instantiated once at startup); all per-conversation state lives in the channel-keyed
session registry, never on ``self``. One ``chat()`` call = one Mythic request =
exactly one terminal status.

Always-terminal (the safety-critical invariant): every request path ends with a
terminal status so the channel never wedges. This is guaranteed in layers —
the handler finalizes the last visible assistant block with ``complete(complete_request=True)``;
``run_chat_turn`` provides a non-empty fallback when no assistant block was emitted and sends
``send_error(complete_request=True)`` on a handler exception. The SDK's ``ChatRequestHandler``
emits ``cancelled`` on ``CancelledError`` and ``error`` on any unhandled exception. This code's
job is to use ``run_chat_turn`` correctly and never swallow ``CancelledError``.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import copy
import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import aiosqlite
from mythic_container import ChatBase as _sdk_chat
from mythic_container.ChatBase import (
    Chat,
    ChatRequest,
    ContainerOnStartMessage,
    ContainerOnStartMessageResponse,
)
from mythic_container.logging import logger

from _version import agent_version

from .config import (
    OPENAI_ROUTE_FIELDS,
    autonomous_solve_ignored,
    build_bloodhound_env,
    build_model_kwargs,
    resolve_llm_profile,
)
from .findings_watcher import FindingsWatcherManager, render_watcher_status
from .hitl import (
    STOP_CHOICE,
    approved_action_ids_for_request,
    approval_response_matches,
    continuation_binding_of,
    continuation_card,
    continuation_choice,
    make_card_emitter,
    resume_steer_message_for_request,
)
from .metadata import build_channel_metadata
from .models import SAGE_MODELS
from .operation_memory_runtime import (
    OperationMemoryRuntime,
    assess_finding_id,
    render_assessment_markdown,
    render_findings_markdown,
)
from .operation_memory import WatcherOwnerConflict
from .restoration import (
    CLAIM_CLOSED_NOTICE,
    CONTINUATION_STOPPED_NOTICE,
    CONTROL_UNCONFIRMED_NOTICE,
    SAVED_DECISION_UNAVAILABLE_NOTICE,
    STALE_RESPONSE_NOTICE,
    STOPPED_CLOSURE,
    LEGACY_MODE_NOTICE,
    LEGACY_PENDING_NOTICE,
    MISMATCHED_CARD_CLOSURE,
    MISMATCHED_CARD_NOTICE,
    RESTORED_NOTICE,
    REJECTED_CLAIM_CLOSURE,
    RETIRED_CARD_CLOSURE,
    RETIRED_REFUSAL,
    TURN_CLOSED_NOTICE,
    UNKNOWN_OUTCOME_CLOSURE,
    card_closed_notice,
    card_row_state,
    control_row_choice,
    changed_refusal,
    channel_generations,
    describe_receipts,
    input_response_from_card_row,
    open_container_requests,
    operator_prompt_for_request,
    paused_action_requests,
    pending_record,
    read_card_row,
    read_control_row,
    request_number,
    restored_pending_context,
    saved_decision_digest,
    stored_request_prompt,
    unproven_refusal,
    unreadable_refusal,
    unrun_approved_action_error,
)
from .session import (
    CHANNEL_SESSION_ACTIVE,
    ChannelSessionRecordInvalid,
    bind_channel_thread_id,
    channel_session_db_path,
    channel_session_key,
    claim_channel_marker,
    drop_channel_session,
    get_channel_session,
    load_channel_session_record,
    next_channel_thread_id,
    put_channel_session,
    save_channel_session_record,
    update_channel_session_record,
)
from .slash import handle_slash, looks_like_tool_inventory_prompt, render_tool_inventory
from .streaming import ChatStreamEmitter
from .watcher_control import WatcherControlBoundaryError
from .watcher_graph import build_watcher_graph, render_watcher_explanation


_CHANNEL_METADATA_HEARTBEAT_SECONDS = 2.0
# The terminal request statuses Mythic stores; it rewrites any other status sent with complete=True.
_MYTHIC_TERMINAL_REQUEST_STATUSES = frozenset({"complete", "error", "cancelled"})
_CHANNEL_TURN_LOCKS: dict[str, asyncio.Lock] = {}
# Mythic may resend on-start for an operation (restarts, file sync, heartbeat gaps); one at a time.
_ONSTART_RECONCILE_LOCKS: dict[int, asyncio.Lock] = {}
_WATCHER_TURN_TASKS: dict[tuple[int, int], asyncio.Task[Any]] = {}
# A new channel can receive /help or /mode before its first Model exists. This
# process-local exception permits its first real prompt; a restart clears it.
_NEW_CHANNEL_WITHOUT_SESSION: set[tuple[int, int]] = set()


def _nonempty_native_response_text(value: Any) -> str:
    """Return the model's real terminal text when a quiet turn produced one."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text if text.strip() else ""


def _channel_has_prior_context(request: ChatRequest) -> bool:
    """Detect an existing native channel from Mythic's persisted message context.

    The current operator message may itself be present in ``Context``. Only a different
    persisted message proves that this is a continuation whose in-memory scope could
    have been lost by a Sage restart. No inference is made from globally allocated
    request/message numbers, which are shared across channels.
    """
    current_id = getattr(request, "RequestMessageID", None)
    for message in getattr(request, "Context", ()) or ():
        message_id = getattr(message, "ID", None)
        if message_id and message_id != current_id:
            return True
    return False


def provider_configuration_rejection(error: BaseException) -> Any:
    try:
        from ai.langgraph.operator_error import provider_configuration_rejection as classify
    except ImportError:  # pragma: no cover - packaged import fallback
        from ..ai.langgraph.operator_error import provider_configuration_rejection as classify  # type: ignore
    return classify(error)


def _provider_configuration_text(error: BaseException, request: ChatRequest) -> str:
    """The configuration action for a provider refusal of this chat's route, naming each setting's source."""
    try:
        from ai.langgraph.operator_error import provider_configuration_notice
    except ImportError:  # pragma: no cover - packaged import fallback
        from ..ai.langgraph.operator_error import provider_configuration_notice  # type: ignore
    try:
        profile = resolve_llm_profile(request)
    except Exception:
        profile = None
    return provider_configuration_notice(error, profile)


def _resource_budget_notice(error: BaseException) -> str:
    """Distinguish a locally refused reservation from a provider connection failure.

    OpenAI-compatible clients may wrap transport exceptions in ``APIConnectionError``.
    Walk only the exception chain and match the concrete local budget class; arbitrary
    error text mentioning a budget is not evidence of a local refusal.
    """
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if (
            type(current).__name__ == "ResourceBudgetExceeded"
            and type(current).__module__.endswith("resource_budget")
        ):
            return "Sage's configured resource budget stopped this request."
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return ""


def _with_task_provenance(model: Any, content: str) -> str:
    """Compose one native terminal narrative with exact request/task lineage."""
    provenance_getter = getattr(
        model,
        "current_request_task_provenance_notice",
        None,
    )
    provenance_notice = (
        str(provenance_getter() or "").strip()
        if callable(provenance_getter)
        else ""
    )
    if provenance_notice and provenance_notice not in content:
        return f"{content.rstrip()}\n\n{provenance_notice}"
    return content


def _model_config_signature(
    kwargs: dict[str, Any],
    bloodhound_env: dict[str, str] | None = None,
) -> str:
    """Bind a reusable session to the exact resolved ChatRequest constructor config.

    ``bloodhound_env`` is folded in even though it is not a ``Model`` constructor kwarg. It has to
    be: ``Model.initialize()`` wires the BloodHound agent's tools from the MCP servers connected at
    that moment, so a session whose graph already resolved its tool list cannot pick up a later
    connection. Changing BloodHound configuration must therefore rotate the session, not mutate it.

    Folded in only when non-empty, so a request with no BloodHound configuration hashes exactly as
    it did before this parameter existed — existing sessions do not churn on upgrade. The result is
    a SHA-256 digest, so no credential value is retained.
    """
    payload: dict[str, Any] = dict(kwargs)
    if bloodhound_env:
        payload["__bloodhound__"] = dict(sorted(bloodhound_env.items()))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _core_config_signature(
    kwargs: dict[str, Any],
    bloodhound_env: dict[str, str] | None = None,
) -> str:
    """The session signature without the OpenAI route settings (API, Daybreak, safety identifier).

    Two configurations with equal core signatures differ only in how this chat reaches the same model, so an
    idle channel may apply the change on its existing conversation generation. Everything else (provider,
    model, endpoint, key, mode, BloodHound) still rotates the session.
    """
    payload = dict(kwargs)
    config = dict(payload.get("config") or {})
    configurable = {
        key: value
        for key, value in dict(config.get("configurable") or {}).items()
        if key not in OPENAI_ROUTE_FIELDS
    }
    payload["config"] = {**config, "configurable": configurable}
    return _model_config_signature(payload, bloodhound_env)


def _bloodhound_env_or_empty(request: ChatRequest) -> dict[str, str]:
    try:
        return build_bloodhound_env(request)
    except Exception as exc:  # pragma: no cover - resolution must never block a chat turn
        logger.debug(f"BloodHound credential resolution skipped for signature: {exc}")
        return {}


def _claim_outstanding(record: dict[str, Any]) -> bool:
    """An approval decision claimed but not yet known to have finished (a consumed claim is history)."""
    claim = record.get("claim")
    return isinstance(claim, dict) and claim.get("status") != "consumed"


SETTINGS_CONTINUED_NOTICE = (
    "Applied this chat's updated OpenAI settings (OpenAI API, Daybreak or Safety Identifier). The "
    "conversation continues with its earlier messages; the model, provider and endpoint are unchanged."
)


def _runtime_routing_matches(
    model: Any,
    kwargs: dict[str, Any],
    *,
    config_signature: str = "",
) -> bool:
    """Detect routing drift while preserving an override bound to unchanged base config."""
    resolved_signature = config_signature or _model_config_signature(kwargs)
    stored_signature = str(getattr(model, "_chat_request_config_signature", "") or "")
    override_mode = str(getattr(model, "_chat_mode_override", "") or "")
    override_signature = str(
        getattr(model, "_chat_mode_override_base_signature", "") or ""
    )
    expected_mode = str(kwargs.get("mode", ""))
    expected_autonomy = bool(kwargs.get("autonomous_solve"))
    base_autonomy = bool(
        getattr(model, "_chat_request_base_autonomous_solve", expected_autonomy)
    )
    override_base_autonomy = getattr(
        model,
        "_chat_mode_override_base_autonomous_solve",
        None,
    )
    if (
        override_mode in {"conversation", "supervised", "auto"}
        and stored_signature
        and stored_signature == resolved_signature == override_signature
        and override_base_autonomy is not None
        and bool(override_base_autonomy) == expected_autonomy
        and base_autonomy == expected_autonomy
    ):
        expected_mode = override_mode
        expected_autonomy = (
            True
            if override_mode == "auto"
            else False
            if override_mode == "conversation"
            else bool(override_base_autonomy)
        )
    return bool(
        str(getattr(model, "mode", "")) == expected_mode
        and bool(getattr(model, "_autonomous_solve", False))
        == expected_autonomy
        and str(getattr(model, "policy_mode", "")) == str(kwargs.get("policy_mode", ""))
        and int(getattr(model, "_max_steps", -1)) == int(kwargs.get("max_steps", -2))
    )


def _build_request_contract(
    request: ChatRequest,
    model: Any,
    *,
    has_input_response: bool,
):
    """Compile the request contract from typed native transport/session fields only."""
    try:
        from ai.langgraph.request_contract import (
            RequestContract,
            RequestIntent,
            build_request_contract,
        )
    except ImportError:  # pragma: no cover
        from ..ai.langgraph.request_contract import (  # type: ignore
            RequestContract,
            RequestIntent,
            build_request_contract,
        )
    pending_context = getattr(model, "_pending_approval_context", None)
    active_contract = getattr(model, "_request_contract", None)
    if (
        has_input_response
        and isinstance(active_contract, RequestContract)
        and isinstance(pending_context, dict)
        and str(pending_context.get("request_id") or "") == active_contract.request_id
        and str(pending_context.get("request_contract_digest") or "")
        == active_contract.digest
    ):
        # Approval is a typed transition of the already-paused request. Reinstall the same immutable
        # contract so the resumed action reaches the sink under the digest the operator reviewed.
        return active_contract
    parent_request_id = (
        str(pending_context.get("request_id") or "")
        if isinstance(pending_context, dict)
        else ""
    )
    return build_request_contract(
        request_id=f"chat:{request.ChannelID}:request:{request.RequestID}",
        channel_id=str(request.ChannelID),
        operation_id=str(request.OperationID),
        mode=str(getattr(model, "mode", "conversation") or "conversation"),
        autonomous_solve=bool(getattr(model, "_autonomous_solve", False)),
        intent=RequestIntent.CONTINUE if has_input_response else None,
        parent_request_id=parent_request_id,
    )


def _unrun_approved_tools(model: Any, event_ids: tuple[str, ...], call_ids: set[str]) -> list[str]:
    """Tool names of approved calls whose tool event, open at the claim, is still open."""
    ledger = getattr(model, "_request_event_ledger", None)
    open_now = set(model._open_tool_lifecycle_ids())
    names: list[str] = []
    for event_id in event_ids:
        if event_id not in open_now:
            continue
        opened = ledger.actual_events(event_id=event_id, kind="tool")
        metadata = dict(opened[0].metadata) if opened else {}
        if metadata.get("tool_call_id") in call_ids:
            names.append(str(metadata.get("tool_name") or "unknown_tool"))
    return names


def _bloodhound_unavailable_message(reason: str | None = None) -> str:
    """Compose the operator-facing autonomous refusal, with a fallback that still names BloodHound.

    Lazy import to match this module's convention of keeping `ai.*` off the import path. The
    fallback matters: if the import fails, the refusal must still say what is wrong rather than
    reverting to an internal phrase like "exact-tool admission", which would be a defect.
    """
    try:
        from ai.bloodhound_config import autonomous_unavailable_message
    except ImportError:  # pragma: no cover
        try:
            from ..ai.bloodhound_config import autonomous_unavailable_message  # type: ignore
        except ImportError:
            detail = f" {reason.strip()}" if reason and reason.strip() else ""
            return (
                "Autonomous execution is unavailable because BloodHound is not connected."
                f"{detail} Connect BloodHound and start a new chat."
            )
    return autonomous_unavailable_message(reason)


class SageChat(Chat):
    name = "sage"
    description = "Sage — AI red-team operator assistant (native Mythic v4.0.0 chat container)."
    # Mythic shows semver as the container's own version, so it is Sage's release version, not Mythic's.
    semver = agent_version()
    # rc5: the chat container's service icon (light + dark). Only sage.svg exists today, so it serves both
    # until a dedicated dark variant lands. Path is resolved from this module (…/sage_chat/) up to the sage root.
    agent_icon_path = str(Path(__file__).resolve().parent.parent / "sage.svg")
    dark_mode_agent_icon_path = str(Path(__file__).resolve().parent.parent / "sage.svg")
    models = SAGE_MODELS

    def _findings_watcher(self) -> FindingsWatcherManager:
        watcher = getattr(self, "_findings_watcher_manager", None)
        if watcher is None:
            watcher = FindingsWatcherManager(
                runtime=getattr(self, "_operation_memory_runtime", None)
            )
            self._findings_watcher_manager = watcher
        return watcher

    def _operation_memory(self) -> OperationMemoryRuntime:
        runtime = getattr(self, "_operation_memory_runtime", None)
        return runtime if runtime is not None else self._findings_watcher().runtime

    async def _retain_report_collection(self, request: ChatRequest, model: Any) -> None:
        """Admit only this native turn's paired task reads after checkpoint completion."""
        if (getattr(model, "mode", None) not in {"conversation", "supervised"}
                or not getattr(model, "_thread_id_override", None)):
            return
        getter = getattr(getattr(model, "graph", None), "aget_state", None)
        collector = getattr(model, "native_report_collection", None)
        if not callable(getter) or not callable(collector):
            return
        identity = f"chat:{request.ChannelID}:request:{request.RequestID}"
        try:
            snapshot = await getter(model._graph_run_config(model._session_thread_id()))
            values = getattr(snapshot, "values", None)
            if not isinstance(values, dict):
                return
            records = collector(values, identity)
            if not records:
                return
            client = getattr(getattr(model, "mythic_client", None), "client", None)
            from .report_identity import canonical_engagement_uuid
            from .report_observations import admit_observations
            engagement = await canonical_engagement_uuid(client, request.OperationID)
            if engagement and engagement == getattr(model, "_report_engagement_at_turn", None):
                await admit_observations(
                    self._operation_memory().store, request.OperationID, engagement,
                    channel_id=request.ChannelID, request_id=identity,
                    checkpoint_thread=model._session_thread_id(), records=records)
        except Exception as exc:
            logger.warning("Retained report collection unavailable (%s)", type(exc).__name__)

    async def handle_identity_report(self, request: ChatRequest, model: Any | None) -> str:
        """Read the exact authenticated operation marker, then local retained sources."""
        unavailable = (
            "Retained task observations are unavailable. This report cannot establish identity or access."
        )
        mode = getattr(model, "mode", None)
        if model is None:
            try:
                mode = build_model_kwargs(request)["mode"]
            except Exception:
                return unavailable
        if mode not in {"conversation", "supervised"}:
            return "`/report identity` is available only in conversation or supervised mode."
        try:
            client = getattr(getattr(model, "mythic_client", None), "client", None)
            if client is None:
                from ai.langgraph.mythic_tools import MythicTools
                auth = MythicTools(operation_id=request.OperationID,
                    channel_id=request.ChannelID, apitoken_id=request.APITokenID)
                await auth.login()
                client = auth.client
            from .report_identity import canonical_engagement_uuid
            from .report_observations import retained_observations
            engagement = await canonical_engagement_uuid(client, request.OperationID)
            if not engagement:
                return unavailable
            if model is not None and engagement != getattr(model, "_report_engagement_uuid", None):
                return unavailable
            records, omitted, capacity_reached = await retained_observations(
                self._operation_memory().store, request.OperationID, engagement)
            from ai.langgraph.assisted_reporting import render_identity_report
            return render_identity_report(records, operation_scope=(request.OperationID, engagement),
                                          omitted_sources=omitted, capacity_reached=capacity_reached)
        except Exception as exc:
            logger.warning("Identity report unavailable (%s)", type(exc).__name__)
            return unavailable

    async def on_container_start(
        self, message: ContainerOnStartMessage
    ) -> ContainerOnStartMessageResponse:
        watcher = self._findings_watcher()
        watcher_error: Exception | None = None
        try:
            await watcher.restore_operation(
                message.OperationID,
                server_name=message.ServerName,
                bootstrap_token=message.APIToken,
            )
        except Exception as exc:
            watcher_error = exc
        # A restart leaves this container's open requests with no owner, and Mythic refuses every
        # new prompt in their channels until they end. Never raises: the SDK requeues on an error.
        try:
            await self._reconcile_open_requests(message)
        except Exception as exc:
            logger.warning("Open-request reconciliation failed (%s)", type(exc).__name__)
        if watcher_error is not None:
            return ContainerOnStartMessageResponse(
                ContainerName=self.name,
                EventLogErrorMessage=(
                    "Sage findings watcher startup failed before scheduling: "
                    f"{type(watcher_error).__name__}."
                ),
            )
        return ContainerOnStartMessageResponse(
            ContainerName=self.name,
            EventLogInfoMessage=(
                "Sage Watcher startup profile reconciliation completed for operation "
                f"{message.OperationID}; use /watcher status to inspect the exact state."
            ),
        )

    async def _onstart_client(self, message: ContainerOnStartMessage) -> Any:
        """Mythic client for the on-start token, through the Watcher's existing login owner."""
        return await self._findings_watcher().control_plane.login_onstart(
            str(message.APIToken or ""), server_name=str(message.ServerName or "")
        )

    async def _reconcile_open_requests(self, message: ContainerOnStartMessage) -> None:
        """End, or offer the saved decision of, each request a restart left without an owner."""
        operation_id = int(message.OperationID)
        if operation_id <= 0:
            return
        lock = _ONSTART_RECONCILE_LOCKS.setdefault(operation_id, asyncio.Lock())
        async with lock:
            client = await self._onstart_client(message)
            rows = await open_container_requests(client, operation_id, self.name)
            for row in rows:
                try:
                    outcome = await self._reconcile_open_request(
                        client, operation_id, int(row["channel_id"]), int(row["id"])
                    )
                except Exception as exc:
                    outcome = f"failed ({type(exc).__name__})"
                logger.info(
                    "On-start reconciliation: operation %s channel %s request %s: %s",
                    operation_id, row["channel_id"], row["id"], outcome,
                )

    @staticmethod
    def _request_task_live(request_id: int) -> bool:
        task = _sdk_chat.chatRequestTasks.get(int(request_id))
        return task is not None and not task.done()

    async def _reconcile_open_request(
        self,
        client: Any,
        operation_id: int,
        channel_id: int,
        request_id: int,
    ) -> str:
        target = ChatRequest(
            container_name=self.name,
            operation_id=operation_id,
            channel_id=channel_id,
            request_id=request_id,
        )
        # Fencing: never touch a request this process is serving.
        if self._request_task_live(request_id):
            return "fenced: request task live"
        lock = _CHANNEL_TURN_LOCKS.setdefault(channel_session_key(target), asyncio.Lock())
        if lock.locked():
            return "fenced: channel turn in progress"
        async with lock:
            if self._request_task_live(request_id):
                return "fenced: request task live"
            if await get_channel_session(target) is not None:
                return "fenced: channel session registered"
            request_key = f"chat:{channel_id}:request:{request_id}"
            try:
                record = await load_channel_session_record(operation_id, channel_id)
            except Exception:
                record = None
            if isinstance(record, dict) and record.get("status") == CHANNEL_SESSION_ACTIVE:
                claim = record.get("claim")
                if (
                    isinstance(claim, dict)
                    and claim.get("status") == "claimed"
                    and claim.get("request_id") == request_key
                ):
                    # The next prompt's restore reports the exact task status; never resumed.
                    await self._close_open_request(target, CLAIM_CLOSED_NOTICE)
                    return "closed: unconsumed claim"
                pending = record.get("pending")
                context = pending.get("context") if isinstance(pending, dict) else None
                if isinstance(context, dict) and context.get("request_id") == request_key:
                    return await self._offer_or_close(client, target, record, context)
            await self._close_open_request(target, TURN_CLOSED_NOTICE)
            return "closed: no card"

    _CONTINUATION_BINDING_KEYS = (
        "control_id", "request_id", "native_request_id", "thread_id",
        "source_approval_id", "source_message_id", "decision_digest", "operation_id",
    )

    @classmethod
    def _continuation_binding(cls, continuation: dict[str, Any]) -> dict[str, str]:
        return {key: str(continuation.get(key) or "") for key in cls._CONTINUATION_BINDING_KEYS}

    @staticmethod
    def _continuation_key(continuation: dict[str, Any]) -> str:
        return f"restoration:{continuation.get('native_request_id')}:continue:{continuation.get('control_id')}"

    async def _offer_or_close(
        self,
        client: Any,
        target: ChatRequest,
        record: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        """Leave a pending card open; offer a decided card's saved decision; close anything unprovable.

        A decided card row cannot say whether Mythic delivered the decision, still has it queued,
        or refused it while Sage was down. Startup therefore never applies or discards it: it
        offers one Continue/Stop control in the same request and keeps the original approval
        context valid, so either the original delivery or Continue claims it exactly once.
        """
        operation_id, channel_id = int(target.OperationID), int(target.ChannelID)
        thread_id = str(record.get("thread_id") or "")
        approval_id = str(context.get("approval_id") or "")
        try:
            row = await read_card_row(client, operation_id, channel_id, approval_id)
        except Exception:
            row = None
        state = card_row_state(row)
        if state == "pending":
            return "left open: card pending in Mythic"
        if state != "decided":
            return await self._close_card_request(
                target, thread_id, "its card could not be read in Mythic"
            )
        pending = record.get("pending") if isinstance(record.get("pending"), dict) else {}
        if not isinstance(pending.get("lifecycle"), dict):
            return await self._close_card_request(
                target, thread_id, "it was saved without its request state"
            )
        try:
            action_requests = await paused_action_requests(channel_session_db_path(), thread_id)
        except Exception:
            action_requests = []
        restored, _contract, reason = restored_pending_context(pending, action_requests, thread_id)
        if restored is None:
            return await self._close_card_request(target, thread_id, reason)
        envelope = input_response_from_card_row(row)
        digest = saved_decision_digest(envelope)
        continuation = record.get("continuation") if isinstance(record.get("continuation"), dict) else None
        same = bool(
            continuation
            and continuation.get("source_approval_id") == approval_id
            and continuation.get("decision_digest") == digest
            and continuation.get("native_request_id") == str(target.RequestID)
            and continuation.get("thread_id") == thread_id
        )
        n = 1
        if same:
            if continuation.get("status") != "offered":
                return await self._close_card_request(
                    target, thread_id, "its saved decision was already handled"
                )
            try:
                control = await read_control_row(
                    client, operation_id, channel_id, str(continuation.get("control_id") or "")
                )
            except Exception:
                return "left open: Continue/Stop card unreadable"
            choice = control_row_choice(control)
            if choice == "pending":
                return "left open: Continue/Stop card pending"
            if choice == "stop":
                return await self._revoke_continuation(target, thread_id, continuation)
            if control is None:
                # Saved before its card reached Mythic: send it under the same stable key.
                await self._send_continuation_card(target, continuation, envelope, row)
                return "continuation sent"
            if choice != "continue":
                return await self._close_card_request(
                    target, thread_id, "its Continue/Stop card could not be read in Mythic"
                )
            # Continue was chosen while Sage could not receive it: never resend an answered key.
            n = int(continuation.get("n") or 1) + 1
        binding = {
            "control_id": f"{approval_id}:{n}",
            "request_id": str(context.get("request_id") or ""),
            "native_request_id": str(target.RequestID),
            "thread_id": thread_id,
            "source_approval_id": approval_id,
            "source_message_id": str(row.get("id")),
            "decision_digest": digest,
            "operation_id": str(operation_id),
        }
        continuation = {**binding, "n": n, "status": "offered"}
        # Write-ahead: the record names the control before it exists, so a crash before the send
        # is repaired by sending the same key at the next start.
        saved = await update_channel_session_record(
            operation_id, channel_id, expected_thread_id=thread_id, continuation=continuation,
        )
        if not saved:
            return await self._close_card_request(target, thread_id, "its saved session changed")
        await self._send_continuation_card(target, continuation, envelope, row)
        return "continuation offered"

    async def _send_continuation_card(
        self,
        target: ChatRequest,
        continuation: dict[str, Any],
        envelope: Any,
        source_row: dict[str, Any],
    ) -> None:
        source_card = ((source_row.get("metadata") or {}).get("input_requested") or {})
        title = str(source_card.get("title") or "").strip()
        kwargs = continuation_card(
            self._continuation_binding(continuation),
            saved_action=str(getattr(envelope, "Action", "") or ""),
            tool_label=f"“{title}”" if title else "",
        )
        await self.send_single_choice_request(
            target,
            title=kwargs["title"],
            prompt=kwargs["prompt"],
            choices=kwargs["choices"],
            description=kwargs["description"],
            data=kwargs["data"],
            response_key=self._continuation_key(continuation),
        )

    async def _revoke_continuation(
        self, target: ChatRequest, thread_id: str, continuation: dict[str, Any]
    ) -> str:
        """Stop chosen (or the request cancelled) while Sage could not receive it: nothing runs."""
        try:
            await update_channel_session_record(
                int(target.OperationID),
                int(target.ChannelID),
                expected_thread_id=thread_id,
                pending=None,
                continuation={**continuation, "status": "stopped"},
            )
        except Exception:
            logger.warning("Stopped saved decision was not recorded", exc_info=True)
        await self._close_open_request(target, CONTINUATION_STOPPED_NOTICE)
        return "closed: saved decision stopped"

    async def _resolve_saved_decision(
        self, request: ChatRequest, model: Any
    ) -> tuple[str, str, dict[str, Any] | None]:
        """Route an input response against the channel's saved decision: ``(outcome, notice, continuation)``.

        ``continue``: a Continue answer; ``request.InputResponse`` now carries the original card's
        stored decision, re-read from Mythic and unchanged. ``superseded``: the original decision
        arrived itself while a control was offered and not stopped. ``stop``: Stop was chosen (or
        the request cancelled, or the saved decision cannot be proven); nothing may run.
        ``hold``: whether the control was answered cannot be read; nothing runs, the request stays
        open. ``stale``: a duplicate or late answer for a request whose decision is already handled
        or still pending on another card; it must neither act nor end that request. ``""``: no
        saved decision is involved; the existing claim path decides.
        """
        operation_id, channel_id = int(request.OperationID), int(request.ChannelID)
        native = int(request.RequestID)
        try:
            record = await load_channel_session_record(operation_id, channel_id)
        except Exception:
            record = None
        if (
            not isinstance(record, dict)
            or record.get("status") != CHANNEL_SESSION_ACTIVE
            or record.get("thread_id") != getattr(model, "_thread_id_override", None)
        ):
            return "", "", None
        continuation = record.get("continuation") if isinstance(record.get("continuation"), dict) else None
        offered = bool(
            continuation
            and continuation.get("status") == "offered"
            and continuation.get("native_request_id") == str(native)
        )
        pending = record.get("pending") if isinstance(record.get("pending"), dict) else {}
        pending_context = pending.get("context") if isinstance(pending.get("context"), dict) else {}
        binding = continuation_binding_of(request)
        if binding is not None:
            expected = self._continuation_binding(continuation) if offered else None
            if expected is None or binding != expected:
                return "stale", STALE_RESPONSE_NOTICE, None
            choice = continuation_choice(request, expected)
            if not choice:
                return "stale", STALE_RESPONSE_NOTICE, None
            if choice == STOP_CHOICE:
                return "stop", CONTINUATION_STOPPED_NOTICE, continuation
            try:
                client = await self._mythic_read_client(request, model)
                row = await read_card_row(
                    client, operation_id, channel_id, str(continuation.get("source_approval_id") or "")
                )
            except Exception:
                row = None
            envelope = input_response_from_card_row(row)
            if (
                envelope is None
                or saved_decision_digest(envelope) != continuation.get("decision_digest")
                or str(envelope.InputRequestMessageID) != str(continuation.get("source_message_id"))
            ):
                return "stop", SAVED_DECISION_UNAVAILABLE_NOTICE, continuation
            request.InputResponse = envelope
            return "continue", "", continuation
        matches = approval_response_matches(request, getattr(model, "_pending_approval_context", None))
        if (
            offered
            and matches
            and pending_context.get("approval_id") == continuation.get("source_approval_id")
        ):
            try:
                client = await self._mythic_read_client(request, model)
                control = await read_control_row(
                    client, operation_id, channel_id, str(continuation.get("control_id") or "")
                )
            except Exception:
                control = None
            choice = control_row_choice(control)
            if choice == "stop":
                return "stop", CONTINUATION_STOPPED_NOTICE, continuation
            if choice in {"pending", "continue"}:
                return "superseded", "", continuation
            return "hold", CONTROL_UNCONFIRMED_NOTICE, continuation
        if not matches:
            claim = record.get("claim") if isinstance(record.get("claim"), dict) else {}
            owned = (
                request_number(claim.get("request_id"), channel_id) == native
                or request_number(pending_context.get("request_id"), channel_id) == native
                or bool(continuation and continuation.get("native_request_id") == str(native))
            )
            if owned:
                return "stale", STALE_RESPONSE_NOTICE, None
        return "", "", None

    async def _retire_control_row(
        self, request: ChatRequest, model: Any, continuation: dict[str, Any]
    ) -> None:
        """Mark an unused Continue/Stop card no longer answerable before its request ends."""
        try:
            client = await self._mythic_read_client(request, model)
            row = await read_control_row(
                client, int(request.OperationID), int(request.ChannelID),
                str(continuation.get("control_id") or ""),
            )
            if control_row_choice(row) != "pending":
                return
            card = dict(((row.get("metadata") or {}).get("input_requested")) or {})
            card.update({
                "status": "cancelled",
                "response": {
                    "action": "cancel",
                    "reason": "The original decision arrived and was applied; this card is no longer needed.",
                    "input_request_message_id": row.get("id"),
                },
            })
            await self.send_response(
                request,
                response_key=self._continuation_key(continuation),
                content="",
                status="complete",
                complete=False,
                complete_request=False,
                metadata={"special_type": "input_requested", "input_requested": card},
            )
        except Exception:
            logger.warning("Unused Continue/Stop card was not retired", exc_info=True)

    async def _close_card_request(self, target: ChatRequest, thread_id: str, reason: str) -> str:
        # Clearing the card lets the next prompt's restore retire the paused call with history kept.
        try:
            await update_channel_session_record(
                int(target.OperationID),
                int(target.ChannelID),
                expected_thread_id=thread_id,
                pending=None,
            )
        except Exception:
            logger.warning("Pending card could not be cleared after reconciliation", exc_info=True)
        await self._close_open_request(target, card_closed_notice(reason))
        return f"closed: card not re-asked ({reason})"

    async def _close_open_request(self, target: ChatRequest, notice: str) -> None:
        await self.send_complete(
            target,
            f"restoration:{target.RequestID}:closed",
            content=notice,
            complete_request=True,
        )

    async def handle_finding_command(
        self,
        request: ChatRequest,
        model: Any | None,
    ) -> str:
        """Render the watcher-owned current view without entering the model runtime."""
        try:
            view, snapshot = await self._operation_memory().current_view(
                str(request.OperationID)
            )
            watcher = self._findings_watcher().status(str(request.OperationID))
            return (
                render_findings_markdown(view, snapshot)
                + "\n\n---\n\n"
                + render_watcher_status(watcher)
            )
        except Exception as exc:
            logger.warning(
                f"Operation-memory view failed for operation "
                f"{request.OperationID}: {exc}"
            )
            return (
                "Operation findings are unavailable because Sage could not read "
                "the watcher-owned operation evidence index."
            )

    async def handle_watcher_command(self, request: ChatRequest, argument: str) -> str:
        action = str(argument or "status").strip().casefold() or "status"
        try:
            if action == "apply":
                channel = await self._findings_watcher().control_plane.inspect_request_channel(request)
                record = await self._findings_watcher().apply_profile(request, channel)
                return (
                    f"Watcher profile generation `{record.generation}` applied from locked owner "
                    f"`{record.owner_channel_name}` (`{record.owner_channel_id}`).\n\n"
                    + render_watcher_status(self._findings_watcher().status(str(request.OperationID)))
                )
            owner_channel_id: int | None = None
            if action != "status":
                channel = await self._findings_watcher().control_plane.inspect_request_channel(request)
                if not channel.valid_owner_candidate:
                    raise WatcherOwnerConflict("Watcher control channel is not active and locked")
                owner_channel_id = channel.channel_id
            status = await self._findings_watcher().command(
                str(request.OperationID), action, owner_channel_id=owner_channel_id
            )
        except (ValueError, WatcherOwnerConflict, WatcherControlBoundaryError) as exc:
            return (
                f"Watcher control denied: `{type(exc).__name__}`. "
                "Use `/watcher status`; apply or mutate only from the exact active locked `Sage Watcher` owner. "
                "Valid controls: `/watcher apply|scan|pause|resume|interval <seconds>|interval default`."
            )
        return render_watcher_status(status)

    @staticmethod
    def _bloodhound_connection_locally_pinned(server: Any) -> bool:
        try:
            from ai.mcp import MCPManager
        except ImportError:  # pragma: no cover
            from ..ai.mcp import MCPManager  # type: ignore
        return bool(MCPManager.is_bloodhound_server(server))

    async def _ensure_bloodhound_connected(
        self,
        *,
        autonomous_required: bool = False,
        request: Any = None,
    ) -> bool:
        """Lazily auto-connect the BloodHound MCP on the chat-request path.

        Connected here rather than at container boot on purpose: the MCP stdio session is bound to the
        event loop that creates it, so it must be opened from the same serving loop that later runs the
        graph (see ``ai/bloodhound_config.ensure_bloodhound_connected``). Process-global, so only the
        first channel to reach this actually connects; all later sessions no-op. Needs
        ``SAGE_BLOODHOUND_MCP_DIR`` (or an explicit dir) to locate the MCP server.

        Supervised/non-autonomous chat remains fail-soft so operators can still inspect a degraded
        session. Autonomous chat is different: it must fail closed before ``Model.initialize()``
        unless the canonical BloodHound server exposes the exact required tool names.
        """
        try:
            from ai.bloodhound_config import (
                bloodhound_tool_admission,
                credential_diagnostic,
                ensure_bloodhound_connected,
            )
        except ImportError:  # pragma: no cover
            from ..ai.bloodhound_config import (  # type: ignore
                bloodhound_tool_admission,
                credential_diagnostic,
                ensure_bloodhound_connected,
            )
        bloodhound_env: dict[str, str] = {}
        if request is not None:
            try:
                try:
                    from .config import build_bloodhound_env
                except ImportError:  # pragma: no cover
                    from config import build_bloodhound_env  # type: ignore
                bloodhound_env = build_bloodhound_env(request)
            except Exception as exc:  # pragma: no cover - resolution must never block connect
                logger.debug(f"BloodHound credential resolution skipped: {exc}")
        try:
            connected, message = await ensure_bloodhound_connected(env=bloodhound_env or None)
            summary = f"BloodHound auto-connect (chat): {message}" + (
                f" [credentials supplied: {sorted(bloodhound_env)}]" if bloodhound_env else ""
            )
            if connected:
                logger.info(summary)
            else:
                # WARNING, not INFO, because Mythic runs this container at DEBUG_LEVEL=warning and
                # sets that level itself. An INFO diagnostic here is discarded before it reaches
                # anyone: the container showed four `McpError: Connection closed` failures and ZERO
                # explanations, because the one line that named the missing variable was filtered
                # out. Success stays at INFO — a working connect is not news.
                logger.warning(f"{summary}\n{credential_diagnostic(bloodhound_env)}")
            admission = bloodhound_tool_admission()
            admitted = bool(
                connected
                and admission.get("ready")
                and self._bloodhound_connection_locally_pinned(admission.get("server"))
            )
            if autonomous_required and not admitted:
                raise RuntimeError(
                    _bloodhound_unavailable_message(
                        (
                            "The connected BloodHound MCP is not the one "
                            "SAGE_BLOODHOUND_MCP_DIR and the configured launcher point at."
                            if connected and admission.get("ready")
                            else admission.get("reason")
                        )
                    )
                )
        except Exception as exc:
            if autonomous_required:
                raise RuntimeError(_bloodhound_unavailable_message(str(exc))) from exc
            # Same reasoning as the not-connected branch above: this path swallows the failure so
            # conversation chat stays fail-soft, and at DEBUG it swallowed the reason too.
            logger.warning(
                f"BloodHound auto-connect (chat) skipped: {exc}\n"
                f"{credential_diagnostic(bloodhound_env)}"
            )
            return False
        if autonomous_required:
            # Exact tool names do not prove the server can report ingest status, and an autonomous solve
            # that cannot verify its graph collection must not start. One live status read decides it.
            # Conversation and supervised stay fail-soft: their degraded-ingest notice is sent per turn.
            try:
                from ai.bloodhound_config import autonomous_ingest_unavailable_message, probe_ingest_contract
            except ImportError:  # pragma: no cover
                from ..ai.bloodhound_config import (  # type: ignore
                    autonomous_ingest_unavailable_message,
                    probe_ingest_contract,
                )
            contract = await probe_ingest_contract()
            if not contract.ready:
                logger.warning(f"BloodHound ingest contract not ready ({contract.kind}): {contract.reason}")
                raise RuntimeError(autonomous_ingest_unavailable_message(contract))
        return admitted

    async def _refresh_auth_context(self, model: Any, request: ChatRequest) -> None:
        """Assert that a reused model still belongs to the same Mythic auth identity.

        A Model contains checkpoint history, engagement evidence, and raw credential caches. Re-login
        cannot safely transfer that state to a different token or operation; callers must rotate the
        entire session instead.
        """
        token_changed = getattr(model, "apitoken_id", None) != request.APITokenID
        operation_changed = getattr(model, "operation_id", None) != request.OperationID
        if token_changed or operation_changed:
            raise RuntimeError("Mythic auth identity changed; a fresh Sage session is required.")

    @staticmethod
    async def _stop_and_close_request_lifecycles(
        model: Any,
        *,
        status: str,
        reason: str = "session_rotated",
    ) -> None:
        """End a session's request lifecycle and tell the operator why.

        `reason` defaults to session rotation rather than to an operator stop, because **no caller
        of this helper is an operator pressing stop** — they are identity/config rotation and
        refused resumes. Reporting "Session stopped by operator" here sends operators looking for a
        stop they never issued.
        """
        try:
            from ai.langgraph.model import stop_notice_for
        except ImportError:  # matches the lazy relative-import fallback used elsewhere in this file
            from ..ai.langgraph.model import stop_notice_for  # type: ignore

        try:
            model.request_stop(reason)
        except Exception:
            logger.warning("request_stop() failed during lifecycle cleanup", exc_info=True)
        emit_terminal = getattr(model, "_emit_operator_stop", None)
        if callable(emit_terminal) and status in {"stopped", "cancelled"}:
            try:
                await emit_terminal(
                    _with_task_provenance(model, stop_notice_for(reason)),
                    status=status,
                )
                return
            except Exception:
                logger.warning(
                    "request lifecycle terminalization failed",
                    exc_info=True,
                )
        close_all = getattr(model, "_close_all_request_lifecycles", None)
        if not callable(close_all):
            close_all = getattr(model, "_close_all_delegations", None)
        if callable(close_all):
            try:
                await close_all(status=status)
            except Exception:
                logger.warning("request lifecycle cleanup failed", exc_info=True)

    async def _rotate_auth_changed_session(self, request: ChatRequest, model: Any | None) -> Any | None:
        if model is None:
            return None
        if (
            getattr(model, "apitoken_id", None) == request.APITokenID
            and getattr(model, "operation_id", None) == request.OperationID
        ):
            return model
        await self._stop_and_close_request_lifecycles(
            model, status="stopped", reason="session_rotated"
        )
        await drop_channel_session(request, expected_model=model)
        logger.info("Rotated Sage channel session after Mythic token/operation identity changed")
        return None

    async def _get_or_create_model(self, request: ChatRequest) -> tuple[Any, bool]:
        """Return ``(model, preexisted)`` for this channel.

        A found session is reused only when its full resolved constructor config still matches the
        current request. Provider/model credentials and routing topology are initialization-owned, so
        any change rotates the Model instead of mutating a partially stale graph in place.
        """
        kwargs = build_model_kwargs(request)
        try:
            bloodhound_env = build_bloodhound_env(request)
        except Exception as exc:  # pragma: no cover - resolution must never block a chat turn
            logger.debug(f"BloodHound credential resolution skipped for signature: {exc}")
            bloodhound_env = {}
        config_signature = _model_config_signature(kwargs, bloodhound_env)
        existing = await get_channel_session(request)
        existing = await self._rotate_auth_changed_session(request, existing)
        if existing is not None and (
            getattr(existing, "_chat_request_config_signature", "") != config_signature
            or not _runtime_routing_matches(
                existing,
                kwargs,
                config_signature=config_signature,
            )
        ):
            continued = await self._continue_settings_change(
                request, existing, kwargs, config_signature,
                _core_config_signature(kwargs, bloodhound_env),
            )
            if continued is not None:
                return continued, True
            await self._stop_and_close_request_lifecycles(
                existing,
                status="stopped",
                reason="session_rotated",
            )
            await drop_channel_session(request, expected_model=existing)
            logger.info("Rotated Sage channel session after ChatRequest configuration changed")
            existing = None
        if existing is not None:
            existing._native_chat_explicit_hitl = True
            await self._refresh_auth_context(existing, request)
            autonomous_now = bool(getattr(existing, "_autonomous_solve", False))
            if autonomous_now:
                admitted = await self._ensure_bloodhound_connected(
                    autonomous_required=True, request=request
                )
                if not getattr(existing, "_bloodhound_exact_admission_at_initialize", False):
                    raise RuntimeError(
                        _bloodhound_unavailable_message(
                            "This session's graph was built while BloodHound was unavailable, so it "
                            "has no attack-graph tools; start a new chat once BloodHound is "
                            "connected."
                        )
                    )
                if not admitted:
                    raise RuntimeError(
                        _bloodhound_unavailable_message(
                            "BloodHound was connected when this session started but is not "
                            "available on this turn."
                        )
                    )
            else:
                # Fail-soft keep-warm, in every mode, not only autonomous: without it a reused
                # conversation/supervised session never tries to connect BloodHound, and an operator
                # who configured it and kept chatting sees nothing happen, not even an error.
                # Idempotent when already connected. Note this cannot retro-fit tools into
                # THIS session's graph (Model.initialize() already resolved its tool list); the
                # signature change above is what rebuilds the session when configuration changes.
                await self._ensure_bloodhound_connected(request=request)
            return existing, True

        model = await self._build_model(request, kwargs, config_signature)
        await put_channel_session(request, model)
        await self._save_session_record(request, model)
        return model, False

    async def _settings_change_is_idle(self, request: ChatRequest, model: Any) -> bool:
        """True when nothing on this session could carry consent or work across a settings change."""
        if getattr(request, "InputResponse", None) is not None:
            return False
        if getattr(model, "_hitl_card_pending", False) or isinstance(
            getattr(model, "_controller_hitl_pending", None), dict
        ):
            return False
        thread_id = str(getattr(model, "_thread_id_override", "") or "")
        if not thread_id:
            return False
        try:
            if await model._hitl_interrupt_pending(thread_id):
                return False
            record = await load_channel_session_record(int(request.OperationID), int(request.ChannelID))
        except Exception:
            logger.warning("Settings change could not verify an idle channel", exc_info=True)
            return False
        return (
            record is not None
            and record.get("status") == CHANNEL_SESSION_ACTIVE
            and record.get("thread_id") == thread_id
            and not record.get("pending")
            and not _claim_outstanding(record)
        )

    async def _continue_settings_change(
        self,
        request: ChatRequest,
        existing: Any,
        kwargs: dict[str, Any],
        config_signature: str,
        core_signature: str,
    ) -> Any | None:
        """Rebuild an idle session on its own generation when only the OpenAI route settings changed.

        Returns the new registered Model, or ``None`` to keep today's rotation (any other change, an
        outstanding approval or controller step, an approval response, or a record that cannot be verified).
        The checkpoint stays the owner of history; the new Model restores from it exactly as a restart does.
        """
        stored_core = str(getattr(existing, "_chat_request_core_signature", "") or "")
        if not stored_core or stored_core != core_signature:
            return None
        if not await self._settings_change_is_idle(request, existing):
            return None
        thread_id = str(existing._thread_id_override)
        old_signature = str(getattr(existing, "_chat_request_config_signature", "") or "")
        await drop_channel_session(request, expected_model=existing, retire=False)
        model = await self._build_model(request, kwargs, config_signature, thread_id=thread_id)
        model._chat_request_base_autonomous_solve = bool(
            getattr(existing, "_chat_request_base_autonomous_solve", kwargs.get("autonomous_solve"))
        )
        override = str(getattr(existing, "_chat_mode_override", "") or "")
        if override and getattr(existing, "_chat_mode_override_base_signature", "") == old_signature:
            self._apply_saved_mode_override(model, {
                "mode_override": override,
                "mode_override_base_signature": config_signature,
                "mode_override_base_autonomous_solve": getattr(
                    existing, "_chat_mode_override_base_autonomous_solve", None
                ),
            }, config_signature)
        engagement = getattr(existing, "_report_engagement_uuid", None)
        if engagement:
            model._report_engagement_uuid = engagement
        try:
            await model.restore_session_state_from_checkpoint(thread_id)
        except LookupError:
            pass  # a generation whose only request failed before any checkpoint write
        await put_channel_session(request, model)
        await self._save_session_record(request, model)
        model._settings_continuation_notice = SETTINGS_CONTINUED_NOTICE
        logger.info("Applied OpenAI route settings on the channel's existing generation")
        return model

    async def _construct_model(self, kwargs: dict[str, Any]) -> Any:
        # Lazy import: keep the heavy LangGraph/LangChain import off the module load path so the pure
        # config/streaming/models modules (and their tests) don't pull it in.
        try:
            from ai.langgraph.model import Model
        except ImportError:  # pragma: no cover
            from ..ai.langgraph.model import Model  # type: ignore

        return Model(**kwargs)

    async def _initialize_model(self, model: Any) -> None:
        await model.initialize()
        # The chat container always runs at full detail — the collapsible tool cards ARE the "verbose"
        # view, so there is no operator verbose toggle (removed). set_verbose(True) also enables the local
        # tmux stream log; user-facing tool detail always renders as cards regardless of this flag.
        model.set_verbose(True)

    async def _build_model(
        self,
        request: ChatRequest,
        kwargs: dict[str, Any],
        config_signature: str,
        *,
        thread_id: str = "",
    ) -> Any:
        """Construct and initialize one channel Model; a restore passes its stored generation."""
        model = await self._construct_model(kwargs)
        model._chat_request_config_signature = config_signature
        model._chat_request_core_signature = _core_config_signature(
            kwargs, _bloodhound_env_or_empty(request)
        )
        model._chat_request_base_autonomous_solve = bool(kwargs.get("autonomous_solve"))
        model._chat_mode_override = ""
        model._chat_mode_override_base_signature = ""
        model._chat_mode_override_base_autonomous_solve = None
        # Native chat is a real interactive approval transport. Set this before graph construction so any
        # runtime checks see the same command identity as the legacy `chat` task path.
        model.command_name = "chat"
        model._native_chat_explicit_hitl = True
        # A restored channel keeps its existing checkpoint generation, bound before initialize().
        if thread_id:
            model._thread_id_override = thread_id
        bind_channel_thread_id(request, model)
        # Auto-connect the BloodHound MCP BEFORE the graph is built — Model.initialize() wires the
        # BloodHound agent's tools from the currently-connected MCP servers, so a later connect
        # wouldn't be seen by this session's graph.
        admitted_at_initialize = await self._ensure_bloodhound_connected(
            autonomous_required=bool(kwargs.get("autonomous_solve")), request=request
        )
        model._bloodhound_exact_admission_at_initialize = bool(admitted_at_initialize)
        await self._initialize_model(model)
        return model

    # ------------------------------------------------------------------------------------------
    # Channel restoration after a process restart
    # ------------------------------------------------------------------------------------------

    @staticmethod
    def _session_record_for(request: ChatRequest, model: Any) -> dict[str, Any]:
        override_base = getattr(model, "_chat_mode_override_base_autonomous_solve", None)
        return {
            "operation_id": int(request.OperationID),
            "channel_id": int(request.ChannelID),
            "status": CHANNEL_SESSION_ACTIVE,
            "retired_reason": "",
            "thread_id": str(getattr(model, "_thread_id_override", "") or ""),
            "apitoken_id": int(request.APITokenID),
            "config_signature": str(getattr(model, "_chat_request_config_signature", "") or ""),
            "core_signature": str(getattr(model, "_chat_request_core_signature", "") or ""),
            "base_autonomous_solve": bool(
                getattr(model, "_chat_request_base_autonomous_solve", False)
            ),
            "mode_override": str(getattr(model, "_chat_mode_override", "") or ""),
            "mode_override_base_signature": str(
                getattr(model, "_chat_mode_override_base_signature", "") or ""
            ),
            "mode_override_base_autonomous_solve": (
                None if override_base is None else bool(override_base)
            ),
            "engagement_uuid": getattr(model, "_report_engagement_uuid", None),
            "pending": None,
            "claim": None,
        }

    async def _save_session_record(self, request: ChatRequest, model: Any) -> None:
        if not getattr(model, "_thread_id_override", None):
            return
        await save_channel_session_record(self._session_record_for(request, model))

    async def _update_session_record(
        self,
        request: ChatRequest,
        model: Any,
        *,
        required: bool,
        **fields: Any,
    ) -> None:
        """Merge fields into this generation's record; ``required`` writes fail the turn."""
        thread_id = str(getattr(model, "_thread_id_override", "") or "")
        if not thread_id:
            return
        try:
            record = await load_channel_session_record(
                int(request.OperationID), int(request.ChannelID)
            )
            if record is None:
                record = self._session_record_for(request, model)
            elif record.get("thread_id") != thread_id:
                if await get_channel_session(request) is model:
                    # This process's registered Model is the channel's live session; a record
                    # naming another generation is stale and is replaced by this one.
                    record = self._session_record_for(request, model)
                elif required:
                    raise RuntimeError(
                        "This channel's saved session no longer matches its active session."
                    )
                return
            record.update(fields)
            await save_channel_session_record(record)
        except Exception:
            if required:
                raise
            logger.warning("Channel session record update failed", exc_info=True)

    async def _mark_claim_consumed(
        self, request: ChatRequest, model: Any, approval_id: str
    ) -> None:
        """Record that a claimed decision's resume completed; a newer card's state is untouched."""
        try:
            record = await load_channel_session_record(
                int(request.OperationID), int(request.ChannelID)
            )
            claim = (record or {}).get("claim")
            if (
                isinstance(claim, dict)
                and claim.get("approval_id") == approval_id
                and record.get("thread_id") == getattr(model, "_thread_id_override", None)
            ):
                record["claim"] = {**claim, "status": "consumed"}
                await save_channel_session_record(record)
        except Exception:
            # Left "claimed", a restart checks the checkpoint: a finished resume counts as consumed.
            logger.warning("Approval claim completion was not saved", exc_info=True)

    async def _persist_session_settings(self, request: ChatRequest, model: Any) -> None:
        """Save the runtime `/mode` override and engagement binding this restore must reapply."""
        record = self._session_record_for(request, model)
        await self._update_session_record(
            request,
            model,
            required=False,
            **{
                key: record[key]
                for key in (
                    "mode_override",
                    "mode_override_base_signature",
                    "mode_override_base_autonomous_solve",
                    "base_autonomous_solve",
                    "engagement_uuid",
                )
            },
        )

    async def _mythic_read_client(self, request: ChatRequest, model: Any | None = None) -> Any:
        """The channel token's Mythic client, for read-only restoration lookups."""
        client = getattr(getattr(model, "mythic_client", None), "client", None)
        if client is not None:
            return client
        from ai.langgraph.mythic_tools import MythicTools

        auth = MythicTools(
            operation_id=request.OperationID,
            channel_id=request.ChannelID,
            apitoken_id=request.APITokenID,
        )
        await auth.login()
        return auth.client

    @staticmethod
    def _apply_saved_mode_override(model: Any, record: dict[str, Any], signature: str) -> None:
        """Reapply a saved `/mode` override exactly as `/mode` set it, only on unchanged config."""
        override = str(record.get("mode_override") or "")
        override_base = record.get("mode_override_base_autonomous_solve")
        if (
            override not in {"conversation", "supervised", "auto"}
            or str(record.get("mode_override_base_signature") or "") != signature
            or not isinstance(override_base, bool)
        ):
            return
        model.mode = override
        model._autonomous_solve = (
            True if override == "auto" else False if override == "conversation" else override_base
        )
        model._chat_mode_override = override
        model._chat_mode_override_base_signature = signature
        model._chat_mode_override_base_autonomous_solve = override_base

    async def _retire_into_new_generation(
        self,
        request: ChatRequest,
        model: Any,
        closure_text: str,
    ) -> None:
        """Close the restored generation's open work in code and continue on a new generation."""
        new_thread_id = next_channel_thread_id(request)
        model.retire_restored_generation(new_thread_id, closure_text)
        model._pending_approval_context = None
        record = self._session_record_for(request, model)
        await save_channel_session_record(record)

    async def _restore_channel_session(
        self,
        request: ChatRequest,
    ) -> tuple[Any | None, str, list[str]]:
        """Rebuild this channel's Model after a restart: ``(model, refusal, notices)``.

        The model is registered only on success. Authority is never carried from the old process:
        the request's current config/identity must match what the session was saved under, and a
        paused action resumes only through the same exact approval correlation as before.
        """
        operation_id, channel_id = int(request.OperationID), int(request.ChannelID)
        try:
            record = await load_channel_session_record(operation_id, channel_id)
        except ChannelSessionRecordInvalid as exc:
            return None, unreadable_refusal(str(exc)), []
        kwargs = build_model_kwargs(request)
        bloodhound_env = _bloodhound_env_or_empty(request)
        signature = _model_config_signature(kwargs, bloodhound_env)
        if record is None:
            return await self._adopt_legacy_session(request, kwargs, signature)
        if record.get("status") != CHANNEL_SESSION_ACTIVE:
            return None, RETIRED_REFUSAL, []
        if record.get("apitoken_id") != int(request.APITokenID):
            return None, changed_refusal("Mythic identity"), []
        settings_changed = False
        if record.get("config_signature") != signature:
            # Only the OpenAI route settings changed, and nothing is pending: continue this generation.
            core = str(record.get("core_signature") or "")
            if (
                not core
                or core != _core_config_signature(kwargs, bloodhound_env)
                or record.get("pending")
                or _claim_outstanding(record)
                or getattr(request, "InputResponse", None) is not None
            ):
                return None, changed_refusal("configuration"), []
            settings_changed = True
            old_signature = record.get("config_signature")
            record = dict(record)
            if record.get("mode_override_base_signature") == old_signature:
                record["mode_override_base_signature"] = signature
            record["config_signature"] = signature
        thread_id = str(record["thread_id"])
        model = await self._build_model(request, kwargs, signature, thread_id=thread_id)
        model._chat_request_base_autonomous_solve = bool(
            record.get("base_autonomous_solve", kwargs.get("autonomous_solve"))
        )
        self._apply_saved_mode_override(model, record, signature)
        if not _runtime_routing_matches(model, kwargs, config_signature=signature):
            return None, changed_refusal("configuration"), []
        engagement = record.get("engagement_uuid")
        if isinstance(engagement, str) and engagement:
            model._report_engagement_uuid = engagement
        notices = [RESTORED_NOTICE] + ([SETTINGS_CONTINUED_NOTICE] if settings_changed else [])
        if settings_changed:
            await save_channel_session_record({**record, **self._session_record_for(request, model),
                                               "pending": None, "claim": None})
        try:
            values = await model.restore_session_state_from_checkpoint(thread_id)
        except LookupError:
            values = None
            notices = [
                "Sage restarted and found no saved conversation for this channel, so earlier "
                "messages are not part of its context. State the current scope and evidence "
                "before continuing."
            ]
        except Exception as exc:
            return None, unreadable_refusal(
                f"its saved conversation is unreadable ({type(exc).__name__})"
            ), []
        await put_channel_session(request, model)
        if values is not None:
            notices.extend(await self._restore_pending_work(request, model, record, thread_id))
        return model, "", notices

    async def _restore_pending_work(
        self,
        request: ChatRequest,
        model: Any,
        record: dict[str, Any],
        thread_id: str,
    ) -> list[str]:
        """Reattach a provably unconsumed card, or retire paused work that cannot resume."""
        try:
            from ai.langgraph.model import _collect_hitl_action_requests
        except ImportError:  # pragma: no cover
            from ..ai.langgraph.model import _collect_hitl_action_requests  # type: ignore
        snapshot = await model.graph.aget_state(model._graph_run_config(thread_id))
        action_requests = _collect_hitl_action_requests(snapshot)
        claim = record.get("claim") if isinstance(record.get("claim"), dict) else None
        if claim and claim.get("status") == "claimed":
            if not action_requests:
                # The approved resume finished and was checkpointed; its result is in history.
                await self._update_session_record(
                    request, model, required=True, claim={**claim, "status": "consumed"},
                )
                return []
            if claim.get("decision") == "deny":
                await self._retire_into_new_generation(request, model, REJECTED_CLAIM_CLOSURE)
                return [
                    "Sage restarted while applying your rejection of an action. The action was not "
                    "executed and will not be resent."
                ]
            detail = await describe_receipts(
                getattr(model, "mythic_client", None), list(claim.get("receipts") or [])
            )
            await self._retire_into_new_generation(
                request, model, UNKNOWN_OUTCOME_CLOSURE.format(detail=detail)
            )
            return [
                "An approved action was interrupted by the restart before its result was saved. "
                f"Sage will not resend it. {detail}"
            ]
        if not action_requests:
            if record.get("pending"):
                await self._update_session_record(request, model, required=True, pending=None)
                return [
                    "The approval card raised before the restart can no longer be resumed. Its "
                    "action was not executed."
                ]
            return []
        context, contract, reason = restored_pending_context(
            record.get("pending"), action_requests, thread_id
        )
        if context is None:
            await self._retire_into_new_generation(request, model, RETIRED_CARD_CLOSURE)
            return [
                f"The action waiting for approval before the restart was not restored ({reason}). "
                "It was not executed and will not be resent."
            ]
        lifecycle = (record.get("pending") or {}).get("lifecycle")
        if lifecycle is None:
            # Saved by an earlier version without its request state: resuming would start a second
            # lifecycle for the same request, so it is retired with its history kept.
            await self._retire_into_new_generation(request, model, RETIRED_CARD_CLOSURE)
            return [LEGACY_PENDING_NOTICE]
        checkpoint_subgoal = copy.deepcopy((getattr(model, "state", None) or {}).get("_subgoal_state"))
        resuming = (
            getattr(request, "InputResponse", None) is not None
            and isinstance(lifecycle, dict)
            and lifecycle.get("native_request_id") == int(request.RequestID)
        )
        if resuming:
            # The decision arrives in the card's own request: continue that request's lifecycle.
            try:
                model._restored_emitter_rows = model.restore_pending_request(contract, lifecycle)
            except (TypeError, ValueError) as exc:
                await self._retire_into_new_generation(request, model, RETIRED_CARD_CLOSURE)
                return [
                    "The action waiting for approval before the restart was not restored (its saved "
                    f"request state is unreadable: {type(exc).__name__}). It was not executed and "
                    "will not be resent."
                ]
            # The graph was compiled at initialize for the configured mode and default authority;
            # resume against one built for the restored mode and contract, and only if it still
            # has the paused step with the same actions.
            model._refresh_graph_for_turn()
            present, paused_calls = await model.restored_paused_tool_calls(
                thread_id, str(context.get("action_digest") or "")
            )
            if not present:
                # The restored request lifecycle stays installed so its cards close on this
                # request's terminal, which carries the notice (the handler skips the claim).
                await self._retire_into_new_generation(request, model, MISMATCHED_CARD_CLOSURE)
                model._restored_refusal_notice = MISMATCHED_CARD_NOTICE
                return []
            model._restored_paused_calls = paused_calls
        else:
            model.install_request_contract(contract)
        self._restore_subgoal_state(model, contract, checkpoint_subgoal)
        model._pending_approval_context = context
        return []

    @staticmethod
    def _restore_subgoal_state(model: Any, contract: Any, raw: Any) -> None:
        """Keep the paused request's checkpointed subgoal progress after reinstalling its contract."""
        if not isinstance(raw, dict) or not raw:
            return
        try:
            from ai.langgraph.subgoal_state import SubgoalState
        except ImportError:  # pragma: no cover
            from ..ai.langgraph.subgoal_state import SubgoalState  # type: ignore
        try:
            subgoal = SubgoalState.from_dict(raw)
        except ValueError:
            return
        if subgoal.request_id != contract.request_id:
            return
        lock = getattr(model, "_subgoal_authority_lock", None)
        if lock is None:
            model._subgoal_authority = subgoal
        else:
            with lock:
                model._subgoal_authority = subgoal
        state = getattr(model, "state", None)
        if isinstance(state, dict):
            state["_subgoal_state"] = subgoal.to_dict()

    async def _adopt_legacy_session(
        self,
        request: ChatRequest,
        kwargs: dict[str, Any],
        signature: str,
    ) -> tuple[Any | None, str, list[str]]:
        """Adopt a pre-record channel only when Mythic's rows prove which generation it used."""
        try:
            from ai.langgraph.sqlite_checkpointer import SageAsyncSqliteSaver
        except ImportError:  # pragma: no cover
            from ..ai.langgraph.sqlite_checkpointer import SageAsyncSqliteSaver  # type: ignore
        operation_id, channel_id = int(request.OperationID), int(request.ChannelID)
        db_path = channel_session_db_path()
        generations = await channel_generations(db_path, channel_id)
        if not generations:
            return None, unproven_refusal("no saved conversation was found"), []
        latest: dict[str, tuple[int, str, dict[str, Any]]] = {}
        async with aiosqlite.connect(db_path, timeout=5) as conn:
            saver = SageAsyncSqliteSaver(conn)
            for thread_id in generations:
                saved = await saver.aget_tuple(
                    {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
                )
                values = (
                    dict((saved.checkpoint or {}).get("channel_values") or {})
                    if saved is not None else {}
                )
                request_id = str(values.get("_request_id") or "")
                number = request_number(request_id, channel_id)
                if number is not None:
                    latest[thread_id] = (number, request_id, values)
        if not latest:
            return None, unproven_refusal("no saved request names this channel"), []
        client = await self._mythic_read_client(request)
        proven: list[tuple[int, str, dict[str, Any]]] = []
        for thread_id, (number, request_id, values) in latest.items():
            stored = await stored_request_prompt(client, operation_id, channel_id, number)
            recorded = operator_prompt_for_request(values, request_id)
            if stored is not None and recorded is not None and stored == recorded:
                proven.append((number, thread_id, values))
        if not proven:
            return None, unproven_refusal("Mythic's records do not match a saved request"), []
        highest = max(number for number, _, _ in proven)
        owners = [entry for entry in proven if entry[0] == highest]
        if len(owners) != 1:
            return None, unproven_refusal("more than one saved conversation claims it"), []
        _, thread_id, values = owners[0]
        model = await self._build_model(request, kwargs, signature, thread_id=thread_id)
        notices = [RESTORED_NOTICE]
        saved_mode = str(values.get("mode") or "")
        if saved_mode in {"conversation", "supervised", "auto"} and saved_mode != model.mode:
            notices.append(LEGACY_MODE_NOTICE.format(mode=model.mode, previous=saved_mode))
        try:
            await model.restore_session_state_from_checkpoint(thread_id)
        except Exception as exc:
            return None, unreadable_refusal(
                f"its saved conversation is unreadable ({type(exc).__name__})"
            ), []
        await put_channel_session(request, model)
        await self._save_session_record(request, model)
        # A legacy card's approval context lived only in the old process; it cannot be proven.
        record = self._session_record_for(request, model)
        notices.extend(await self._restore_pending_work(request, model, record, thread_id))
        return model, "", notices

    async def _notify_bloodhound_degraded_once(self, model: Any, request: ChatRequest) -> None:
        """Tell the operator, in the chat, that the attack graph is unavailable. Once per session.

        The diagnostic logged at WARNING lands in the container log, which an operator has
        to go and look for. This puts it where they already are. Once per session rather than per
        turn: they saw it on the first degraded turn, and Mythic renders a live
        BloodHound-connected chip at the top of the chat, so repeating it would be the third copy of
        a fact already on screen.

        The flag lives on the session model, so a NEW chat notifies again — which is right, because
        a new chat is a new operator context and the connection is process-global, meaning the answer
        may have changed since the last one.

        Fail-soft in the strongest sense: any failure here is swallowed. A notice about a degraded
        optional dependency must never be the thing that breaks a working turn.
        """
        if getattr(model, "_bloodhound_degraded_notice_sent", False):
            return
        try:
            try:
                from ai.bloodhound_config import (
                    bloodhound_tool_admission,
                    degraded_chat_notice,
                    ingest_degraded_chat_notice,
                    probe_ingest_contract,
                )
            except ImportError:  # pragma: no cover
                from ..ai.bloodhound_config import (  # type: ignore
                    bloodhound_tool_admission,
                    degraded_chat_notice,
                    ingest_degraded_chat_notice,
                    probe_ingest_contract,
                )
            if bloodhound_tool_admission().get("ready"):
                # Connected with the exact tools is not the same as able to ingest. One status read per turn
                # until the operator has been told; a healthy server is re-read, never remembered as healthy.
                if getattr(model, "_bloodhound_ingest_notice_sent", False):
                    return
                contract = await probe_ingest_contract()
                if contract.ready:
                    return
                await self.send_response(
                    request,
                    response_key=f"bloodhound_ingest_degraded:{request.ChannelID}",
                    content=ingest_degraded_chat_notice(contract),
                    status="complete",
                    complete=False,
                    metadata={},
                )
                model._bloodhound_ingest_notice_sent = True
                return
            try:
                from .config import build_bloodhound_env
            except ImportError:  # pragma: no cover
                from config import build_bloodhound_env  # type: ignore
            await self.send_response(
                request,
                response_key=f"bloodhound_degraded:{request.ChannelID}",
                content=degraded_chat_notice(build_bloodhound_env(request)),
                status="complete",
                complete=False,
                metadata={},
            )
            model._bloodhound_degraded_notice_sent = True
        except Exception as exc:  # pragma: no cover - never break a turn over a notice
            logger.warning(f"BloodHound degraded notice not delivered: {exc}")

    async def _notify_autonomous_solve_ignored_once(self, model: Any, request: ChatRequest) -> None:
        """Tell the operator once per session that Conversation mode ignores Autonomous Solve.

        Conversation is read-only, so the toggle cannot widen it; saying so in the chat keeps the
        operator from assuming an autonomous run is underway. Skipped once `/mode` has moved the
        session out of conversation. Fail-soft: a notice must never break a working turn.
        """
        if getattr(model, "_autonomous_solve_ignored_notice_sent", False):
            return
        try:
            if str(getattr(model, "mode", "")) != "conversation" or not autonomous_solve_ignored(request):
                return
            await self.send_response(
                request,
                response_key=f"autonomous_solve_ignored:{request.ChannelID}",
                content=(
                    "Autonomous Solve is on, but Mode is **Conversation**, which is read-only. "
                    "Sage will answer without running tasks. To run autonomously, set Mode to "
                    "**Autonomous**; for approved actions, set it to **Supervised**."
                ),
                status="complete",
                complete=False,
                metadata={},
            )
            model._autonomous_solve_ignored_notice_sent = True
        except Exception as exc:  # pragma: no cover - never break a turn over a notice
            logger.warning(f"Autonomous Solve notice not delivered: {exc}")

    async def _chat_watcher(self, request: ChatRequest) -> None:
        """Serve one stateless Watcher control/explanation turn without a Sage session."""

        async def _handler(_turn) -> None:
            slash = getattr(request, "SlashCommand", None)
            slash_name = str(getattr(slash, "Name", "") or "").lower().lstrip("/")
            watcher_key = (int(request.OperationID), int(request.ChannelID))
            if slash_name == "stop":
                active = _WATCHER_TURN_TASKS.get(watcher_key)
                if active is not None and active is not asyncio.current_task() and not active.done():
                    active.cancel()
                    content = "Stop requested for the active Watcher explanation on this channel."
                else:
                    content = "No running Watcher explanation to stop on this channel."
                await self.send_complete(
                    request,
                    f"watcher-slash:{request.RequestID}",
                    content=content,
                    complete_request=True,
                )
                return None
            if slash is not None and slash_name not in {"findings", "watcher"}:
                await self.send_complete(
                    request,
                    f"watcher-slash:{request.RequestID}",
                    content=(
                        f"`/{slash_name or 'unknown'}` is not available to Sage Watcher. "
                        "Available controls: `/findings`, `/watcher`, and `/stop`."
                    ),
                    complete_request=True,
                )
                return None
            if slash is not None:
                if await handle_slash(self, request, None, f"watcher-slash:{request.RequestID}"):
                    return None
            try:
                channel = await self._findings_watcher().control_plane.inspect_request_channel(request)
                if not channel.valid_owner_candidate:
                    raise WatcherOwnerConflict("Watcher explanation requires the active locked owner channel")
                profile = self._findings_watcher().console_profile(
                    str(request.OperationID), request.ChannelID
                )
                view, _snapshot = await self._operation_memory().current_view(
                    str(request.OperationID)
                )
                findings = [
                    {
                        "finding_id": item.finding_id,
                        "title": item.title,
                        "state": item.state.value,
                        "confidence": item.confidence,
                        "observed_at_utc": item.observed_at_utc,
                        "evidence": [dict(pointer) for pointer in item.evidence],
                        "missing_assumptions": list(item.missing_assumptions),
                        "rationale": item.rationale,
                    }
                    for item in view
                ]
                graph = build_watcher_graph(profile)
                current = asyncio.current_task()
                if current is None:  # pragma: no cover - asyncio always binds a running task here
                    raise RuntimeError("Watcher explanation has no active asyncio task")
                _WATCHER_TURN_TASKS[watcher_key] = current
                try:
                    result = await graph.ainvoke(
                        {
                            "request": str(request.Prompt or ""),
                            "findings": findings,
                            "summary": "",
                            "citations": [],
                        }
                    )
                finally:
                    if _WATCHER_TURN_TASKS.get(watcher_key) is current:
                        _WATCHER_TURN_TASKS.pop(watcher_key, None)
                content = render_watcher_explanation(
                    str(result.get("summary") or ""),
                    list(result.get("citations") or []),
                )
            except (WatcherOwnerConflict, WatcherControlBoundaryError, ValueError) as exc:
                content = (
                    f"Watcher explanation unavailable: `{type(exc).__name__}`. "
                    "Use `/watcher status`; the exact locked owner may use `/watcher apply` to recover."
                )
            await self.send_complete(
                request,
                f"watcher:{request.RequestID}:turn",
                content=content,
                complete_request=True,
            )
            return None

        await self.run_chat_turn(
            request,
            _handler,
            response_key=f"watcher:{request.RequestID}:turn",
            model=request.Model,
            complete_content="Watcher turn completed.",
        )

    async def chat(self, request: ChatRequest) -> None:
        if str(request.Model or "") == "Sage Watcher":
            await self._chat_watcher(request)
            return
        prompt = request.Prompt or ""
        resume_match = (
            re.fullmatch(r"SAGE_RESUME_CONTROLLER ([1-9][0-9]*)", prompt.strip())
            if os.environ.get("SAGE_AUTO_CONTROLLER_RECOVERY") == "1" else None
        )
        resume_request_id = resume_match.group(1) if resume_match else ""
        assessment_finding_id = assess_finding_id(prompt)

        async def _serialized_handler(turn) -> dict[str, Any] | None:
            model: Any | None = None
            native_response_text = ""
            # A process restart clears the channel-keyed Model registry. Its scope,
            # evidence selections, and turn authority are not reconstructed from chat
            # prose. The marker in Sage's existing checkpoint DB catches a resumed
            # channel even when Mythic omits Context; prior Context catches an old
            # unmarked channel during rollout.
            existing_at_entry = await get_channel_session(request)
            first_channel_claim = await claim_channel_marker(request)
            channel_identity = (int(request.OperationID), int(request.ChannelID))
            slash_only_new_channel = channel_identity in _NEW_CHANNEL_WITHOUT_SESSION
            recovery_state = None
            if resume_request_id:
                try:
                    from ai.langgraph.controller_recovery import ControllerRecoveryStore, RecoveryBlocked
                except ImportError:  # pragma: no cover
                    from ..ai.langgraph.controller_recovery import ControllerRecoveryStore, RecoveryBlocked
                recovery_store = ControllerRecoveryStore(*channel_identity)
                recovery_info = recovery_store.inspect()
                if os.environ.get("SAGE_EVAL_UNBOUNDED_ARM", "").strip() != "1":
                    recovery_error = "controller recovery requires the explicitly unbounded evaluation arm"
                elif existing_at_entry is not None or recovery_info["active"]:
                    recovery_error = "autonomous controller is already active"
                elif recovery_info["phase"] not in {"clean", "terminal"}:
                    recovery_error = f"controller recovery blocked at {recovery_info['phase'] or 'missing'} boundary"
                else:
                    record = recovery_store.load()
                    recovery_state = record["payload"] if record else None
                    recovery_error = (
                        "controller recovery request identity changed"
                        if not recovery_state or recovery_state.get("original_request_id") !=
                        f"chat:{request.ChannelID}:request:{resume_request_id}"
                        else ""
                    )
                    if not recovery_error:
                        try:
                            from ai.langgraph.controller_recovery import binding_digest
                        except ImportError:  # pragma: no cover
                            from ..ai.langgraph.controller_recovery import binding_digest
                        saved_binding = recovery_state.get("binding") or {}
                        resolved_kwargs = build_model_kwargs(request)
                        resolved_signature = _model_config_signature(
                            resolved_kwargs, build_bloodhound_env(request)
                        )
                        original_contract = replace(
                            _build_request_contract(
                                request,
                                SimpleNamespace(
                                    mode=resolved_kwargs["mode"],
                                    _autonomous_solve=resolved_kwargs["autonomous_solve"],
                                ),
                                has_input_response=False,
                            ),
                            request_id=str(recovery_state["original_request_id"]),
                        )
                        if (
                            not isinstance(saved_binding, dict)
                            or binding_digest(saved_binding) != record["binding_digest"]
                            or int(saved_binding.get("apitoken_id", -1)) != int(request.APITokenID)
                            or saved_binding.get("config_signature") != resolved_signature
                            or saved_binding.get("provider") != resolved_kwargs["provider"]
                            or saved_binding.get("model") != resolved_kwargs["model"]
                            or saved_binding.get("mode") != "auto"
                            or saved_binding.get("request_contract_digest") != original_contract.digest
                        ):
                            recovery_error = "authenticated run binding changed"
                if recovery_error:
                    await self.send_complete(
                        request, turn.response_key,
                        content=f"Sage recovery blocked: {recovery_error}. No controller action was issued.",
                        complete_request=True,
                    )
                    return None
                if recovery_info["phase"] == "terminal":
                    await self.send_complete(
                        request, turn.response_key,
                        metadata={
                            "channel_id": request.ChannelID,
                            "runtime_telemetry": recovery_state.get("terminal_telemetry")
                            or (recovery_state.get("resources") or {}).get("runtime_telemetry")
                            or {},
                        },
                        content=recovery_state.get("terminal_report") or (
                            "Saved Sage controller terminal: "
                            f"{recovery_state.get('terminal_status', 'unknown')} — "
                            f"{recovery_state.get('terminal_reason', '')}. "
                            f"Cycles: {len(recovery_state.get('cycles') or [])}; "
                            f"verified effects: {len(recovery_state.get('achieved_effects') or [])}. "
                            "No controller action was issued."
                        ),
                        complete_request=True,
                    )
                    return None
            slash = getattr(request, "SlashCommand", None)
            read_only_state = (
                slash is not None
                and str(getattr(slash, "Name", "") or "").lower().lstrip("/") == "state"
                and not str(getattr(slash, "Argument", "") or "").strip()
            )
            read_only_report = (
                slash is not None
                and str(getattr(slash, "Name", "") or "").lower().lstrip("/") == "report"
            )
            tool_discovery = (slash is not None and str(getattr(slash, "Name", "") or "").lower().lstrip("/") == "tools") or (
                slash is None and looks_like_tool_inventory_prompt(prompt)
            )
            # Set only when this request restored the session: a resume the restored graph refused,
            # and the paused calls (fingerprint -> tool_call_id) it will resume.
            restored_refusal = ""
            restored_paused_calls: dict[str, str] | None = None
            if existing_at_entry is None and not read_only_state and not read_only_report and not tool_discovery and (
                (not first_channel_claim and not slash_only_new_channel)
                or (first_channel_claim and _channel_has_prior_context(request))
            ) and recovery_state is None:
                # This process has no Model for a channel it has seen before. Rebuild it from the
                # channel record and its checkpoint, or explain specifically why that is unsafe.
                restored_model, refusal, restoration_notices = await self._restore_channel_session(
                    request
                )
                if restored_model is None:
                    await self.send_complete(
                        request,
                        turn.response_key,
                        content=refusal,
                        complete_request=True,
                    )
                    return None
                # A restored Model has no emitter from an earlier turn. Bind this request's, so any
                # lifecycle notice before the turn's own emitter (a set-aside card) reaches this
                # channel instead of the legacy task RPC.
                restored_model._response_emitter = ChatStreamEmitter(self, request)
                # Continue the rows this request created before the restart, so new blocks never
                # reuse (and overwrite) one of them.
                saved_rows = getattr(restored_model, "_restored_emitter_rows", None)
                restored_model._restored_emitter_rows = None
                restored_refusal = str(getattr(restored_model, "_restored_refusal_notice", "") or "")
                restored_model._restored_refusal_notice = ""
                restored_paused_calls = getattr(restored_model, "_restored_paused_calls", None)
                restored_model._restored_paused_calls = None
                if saved_rows is not None:
                    restored_model._response_emitter.import_rows(saved_rows)
                if restoration_notices:
                    await self.send_response(
                        request,
                        response_key=f"restoration:{request.RequestID}",
                        content="\n\n".join(restoration_notices),
                        status="complete",
                        complete=False,
                        metadata={},
                    )
            if slash is None and tool_discovery:
                existing = await self._rotate_auth_changed_session(request, existing_at_entry)
                if existing is not None:
                    await self._refresh_auth_context(existing, request)
                await self.send_complete(
                    request, turn.response_key,
                    content=render_tool_inventory(existing),
                    complete_request=True,
                )
                if first_channel_claim and existing_at_entry is None:
                    _NEW_CHANNEL_WITHOUT_SESSION.add(channel_identity)
                return None
            # Slash commands dispatch first — they operate on the existing session (if any) and don't
            # need a fresh Model.initialize(). A handled command sends its own terminal → return None.
            # Unknown structured slash commands also terminate locally; control input is never
            # reinterpreted as a model prompt.
            if slash is not None:
                existing = await get_channel_session(request)
                existing = await self._rotate_auth_changed_session(request, existing)
                if existing is not None:
                    await self._refresh_auth_context(existing, request)
                if await handle_slash(self, request, existing, f"slash:{request.RequestID}"):
                    if existing is not None:
                        # `/mode` changes routing outside Mythic's stored config; keep it restorable.
                        await self._persist_session_settings(request, existing)
                    if first_channel_claim and existing_at_entry is None:
                        _NEW_CHANNEL_WITHOUT_SESSION.add(channel_identity)
                    return None

            _NEW_CHANNEL_WITHOUT_SESSION.discard(channel_identity)
            model, preexisted = await self._get_or_create_model(request)
            if existing_at_entry is not None and not preexisted:
                # Config/auth rotation also loses the prior scope. A new Model may have
                # initialized, but it must not consume this old channel's next prompt.
                await drop_channel_session(request, expected_model=model)
                await self.send_complete(
                    request,
                    turn.response_key,
                    content=(
                        "Sage replaced this channel's session. Its prior scope and evidence "
                        "are not restored. Start a new Sage chat and state the current scope "
                        "and evidence before continuing. No action was issued."
                    ),
                    complete_request=True,
                )
                return None
            settings_notice = str(getattr(model, "_settings_continuation_notice", "") or "")
            if settings_notice:
                model._settings_continuation_notice = ""
                await self.send_response(
                    request,
                    response_key=f"settings:{request.RequestID}",
                    content=settings_notice,
                    status="complete",
                    complete=False,
                    metadata={},
                )
            thread_id = bind_channel_thread_id(request, model)
            has_input_response = getattr(request, "InputResponse", None) is not None
            retry_state = getattr(model, "_provider_retry_state", None)
            if retry_state is not None and preexisted and recovery_state is None and not has_input_response:
                retry_state.reset_for_new_request()
            controller_pending = isinstance(getattr(model, "_controller_hitl_pending", None), dict)
            hitl_pending = False
            hitl_probe_failed = False
            try:
                hitl_pending = await model._hitl_interrupt_pending(thread_id)
            except Exception:
                hitl_probe_failed = True
                logger.warning("HITL checkpoint probe failed; refusing implicit fresh-prompt resume", exc_info=True)

            if not has_input_response and preexisted and (
                controller_pending or hitl_pending or hitl_probe_failed
            ):
                await self._stop_and_close_request_lifecycles(
                    model,
                    status="stopped",
                    reason="resume_refused",
                )
                await drop_channel_session(request, expected_model=model)
                model, preexisted = await self._get_or_create_model(request)
                thread_id = bind_channel_thread_id(request, model)
                controller_pending = False
                hitl_pending = False
                hitl_probe_failed = False
            elif not has_input_response and hitl_probe_failed:
                # A newly-created model uses a collision-resistant checkpoint generation, so it cannot
                # legitimately have a pending interrupt. Failure to inspect that state is a storage/runtime
                # error; fail closed instead of invoking against an unknown checkpoint.
                await drop_channel_session(request, expected_model=model)
                raise RuntimeError("Unable to verify fresh Sage HITL checkpoint state.")

            saved_decision, saved_notice, saved_continuation = "", "", None
            if has_input_response and not hitl_probe_failed and not restored_refusal:
                saved_decision, saved_notice, saved_continuation = await self._resolve_saved_decision(
                    request, model
                )
                if saved_decision in {"stale", "hold"}:
                    # Never a terminal: the request belongs to the decision already handled, or
                    # its Continue/Stop card is still the operator's to answer.
                    await self.send_response(
                        request,
                        response_key=f"restoration:{request.RequestID}:{saved_decision}:{uuid4().hex}",
                        content=saved_notice,
                        status="complete",
                        complete=False,
                        complete_request=False,
                        metadata={},
                    )
                    return None
            if not has_input_response:
                model._pending_approval_context = None
            request_contract = _build_request_contract(
                request,
                model,
                has_input_response=has_input_response,
            )
            if recovery_state is not None:
                try:
                    from ai.langgraph.controller_recovery import binding_for_model, binding_digest
                except ImportError:  # pragma: no cover
                    from ..ai.langgraph.controller_recovery import binding_for_model, binding_digest
                original_request_id = str(recovery_state["original_request_id"])
                request_contract = replace(request_contract, request_id=original_request_id)
                recovered_binding = binding_for_model(model, request_contract)
                if (
                    request_contract.digest != recovery_state.get("request_contract_digest")
                    or binding_digest(recovered_binding) != recovery_store.load()["binding_digest"]
                    or str(getattr(model, "mode", "")) != "auto"
                ):
                    await drop_channel_session(request, expected_model=model)
                    await self.send_complete(
                        request, turn.response_key,
                        content="Sage recovery blocked: authenticated run binding changed. No controller action was issued.",
                        complete_request=True,
                    )
                    return None
                model._controller_resume_state = recovery_state
            begin_visibility = getattr(model, "begin_visibility_turn", None)
            if callable(begin_visibility):
                scope = f"chat:{request.ChannelID}:request:{request.RequestID}"
                if callable(getattr(model, "request_event_transcript", None)):
                    begin_visibility(
                        scope,
                        operator_prompt=prompt,
                        native_request_id=str(request.RequestID),
                        logical_request_id=request_contract.request_id,
                    )
                else:
                    begin_visibility(scope)
            install_request_contract = getattr(
                model,
                "install_request_contract",
                None,
            )
            if callable(install_request_contract):
                install_request_contract(request_contract)
            else:
                # Compatibility for lightweight test/eval models. Production Model instances install
                # the contract through the typed method above.
                model._request_contract = request_contract
            # Bind the engagement before the turn can read any target task output.
            # A later marker change makes this turn's collection ineligible.
            if getattr(model, "mode", None) in {"conversation", "supervised"}:
                from .report_identity import canonical_engagement_uuid
                current_engagement = await canonical_engagement_uuid(
                    getattr(getattr(model, "mythic_client", None), "client", None),
                    request.OperationID, create=True)
                previous_engagement = getattr(model, "_report_engagement_uuid", None)
                if previous_engagement is None:
                    model._report_engagement_uuid = current_engagement
                    if current_engagement is not None:
                        await self._persist_session_settings(request, model)
                model._report_engagement_at_turn = (
                    current_engagement if current_engagement == model._report_engagement_uuid else None)
            # Reassert on reused sessions too; older in-memory sessions created before this field was wired
            # should gain controller-native HITL without requiring a process restart.
            model.command_name = "chat"
            # Re-bind per-turn: the stream + card emitters are scoped to THIS request; the thread key is the
            # current collision-resistant channel generation. Never cache emitters across turns; an
            # approval resume of the same request only continues the previous emitter's row
            # bookkeeping. _hitl_card_pending is reset each turn; the interrupt surface sets it True when it emits a channel-release card,
            # so we then return None and let run_chat_turn skip its own terminal completion.
            stream_emitter = ChatStreamEmitter(
                self, request, continues=getattr(model, "_response_emitter", None)
            )
            model._response_emitter = stream_emitter
            retry_state = getattr(model, "_provider_retry_state", None)
            if retry_state is not None:
                async def publish_retry_event(event: dict[str, Any]) -> None:
                    phase = event.get("event")
                    await self.send_response(
                        request,
                        response_key=f"provider_retry:{request.RequestID}",
                        content=("Provider retry wait finished." if phase == "retry_wait_completed"
                                 else "Provider retry is waiting."),
                        complete=False,
                        complete_request=False,
                        metadata={
                            "runtime_telemetry": {"provider_retry": event["provider_retry"]},
                            "provider_retry_event_id": event["event_id"],
                        },
                    )
                    stream_emitter.note_created(f"provider_retry:{request.RequestID}")
                retry_state.bind_async_event_sink(publish_retry_event, asyncio.get_running_loop())
            await self._notify_bloodhound_degraded_once(model, request)
            await self._notify_autonomous_solve_ignored_once(model, request)

            def approval_context() -> dict[str, str]:
                authority = getattr(model, "_turn_authority", None)
                active_contract = getattr(model, "_request_contract", request_contract)
                return {
                    "thread_id": thread_id,
                    "turn_id": str(getattr(authority, "turn_id", "") or thread_id),
                    "request_id": active_contract.request_id,
                    "request_contract_digest": active_contract.digest,
                    "operation_id": str(request.OperationID),
                    "apitoken_id": str(request.APITokenID),
                }

            def pending_with_lifecycle(context: dict[str, Any]) -> dict[str, Any]:
                snapshot = getattr(model, "pending_lifecycle_snapshot", None)
                return pending_record(
                    context,
                    getattr(model, "_request_contract", None),
                    snapshot(int(request.RequestID)) if callable(snapshot) else None,
                )

            async def prepare_pending_approval(context: dict[str, Any]) -> None:
                # Durable before the card is exposed: a restart can then reattach this exact card
                # and continue its request lifecycle. A new card also ends any earlier claim, whose
                # action is already checkpointed.
                await self._update_session_record(
                    request,
                    model,
                    required=True,
                    pending=pending_with_lifecycle(context),
                    claim=None,
                )

            async def store_pending_approval(context: dict[str, Any]) -> None:
                model._pending_approval_context = dict(context)

            async def record_approval_receipt(receipt: dict[str, Any]) -> None:
                record = await load_channel_session_record(
                    int(request.OperationID), int(request.ChannelID)
                )
                claim = (record or {}).get("claim")
                if (
                    not isinstance(claim, dict)
                    or claim.get("status") != "claimed"
                    or claim.get("approval_id") != receipt.get("approval_id")
                    or record.get("thread_id") != getattr(model, "_thread_id_override", None)
                ):
                    return
                receipts = [*list(claim.get("receipts") or []), {
                    "task_display_id": receipt.get("task_display_id"),
                    "callback_display_id": receipt.get("callback_display_id"),
                    "command": str(receipt.get("command") or ""),
                }]
                record["claim"] = {**claim, "receipts": receipts}
                await save_channel_session_record(record)

            receipt_setter = getattr(getattr(model, "mythic_client", None), "set_approval_receipt_sink", None)
            if callable(receipt_setter):
                receipt_setter(record_approval_receipt)
            model._hitl_card_emitter = make_card_emitter(
                self,
                request,
                delegation_lookup=getattr(model, "_single_active_delegation", None),
                approval_context_lookup=approval_context,
                approval_context_store=store_pending_approval,
                decision_context_lookup=getattr(model, "_hitl_decision_context", None),
                approval_context_prepare=prepare_pending_approval,
            )
            model._hitl_card_pending = False
            set_active_agent = getattr(model, "set_active_agent", None)
            if callable(set_active_agent):
                set_active_agent("Supervisor")
            last_channel_metadata: dict[str, Any] | None = None

            async def publish_channel_metadata(*, force: bool = False) -> None:
                nonlocal last_channel_metadata
                channel_metadata = build_channel_metadata(model)
                if not force and channel_metadata == last_channel_metadata:
                    return
                try:
                    await turn.update_channel_metadata(channel_metadata)
                    last_channel_metadata = channel_metadata
                except Exception:
                    logger.debug("channel metadata update failed (non-fatal)", exc_info=True)

            async def metadata_heartbeat() -> None:
                while True:
                    await asyncio.sleep(_CHANNEL_METADATA_HEARTBEAT_SECONDS)
                    await publish_channel_metadata()

            try:
                from ai.mcp import MCPManager
            except ImportError:  # pragma: no cover
                from ..ai.mcp import MCPManager  # type: ignore
            execution_observer = getattr(model, "_emit_execution_event", None)
            observer_token = MCPManager.set_execution_observer(execution_observer)
            await publish_channel_metadata(force=True)
            metadata_task = asyncio.create_task(metadata_heartbeat())
            try:
                # Atomically claim the exact pending approval before the first resume await. Mythic can
                # dispatch requests for one channel concurrently; clearing here makes a replay/stale card
                # lose the claim instead of entering the same LangGraph checkpoint twice. A new card raised
                # during resume may safely install its own context without being erased afterward.
                approval_claimed = False
                approval_claim_context: dict[str, Any] = {}
                if (
                    has_input_response
                    and not hitl_probe_failed
                    and saved_decision != "stop"
                    and not restored_refusal
                    and (controller_pending or hitl_pending)
                    and approval_response_matches(
                        request,
                        getattr(model, "_pending_approval_context", None),
                    )
                ):
                    approval_claim_context = dict(model._pending_approval_context or {})
                    model._pending_approval_context = None
                    # Durable claim before any resume await. After a crash an unconsumed claim is
                    # never resumed again; its outcome is reconciled from exact task receipts.
                    await self._update_session_record(
                        request,
                        model,
                        required=True,
                        pending=None,
                        claim={
                            "approval_id": str(approval_claim_context.get("approval_id") or ""),
                            "request_id": str(approval_claim_context.get("request_id") or ""),
                            "action_digest": str(approval_claim_context.get("action_digest") or ""),
                            "decision": (
                                "approve"
                                if approved_action_ids_for_request(request, approval_claim_context)
                                else "deny"
                            ),
                            "status": "claimed",
                            "receipts": [],
                        },
                        **(
                            {"continuation": {
                                **saved_continuation,
                                "status": "used" if saved_decision == "continue" else "superseded",
                            }}
                            if saved_continuation is not None
                            and saved_decision in {"continue", "superseded"}
                            else {}
                        ),
                    )
                    approval_claimed = True
                stale_approval_response = bool(
                    has_input_response and not approval_claimed and saved_decision != "stop"
                    and not restored_refusal
                )
                approved_action_ids = (
                    approved_action_ids_for_request(
                        request,
                        approval_claim_context,
                    )
                    if approval_claimed
                    else ()
                )
                approval_decision = "approve" if approved_action_ids else "deny"
                claimed_open_tool_ids: tuple[str, ...] = ()
                if approval_claimed:
                    open_tool_ids = getattr(
                        model,
                        "_open_tool_lifecycle_ids",
                        None,
                    )
                    if callable(open_tool_ids):
                        claimed_open_tool_ids = tuple(open_tool_ids())
                approval_installed = False
                if approval_claimed:
                    apply_selection = getattr(
                        model,
                        "apply_request_action_selection",
                        None,
                    )
                    if callable(apply_selection):
                        approval_claim_context = apply_selection(
                            approval_claim_context,
                            approved_action_ids,
                        )
                    elif getattr(model, "mythic_client", None) is not None:
                        raise RuntimeError(
                            "Native supervised selection cannot reach the request contract."
                        )
                if approval_claimed and approval_decision == "approve":
                    install_claim = getattr(model, "install_approval_claim", None)
                    if callable(install_claim):
                        install_claim(approval_claim_context)
                        approval_installed = True
                    elif getattr(model, "mythic_client", None) is not None:
                        raise RuntimeError(
                            "Native supervised approval cannot reach the final effect sink."
                        )
                try:
                    if assessment_finding_id is not None and not has_input_response:
                        if str(getattr(model, "mode", "") or "") != "supervised":
                            native_response_text = (
                                "Finding assessment requires `supervised` mode. Run "
                                "`/mode supervised`, then submit the exact "
                                f"`assess {assessment_finding_id}` command. No action was issued."
                            )
                        else:
                            try:
                                finding_view, _ = await self._operation_memory().current_view(
                                    str(request.OperationID)
                                )
                                selected = next(
                                    (
                                        item
                                        for item in finding_view
                                        if item.finding_id == assessment_finding_id
                                    ),
                                    None,
                                )
                                native_response_text = (
                                    render_assessment_markdown(
                                        selected,
                                        str(request.OperationID),
                                    )
                                    if selected is not None
                                    else (
                                        f"`{assessment_finding_id}` is not an active finding in "
                                        f"operation `{request.OperationID}`. No action was issued."
                                    )
                                )
                            except Exception:
                                native_response_text = (
                                    "Operation findings are unavailable because Sage could not "
                                    "read the watcher-owned operation evidence index."
                                )
                    elif saved_decision == "stop":
                        # Nothing resumes: the paused work is retired in code with its history kept,
                        # and the request's restored lifecycle closes on the normal terminal path.
                        model._pending_approval_context = None
                        await self._retire_into_new_generation(request, model, STOPPED_CLOSURE)
                        native_response_text = saved_notice
                    elif restored_refusal:
                        # The restore already retired the paused work; end the request the same way.
                        native_response_text = restored_refusal
                    elif stale_approval_response:
                        await stream_emitter(
                            "That approval request is no longer active. No action was executed; "
                            "submit the instruction again if it is still needed."
                        )
                    elif has_input_response and controller_pending:
                        # Controller-native HITL is not a LangGraph checkpoint interrupt, so it has its own pending
                        # marker and resume seam. Native input cards still map accept -> approve, everything else
                        # -> deny, preserving the same default-deny policy.
                        native_response_text = _nonempty_native_response_text(
                            await model.handle_controller_hitl_resume(
                                approval_decision,
                                expected_action_digest=str(
                                    approval_claim_context.get("action_digest") or ""
                                ),
                                operator_message=resume_steer_message_for_request(request),
                            )
                        )
                    elif has_input_response and hitl_pending:
                        # A prior turn raised a confirmation card and finished; this request is the operator's
                        # answer. Resume the paused graph in place: Confirm → approve; Reject →
                        # default-deny; Respond/Select → deny the guarded action but steer the replan with the
                        # operator's free-text.
                        native_response_text = _nonempty_native_response_text(
                            await model.handle_hitl_resume(
                                approval_decision,
                                thread_id,
                                operator_message=resume_steer_message_for_request(request),
                                expected_action_digest=str(
                                    approval_claim_context.get("action_digest") or ""
                                ),
                                approved_action_ids=approved_action_ids,
                                selection_mode=str(
                                    approval_claim_context.get("selection_mode") or ""
                                ),
                            )
                        )
                    else:
                        if recovery_state is None:
                            # Max Steps caps each new request; an approval resume above keeps its request's
                            # budget, and a recovered autonomous run continues the one it belongs to.
                            reset_step_budget = getattr(model, "reset_step_budget", None)
                            if callable(reset_step_budget):
                                reset_step_budget()
                        native_response_text = _nonempty_native_response_text(
                            await (
                                model.resume_autonomous_controller()
                                if recovery_state is not None
                                else model.invoke(prompt, is_interactive=preexisted)
                            )
                        )
                finally:
                    if approval_installed:
                        clear_claim = getattr(model, "clear_approval_claim", None)
                        if callable(clear_claim):
                            clear_claim()
                if approval_claimed:
                    await self._mark_claim_consumed(
                        request, model, str(approval_claim_context.get("approval_id") or "")
                    )
                    if saved_decision == "superseded" and saved_continuation is not None:
                        await self._retire_control_row(request, model, saved_continuation)
                    if (
                        restored_paused_calls
                        and approval_decision == "approve"
                        and not getattr(model, "_stop_requested", False)
                    ):
                        unrun = _unrun_approved_tools(
                            model,
                            claimed_open_tool_ids,
                            {restored_paused_calls[a] for a in approved_action_ids
                             if a in restored_paused_calls},
                        )
                        if unrun:
                            # The claim is consumed, so nothing is resent; the error path closes
                            # the lifecycles and reports it.
                            raise RuntimeError(unrun_approved_action_error(unrun[0]))
                    # LangGraph does not emit a normal tool-end callback for a
                    # denied/unselected guarded call. Close only the tool events
                    # that remain open after resume; approved calls that already
                    # completed or errored are untouched.
                    close_open_tools = getattr(
                        model,
                        "_close_open_tool_lifecycles",
                        None,
                    )
                    if callable(close_open_tools):
                        await close_open_tools(
                            status="cancelled",
                            event_ids=claimed_open_tool_ids,
                        )
            except asyncio.CancelledError:
                # Operator cancel: cooperatively stop the graph so it stops issuing tasks, then re-raise
                # so the SDK emits the terminal `cancelled` status. Never swallow this.
                await self._stop_and_close_request_lifecycles(
                    model,
                    status="cancelled",
                    reason="operator",  # genuinely the operator: this is the cancel path
                )
                finalize_visibility = getattr(
                    model,
                    "finalize_visibility_turn",
                    None,
                )
                if (
                    callable(finalize_visibility)
                    and callable(getattr(model, "request_event_transcript", None))
                ):
                    reconciled = await finalize_visibility(require_final=True)
                    if not reconciled.get("ok", False):
                        logger.error(
                            "Cancelled request lifecycle reconciliation failed: %s",
                            reconciled,
                        )
                await drop_channel_session(request, expected_model=model)
                raise
            except Exception as error:
                # A graph/runtime exception can occur after a sub-agent card was already opened.
                # Without an explicit terminal update Mythic keeps that card on "Running" even though
                # run_chat_turn will emit an error terminal for the request itself.
                await self._stop_and_close_request_lifecycles(
                    model,
                    status="error",
                    reason="runtime_error",  # a fault in Sage, not an operator action
                )
                configuration_rejected = (
                    provider_configuration_rejection(error) is not None and not has_input_response
                )
                if not configuration_rejected:
                    # A provider refusal of this chat's route leaves the session and its history in
                    # place: correcting the configuration and sending again continues this channel.
                    await drop_channel_session(request, expected_model=model)
                record_terminal = getattr(model, "record_request_terminal", None)
                record_final = getattr(model, "record_final_response", None)
                record_projection = getattr(
                    model,
                    "record_final_response_projection",
                    None,
                )
                finalize_visibility = getattr(
                    model,
                    "finalize_visibility_turn",
                    None,
                )
                if all(callable(item) for item in (
                    record_terminal,
                    record_final,
                    record_projection,
                    finalize_visibility,
                )):
                    # Not str(error): a single-arg exception stringifies to its payload's
                    # repr, so an escaped langgraph control-flow exception would publish an
                    # entire Command(update={...}) as this turn's operator-facing error.
                    try:
                        from ai.langgraph.model import stop_notice_for
                        from ai.langgraph.operator_error import operator_error_text
                    except ImportError:  # pragma: no cover - packaged import fallback
                        from ..ai.langgraph.model import stop_notice_for  # type: ignore
                        from ..ai.langgraph.operator_error import (  # type: ignore
                            operator_error_text,
                        )
                    # A DELIBERATE halt (step budget exhausted, operator stop) arrives here as an
                    # argless exception, which would otherwise render as the bare "Sage request
                    # failed." and present an intentional stop as an unexplained crash, even after
                    # the requested work completed. When the exception states why it halted, render
                    # that reason through the same notice table `/stop` and session rotation use.
                    halt_reason = str(getattr(error, "stop_reason", "") or "")
                    if halt_reason:
                        error_text = stop_notice_for(
                            halt_reason,
                            str(getattr(error, "stop_detail", "") or ""),
                        ).strip()
                    else:
                        error_text = (
                            _resource_budget_notice(error)
                            or _provider_configuration_text(error, request)
                            or operator_error_text(error)
                            or "Sage request failed."
                        )
                    error_text = _with_task_provenance(model, error_text)
                    preterminal = await finalize_visibility(require_final=False)
                    if not preterminal.get("ok", False):
                        logger.error(
                            "Failed request lifecycle was already degraded: %s",
                            preterminal,
                        )
                    record_terminal("error")
                    response_key = turn.response_key
                    event_id = record_final(
                        error_text,
                        response_key=response_key,
                    )
                    control_transitions = getattr(
                        model,
                        "request_control_transitions",
                        None,
                    )
                    error_metadata = {
                        "channel_id": request.ChannelID,
                        "event_id": event_id,
                    }
                    if callable(control_transitions):
                        error_metadata["control_transitions"] = (
                            control_transitions()
                        )
                    await self.send_error(
                        request,
                        response_key,
                        error=error_text,
                        metadata=turn._metadata(error_metadata),
                        complete_request=True,
                    )
                    record_projection(event_id, response_key=response_key)
                    reconciled = await finalize_visibility(require_final=True)
                    if not reconciled.get("ok", False):
                        logger.error(
                            "Failed request lifecycle reconciliation failed: %s",
                            reconciled,
                        )
                    return None
                raise
            finally:
                metadata_task.cancel()
                with suppress(asyncio.CancelledError):
                    await metadata_task
                MCPManager.reset_execution_observer(observer_token)
                if callable(set_active_agent):
                    set_active_agent("Idle")
                await publish_channel_metadata()
            # Refresh the header's live count chips (MCP servers/tools, rounds, BloodHound) now that the
            # turn's work is done. The publisher de-duplicates unchanged payloads.
            await publish_channel_metadata()
            await self._retain_report_collection(request, model)
            if getattr(model, "_hitl_card_pending", False):
                # A confirmation card already released this request (complete_request=False). Returning None
                # tells run_chat_turn to send no terminal while the graph waits on disk.
                # Refresh the saved request state to its exact state at release; the write made
                # before the card was exposed stays in force if this one fails.
                released = getattr(model, "_pending_approval_context", None)
                if isinstance(released, dict):
                    try:
                        record = await load_channel_session_record(
                            int(request.OperationID), int(request.ChannelID)
                        )
                        saved = ((record or {}).get("pending") or {}).get("context") or {}
                        if (
                            saved.get("approval_id") == released.get("approval_id")
                            and record.get("thread_id") == getattr(model, "_thread_id_override", None)
                        ):
                            record["pending"] = pending_with_lifecycle(released)
                            await save_channel_session_record(record)
                    except Exception:
                        logger.warning("Saved request state was not refreshed at release", exc_info=True)
                return None
            runtime_telemetry = {}
            get_runtime_telemetry = getattr(model, "controller_runtime_telemetry", None)
            if callable(get_runtime_telemetry):
                runtime_telemetry = dict(get_runtime_telemetry() or {})
            terminal_metadata = {"channel_id": request.ChannelID}
            if runtime_telemetry:
                terminal_metadata["runtime_telemetry"] = runtime_telemetry
            # The terminal must land in the MAIN chat window. When the last streamed block was a
            # delegation/event (sub-agent) stream, its response_key points at the sub-agent panel —
            # collapsed by default — so keying the terminal there leaves the operator with no visible
            # final (the channel-3 "nothing in the main window" defect: the whole request ended inside
            # the Mythic_Operator panel). Reuse the last streamed key only when it is a main-window
            # assistant block; otherwise force the main turn key.
            #
            # Reuse it only while it is still the newest row the request created. Mythic orders the
            # transcript by row creation, so finalizing an older block put the request's checkpoint
            # above the progress messages and sub-agent panels that came after it. The key is chosen
            # only after the pre-terminal closes below, which can still create rows.
            _main_turn_key = f"assistant:{request.RequestID}:turn"

            def terminal_key() -> str:
                return stream_emitter.reusable_terminal_key() or _main_turn_key

            _last_streamed = stream_emitter.last_content
            if _last_streamed and _last_streamed.lstrip().startswith("Command("):
                _last_streamed = ""
            # A self-requested halt (loop-breaker, no-progress, step-budget, …) returns through this normal
            # terminal path; its status must not be recorded as `complete`, and its fallback narrative must
            # explain the halt rather than claim "Completed." (the channel-3 false-success defect).
            terminal_status = "complete"
            halt_notice = ""
            disposition = getattr(model, "request_terminal_disposition", None)
            if callable(disposition):
                terminal_status, halt_notice = disposition()
            # How the request really ended, recorded where Mythic keeps it: a halt's own status, else
            # the outcome the final report declared (completed / blocked / checkpoint).
            turn_outcome = terminal_status if terminal_status != "complete" else ""
            declared_outcome = getattr(model, "request_turn_outcome", None)
            if not turn_outcome and callable(declared_outcome):
                turn_outcome = declared_outcome()
            if turn_outcome:
                terminal_metadata["turn_outcome"] = turn_outcome
            response_content = (
                native_response_text
                or _last_streamed
                or halt_notice
                or "Completed."
            )
            # Model prose is not the provenance authority. Apply one deterministic
            # request/task-lineage composer to every service-owned terminal narrative, including
            # normal, handled-error, and lifecycle stop/cancel paths.
            response_content = _with_task_provenance(model, response_content)
            record_final = getattr(model, "record_final_response", None)
            record_terminal = getattr(model, "record_request_terminal", None)
            record_projection = getattr(
                model,
                "record_final_response_projection",
                None,
            )
            finalize_visibility = getattr(model, "finalize_visibility_turn", None)
            lifecycle_event_id = ""
            if callable(record_final) and callable(finalize_visibility):
                # A guarded tool that never executes never receives a tool-end callback, so
                # its `started` ledger event has no terminal and reconciliation below fails — turning
                # a request that behaved correctly into `status=error` for the operator. Two paths
                # produce one: an operator rejection, and a call the turn-authority gate strips before
                # execution. The cancelled and failed paths already close these; the NORMAL terminal
                # path did not.
                #
                # Safe to close unconditionally *here*: this branch runs only once the request is
                # terminating with a final response. A card still awaiting an operator decision
                # releases the channel and returns before reaching this point, so nothing pending can
                # be closed out from under the operator.
                #
                # Delegations need the same close: a refusal that ends the request mid-delegation (an
                # unplannable capability, an identical re-proposal stop) leaves the specialist's
                # delegation open, and the reconciliation below raised on it.
                close_open = getattr(model, "_close_all_request_lifecycles", None)
                if not callable(close_open):
                    close_open = getattr(model, "_close_open_tool_lifecycles", None)
                if callable(close_open):
                    await close_open(status="stopped")

                preterminal = await finalize_visibility(require_final=False)
                if not preterminal.get("ok", False):
                    raise RuntimeError(
                        "Request lifecycle reconciliation failed before terminal response."
                    )
                if callable(record_terminal):
                    record_terminal(terminal_status)
                # Closing a delegation above creates a row when its close content is new text (a
                # refusal appended to the specialist's streamed text), so the key is chosen only now.
                response_key = terminal_key()
                lifecycle_event_id = record_final(
                    response_content,
                    response_key=response_key,
                )
                terminal_metadata["event_id"] = lifecycle_event_id
                control_transitions = getattr(
                    model,
                    "request_control_transitions",
                    None,
                )
                if callable(control_transitions):
                    terminal_metadata["control_transitions"] = (
                        control_transitions()
                    )
            else:
                # A model without the request ledger closes nothing here. Its key is chosen before the
                # reconcile, whose failure warning must stay a separate block, not be overwritten.
                response_key = terminal_key()
                if callable(finalize_visibility):
                    await finalize_visibility()
            # Mythic stores only complete, error, and cancelled as a request's terminal status and rewrites
            # any other status sent with complete=True to `complete`. So a self-halt (`stopped`) still
            # terminates as `complete` here; its real ending rides in the terminal's `turn_outcome`
            # metadata and in the control-transition ledger, and its content is the halt notice.
            if terminal_status not in _MYTHIC_TERMINAL_REQUEST_STATUSES:
                terminal_status = "complete"
            if terminal_status == "complete":
                await self.send_complete(
                    request,
                    response_key,
                    metadata=turn._metadata(terminal_metadata),
                    content=response_content,
                    complete_request=True,
                )
            else:
                await self.send_response(
                    request,
                    response_key,
                    content=response_content,
                    complete=True,
                    complete_request=True,
                    status=terminal_status,
                    metadata=turn._metadata(terminal_metadata),
                )
            if lifecycle_event_id and callable(record_projection):
                record_projection(
                    lifecycle_event_id,
                    response_key=response_key,
                )
                reconciled = await finalize_visibility(require_final=True)
                if not reconciled.get("ok", False):
                    logger.error(
                        "Request lifecycle reconciliation failed after terminal response: %s",
                        reconciled,
                    )
            return None

        async def _handler(turn) -> dict[str, Any] | None:
            # `/stop` must bypass serialization so it can cancel a long-running turn that currently
            # owns the channel lock. Every other request is serialized because Model state, graph,
            # authority, checkpoint resume, and emitters are channel-scoped mutable state.
            slash = getattr(request, "SlashCommand", None)
            slash_name = str(getattr(slash, "Name", "") or "").lower().lstrip("/")
            if slash_name == "stop":
                existing = await get_channel_session(request)
                if await handle_slash(self, request, existing, f"slash:{request.RequestID}"):
                    return None
            key = channel_session_key(request)
            lock = _CHANNEL_TURN_LOCKS.setdefault(key, asyncio.Lock())
            async with lock:
                try:
                    return await _serialized_handler(turn)
                except asyncio.CancelledError:
                    # Cancellation can land after invoke() returns, while channel metadata
                    # or the normal terminal response is still awaiting publication. That
                    # bypasses the invoke-local cancellation handler above.
                    model = await get_channel_session(request)
                    emitter = getattr(model, "_response_emitter", None)
                    ledger = getattr(model, "_request_event_ledger", None)
                    terminal_events = (
                        ledger.actual_events(kind="control_transition", phase="request_terminal")
                        if callable(getattr(ledger, "actual_events", None)) else ()
                    )
                    if (
                        model is not None
                        and isinstance(emitter, ChatStreamEmitter)
                        and emitter._request is request
                        and not terminal_events
                    ):
                        async def finish_cancel() -> None:
                            await self._stop_and_close_request_lifecycles(
                                model, status="cancelled", reason="operator",
                            )
                            reconciler = getattr(model, "finalize_visibility_turn", None)
                            if callable(reconciler):
                                report = await reconciler(require_final=True)
                                if not report.get("ok", False):
                                    logger.error(
                                        "Cancelled request lifecycle reconciliation failed: %s",
                                        report,
                                    )
                            await drop_channel_session(request, expected_model=model)

                        cleanup = asyncio.create_task(finish_cancel())
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            await cleanup
                    raise

        await self.run_chat_turn(
            request,
            _handler,
            response_key=f"assistant:{request.RequestID}:turn",
            model=request.Model,
            complete_content="Completed.",
        )
