from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from mythic_container.ChatBase import ContainerOnStartMessage

from sage_chat.findings_watcher import (
    DEFAULT_WATCHER_INTERVAL_SECONDS,
    MAXIMUM_WATCHER_INTERVAL_SECONDS,
    WATCHER_INTERVAL_ENV,
    FindingsWatcherManager,
    _profile_binding_sha256,
    _interval_seconds,
    render_watcher_status,
)
from sage_chat.config import ResolvedLLMProfile
from sage_chat.mythic_findings_delivery import (
    WatcherBotIdentity,
    WatcherConfigurationError,
    WatcherMythicSession,
)
from sage_chat.operation_memory import OperationMemoryStore
from sage_chat.operation_reasoner import FindingReasoningError
from sage_chat.service import SageChat
from sage_chat.watcher_control import WatcherChannel


class FakeRuntime:
    def __init__(self, path, *, error: Exception | None = None, changed: int = 0):
        self.store = OperationMemoryStore(path)
        self.error = error
        self.changed = changed
        self.calls = []
        self.called = asyncio.Event()

    async def refresh(self, client, operation, **kwargs):
        await self.store.initialize()
        self.calls.append((client, operation, kwargs))
        self.called.set()
        if self.error is not None:
            raise self.error
        snapshot = await self.store.snapshot(operation)
        return SimpleNamespace(
            view=(),
            snapshot=snapshot,
            changed_source_count=self.changed,
            reasoning=(
                SimpleNamespace(model_called=True) if self.changed else None
            ),
        )

    async def current_view(self, operation):
        await self.store.initialize()
        return (), await self.store.snapshot(operation)

    async def close(self):
        await self.store.close()


class FakeDelivery:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.connect_calls = []
        self.ensure_calls = []
        self.bootstrap_calls = []
        self.drain_calls = []
        self.drained = asyncio.Event()

    async def connect(self, operation, *, server_name=""):
        self.connect_calls.append((operation, server_name))
        if self.error is not None:
            raise self.error
        return WatcherMythicSession(
            client=object(),
            identity=WatcherBotIdentity(str(operation), 4, "sage-bot", ("*",)),
        )

    async def bootstrap_channel(self, operation, *, bootstrap_token, server_name=""):
        self.bootstrap_calls.append((operation, bootstrap_token, server_name))
        if self.error is not None:
            raise self.error
        return 12

    async def ensure_channel(self, session):
        self.ensure_calls.append(session)
        return 12

    async def drain(
        self, store, operation, session, *, view, snapshot, admission_guard=None
    ):
        if admission_guard is not None:
            await admission_guard()
        self.drain_calls.append((store, operation, session, view, snapshot))
        self.drained.set()
        return 0


class FakeControlPlane:
    def __init__(self, *, selection="unconfigured", channel=None):
        self.selection = selection
        self.channel = channel
        self.metadata_calls = []

    async def active_profile_from_onstart(
        self, operation_id, *, bootstrap_token, server_name
    ):
        return self.selection, self.channel

    async def inspect_channel(self, client, *, channel_id, operation_id):
        if self.channel is not None:
            return self.channel
        return WatcherChannel(
            channel_id=int(channel_id),
            operation_id=int(operation_id),
            name="watcher-owner",
            model="Sage Watcher",
            container="sage",
            locked=True,
            archived=False,
            backing_apitoken_id=9,
            config={},
            channel_metadata={},
        )

    async def publish_profile_metadata(self, **kwargs):
        self.metadata_calls.append(kwargs)


def _owner_channel(operation=7, channel_id=41, *, config=None):
    return WatcherChannel(
        channel_id=channel_id,
        operation_id=operation,
        name="watcher-owner",
        model="Sage Watcher",
        container="sage",
        locked=True,
        archived=False,
        backing_apitoken_id=9,
        config=config or {
            "SAGE_WATCHER_PROVIDER": "openai",
            "SAGE_WATCHER_MODEL": "test-model",
        },
        channel_metadata={},
    )


def _manager(*args, **kwargs):
    kwargs.setdefault("control_plane", FakeControlPlane())
    return FindingsWatcherManager(*args, **kwargs)


async def _start_test_profile(manager, operation="7", *, server_name=""):
    await manager.runtime.store.initialize()
    record = await manager.runtime.store.apply_watcher_profile(
        operation,
        owner_channel_id=41,
        owner_channel_name="watcher-owner",
        provider="openai",
        model="test-model",
        config_sources={"provider": "ui-config", "model": "ui-config"},
        interval_seconds=manager.interval_seconds,
        credentials_required=False,
    )
    state = manager._install_active_profile(
        record,
        ResolvedLLMProfile(provider="openai", model="test-model"),
        server_name=server_name,
    )
    state.findings_channel_id = 12
    await manager._start_loop(operation)


