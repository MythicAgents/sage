"""``SageChat`` — the native Mythic v4.0.0 chat container fronting Sage's ``Model`` runtime.

Singleton (instantiated once at startup); all per-conversation state lives in the channel-keyed
session registry, never on ``self`` (PRD Section 5). One ``chat()`` call = one Mythic request =
exactly one terminal status.

Always-terminal (the safety-critical invariant, Section 6): every request path ends with a
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
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from mythic_container.ChatBase import Chat, ChatRequest
from mythic_container.logging import logger

from .config import build_model_kwargs
from .hitl import (
    approval_response_matches,
    make_card_emitter,
    resume_decision_for_request,
    resume_steer_message_for_request,
)
from .metadata import build_channel_metadata
from .models import SAGE_MODELS
from .session import (
    bind_channel_thread_id,
    channel_session_key,
    drop_channel_session,
    get_channel_session,
    put_channel_session,
)
from .slash import handle_slash
from .streaming import ChatStreamEmitter


_CHANNEL_METADATA_HEARTBEAT_SECONDS = 2.0
_CHANNEL_TURN_LOCKS: dict[str, asyncio.Lock] = {}


def _nonempty_native_response_text(value: Any) -> str:
    """Return the model's real terminal text when a quiet turn produced one."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text if text.strip() else ""


def _model_config_signature(kwargs: dict[str, Any]) -> str:
    """Bind a reusable session to the exact resolved ChatRequest constructor config."""
    encoded = json.dumps(kwargs, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
        override_mode in {"supervised", "auto"}
        and stored_signature
        and stored_signature == resolved_signature == override_signature
        and override_base_autonomy is not None
        and bool(override_base_autonomy) == expected_autonomy
        and base_autonomy == expected_autonomy
    ):
        expected_mode = override_mode
        expected_autonomy = True if override_mode == "auto" else bool(override_base_autonomy)
    return bool(
        str(getattr(model, "mode", "")) == expected_mode
        and bool(getattr(model, "_autonomous_solve", False))
        == expected_autonomy
        and str(getattr(model, "policy_mode", "")) == str(kwargs.get("policy_mode", ""))
        and int(getattr(model, "_max_steps", -1)) == int(kwargs.get("max_steps", -2))
    )


