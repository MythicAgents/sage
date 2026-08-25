"""In-process, operation-scoped background watcher for assisted findings."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Any, Mapping

from mythic_container.ChatBase import ChatConfigView, ChatRequest
from mythic_container.logging import logger

from .config import ResolvedLLMProfile, resolve_watcher_llm_profile
from .mythic_findings_delivery import (
    MythicFindingsDelivery,
    WatcherConfigurationError,
    WatcherMythicSession,
)
from .operation_memory import WatcherOwnerConflict, WatcherProfileRecord, _required_text
from .operation_memory_runtime import OperationMemoryRuntime
from .operation_reasoner import FindingReasoningError, OperationFindingReasoner
from .watcher_control import WatcherChannel, WatcherControlPlane


WATCHER_INTERVAL_ENV = "SAGE_WATCHER_INTERVAL_SECONDS"
DEFAULT_WATCHER_INTERVAL_SECONDS = 300
MINIMUM_WATCHER_INTERVAL_SECONDS = 5
MAXIMUM_WATCHER_INTERVAL_SECONDS = 86_400


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_interval(raw: Any, *, source: str) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer") from exc
    if value < MINIMUM_WATCHER_INTERVAL_SECONDS:
        raise ValueError(
            f"{source} must be at least {MINIMUM_WATCHER_INTERVAL_SECONDS}"
        )
    if value > MAXIMUM_WATCHER_INTERVAL_SECONDS:
        raise ValueError(
            f"{source} must be at most {MAXIMUM_WATCHER_INTERVAL_SECONDS}"
        )
    return value


def _interval_seconds() -> int:
    raw = os.getenv(WATCHER_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_WATCHER_INTERVAL_SECONDS
    return _validated_interval(raw, source=WATCHER_INTERVAL_ENV)


def _profile_binding_sha256(profile: ResolvedLLMProfile) -> str:
    payload = {
        "provider": profile.provider,
        "model": profile.model,
        "api_endpoint": profile.api_endpoint,
        "api_key": profile.api_key,
        "aws_access_key_id": profile.aws_access_key_id,
        "aws_secret_access_key": profile.aws_secret_access_key,
        "aws_session_token": profile.aws_session_token,
        "region": profile.region,
        "sources": dict(profile.sources),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass
class WatcherOperationState:
    operation_id: str
    server_name: str
    status: str = "starting"
    paused: bool = False
    started_at: str = field(default_factory=_utc_now)
    last_scan_at: str = ""
    last_success_at: str = ""
    last_model_scan_at: str = ""
    bot_username: str = ""
    findings_channel_id: int = 0
    last_error_code: str = ""
    source_changes: int = 0
    active_findings: int = 0
    pending_deliveries: int = 0
    scans: int = 0
    owner_channel_id: int = 0
    owner_channel_name: str = ""
    generation: int = 0
    provider: str = ""
    model: str = ""
    config_sources: dict[str, str] = field(default_factory=dict)
    credentials_required: bool = False

    def snapshot(self, interval_seconds: int) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "status": "paused" if self.paused else self.status,
            "paused": self.paused,
            "started_at": self.started_at,
            "last_scan_at": self.last_scan_at,
            "last_success_at": self.last_success_at,
            "last_model_scan_at": self.last_model_scan_at,
            "bot_username": self.bot_username,
            "findings_channel_id": self.findings_channel_id,
            "last_error_code": self.last_error_code,
            "source_changes": self.source_changes,
            "active_findings": self.active_findings,
            "pending_deliveries": self.pending_deliveries,
            "scan_count": self.scans,
            "interval_seconds": interval_seconds,
            "channel": "#sage-findings",
            "owner_channel_id": self.owner_channel_id,
            "owner_channel_name": self.owner_channel_name,
            "generation": self.generation,
            "provider": self.provider,
            "model": self.model,
            "config_sources": dict(self.config_sources),
            "credentials_required": self.credentials_required,
        }


def render_watcher_status(status: Mapping[str, Any]) -> str:
    def cell(name: str) -> str:
        value = str(status.get(name) or "-")
        return value.replace("|", "\\|")

    state = str(status.get("status") or "unconfigured")
    remedy = {
        "unconfigured": "Create and lock the intended Sage Watcher channel, then run `/watcher apply` there.",
        "credentials-required": "Run `/watcher apply` from the exact locked owner to rehydrate request-scoped secrets.",
        "controller-missing": "Archive the missing owner in Mythic, then apply the intended locked replacement.",
        "conflict": "Archive duplicate active Watcher candidates in Mythic until one exact owner remains.",
        "unsupported-operation": "Use one operation per beta Sage deployment; start a separate deployment for another operation.",
        "paused": "Run `/watcher resume` from the exact locked owner.",
        "degraded": "Inspect the last error, restore the named identity/configuration dependency, then reapply or restart Sage.",
        "stale-generation": "Inspect the current owner generation and reapply only from that exact locked channel.",
    }.get(state, "No recovery action is required while the scheduler remains healthy.")
    return "\n".join(
        (
            f"**Sage findings watcher — operation `{cell('operation_id')}`**",
            "",
            "| State | Owner | Generation | Route | Bot | Last successful scan | Findings | Pending | Interval |",
            "|---|---|---:|---|---|---|---:|---:|---:|",
            f"| `{cell('status')}` | `{cell('owner_channel_name')}` (`{cell('owner_channel_id')}`) "
            f"| {int(status.get('generation') or 0)} | `{cell('provider')}/{cell('model')}` "
            f"| `{cell('bot_username')}` | `{cell('last_success_at')}` "
            f"| {int(status.get('active_findings') or 0)} | {int(status.get('pending_deliveries') or 0)} "
            f"| {int(status.get('interval_seconds') or 0)}s |",
            "",
            f"Managed channel: `{cell('channel')}` (ID `{cell('findings_channel_id')}`) "
            f"· scans: {int(status.get('scan_count') or 0)} "
            f"· last error: `{cell('last_error_code')}`",
            "",
            f"Remedy: {remedy}",
            "",
            "Controls from the exact locked owner: `/watcher apply`, `/watcher scan`, `/watcher pause`, `/watcher resume`, "
            "`/watcher interval <seconds>`, `/watcher interval default`.",
        )
    )


class FindingsWatcherManager:
    """Own one serial poll loop per operation in the persistent chat process."""

    def __init__(
        self,
        runtime: OperationMemoryRuntime | None = None,
        *,
        reasoner: OperationFindingReasoner | None = None,
        delivery: MythicFindingsDelivery | None = None,
        control_plane: WatcherControlPlane | None = None,
        interval_seconds: int | None = None,
    ) -> None:
        self.runtime = runtime or OperationMemoryRuntime()
        self.reasoner = reasoner or OperationFindingReasoner()
        self.delivery = delivery or MythicFindingsDelivery()
        self.control_plane = control_plane or WatcherControlPlane()
        self.interval_seconds = (
            _interval_seconds() if interval_seconds is None else int(interval_seconds)
        )
        if self.interval_seconds < MINIMUM_WATCHER_INTERVAL_SECONDS:
            raise ValueError("watcher interval is below the frozen minimum")
        if self.interval_seconds > MAXIMUM_WATCHER_INTERVAL_SECONDS:
            raise ValueError("watcher interval exceeds the supported maximum")
        self._states: dict[str, WatcherOperationState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wakes: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, WatcherMythicSession] = {}
        self._interval_overrides: dict[str, int] = {}
        self._profiles: dict[str, ResolvedLLMProfile] = {}
        self._configured_operation: str = ""
        self._apply_lock = asyncio.Lock()

    def _effective_interval(self, operation: str) -> int:
        return self._interval_overrides.get(operation, self.interval_seconds)

    @staticmethod
    def _state_from_profile(record: WatcherProfileRecord) -> WatcherOperationState:
        return WatcherOperationState(
            record.operation_id,
            "",
            status=record.lifecycle_state,
            paused=record.paused,
            owner_channel_id=record.owner_channel_id,
            owner_channel_name=record.owner_channel_name,
            generation=record.generation,
            provider=record.provider,
            model=record.model,
            config_sources=dict(record.config_sources),
            credentials_required=record.credentials_required,
        )

    def _install_active_profile(
        self,
        record: WatcherProfileRecord,
        profile: ResolvedLLMProfile,
        *,
        server_name: str = "",
    ) -> WatcherOperationState:
        operation = record.operation_id
        state = self._states.get(operation) or self._state_from_profile(record)
        state.server_name = str(server_name or state.server_name).strip()
        state.owner_channel_id = record.owner_channel_id
        state.owner_channel_name = record.owner_channel_name
        state.generation = record.generation
        state.provider = record.provider
        state.model = record.model
        state.config_sources = dict(record.config_sources)
        state.paused = record.paused
        state.credentials_required = record.credentials_required
        state.status = record.lifecycle_state
        self._states[operation] = state
        self._profiles[operation] = profile
        self._interval_overrides[operation] = record.interval_seconds
        self._wakes.setdefault(operation, asyncio.Event())
        self._locks.setdefault(operation, asyncio.Lock())
        self.reasoner = OperationFindingReasoner(model_profile=profile)
        self._configured_operation = operation
        return state

    async def _start_loop(self, operation: str) -> None:
        state = self._states[operation]
        if state.credentials_required or state.status in {"unconfigured", "conflict", "controller-missing", "unsupported-operation"}:
            return
        task = self._tasks.get(operation)
        if task is None or task.done():
            self._tasks[operation] = asyncio.create_task(
                self._run(operation), name=f"sage-findings-watcher-op-{operation}"
            )

    async def apply_profile(self, request: ChatRequest, channel: WatcherChannel) -> WatcherProfileRecord:
        operation = _required_text(request.OperationID, "operation_id")
        if request.Model != "Sage Watcher" or not channel.valid_owner_candidate:
            raise WatcherOwnerConflict("/watcher apply requires the exact active locked Sage Watcher channel")
        if channel.channel_id != int(request.ChannelID) or channel.operation_id != int(request.OperationID):
            raise WatcherOwnerConflict("Watcher request/channel binding mismatch")
        async with self._apply_lock:
            if self._configured_operation and self._configured_operation != operation:
                raise WatcherOwnerConflict("this beta deployment already owns a different operation")
            lock = self._locks.setdefault(operation, asyncio.Lock())
            async with lock:
                profile = resolve_watcher_llm_profile(request, include_secrets=True)
                if not profile.model:
                    raise ValueError("SAGE_WATCHER_MODEL is not configured")
                config = ChatConfigView.from_request(request)
                raw_interval = (
                    config.text(WATCHER_INTERVAL_ENV, "")
                    if config.has(WATCHER_INTERVAL_ENV)
                    else ""
                )
                interval = (
                    _validated_interval(raw_interval, source=WATCHER_INTERVAL_ENV)
                    if str(raw_interval).strip()
                    else _interval_seconds()
                )
                sources = dict(profile.sources)
                existing = await self.runtime.store.watcher_profile(operation)
                if (
                    existing is not None
                    and existing.owner_channel_id != channel.channel_id
                    and existing.lifecycle_state != "controller-missing"
                ):
                    raise WatcherOwnerConflict(
                        "a different active locked Watcher owner already controls this operation"
                    )
                expected_generation = existing.generation if existing is not None else 0
                generation = expected_generation + 1
                paused = existing.paused if existing is not None else False
                lifecycle_state = "paused" if paused else "starting"
                binding = _profile_binding_sha256(profile)
                await self.control_plane.publish_profile_metadata(
                    operation_id=int(operation),
                    channel_id=channel.channel_id,
                    generation=generation,
                    lifecycle_state=lifecycle_state,
                    provider=profile.provider,
                    model=profile.model,
                    config_sources=sources,
                    profile_binding_sha256=binding,
                    interval_seconds=interval,
                    paused=paused,
                )
                record = await self.runtime.store.apply_watcher_profile(
                    operation,
                    owner_channel_id=channel.channel_id,
                    owner_channel_name=channel.name,
                    provider=profile.provider,
                    model=profile.model,
                    config_sources=sources,
                    profile_binding_sha256=binding,
                    interval_seconds=interval,
                    credentials_required=False,
                    expected_generation=expected_generation,
                    generation=generation,
                )
                state = self._install_active_profile(record, profile)
                state.status = lifecycle_state
                state.credentials_required = False
        await self._start_loop(operation)
        return record

    async def restore_operation(
        self,
        operation_id: Any,
        *,
        server_name: str = "",
        bootstrap_token: Any | None = None,
    ) -> None:
        operation = _required_text(operation_id, "operation_id")
        if int(operation) <= 0:
            raise ValueError("operation_id must be positive")
        if bootstrap_token is None:
            raise ValueError("onStart bootstrap token is required")
        if self._configured_operation and self._configured_operation != operation:
            self._states[operation] = WatcherOperationState(operation, str(server_name), status="unsupported-operation")
            return
        channel_id = await self.delivery.bootstrap_channel(
            operation, bootstrap_token=bootstrap_token, server_name=server_name
        )
        selection, channel = await self.control_plane.active_profile_from_onstart(
            int(operation), bootstrap_token=str(bootstrap_token), server_name=server_name
        )
        if selection != "selected" or channel is None:
            self._states[operation] = WatcherOperationState(
                operation,
                str(server_name),
                status=selection,
                findings_channel_id=int(channel_id or 0),
            )
            return
        record = await self.runtime.store.watcher_profile(operation)
        if record is None or record.owner_channel_id != channel.channel_id:
            self._states[operation] = WatcherOperationState(
                operation,
                str(server_name),
                status="controller-missing" if record is not None else "unconfigured",
                owner_channel_id=channel.channel_id,
                owner_channel_name=channel.name,
            )
            return
        marker = channel.applied_marker
        if marker is None or int(marker["generation"]) != record.generation:
            state = self._state_from_profile(record)
            state.server_name = str(server_name)
            state.findings_channel_id = int(channel_id or 0)
            state.status = "stale-generation"
            state.last_error_code = "WatcherGenerationFence"
            self._states[operation] = state
            return
        marker_sources = marker.get("config_sources")
        marker_binding = str(marker.get("profile_binding_sha256") or "")
        marker_matches = bool(
            marker.get("provider") == record.provider
            and marker.get("model") == record.model
            and isinstance(marker_sources, Mapping)
            and dict(marker_sources) == dict(record.config_sources)
            and marker_binding == record.profile_binding_sha256
            and bool(marker_binding)
            and marker.get("interval_seconds") == record.interval_seconds
            and marker.get("paused") is record.paused
        )
        if not marker_matches:
            state = self._state_from_profile(record)
            state.server_name = str(server_name)
            state.findings_channel_id = int(channel_id or 0)
            state.status = "degraded"
            state.last_error_code = "WatcherProfileBindingDrift"
            self._states[operation] = state
            return
        synthetic = ChatRequest(
            operation_id=int(operation),
            channel_id=channel.channel_id,
            model="Sage Watcher",
            config=dict(channel.config),
            secrets={},
        )
        profile = resolve_watcher_llm_profile(synthetic, include_secrets=False)
        required = "user-secret" in set(record.config_sources.values())
        if required:
            record = await self.runtime.store.update_watcher_profile_state(
                operation,
                expected_generation=record.generation,
                lifecycle_state="credentials-required",
                credentials_required=True,
            )
        state = self._install_active_profile(record, profile, server_name=server_name)
        state.findings_channel_id = int(channel_id or 0)
        if required:
            state.last_error_code = "WatcherCredentialsRequired"
            return
        observed_sources = dict(profile.sources)
        source_drift = any(
            observed_sources.get(field, "default") != source
            for field, source in record.config_sources.items()
        )
        identity_drift = profile.provider != record.provider or profile.model != record.model
        binding_drift = _profile_binding_sha256(profile) != record.profile_binding_sha256
        if not profile.model or source_drift or identity_drift or binding_drift:
            record = await self.runtime.store.update_watcher_profile_state(
                operation,
                expected_generation=record.generation,
                lifecycle_state="degraded",
                credentials_required=False,
            )
            state.status = record.lifecycle_state
            state.last_error_code = (
                "WatcherProfileSourceDrift"
                if source_drift or identity_drift or binding_drift
                else "WatcherConfigurationMissing"
            )
            return
        if profile.model:
            await self._start_loop(operation)

    async def _run(self, operation: str) -> None:
        wake = self._wakes[operation]
        first = True
        while True:
            state = self._states[operation]
            if state.paused:
                wake.clear()
                await wake.wait()
                continue
            if not first:
                wake.clear()
                try:
                    await asyncio.wait_for(
                        wake.wait(), timeout=float(self._effective_interval(operation))
                    )
                except TimeoutError:
                    pass
                if state.paused:
                    continue
            first = False
            try:
                await self.poll_once(operation)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    f"Sage findings watcher scan failed for operation {operation}",
                    exc_info=True,
                )

    async def _record_poll_failure(
        self,
        operation: str,
        state: WatcherOperationState,
        *,
        status: str,
        error_code: str,
    ) -> Mapping[str, Any]:
        self._sessions.pop(operation, None)
        state.status = status
        state.last_error_code = error_code
        try:
            record = await self.runtime.store.update_watcher_profile_state(
                operation,
                expected_generation=state.generation,
                lifecycle_state=status,
                credentials_required=False,
            )
            state.status = record.lifecycle_state
        except WatcherOwnerConflict:
            state.status = "stale-generation"
            state.last_error_code = "WatcherGenerationFence"
        return state.snapshot(self._effective_interval(operation))

    async def poll_once(self, operation_id: Any) -> Mapping[str, Any]:
        operation = _required_text(operation_id, "operation_id")
        state = self._states.get(operation)
        if state is None:
            raise ValueError("watcher operation is not registered")
        try:
            return await self._poll_once(operation, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._record_poll_failure(
                operation,
                state,
                status="degraded",
                error_code=type(exc).__name__,
            )

    async def _poll_once(
        self,
        operation: str,
        state: WatcherOperationState,
    ) -> Mapping[str, Any]:
        async with self._locks[operation]:
            if state.paused:
                return state.snapshot(self._effective_interval(operation))
            profile_record = await self.runtime.store.watcher_profile(operation)
            if (
                profile_record is None
                or profile_record.generation != state.generation
                or profile_record.owner_channel_id != state.owner_channel_id
                or profile_record.lifecycle_state in {"controller-missing", "credentials-required"}
            ):
                state.status = "stale-generation" if profile_record is not None else "controller-missing"
                state.last_error_code = "WatcherGenerationFence"
                return state.snapshot(self._effective_interval(operation))
            state.last_scan_at = _utc_now()
            state.scans += 1
            try:
                # Re-authenticate every poll so runtime-token revoke/rotation, operation reassignment,
                # identity disablement, and scope drift cannot hide behind a cached client.
                session = await self.delivery.connect(
                    operation, server_name=state.server_name
                )
                state.findings_channel_id = await self.delivery.ensure_channel(session)
                self._sessions[operation] = session
                state.bot_username = session.identity.username
                owner = await self.control_plane.inspect_channel(
                    session.client,
                    channel_id=state.owner_channel_id,
                    operation_id=int(operation),
                )
                if not owner.valid_owner_candidate:
                    await self.runtime.store.update_watcher_profile_state(
                        operation,
                        expected_generation=state.generation,
                        lifecycle_state="controller-missing",
                    )
                    state.status = "controller-missing"
                    state.last_error_code = "WatcherControllerMissing"
                    return state.snapshot(self._effective_interval(operation))
            except WatcherConfigurationError as exc:
                return await self._record_poll_failure(
                    operation,
                    state,
                    status="unconfigured",
                    error_code=type(exc).__name__,
                )
            except Exception as exc:
                return await self._record_poll_failure(
                    operation,
                    state,
                    status="degraded",
                    error_code=type(exc).__name__,
                )

            try:
                async def owner_guard() -> None:
                    admitted_owner = await self.control_plane.inspect_channel(
                        session.client,
                        channel_id=state.owner_channel_id,
                        operation_id=int(operation),
                    )
                    if not admitted_owner.valid_owner_candidate:
                        state.status = "controller-missing"
                        state.last_error_code = "WatcherControllerMissing"
                        raise WatcherOwnerConflict("Watcher owner changed before admission")

                async def admission_guard() -> None:
                    current = await self.runtime.store.watcher_profile(operation)
                    if (
                        current is None
                        or current.generation != state.generation
                        or current.owner_channel_id != state.owner_channel_id
                    ):
                        state.status = "stale-generation"
                        state.last_error_code = "WatcherGenerationFence"
                        raise WatcherOwnerConflict("Watcher generation changed before admission")
                    await owner_guard()

                refresh = await self.runtime.refresh(
                    session.client,
                    operation,
                    reasoner=self.reasoner,
                    reason_only_when_changed=True,
                    admission_guard=admission_guard,
                    # reconcile_findings owns the store lock until commit.  Its
                    # boundary guard therefore performs the external Mythic
                    # check only; the manager's operation lock already fences
                    # local apply/generation changes for this entire poll.
                    commit_guard=owner_guard,
                )
                view, snapshot = refresh.view, refresh.snapshot
                state.source_changes = refresh.changed_source_count
                if refresh.reasoning is not None and refresh.reasoning.model_called:
                    state.last_model_scan_at = state.last_scan_at
                reasoning_error = ""
            except WatcherOwnerConflict:
                if state.status == "controller-missing":
                    await self.runtime.store.update_watcher_profile_state(
                        operation,
                        expected_generation=state.generation,
                        lifecycle_state="controller-missing",
                    )
                return state.snapshot(self._effective_interval(operation))
            except FindingReasoningError as exc:
                view, snapshot = await self.runtime.current_view(operation)
                reasoning_error = type(exc).__name__
            except Exception as exc:
                return await self._record_poll_failure(
                    operation,
                    state,
                    status="degraded",
                    error_code=type(exc).__name__,
                )

            commit_record = await self.runtime.store.watcher_profile(operation)
            if (
                commit_record is None
                or commit_record.generation != state.generation
                or commit_record.owner_channel_id != state.owner_channel_id
            ):
                state.status = "stale-generation"
                state.last_error_code = "WatcherGenerationFence"
                return state.snapshot(self._effective_interval(operation))
            try:
                owner = await self.control_plane.inspect_channel(
                    session.client,
                    channel_id=state.owner_channel_id,
                    operation_id=int(operation),
                )
            except Exception as exc:
                return await self._record_poll_failure(
                    operation,
                    state,
                    status="degraded",
                    error_code=type(exc).__name__,
                )
            if not owner.valid_owner_candidate:
                await self.runtime.store.update_watcher_profile_state(
                    operation,
                    expected_generation=state.generation,
                    lifecycle_state="controller-missing",
                )
                state.status = "controller-missing"
                state.last_error_code = "WatcherControllerMissing"
                return state.snapshot(self._effective_interval(operation))
            try:
                await self.delivery.drain(
                    self.runtime.store,
                    operation,
                    session,
                    view=view,
                    snapshot=snapshot,
                    admission_guard=admission_guard,
                )
                # Bind the visible healthy state to authority that still exists
                # after the final external effect and outbox acknowledgement.
                await admission_guard()
            except WatcherOwnerConflict:
                if state.status == "controller-missing":
                    await self.runtime.store.update_watcher_profile_state(
                        operation,
                        expected_generation=state.generation,
                        lifecycle_state="controller-missing",
                    )
                return state.snapshot(self._effective_interval(operation))
            snapshot = await self.runtime.store.snapshot(operation)
            state.active_findings = len(view)
            state.pending_deliveries = int(snapshot.get("pending_delivery_count", 0))
            if reasoning_error or snapshot.get("degraded") or state.pending_deliveries:
                state.status = "degraded"
                state.last_error_code = reasoning_error or (
                    "PendingFindingDelivery"
                    if state.pending_deliveries
                    else "OperationMemoryResourceBound"
                )
            else:
                state.status = "running"
                state.last_error_code = ""
                state.last_success_at = state.last_scan_at
            await self.runtime.store.update_watcher_profile_state(
                operation,
                expected_generation=state.generation,
                lifecycle_state=state.status,
                credentials_required=False,
            )
            return state.snapshot(self._effective_interval(operation))

    def status(self, operation_id: Any) -> Mapping[str, Any]:
        operation = _required_text(operation_id, "operation_id")
        state = self._states.get(operation)
        if state is None:
            return WatcherOperationState(operation, "", status="unconfigured").snapshot(
                self._effective_interval(operation)
            )
        task = self._tasks.get(operation)
        if (
            state.generation > 0
            and not state.paused
            and not state.credentials_required
            and state.status not in {
                "unconfigured",
                "conflict",
                "controller-missing",
                "unsupported-operation",
                "stale-generation",
                "degraded",
            }
            and (task is None or task.done())
        ):
            state.status = "degraded"
            state.last_error_code = "WatcherSchedulerStopped"
        if state.last_success_at and state.status == "running":
            try:
                observed = datetime.fromisoformat(state.last_success_at.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - observed).total_seconds()
                if age > (2 * self._effective_interval(operation)):
                    state.status = "degraded"
                    state.last_error_code = "WatcherLivenessStale"
            except ValueError:
                state.status = "degraded"
                state.last_error_code = "WatcherLivenessInvalid"
        return state.snapshot(self._effective_interval(operation))

    def console_profile(self, operation_id: Any, channel_id: Any) -> ResolvedLLMProfile:
        operation = _required_text(operation_id, "operation_id")
        state = self._states.get(operation)
        profile = self._profiles.get(operation)
        if (
            state is None
            or profile is None
            or state.owner_channel_id != int(channel_id)
            or state.credentials_required
            or state.status in {"unconfigured", "conflict", "controller-missing", "stale-generation"}
        ):
            raise WatcherOwnerConflict("Watcher console is not the active credentialed owner generation")
        return profile

    async def command(
        self,
        operation_id: Any,
        action: Any,
        *,
        owner_channel_id: int | None = None,
    ) -> Mapping[str, Any]:
        operation = _required_text(operation_id, "operation_id")
        command = str(action or "status").strip().casefold() or "status"
        parts = command.split()
        interval_update: int | None = None
        restore_default = False
        if len(parts) == 2 and parts[0] == "interval":
            if parts[1] == "default":
                restore_default = True
            elif parts[1].isascii() and parts[1].isdecimal():
                interval_update = int(parts[1])
                if interval_update < MINIMUM_WATCHER_INTERVAL_SECONDS:
                    raise ValueError("watcher interval is below the frozen minimum")
                if interval_update > MAXIMUM_WATCHER_INTERVAL_SECONDS:
                    raise ValueError("watcher interval exceeds the supported maximum")
            else:
                raise ValueError("watcher interval must be an integer or default")
        elif command not in {"status", "scan", "pause", "resume"}:
            raise ValueError("unknown watcher control")
        if command == "status":
            return self.status(operation)
        if operation not in self._states:
            raise WatcherOwnerConflict("Watcher is unconfigured")
        state = self._states[operation]
        if owner_channel_id is None or int(owner_channel_id) != state.owner_channel_id:
            raise WatcherOwnerConflict("Watcher controls require the exact active locked owner")
        if command == "scan":
            return await self.poll_once(operation)
        async with self._locks[operation]:
            record = await self.runtime.store.watcher_profile(operation)
            if (
                record is None
                or record.generation != state.generation
                or record.owner_channel_id != int(owner_channel_id)
            ):
                raise WatcherOwnerConflict("Watcher owner generation changed")
            if restore_default:
                self._interval_overrides.pop(operation, None)
                record = await self.runtime.store.update_watcher_profile_state(
                    operation,
                    expected_generation=state.generation,
                    interval_seconds=self.interval_seconds,
                )
                self._wakes[operation].set()
            elif interval_update is not None:
                self._interval_overrides[operation] = interval_update
                record = await self.runtime.store.update_watcher_profile_state(
                    operation,
                    expected_generation=state.generation,
                    interval_seconds=interval_update,
                )
                self._wakes[operation].set()
            elif command == "pause":
                state.paused = True
                state.status = "paused"
                record = await self.runtime.store.update_watcher_profile_state(
                    operation,
                    expected_generation=state.generation,
                    paused=True,
                    lifecycle_state="paused",
                )
                self._wakes[operation].set()
            elif command == "resume":
                state.paused = False
                state.status = "starting"
                record = await self.runtime.store.update_watcher_profile_state(
                    operation,
                    expected_generation=state.generation,
                    paused=False,
                    lifecycle_state="starting",
                )
                self._wakes[operation].set()
            state.config_sources = dict(record.config_sources)
            await self.control_plane.publish_profile_metadata(
                operation_id=int(operation),
                channel_id=record.owner_channel_id,
                generation=record.generation,
                lifecycle_state=record.lifecycle_state,
                provider=record.provider,
                model=record.model,
                config_sources=record.config_sources,
                profile_binding_sha256=record.profile_binding_sha256,
                interval_seconds=record.interval_seconds,
                paused=record.paused,
            )
        return state.snapshot(self._effective_interval(operation))

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._interval_overrides.clear()
        self._profiles.clear()
        await self.runtime.close()