def test_default_and_environment_watcher_intervals(monkeypatch):
    monkeypatch.delenv(WATCHER_INTERVAL_ENV, raising=False)
    assert DEFAULT_WATCHER_INTERVAL_SECONDS == 300
    assert _interval_seconds() == 300

    monkeypatch.setenv(WATCHER_INTERVAL_ENV, "600")
    assert _interval_seconds() == 600

    assert MAXIMUM_WATCHER_INTERVAL_SECONDS == 86_400
    for invalid in ("0", "4", "-1", "86401", "1" * 401, "abc", "5 seconds"):
        monkeypatch.setenv(WATCHER_INTERVAL_ENV, invalid)
        with pytest.raises(ValueError):
            _interval_seconds()


def test_container_start_restores_profile_and_names_status_surface(monkeypatch):
    async def scenario():
        calls = []

        class Watcher:
            async def restore_operation(
                self, operation, *, server_name="", bootstrap_token=None
            ):
                calls.append((operation, server_name, bootstrap_token))

        chat = SageChat()
        monkeypatch.setattr(chat, "_findings_watcher", lambda: Watcher())
        response = await chat.on_container_start(
            ContainerOnStartMessage(
                container_name="sage",
                operation_id=7,
                server_name="mythic",
                apitoken="short-lived-token-must-not-be-retained",
            )
        )
        assert calls == [(7, "mythic", "short-lived-token-must-not-be-retained")]
        assert response.ContainerName == "sage"
        assert "/watcher status" in response.EventLogInfoMessage

    asyncio.run(scenario())


def test_container_start_bootstrap_failure_is_visible_and_schedules_nothing(
    tmp_path, monkeypatch
):
    async def scenario():
        watcher = _manager(
            FakeRuntime(tmp_path / "memory.db"),
            delivery=FakeDelivery(error=WatcherConfigurationError("invalid bootstrap")),
            reasoner=object(),
            interval_seconds=60,
        )
        chat = SageChat()
        monkeypatch.setattr(chat, "_findings_watcher", lambda: watcher)
        response = await chat.on_container_start(
            ContainerOnStartMessage(
                container_name="sage", operation_id=7, server_name="mythic",
                apitoken="short-lived-sentinel",
            )
        )
        assert response.EventLogInfoMessage == ""
        assert "WatcherConfigurationError" in response.EventLogErrorMessage
        assert "short-lived-sentinel" not in response.EventLogErrorMessage
        assert watcher._states == {}
        assert watcher._tasks == {}
        assert watcher.runtime.calls == []
        await watcher.close()

    asyncio.run(scenario())


def test_bootstrap_is_synchronous_before_schedule_and_persistent_poll_reuses_channel(
    tmp_path,
):
    async def scenario():
        class OrderingDelivery(FakeDelivery):
            def __init__(self):
                super().__init__()
                self.manager = None
                self.bootstrapped = False

            async def bootstrap_channel(
                self, operation, *, bootstrap_token, server_name=""
            ):
                assert self.manager is not None
                assert self.manager._states == {}
                assert self.manager._tasks == {}
                assert bootstrap_token
                self.bootstrapped = True
                return 12

        runtime = FakeRuntime(tmp_path / "memory.db")
        delivery = OrderingDelivery()
        sources = {
            "provider": "ui-config",
            "model": "ui-config",
            "api_endpoint": "default",
            "api_key": "default",
            "aws_access_key_id": "default",
            "aws_secret_access_key": "default",
            "aws_session_token": "default",
            "region": "default",
        }
        profile = ResolvedLLMProfile(
            provider="openai",
            model="test-model",
            sources=tuple(sources.items()),
        )
        binding = _profile_binding_sha256(profile)
        channel = _owner_channel()
        channel = WatcherChannel(
            **{
                **channel.__dict__,
                "channel_metadata": {
                    "watcher": {
                        "generation": 1,
                        "provider": "openai",
                        "model": "test-model",
                        "config_sources": sources,
                        "profile_binding_sha256": binding,
                        "interval_seconds": 60,
                        "paused": False,
                    }
                },
            }
        )
        control = FakeControlPlane(selection="selected", channel=channel)
        manager = _manager(
            runtime,
            delivery=delivery,
            reasoner=object(),
            control_plane=control,
            interval_seconds=60,
        )
        delivery.manager = manager
        await runtime.store.initialize()
        await runtime.store.apply_watcher_profile(
            "7",
            owner_channel_id=41,
            owner_channel_name="watcher-owner",
            provider="openai",
            model="test-model",
            config_sources=sources,
            profile_binding_sha256=binding,
            interval_seconds=60,
            credentials_required=False,
        )
        await manager.restore_operation(
            "7", server_name="mythic", bootstrap_token="short-lived-sentinel"
        )
        assert delivery.bootstrapped
        assert manager.status("7")["findings_channel_id"] == 12
        assert len(manager._tasks) == 1
        assert "short-lived-sentinel" not in repr(vars(manager))
        await asyncio.wait_for(runtime.called.wait(), timeout=1)
        assert len(delivery.ensure_calls) == 1
        assert manager.status("7")["findings_channel_id"] == 12
        assert "short-lived-sentinel" not in repr(manager.status("7"))
        assert "short-lived-sentinel" not in repr(manager._sessions)
        await manager.close()

    asyncio.run(scenario())