class SageChat(Chat):
    name = "sage"
    description = "Sage — AI red-team operator assistant (native Mythic v4.0.0 chat container)."
    semver = "4.0.0"
    # rc5: the chat container's service icon (light + dark). Only sage.svg exists today, so it serves both
    # until a dedicated dark variant lands. Path is resolved from this module (…/sage_chat/) up to the sage root.
    agent_icon_path = str(Path(__file__).resolve().parent.parent / "sage.svg")
    dark_mode_agent_icon_path = str(Path(__file__).resolve().parent.parent / "sage.svg")
    models = SAGE_MODELS

    @staticmethod
    def _bloodhound_connection_locally_pinned(server: Any) -> bool:
        try:
            from ai.mcp import MCPManager
        except ImportError:  # pragma: no cover
            from ..ai.mcp import MCPManager  # type: ignore
        return bool(MCPManager.is_bloodhound_server(server))

    async def _ensure_bloodhound_connected(self, *, autonomous_required: bool = False) -> bool:
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
                ensure_bloodhound_connected,
            )
        except ImportError:  # pragma: no cover
            from ..ai.bloodhound_config import (  # type: ignore
                bloodhound_tool_admission,
                ensure_bloodhound_connected,
            )
        try:
            connected, message = await ensure_bloodhound_connected()
            logger.info(f"BloodHound auto-connect (chat): {message}")
            admission = bloodhound_tool_admission()
            admitted = bool(
                connected
                and admission.get("ready")
                and self._bloodhound_connection_locally_pinned(admission.get("server"))
            )
            if autonomous_required and not admitted:
                raise RuntimeError(
                    (
                        "BloodHound MCP is not bound to SAGE_BLOODHOUND_MCP_DIR and the configured launcher."
                        if connected and admission.get("ready")
                        else admission.get("reason")
                    )
                    or "BloodHound MCP exact-tool admission failed for autonomous chat."
                )
            return admitted
        except Exception as exc:
            if autonomous_required:
                raise RuntimeError(
                    f"Autonomous native chat requires BloodHound MCP exact-tool admission before "
                    f"Model.initialize(): {exc}"
                ) from exc
            logger.debug(f"BloodHound auto-connect (chat) skipped: {exc}")
            return False

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

    async def _rotate_auth_changed_session(self, request: ChatRequest, model: Any | None) -> Any | None:
        if model is None:
            return None
        if (
            getattr(model, "apitoken_id", None) == request.APITokenID
            and getattr(model, "operation_id", None) == request.OperationID
        ):
            return model
        try:
            model.request_stop()
        except Exception:
            logger.debug("request_stop() failed while rotating changed Mythic auth identity", exc_info=True)
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
        config_signature = _model_config_signature(kwargs)
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
            try:
                existing.request_stop()
            except Exception:
                logger.debug("request_stop() failed while rotating changed chat config", exc_info=True)
            await drop_channel_session(request, expected_model=existing)
            logger.info("Rotated Sage channel session after ChatRequest configuration changed")
            existing = None
        if existing is not None:
            existing._native_chat_explicit_hitl = True
            await self._refresh_auth_context(existing, request)
            autonomous_now = bool(getattr(existing, "_autonomous_solve", False))
            if autonomous_now:
                admitted = await self._ensure_bloodhound_connected(autonomous_required=True)
                if not getattr(existing, "_bloodhound_exact_admission_at_initialize", False):
                    raise RuntimeError(
                        "Autonomous native chat requires a fresh channel/session because this "
                        "session graph initialized without BloodHound exact-tool admission."
                    )
                if not admitted:
                    raise RuntimeError(
                        "Autonomous native chat requires BloodHound MCP exact-tool admission on every turn."
                    )
            return existing, True

        # Lazy import: keep the heavy LangGraph/LangChain import off the module load path so the pure
        # config/streaming/models modules (and their tests) don't pull it in.
        try:
            from ai.langgraph.model import Model
        except ImportError:  # pragma: no cover
            from ..ai.langgraph.model import Model  # type: ignore

        model = Model(**kwargs)
        model._chat_request_config_signature = config_signature
        model._chat_request_base_autonomous_solve = bool(kwargs.get("autonomous_solve"))
        model._chat_mode_override = ""
        model._chat_mode_override_base_signature = ""
        model._chat_mode_override_base_autonomous_solve = None
        # Native chat is a real interactive approval transport. Set this before graph construction so any
        # runtime checks see the same command identity as the legacy `chat` task path.
        model.command_name = "chat"
        model._native_chat_explicit_hitl = True
        bind_channel_thread_id(request, model)
        # Auto-connect the BloodHound MCP BEFORE the graph is built — Model.initialize() wires the
        # BloodHound agent's tools from the currently-connected MCP servers, so a later connect wouldn't
        # be seen by this session's graph. Mirrors the legacy task path's ensure_bloodhound_task_preflight
        # (which sage_chat previously omitted, so chat sessions never auto-connected BloodHound at all).
        admitted_at_initialize = await self._ensure_bloodhound_connected(
            autonomous_required=bool(kwargs.get("autonomous_solve"))
        )
        model._bloodhound_exact_admission_at_initialize = bool(admitted_at_initialize)
        await model.initialize()
        # The chat container always runs at full detail — the collapsible tool cards ARE the "verbose"
        # view, so there is no operator verbose toggle (removed). set_verbose(True) also enables the local
        # tmux stream log; user-facing tool detail always renders as cards regardless of this flag.
        model.set_verbose(True)
        await put_channel_session(request, model)
        return model, False

    async def chat(self, request: ChatRequest) -> None:
        prompt = request.Prompt or ""

        async def _serialized_handler(turn) -> dict[str, Any] | None:
            model: Any | None = None
            native_response_text = ""
            # Slash commands dispatch first — they operate on the existing session (if any) and don't
            # need a fresh Model.initialize(). A handled command sends its own terminal → return None.
            # An undeclared/unhandled command falls through to normal prompt handling.
            if getattr(request, "SlashCommand", None) is not None:
                existing = await get_channel_session(request)
                existing = await self._rotate_auth_changed_session(request, existing)
                if existing is not None:
                    await self._refresh_auth_context(existing, request)
                if await handle_slash(self, request, existing, f"slash:{request.RequestID}"):
                    return None

            model, preexisted = await self._get_or_create_model(request)
            thread_id = bind_channel_thread_id(request, model)
            has_input_response = getattr(request, "InputResponse", None) is not None
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
                try:
                    model.request_stop()
                except Exception:
                    logger.debug("request_stop() failed while invalidating stale HITL session", exc_info=True)
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

            if not has_input_response:
                model._pending_approval_context = None
            # Reassert on reused sessions too; older in-memory sessions created before this field was wired
            # should gain controller-native HITL without requiring a process restart.
            model.command_name = "chat"
            # Re-bind per-turn: the stream + card emitters are scoped to THIS request; the thread key is the
            # current collision-resistant channel generation. Never cache emitters across turns. _hitl_card_pending
            # is reset each turn; the interrupt surface sets it True when it emits a channel-release card,
            # so we then return None and let run_chat_turn skip its own terminal completion.
            stream_emitter = ChatStreamEmitter(self, request)
            model._response_emitter = stream_emitter

            def approval_context() -> dict[str, str]:
                authority = getattr(model, "_turn_authority", None)
                return {
                    "thread_id": thread_id,
                    "turn_id": str(getattr(authority, "turn_id", "") or thread_id),
                    "operation_id": str(request.OperationID),
                    "apitoken_id": str(request.APITokenID),
                }

            model._hitl_card_emitter = make_card_emitter(
                self,
                request,
                delegation_lookup=getattr(model, "_single_active_delegation", None),
                approval_context_lookup=approval_context,
                approval_context_store=lambda context: setattr(
                    model, "_pending_approval_context", dict(context)
                ),
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

            begin_visibility = getattr(model, "begin_visibility_turn", None)
            if callable(begin_visibility):
                begin_visibility(
                    f"chat:{request.ChannelID}:request:{request.RequestID}"
                )
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
                    and (controller_pending or hitl_pending)
                    and approval_response_matches(
                        request,
                        getattr(model, "_pending_approval_context", None),
                    )
                ):
                    approval_claim_context = dict(model._pending_approval_context or {})
                    model._pending_approval_context = None
                    approval_claimed = True
                stale_approval_response = bool(has_input_response and not approval_claimed)
                if stale_approval_response:
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
                            resume_decision_for_request(request),
                            expected_action_digest=str(
                                approval_claim_context.get("action_digest") or ""
                            ),
                        )
                    )
                elif has_input_response and hitl_pending:
                    # A prior turn raised a confirmation card and finished; this request is the operator's
                    # answer. Resume the paused graph in place (Section 6): Confirm → approve; Reject →
                    # default-deny; Respond/Select → deny the guarded action but steer the replan with the
                    # operator's free-text (Phase 3).
                    native_response_text = _nonempty_native_response_text(
                        await model.handle_hitl_resume(
                            resume_decision_for_request(request), thread_id,
                            operator_message=resume_steer_message_for_request(request),
                            expected_action_digest=str(
                                approval_claim_context.get("action_digest") or ""
                            ),
                        )
                    )
                else:
                    native_response_text = _nonempty_native_response_text(
                        await model.invoke(prompt, is_interactive=preexisted)
                    )
            except asyncio.CancelledError:
                # Operator cancel: cooperatively stop the graph so it stops issuing tasks, then re-raise
                # so the SDK emits the terminal `cancelled` status (Cody, f). Never swallow this.
                if not getattr(model, "_stop_requested", False):
                    try:
                        model.request_stop()
                    except Exception:
                        logger.warning("request_stop() failed during cancel handling", exc_info=True)
                await drop_channel_session(request, expected_model=model)
                raise
            except Exception:
                # A graph/runtime exception can occur after a sub-agent card was already opened.
                # Without an explicit terminal update Mythic keeps that card on "Running" even though
                # run_chat_turn will emit an error terminal for the request itself.
                close_all = getattr(model, "_close_all_delegations", None)
                if callable(close_all):
                    try:
                        await close_all(status="error")
                    except Exception:
                        logger.debug("sub-agent error cleanup failed (non-fatal)", exc_info=True)
                await drop_channel_session(request, expected_model=model)
                raise
            finally:
                metadata_task.cancel()
                with suppress(asyncio.CancelledError):
                    await metadata_task
                MCPManager.reset_execution_observer(observer_token)
                if callable(set_active_agent):
                    set_active_agent("Idle")
                await publish_channel_metadata()
            finalize_visibility = getattr(model, "finalize_visibility_turn", None)
            if callable(finalize_visibility):
                await finalize_visibility()
            # Refresh the header's live count chips (MCP servers/tools, rounds, BloodHound) now that the
            # turn's work is done. The publisher de-duplicates unchanged payloads.
            await publish_channel_metadata()
            if getattr(model, "_hitl_card_pending", False):
                # A confirmation card already released this request (complete_request=False). Returning None
                # tells run_chat_turn to send no terminal while the graph waits on disk.
                return None
            runtime_telemetry = {}
            get_runtime_telemetry = getattr(model, "controller_runtime_telemetry", None)
            if callable(get_runtime_telemetry):
                runtime_telemetry = dict(get_runtime_telemetry() or {})
            terminal_metadata = {"channel_id": request.ChannelID}
            if runtime_telemetry:
                terminal_metadata["runtime_telemetry"] = runtime_telemetry
            if native_response_text or stream_emitter.last_response_key:
                # Reuse the final visible block's key so Mythic updates it in place instead of creating
                # a separate empty timestamp row for the request terminal. A distinct return from invoke
                # or HITL resume is authoritative terminal content even when progress was streamed first.
                await self.send_complete(
                    request,
                    stream_emitter.last_response_key or turn.response_key,
                    metadata=turn._metadata(terminal_metadata),
                    content=native_response_text or stream_emitter.last_content,
                    complete_request=True,
                )
                return None
            # No assistant text was emitted. Let run_chat_turn create a visible fallback terminal.
            return terminal_metadata

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
                return await _serialized_handler(turn)

        await self.run_chat_turn(
            request,
            _handler,
            response_key=f"assistant:{request.RequestID}:turn",
            model=request.Model,
            complete_content="Completed.",
        )
