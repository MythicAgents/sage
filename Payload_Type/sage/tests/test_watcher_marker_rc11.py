"""The Watcher's applied marker must survive Mythic's channel-metadata update contract.

mythic-container 0.7.0rc11 sends the channel-metadata RPC without any channel identity: the Mythic server
(Mythic-v4.0.0 ``d8a94943``) takes the channel from the RabbitMQ auth context of the request being served, stores
only ``items`` and ``default_display`` from the incoming ``channel_metadata``, sets ``updated_at``, keeps every other
stored key, and reports success even when it drops what it was sent.

``D8A94943MetadataServer`` below is a SYNTHETIC stand-in for that server handler, ported from
``mythic-docker/src/rabbitmq/recv_mythic_rpc_chat_channel_metadata_update.go`` at ``d8a94943``. It replaces only
``RabbitmqConnection.SendRPCMessage``: Sage's real ``WatcherControlPlane``, rc11's real message class and sender, and
the real ``ujson`` serialization all run. No recorded ``d8a94943`` row exists yet; the live check's stored-marker read
is the oracle for the server itself. These tests prove Sage's wiring given that rule.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import mythic_container
import pytest
from mythic_container import rabbitmq as mythic_rabbitmq
from mythic_container.MythicGoRPC.send_mythic_rpc_chat_channel_metadata_update import (
    MYTHIC_RPC_CHAT_CHANNEL_METADATA_UPDATE,
)

from sage_chat.findings_watcher import FindingsWatcherManager, _profile_binding_sha256
from sage_chat.headless import build_chat_request
from sage_chat.operation_memory import OperationMemoryStore
from sage_chat.watcher_control import WatcherControlPlane, _channel


OPERATION = 7
OWNER = 41


def _row(channel_id: int = OWNER, *, channel_metadata: Any = None, archived: bool = False) -> dict[str, Any]:
    return {
        "id": channel_id,
        "operation_id": OPERATION,
        "name": f"watcher-{channel_id}",
        "channel_type": "ai",
        "chat_model": "Sage Watcher",
        "locked": True,
        "archived": archived,
        "apitokens_id": 9,
        "ai_metadata": {
            "config": {"SAGE_WATCHER_PROVIDER": "openai", "SAGE_WATCHER_MODEL": "watcher-model"},
            "channel_metadata": {} if channel_metadata is None else channel_metadata,
        },
        "chat_container": {"name": "sage"},
    }


def _marker(generation: int = 1, **overrides: Any) -> dict[str, Any]:
    marker = {
        "generation": generation,
        "state": "starting",
        "provider": "openai",
        "model": "watcher-model",
        "config_sources": {"provider": "ui-config", "model": "ui-config"},
        "profile_binding_sha256": "a" * 64,
        "interval_seconds": 300,
        "paused": False,
    }
    marker.update(overrides)
    return marker


def _entry(value: Any, *, key: Any = "watcher") -> dict[str, Any]:
    return {"key": key, "label": "Watcher", "value": value, "display_value": "gen 1 · starting"}


class D8A94943MetadataServer:
    """SYNTHETIC: Mythic d8a94943's channel-metadata update handler over an in-memory channel table."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = {int(row["id"]): row for row in rows}
        self.bodies: list[dict[str, Any]] = []

    @staticmethod
    def auth_context(channel_id: int) -> str:
        return f"synthetic-auth-context:{OPERATION}:{channel_id}:sage"

    def _channel_for(self, headers: dict | None) -> int | None:
        value = (headers or {}).get(mythic_rabbitmq.MYTHIC_AUTH_CONTEXT_HEADER)
        prefix = f"synthetic-auth-context:{OPERATION}:"
        if not isinstance(value, str) or not value.startswith(prefix) or not value.endswith(":sage"):
            return None
        return int(value[len(prefix):-len(":sage")])

    async def SendRPCMessage(self, queue: str, body: bytes, retry_policy: int = 0) -> dict[str, Any]:
        assert queue == MYTHIC_RPC_CHAT_CHANNEL_METADATA_UPDATE
        # The real transport attaches the current request's auth context header to every outgoing RPC.
        channel_id = self._channel_for(mythic_rabbitmq._headers_with_current_auth_context())
        message = json.loads(body)
        self.bodies.append(message)
        if channel_id is None or channel_id not in self.rows:
            return {"success": False, "error": "sql: no rows in result set"}
        row = self.rows[channel_id]
        metadata = copy.deepcopy(row["ai_metadata"])
        incoming = message.get("channel_metadata")
        if isinstance(incoming, dict) and len(incoming) > 0:
            stored = metadata.get("channel_metadata")
            channel_metadata = stored if isinstance(stored, dict) else {}
            if incoming.get("items") is not None:
                channel_metadata["items"] = incoming["items"]
            channel_metadata["updated_at"] = "2026-11-24T00:00:00Z"
            if incoming.get("default_display") is not None:
                channel_metadata["default_display"] = incoming["default_display"]
            metadata["channel_metadata"] = channel_metadata
        row["ai_metadata"] = metadata
        return {"success": True, "error": ""}

    async def execute(self, _client: Any, _query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """GraphQL reads of the stored rows (the exact-channel query and the active-Watcher query)."""
        rows = [copy.deepcopy(row) for row in self.rows.values() if row["operation_id"] == variables["operation_id"]]
        if "channel_id" in variables:
            rows = [row for row in rows if row["id"] == variables["channel_id"]]
        return {"chat_channel": rows}


@pytest.fixture
def server(monkeypatch):
    server = D8A94943MetadataServer([_row()])
    monkeypatch.setattr(mythic_container.RabbitmqConnection, "SendRPCMessage", server.SendRPCMessage)
    return server


def _control(server: D8A94943MetadataServer) -> WatcherControlPlane:
    async def login(**_kwargs):
        return object()

    return WatcherControlPlane(login=login, execute=server.execute)


async def _in_request(channel_id: int, operation):
    """Run ``operation`` the way rc11 runs a chat request: with that request's auth context set."""
    token = mythic_rabbitmq.SetRabbitMQAuthContext(D8A94943MetadataServer.auth_context(channel_id))
    try:
        return await operation()
    finally:
        mythic_rabbitmq.ResetRabbitMQAuthContext(token)


async def _publish(control: WatcherControlPlane, marker: dict[str, Any]) -> None:
    await control.publish_profile_metadata(
        operation_id=OPERATION,
        channel_id=OWNER,
        generation=marker["generation"],
        lifecycle_state=marker["state"],
        provider=marker["provider"],
        model=marker["model"],
        config_sources=marker["config_sources"],
        profile_binding_sha256=marker["profile_binding_sha256"],
        interval_seconds=marker["interval_seconds"],
        paused=marker["paused"],
    )


def test_published_marker_is_stored_and_read_back_under_the_new_contract(server):
    async def scenario():
        control = _control(server)
        marker = _marker(3, state="running", interval_seconds=611)
        await _in_request(OWNER, lambda: _publish(control, marker))

        channel = await control.inspect_channel(object(), channel_id=OWNER, operation_id=OPERATION)
        assert channel.has_applied_marker
        assert channel.applied_marker == marker
        selection, selected = await control.active_profile_from_onstart(
            OPERATION, bootstrap_token="ephemeral", server_name="mythic"
        )
        assert selection == "selected"
        assert selected is not None and selected.channel_id == OWNER
        assert selected.applied_marker == marker

    asyncio.run(scenario())


def test_marker_body_carries_no_channel_identity_and_one_watcher_item(server):
    async def scenario():
        await _in_request(OWNER, lambda: _publish(_control(server), _marker(2)))
        (body,) = server.bodies
        assert set(body) == {"channel_metadata", "channel_config", "channel_model"}
        assert set(body["channel_metadata"]) == {"items"}
        (entry,) = body["channel_metadata"]["items"]
        assert entry["key"] == "watcher"
        assert entry["value"] == _marker(2)
        assert isinstance(entry["display_value"], str) and entry["display_value"]

    asyncio.run(scenario())


def test_marker_update_without_a_request_context_fails_closed(server):
    async def scenario():
        with pytest.raises(Exception, match="metadata update failed"):
            await _publish(_control(server), _marker(1))
        assert "items" not in server.rows[OWNER]["ai_metadata"]["channel_metadata"]

    asyncio.run(scenario())


class _Runtime:
    def __init__(self, path):
        self.store = OperationMemoryStore(path)

    async def refresh(self, *_args, **_kwargs):
        raise AssertionError("no refresh in this test")

    async def close(self):
        await self.store.close()


class _Delivery:
    async def bootstrap_channel(self, operation, *, bootstrap_token, server_name=""):
        return 12


class _NoLoopManager(FindingsWatcherManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started: list[str] = []

    async def _start_loop(self, operation):
        self.started.append(operation)


def _owner_request():
    return build_chat_request(
        "/watcher apply",
        model="Sage Watcher",
        config={"SAGE_WATCHER_PROVIDER": "openai", "SAGE_WATCHER_MODEL": "watcher-model"},
        operation_id=OPERATION,
        channel_id=OWNER,
    )


async def _restart(tmp_path, server) -> _NoLoopManager:
    manager = _NoLoopManager(
        _Runtime(tmp_path / "watcher.db"), delivery=_Delivery(), control_plane=_control(server), interval_seconds=300
    )
    await manager.restore_operation(str(OPERATION), server_name="mythic", bootstrap_token="ephemeral-onstart")
    return manager


def test_apply_then_controls_then_restart_passes_the_generation_fence(tmp_path, server):
    async def scenario():
        control = _control(server)
        manager = _NoLoopManager(
            _Runtime(tmp_path / "watcher.db"), delivery=_Delivery(), control_plane=control, interval_seconds=300
        )
        try:
            channel = await control.inspect_channel(object(), channel_id=OWNER, operation_id=OPERATION)
            record = await _in_request(OWNER, lambda: manager.apply_profile(_owner_request(), channel))
            assert record.generation == 1
            await _in_request(OWNER, lambda: manager.command(str(OPERATION), "pause", owner_channel_id=OWNER))
        finally:
            await manager.close()

        restarted = await _restart(tmp_path, server)
        try:
            state = restarted._states[str(OPERATION)]
            assert state.status not in {"stale-generation", "controller-missing", "degraded", "conflict"}
            assert state.generation == 1
            assert state.paused is True
        finally:
            await restarted.close()
        stored = await _control(server).inspect_channel(object(), channel_id=OWNER, operation_id=OPERATION)
        assert stored.applied_marker["generation"] == 1
        assert stored.applied_marker["paused"] is True

    asyncio.run(scenario())


def test_pre_upgrade_legacy_marker_resumes_and_a_new_apply_supersedes_it(tmp_path, monkeypatch):
    async def scenario():
        from sage_chat.config import resolve_watcher_llm_profile

        profile = resolve_watcher_llm_profile(_owner_request(), include_secrets=False)
        sources = dict(profile.sources)
        legacy = _marker(
            1, config_sources=sources, profile_binding_sha256=_profile_binding_sha256(profile)
        )
        server = D8A94943MetadataServer([_row(channel_metadata={"watcher": legacy})])
        monkeypatch.setattr(mythic_container.RabbitmqConnection, "SendRPCMessage", server.SendRPCMessage)
        store = OperationMemoryStore(tmp_path / "watcher.db")
        await store.apply_watcher_profile(
            str(OPERATION),
            owner_channel_id=OWNER,
            owner_channel_name=f"watcher-{OWNER}",
            provider="openai",
            model="watcher-model",
            config_sources=sources,
            profile_binding_sha256=legacy["profile_binding_sha256"],
            interval_seconds=300,
        )
        await store.close()

        restarted = await _restart(tmp_path, server)
        try:
            assert restarted._states[str(OPERATION)].status == "starting"
            assert restarted.started == [str(OPERATION)]
            channel = await _control(server).inspect_channel(object(), channel_id=OWNER, operation_id=OPERATION)
            record = await _in_request(OWNER, lambda: restarted.apply_profile(_owner_request(), channel))
            assert record.generation == 2
        finally:
            await restarted.close()

        stored = server.rows[OWNER]["ai_metadata"]["channel_metadata"]
        assert stored["watcher"]["generation"] == 1  # the server keeps the legacy key
        again = await _restart(tmp_path, server)
        try:
            state = again._states[str(OPERATION)]
            assert state.status == "starting"
            assert state.generation == 2
        finally:
            await again.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "channel_metadata,generation",
    (
        pytest.param({"items": [_entry(_marker(4))]}, 4, id="items-entry"),
        pytest.param({"watcher": _marker(5)}, 5, id="legacy-only"),
        pytest.param({"items": [_entry(_marker(6))], "watcher": _marker(1)}, 6, id="items-wins-over-legacy"),
        pytest.param(
            {"items": [{"key": "channel_id", "value": 41}, _entry(_marker(7))], "updated_at": "x"},
            7,
            id="items-entry-among-others",
        ),
        pytest.param({"items": {"watcher": _marker(1)}, "watcher": _marker(8)}, 8, id="non-list-items-ignored"),
        pytest.param({"items": ["watcher", 3, _entry(_marker(9))]}, 9, id="non-mapping-entries-ignored"),
    ),
)
def test_accepted_marker_forms(channel_metadata, generation):
    channel = _channel(_row(channel_metadata=channel_metadata))
    assert channel.has_applied_marker
    assert channel.applied_marker is not None
    assert channel.applied_marker["generation"] == generation


@pytest.mark.parametrize(
    "channel_metadata",
    (
        pytest.param({"items": [_entry("generation 3")]}, id="string-value"),
        pytest.param({"items": [{"key": "watcher", "label": "Watcher"}]}, id="missing-value"),
        pytest.param({"items": [_entry([_marker(1)])]}, id="list-value"),
        pytest.param({"items": [_entry(_marker(True))]}, id="bool-generation"),
        pytest.param({"items": [_entry(_marker(0))]}, id="zero-generation"),
        pytest.param({"items": [_entry(_marker(-1))]}, id="negative-generation"),
        pytest.param({"items": [_entry(_marker("1"))]}, id="string-generation"),
        pytest.param({"items": [_entry(_marker(1, profile_binding_sha256="A" * 64))]}, id="bad-binding"),
        pytest.param({"items": [_entry(_marker(1, provider=" "))]}, id="blank-provider"),
        pytest.param({"items": [_entry(_marker(1, config_sources={"model": "chat"}))]}, id="unknown-source"),
        pytest.param({"items": [_entry(_marker(1, interval_seconds=4))]}, id="interval-too-short"),
        pytest.param({"items": [_entry(_marker(1, paused="no"))]}, id="non-bool-paused"),
        pytest.param({"items": [_entry(_marker(1)), _entry(_marker(2))]}, id="two-watcher-entries"),
        pytest.param({"items": [_entry("bad")], "watcher": _marker(3)}, id="bad-entry-no-legacy-fallback"),
        pytest.param({"watcher": "generation 3"}, id="legacy-non-mapping"),
    ),
)
def test_marked_but_malformed_forms_fail_closed(channel_metadata):
    channel = _channel(_row(channel_metadata=channel_metadata))
    assert channel.has_applied_marker
    assert channel.applied_marker is None


@pytest.mark.parametrize(
    "channel_metadata",
    (
        pytest.param({}, id="empty"),
        pytest.param({"items": [_entry(_marker(1), key="Watcher")]}, id="items-key-case"),
        pytest.param({"items": [_entry(_marker(1), key="watcher_marker")]}, id="items-other-key"),
        pytest.param({"items": [{"label": "watcher", "value": _marker(1)}]}, id="items-label-not-key"),
        pytest.param({"items": [_entry(_marker(1), key=["watcher"])]}, id="items-non-string-key"),
        pytest.param({"Watcher": _marker(1)}, id="legacy-key-case"),
        pytest.param({"items": {"watcher": _marker(1)}}, id="non-list-items"),
    ),
)
def test_watcher_shaped_values_under_other_keys_are_not_markers(channel_metadata):
    channel = _channel(_row(channel_metadata=channel_metadata))
    assert not channel.has_applied_marker
    assert channel.applied_marker is None


def test_onstart_reports_conflict_for_a_marked_but_malformed_owner():
    async def scenario():
        server = D8A94943MetadataServer([_row(channel_metadata={"items": [_entry(_marker(1)), _entry(_marker(2))]})])
        selection, channel = await _control(server).active_profile_from_onstart(
            OPERATION, bootstrap_token="ephemeral", server_name="mythic"
        )
        assert selection == "conflict"
        assert channel is None

    asyncio.run(scenario())