def test_background_start_scans_without_a_chat_turn_and_reports_health(tmp_path):
    async def scenario():
        runtime = FakeRuntime(tmp_path / "memory.db", changed=1)
        delivery = FakeDelivery()
        manager = _manager(
            runtime,
            delivery=delivery,
            reasoner=object(),
            interval_seconds=60,
        )
        await _start_test_profile(manager, server_name="mythic")
        await asyncio.wait_for(delivery.drained.wait(), timeout=1)
        async def settled():
            while manager.status("7")["status"] == "starting":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(settled(), timeout=1)
        status = manager.status("7")
        assert status["status"] == "running"
        assert status["bot_username"] == "sage-bot"
        assert status["findings_channel_id"] == 12
        assert len(delivery.ensure_calls) == 1
        assert status["last_model_scan_at"]
        assert status["scan_count"] == 1
        assert runtime.calls[0][2]["reason_only_when_changed"] is True
        assert len(delivery.drain_calls) == 1
        assert "#sage-findings" in render_watcher_status(status)
        assert "ID `12`" in render_watcher_status(status)
        await manager.close()

    asyncio.run(scenario())


def test_missing_persistent_bot_token_is_visible_as_unconfigured(tmp_path):
    async def scenario():
        runtime = FakeRuntime(tmp_path / "memory.db")
        manager = _manager(
            runtime,
            delivery=FakeDelivery(error=WatcherConfigurationError("missing")),
            reasoner=object(),
            interval_seconds=60,
        )
        await _start_test_profile(manager)
        async def settled():
            while manager.status("7")["status"] == "starting":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(settled(), timeout=1)
        status = manager.status("7")
        assert status["status"] == "unconfigured"
        assert status["last_error_code"] == "WatcherConfigurationError"
        record = await runtime.store.watcher_profile("7")
        assert record is not None
        assert record.lifecycle_state == "unconfigured"
        assert runtime.calls == []
        await manager.close()

    asyncio.run(scenario())


def test_manager_has_no_internal_whole_poll_timeout_surface(tmp_path):
    with pytest.raises(TypeError, match="poll_timeout_seconds"):
        _manager(
            FakeRuntime(tmp_path / "memory.db"),
            delivery=FakeDelivery(),
            reasoner=object(),
            interval_seconds=60,
            poll_timeout_seconds=0.05,
        )


def test_blocked_poll_remains_pending_until_explicit_cancellation(tmp_path):
    async def scenario():
        runtime = FakeRuntime(tmp_path / "memory.db")

        class BlockFirstConnect(FakeDelivery):
            def __init__(self):
                super().__init__()
                self.attempts = 0
                self.entered = asyncio.Event()

            async def connect(self, operation, *, server_name=""):
                self.attempts += 1
                if self.attempts == 1:
                    self.entered.set()
                    await asyncio.Event().wait()
                return await super().connect(operation, server_name=server_name)

        delivery = BlockFirstConnect()
        manager = _manager(
            runtime,
            delivery=delivery,
            reasoner=object(),
            interval_seconds=5,
        )
        await _start_test_profile(manager, server_name="mythic")
        await asyncio.wait_for(delivery.entered.wait(), timeout=1)
        await asyncio.sleep(0.075)
        status = manager.status("7")
        record = await runtime.store.watcher_profile("7")
        task = manager._tasks["7"]
        assert not task.done()
        assert status["status"] == "starting"
        assert status["last_error_code"] == ""
        assert record is not None
        assert record.lifecycle_state == "starting"
        assert runtime.calls == []

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        record = await runtime.store.watcher_profile("7")
        assert record is not None
        assert record.lifecycle_state == "starting"
        await manager.close()

    asyncio.run(scenario())


