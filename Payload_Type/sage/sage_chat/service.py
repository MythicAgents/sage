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

from mythic_container.ChatBase import (
    Chat,
    ChatRequest,
    ContainerOnStartMessage,
    ContainerOnStartMessageResponse,
)
from mythic_container.logging import logger

from .config import build_bloodhound_env, build_model_kwargs
from .findings_watcher import FindingsWatcherManager, render_watcher_status
from .hitl import (
    approved_action_ids_for_request,
    approval_response_matches,
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
from .session import (
    bind_channel_thread_id,
    channel_session_key,
    drop_channel_session,
    get_channel_session,
    put_channel_session,
)
from .slash import handle_slash
from .streaming import ChatStreamEmitter
from .watcher_control import WatcherControlBoundaryError
from .watcher_graph import build_watcher_graph, render_watcher_explanation


_CHANNEL_METADATA_HEARTBEAT_SECONDS = 2.0
_CHANNEL_TURN_LOCKS: dict[str, asyncio.Lock] = {}
_WATCHER_TURN_TASKS: dict[tuple[int, int], asyncio.Task[Any]] = {}


def _nonempty_native_response_text(value: Any) -> str:
    """Return the model's real terminal text when a quiet turn produced one."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text if text.strip() else ""


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


def _bloodhound_unavailable_message(reason: str | None = None) -> str:
    """Compose the operator-facing autonomous refusal, with a fallback that still names BloodHound.

    Lazy import to match this module's convention of keeping `ai.*` off the import path. The
    fallback matters: if the import fails, the refusal must still say what is wrong rather than
    reverting to an internal phrase like "exact-tool admission", which is the defect D6 fixes.
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
    semver = "4.0.0"
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

    async def on_container_start(
        self, message: ContainerOnStartMessage
    ) -> ContainerOnStartMessageResponse:
        watcher = self._findings_watcher()
        try:
            await watcher.restore_operation(
                message.OperationID,
                server_name=message.ServerName,
                bootstrap_token=message.APIToken,
            )
        except Exception as exc:
            return ContainerOnStartMessageResponse(
                ContainerName=self.name,
                EventLogErrorMessage=(
                    "Sage findings watcher startup failed before scheduling: "
                    f"{type(exc).__name__}."
                ),
            )
        return ContainerOnStartMessageResponse(
            ContainerName=self.name,
            EventLogInfoMessage=(
                "Sage Watcher startup profile reconciliation completed for operation "
                f"{message.OperationID}; use /watcher status to inspect the exact state."
            ),
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
            return admitted
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

        `reason` defaults to session rotation rather than to an operator stop, because **no caller of
        this helper is an operator pressing stop** — they are identity/config rotation and refused
        resumes. It previously emitted "Session stopped by operator" on all of them, which sent
        operators looking for a stop they never issued.
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
                # Fail-soft keep-warm. Previously only the autonomous branch attempted this, so a
                # reused conversation/supervised session never tried to connect BloodHound at all —
                # an operator who configured it and kept chatting saw nothing happen, not even an
                # error. Idempotent when already connected. Note this cannot retro-fit tools into
                # THIS session's graph (Model.initialize() already resolved its tool list); the
                # signature change above is what rebuilds the session when configuration changes.
                await self._ensure_bloodhound_connected(request=request)
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
            autonomous_required=bool(kwargs.get("autonomous_solve")), request=request
        )
        model._bloodhound_exact_admission_at_initialize = bool(admitted_at_initialize)
        await model.initialize()
        # The chat container always runs at full detail — the collapsible tool cards ARE the "verbose"
        # view, so there is no operator verbose toggle (removed). set_verbose(True) also enables the local
        # tmux stream log; user-facing tool detail always renders as cards regardless of this flag.
        model.set_verbose(True)
        await put_channel_session(request, model)
        return model, False

    async def _notify_bloodhound_degraded_once(self, model: Any, request: ChatRequest) -> None:
        """Tell the operator, in the chat, that the attack graph is unavailable. Once per session.

        The diagnostic this ISA raised to WARNING lands in the container log, which an operator has
        to go and look for. This puts it where they already are. Once per session rather than per
        turn (D5): they saw it on the first degraded turn, and Mythic renders a live
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
                from ai.bloodhound_config import bloodhound_tool_admission, degraded_chat_notice
            except ImportError:  # pragma: no cover
                from ..ai.bloodhound_config import (  # type: ignore
                    bloodhound_tool_admission,
                    degraded_chat_notice,
                )
            if bloodhound_tool_admission().get("ready"):
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
        assessment_finding_id = assess_finding_id(prompt)

        async def _serialized_handler(turn) -> dict[str, Any] | None:
            model: Any | None = None
            native_response_text = ""
            # Slash commands dispatch first — they operate on the existing session (if any) and don't
            # need a fresh Model.initialize(). A handled command sends its own terminal → return None.
            # Unknown structured slash commands also terminate locally; control input is never
            # reinterpreted as a model prompt.
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

            if not has_input_response:
                model._pending_approval_context = None
            request_contract = _build_request_contract(
                request,
                model,
                has_input_response=has_input_response,
            )
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
            # Reassert on reused sessions too; older in-memory sessions created before this field was wired
            # should gain controller-native HITL without requiring a process restart.
            model.command_name = "chat"
            # Re-bind per-turn: the stream + card emitters are scoped to THIS request; the thread key is the
            # current collision-resistant channel generation. Never cache emitters across turns. _hitl_card_pending
            # is reset each turn; the interrupt surface sets it True when it emits a channel-release card,
            # so we then return None and let run_chat_turn skip its own terminal completion.
            stream_emitter = ChatStreamEmitter(self, request)
            model._response_emitter = stream_emitter
            await self._notify_bloodhound_degraded_once(model, request)

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
                            )
                        )
                    elif has_input_response and hitl_pending:
                        # A prior turn raised a confirmation card and finished; this request is the operator's
                        # answer. Resume the paused graph in place (Section 6): Confirm → approve; Reject →
                        # default-deny; Respond/Select → deny the guarded action but steer the replan with the
                        # operator's free-text (Phase 3).
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
                        native_response_text = _nonempty_native_response_text(
                            await model.invoke(prompt, is_interactive=preexisted)
                        )
                finally:
                    if approval_installed:
                        clear_claim = getattr(model, "clear_approval_claim", None)
                        if callable(clear_claim):
                            clear_claim()
                if approval_claimed:
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
                # so the SDK emits the terminal `cancelled` status (Cody, f). Never swallow this.
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
                    # argless exception, so it used to render the bare "Sage request failed." — an
                    # intentional stop presented as an unexplained crash, once after the requested
                    # work had already completed. When the exception states why it halted, render
                    # that reason through the same notice table `/stop` and session rotation use.
                    halt_reason = str(getattr(error, "stop_reason", "") or "")
                    if halt_reason:
                        error_text = stop_notice_for(
                            halt_reason,
                            str(getattr(error, "stop_detail", "") or ""),
                        ).strip()
                    else:
                        error_text = operator_error_text(error) or "Sage request failed."
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
            response_key = stream_emitter.last_response_key or turn.response_key
            _last_streamed = stream_emitter.last_content
            if _last_streamed and _last_streamed.lstrip().startswith("Command("):
                _last_streamed = ""
            response_content = (
                native_response_text
                or _last_streamed
                or "Completed."
            )
            # ISC-71: model prose is not the provenance authority. Apply one deterministic
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
                # ISC-74: a guarded tool that never executes never receives a tool-end callback, so
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
                close_open_tools = getattr(model, "_close_open_tool_lifecycles", None)
                if callable(close_open_tools):
                    await close_open_tools(status="stopped")

                preterminal = await finalize_visibility(require_final=False)
                if not preterminal.get("ok", False):
                    raise RuntimeError(
                        "Request lifecycle reconciliation failed before terminal response."
                    )
                if callable(record_terminal):
                    record_terminal("complete")
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
            elif callable(finalize_visibility):
                await finalize_visibility()
            await self.send_complete(
                request,
                response_key,
                metadata=turn._metadata(terminal_metadata),
                content=response_content,
                complete_request=True,
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
                return await _serialized_handler(turn)

        await self.run_chat_turn(
            request,
            _handler,
            response_key=f"assistant:{request.RequestID}:turn",
            model=request.Model,
            complete_content="Completed.",
        )
