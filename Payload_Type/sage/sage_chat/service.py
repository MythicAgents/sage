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
from pathlib import Path
from typing import Any

from mythic_container.ChatBase import Chat, ChatRequest
from mythic_container.logging import logger

from .config import build_model_kwargs
from .hitl import make_card_emitter, resume_decision_for_request, resume_steer_message_for_request
from .metadata import build_channel_metadata
from .models import SAGE_MODELS
from .session import channel_session_key, get_channel_session, put_channel_session
from .slash import handle_slash
from .streaming import ChatStreamEmitter


class SageChat(Chat):
    name = "sage"
    description = "Sage — AI red-team operator assistant (native Mythic v4.0.0 chat container)."
    semver = "4.0.0"
    # rc5: the chat container's service icon (light + dark). Only sage.svg exists today, so it serves both
    # until a dedicated dark variant lands. Path is resolved from this module (…/sage_chat/) up to the sage root.
    agent_icon_path = str(Path(__file__).resolve().parent.parent / "sage.svg")
    dark_mode_agent_icon_path = str(Path(__file__).resolve().parent.parent / "sage.svg")
    models = SAGE_MODELS

    async def _ensure_bloodhound_connected(self) -> None:
        """Lazily auto-connect the BloodHound MCP on the chat-request path (fail-soft, idempotent).

        Connected here rather than at container boot on purpose: the MCP stdio session is bound to the
        event loop that creates it, so it must be opened from the same serving loop that later runs the
        graph (see ``ai/bloodhound_config.ensure_bloodhound_connected``). Process-global, so only the
        first channel to reach this actually connects; all later sessions no-op. Needs
        ``SAGE_BLOODHOUND_MCP_DIR`` (or an explicit dir) to locate the MCP server — otherwise it no-ops.
        """
        try:
            from ai.bloodhound_config import ensure_bloodhound_connected
        except ImportError:  # pragma: no cover
            from ..ai.bloodhound_config import ensure_bloodhound_connected  # type: ignore
        try:
            _connected, message = await ensure_bloodhound_connected()
            logger.info(f"BloodHound auto-connect (chat): {message}")
        except Exception as exc:  # never let BloodHound block a chat turn
            logger.debug(f"BloodHound auto-connect (chat) skipped: {exc}")

    async def _refresh_auth_context(self, model: Any, request: ChatRequest) -> None:
        """Re-bind the per-turn Mythic auth context on a reused session (Cody, c).

        ``APITokenID`` (and Config) can change on any request while the ``Model`` persists per channel.
        When the channel bot token changes, update the context and re-login ``MythicTools`` so tools use
        the current token/scope instead of a stale one. No-op when nothing changed.
        """
        if getattr(model, "apitoken_id", None) == request.APITokenID:
            return
        model.apitoken_id = request.APITokenID
        model.operation_id = request.OperationID
        client = getattr(model, "mythic_client", None)
        if client is not None:
            client.apitoken_id = request.APITokenID
            client.operation_id = request.OperationID
            client.channel_id = request.ChannelID
            try:
                await client.login()
            except Exception:
                logger.warning("MythicTools re-login after token change failed; tools may be stale", exc_info=True)

    async def _get_or_create_model(self, request: ChatRequest) -> tuple[Any, bool]:
        """Return ``(model, preexisted)`` for this channel.

        A found session is reused (multi-turn continuity via the checkpointer). Config and token can
        change on any request (Cody, c) — for Phase 1 we re-bind the per-turn emitter and thread key
        in ``chat()`` on every turn; full per-turn config/token refresh lands with the Phase 2 auth
        rewrite. Isolated here so unit tests can inject a stub model without a live LLM.
        """
        existing = await get_channel_session(request)
        if existing is not None:
            await self._refresh_auth_context(existing, request)
            return existing, True

        # Lazy import: keep the heavy LangGraph/LangChain import off the module load path so the pure
        # config/streaming/models modules (and their tests) don't pull it in.
        try:
            from ai.langgraph.model import Model
        except ImportError:  # pragma: no cover
            from ..ai.langgraph.model import Model  # type: ignore

        kwargs = build_model_kwargs(request)
        model = Model(**kwargs)
        # Native chat is a real interactive approval transport. Set this before graph construction so any
        # runtime checks see the same command identity as the legacy `chat` task path.
        model.command_name = "chat"
        model._thread_id_override = channel_session_key(request)
        # Auto-connect the BloodHound MCP BEFORE the graph is built — Model.initialize() wires the
        # BloodHound agent's tools from the currently-connected MCP servers, so a later connect wouldn't
        # be seen by this session's graph. Mirrors the legacy task path's ensure_bloodhound_task_preflight
        # (which sage_chat previously omitted, so chat sessions never auto-connected BloodHound at all).
        await self._ensure_bloodhound_connected()
        await model.initialize()
        # The chat container always runs at full detail — the collapsible tool cards ARE the "verbose"
        # view, so there is no operator verbose toggle (removed). set_verbose(True) also enables the local
        # tmux stream log; user-facing tool detail always renders as cards regardless of this flag.
        model.set_verbose(True)
        await put_channel_session(request, model)
        return model, False

    async def chat(self, request: ChatRequest) -> None:
        prompt = request.Prompt or ""

        async def _handler(turn) -> dict[str, Any] | None:
            # Slash commands dispatch first — they operate on the existing session (if any) and don't
            # need a fresh Model.initialize(). A handled command sends its own terminal → return None.
            # An undeclared/unhandled command falls through to normal prompt handling.
            if getattr(request, "SlashCommand", None) is not None:
                existing = await get_channel_session(request)
                if await handle_slash(self, request, existing, f"slash:{request.RequestID}"):
                    return None

            model, preexisted = await self._get_or_create_model(request)
            thread_id = channel_session_key(request)
            # Reassert on reused sessions too; older in-memory sessions created before this field was wired
            # should gain controller-native HITL without requiring a process restart.
            model.command_name = "chat"
            # Re-bind per-turn: the stream + card emitters are scoped to THIS request; the thread key is the
            # stable channel id. Never cache emitters across turns (Section 7 / Cody c). _hitl_card_pending
            # is reset each turn; the interrupt surface sets it True when it emits a channel-release card,
            # so we then return None and let run_chat_turn skip its own terminal completion.
            stream_emitter = ChatStreamEmitter(self, request)
            model._response_emitter = stream_emitter
            model._hitl_card_emitter = make_card_emitter(
                self,
                request,
                delegation_lookup=getattr(model, "_single_active_delegation", None),
            )
            model._hitl_card_pending = False
            model._thread_id_override = thread_id
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
            try:
                if isinstance(getattr(model, "_controller_hitl_pending", None), dict):
                    # Controller-native HITL is not a LangGraph checkpoint interrupt, so it has its own pending
                    # marker and resume seam. Native input cards still map accept -> approve, everything else
                    # -> deny, preserving the same default-deny policy.
                    await model.handle_controller_hitl_resume(resume_decision_for_request(request))
                elif await model._hitl_interrupt_pending(thread_id):
                    # A prior turn raised a confirmation card and finished; this request is the operator's
                    # answer. Resume the paused graph in place (Section 6): Confirm → approve; Reject →
                    # default-deny; Respond/Select → deny the guarded action but steer the replan with the
                    # operator's free-text (Phase 3).
                    await model.handle_hitl_resume(
                        resume_decision_for_request(request), thread_id,
                        operator_message=resume_steer_message_for_request(request),
                    )
                else:
                    await model.invoke(prompt, is_interactive=preexisted)
            except asyncio.CancelledError:
                # Operator cancel: cooperatively stop the graph so it stops issuing tasks, then re-raise
                # so the SDK emits the terminal `cancelled` status (Cody, f). Never swallow this.
                try:
                    model.request_stop()
                except Exception:
                    logger.warning("request_stop() failed during cancel handling", exc_info=True)
                raise
            finally:
                MCPManager.reset_execution_observer(observer_token)
            finalize_visibility = getattr(model, "finalize_visibility_turn", None)
            if callable(finalize_visibility):
                await finalize_visibility()
            # Refresh the header's live count chips (MCP servers/tools, rounds, BloodHound) now that the
            # turn's work is done. Fire-and-forget: a header update must never fail a chat turn.
            try:
                await turn.update_channel_metadata(build_channel_metadata(model))
            except Exception:
                logger.debug("channel metadata update failed (non-fatal)", exc_info=True)
            if getattr(model, "_hitl_card_pending", False):
                # A confirmation card already released this request (complete_request=False). Returning None
                # tells run_chat_turn to send no terminal while the graph waits on disk.
                return None
            if stream_emitter.last_response_key:
                # Reuse the final visible block's key so Mythic updates it in place instead of creating
                # a separate empty timestamp row for the request terminal.
                await self.send_complete(
                    request,
                    stream_emitter.last_response_key,
                    metadata=turn._metadata({"channel_id": request.ChannelID}),
                    content=stream_emitter.last_content,
                    complete_request=True,
                )
                return None
            # No assistant text was emitted. Let run_chat_turn create a visible fallback terminal.
            return {"channel_id": request.ChannelID}

        await self.run_chat_turn(
            request,
            _handler,
            response_key=f"assistant:{request.RequestID}:turn",
            model=request.Model,
            complete_content="Completed.",
        )