def test_unexpected_pre_ingest_failure_is_durably_degraded(tmp_path):
    async def scenario():
        runtime = FakeRuntime(tmp_path / "memory.db")
        manager = _manager(
            runtime,
            delivery=FakeDelivery(error=RuntimeError("owner read unavailable")),
            reasoner=object(),
            interval_seconds=60,
        )
        await _start_test_profile(manager)

        async def settled():
            while manager.status("7")["status"] == "starting":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(settled(), timeout=1)
        status = manager.status("7")
        record = await runtime.store.watcher_profile("7")
        assert status["status"] == "degraded"
        assert status["last_error_code"] == "RuntimeError"
        assert record is not None
        assert record.lifecycle_state == "degraded"
        assert runtime.calls == []
        await manager.close()

    asyncio.run(scenario())


def test_reasoning_failure_preserves_view_and_marks_degraded(tmp_path):
    async def scenario():
        runtime = FakeRuntime(
            tmp_path / "memory.db", error=FindingReasoningError("bad model output")
        )
        delivery = FakeDelivery()
        manager = _manager(
            runtime,
            delivery=delivery,
            reasoner=object(),
            interval_seconds=60,
        )
        await _start_test_profile(manager)
        await asyncio.wait_for(delivery.drained.wait(), timeout=1)
        async def settled():
            while manager.status("7")["status"] == "starting":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(settled(), timeout=1)
        status = manager.status("7")
        assert status["status"] == "degraded"
        assert status["last_error_code"] == "FindingReasoningError"
        assert len(delivery.drain_calls) == 1
        await manager.close()

    asyncio.run(scenario())


def test_operation_scoped_pause_resume_and_scan_controls(tmp_path):
    async def scenario():
        runtime = FakeRuntime(tmp_path / "memory.db")
        manager = _manager(
            runtime,
            delivery=FakeDelivery(),
            reasoner=object(),
            interval_seconds=60,
        )
        await _start_test_profile(manager)
        await asyncio.wait_for(runtime.called.wait(), timeout=1)
        await manager.command("7", "pause", owner_channel_id=41)
        assert manager.status("7")["status"] == "paused"
        scans = manager.status("7")["scan_count"]
        await manager.command("7", "scan", owner_channel_id=41)
        assert manager.status("7")["scan_count"] == scans
        await manager.command("7", "resume", owner_channel_id=41)
        assert manager.status("7")["status"] == "starting"
        await manager.command("7", "scan", owner_channel_id=41)
        assert manager.status("7")["status"] == "running"
        assert manager.status("7")["scan_count"] == scans + 1
        await manager.close()

    asyncio.run(scenario())


def test_operation_scoped_interval_control_and_default_restore(tmp_path):
    async def scenario():
        manager = _manager(
            FakeRuntime(tmp_path / "memory.db"),
            delivery=FakeDelivery(),
            reasoner=object(),
            interval_seconds=60,
        )

        await _start_test_profile(manager)
        status = await manager.command("7", "interval 300", owner_channel_id=41)
        assert status["interval_seconds"] == 300
        assert manager.status("8")["interval_seconds"] == 60

        status = await manager.command("7", "interval 5", owner_channel_id=41)
        assert status["interval_seconds"] == 5

        status = await manager.command("7", "interval default", owner_channel_id=41)
        assert status["interval_seconds"] == 60
        assert "7" not in manager._interval_overrides
        assert "/watcher interval <seconds>" in render_watcher_status(status)
        await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command",
    (
        "interval",
        "interval 0",
        "interval 4",
        "interval -1",
        "interval 86401",
        "interval " + "1" * 401,
        "interval abc",
        "interval 300 extra",
    ),
)
def test_invalid_interval_controls_fail_without_state_change(tmp_path, command):
    async def scenario():
        manager = _manager(
            FakeRuntime(tmp_path / "memory.db"),
            delivery=FakeDelivery(),
            reasoner=object(),
            interval_seconds=60,
        )
        with pytest.raises((ValueError, Exception)) as raised:
            await manager.command("7", command)
        assert isinstance(raised.value, ValueError)
        assert manager._states == {}
        assert manager._tasks == {}
        assert manager._interval_overrides == {}
        await manager.close()

    asyncio.run(scenario())


def test_constructor_rejects_interval_above_supported_maximum(tmp_path):
    with pytest.raises(ValueError, match="supported maximum"):
        _manager(
            FakeRuntime(tmp_path / "memory.db"),
            delivery=FakeDelivery(),
            reasoner=object(),
            interval_seconds=86_401,
        )
