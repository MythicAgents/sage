"""Narrow Mythic control-plane adapter for the locked Sage Watcher profile."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Awaitable, Callable, Mapping

from mythic import mythic
from mythic_container.ChatBase import ChatAPITokenProvider, ChatRequest
from mythic_container.MythicGoRPC import (
    MythicRPCChatChannelMetadataUpdateMessage,
    SendMythicRPCChatChannelMetadataUpdate,
)


_CHANNEL_QUERY = """
query SageWatcherChannel($channel_id: Int!, $operation_id: Int!) {
  chat_channel(where: {id: {_eq: $channel_id}, operation_id: {_eq: $operation_id}}, limit: 2) {
    id
    operation_id
    name
    channel_type
    chat_model
    locked
    archived
    apitokens_id
    ai_metadata
    chat_container { name }
  }
}
"""

_ACTIVE_WATCHERS_QUERY = """
query SageWatcherActiveProfiles($operation_id: Int!) {
  chat_channel(where: {
    operation_id: {_eq: $operation_id},
    channel_type: {_eq: "ai"},
    chat_model: {_eq: "Sage Watcher"}
  }, order_by: {id: asc}, limit: 1024) {
    id
    operation_id
    name
    channel_type
    chat_model
    locked
    archived
    apitokens_id
    ai_metadata
    chat_container { name }
  }
}
"""


class WatcherControlBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class WatcherChannel:
    channel_id: int
    operation_id: int
    name: str
    model: str
    container: str
    locked: bool
    archived: bool
    backing_apitoken_id: int
    config: Mapping[str, Any]
    channel_metadata: Mapping[str, Any]

    @property
    def valid_owner_candidate(self) -> bool:
        return bool(
            self.channel_id > 0
            and self.operation_id > 0
            and self.model == "Sage Watcher"
            and self.container == "sage"
            and self.locked
            and not self.archived
        )

    @property
    def has_applied_marker(self) -> bool:
        return "watcher" in self.channel_metadata

    @property
    def applied_marker(self) -> Mapping[str, Any] | None:
        if not self.has_applied_marker:
            return None
        marker = self.channel_metadata.get("watcher")
        if not isinstance(marker, Mapping):
            return None
        generation = marker.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            return None
        binding = marker.get("profile_binding_sha256")
        if not (
            isinstance(binding, str)
            and len(binding) == 64
            and all(character in "0123456789abcdef" for character in binding)
        ):
            return None
        if not isinstance(marker.get("provider"), str) or not str(marker["provider"]).strip():
            return None
        if not isinstance(marker.get("model"), str) or not str(marker["model"]).strip():
            return None
        sources = marker.get("config_sources")
        if not isinstance(sources, Mapping) or any(
            str(value) not in {"ui-config", "user-secret", "environment", "default"}
            for value in sources.values()
        ):
            return None
        interval = marker.get("interval_seconds")
        if isinstance(interval, bool) or not isinstance(interval, int) or not 5 <= interval <= 86_400:
            return None
        if not isinstance(marker.get("paused"), bool):
            return None
        return marker


def _positive_port() -> int:
    try:
        value = int(os.getenv("NGINX_PORT", "7443"))
    except ValueError as exc:
        raise WatcherControlBoundaryError("NGINX_PORT must be an integer") from exc
    if value <= 0:
        raise WatcherControlBoundaryError("NGINX_PORT must be positive")
    return value


def _ssl_enabled() -> bool:
    return os.getenv("NGINX_SSL", "true").strip().casefold() not in {"0", "false", "no", "off"}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _channel(row: Mapping[str, Any]) -> WatcherChannel:
    metadata = _mapping(row.get("ai_metadata"))
    container = _mapping(row.get("chat_container"))
    try:
        channel_id = int(row.get("id"))
        operation_id = int(row.get("operation_id"))
        backing_id = int(row.get("apitokens_id") or 0)
    except (TypeError, ValueError) as exc:
        raise WatcherControlBoundaryError("Mythic returned malformed Watcher channel identity") from exc
    if str(row.get("channel_type") or "") != "ai":
        raise WatcherControlBoundaryError("Watcher owner channel is not an AI channel")
    return WatcherChannel(
        channel_id=channel_id,
        operation_id=operation_id,
        name=str(row.get("name") or "").strip(),
        model=str(row.get("chat_model") or "").strip(),
        container=str(container.get("name") or "").strip(),
        locked=row.get("locked") is True,
        archived=row.get("archived") is True,
        backing_apitoken_id=backing_id,
        config=dict(_mapping(metadata.get("config"))),
        channel_metadata=dict(_mapping(metadata.get("channel_metadata"))),
    )


async def _execute(client: Any, query: str, variables: Mapping[str, Any]) -> Mapping[str, Any]:
    result = await mythic.execute_custom_query(client, query, variables=dict(variables))
    if not isinstance(result, Mapping):
        raise WatcherControlBoundaryError("Mythic returned a non-object Watcher query")
    return result


class WatcherControlPlane:
    """Read exact channel identity and publish only redaction-safe lifecycle metadata."""

    def __init__(
        self,
        *,
        login: Callable[..., Awaitable[Any]] | None = None,
        execute: Callable[[Any, str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None,
        metadata_update: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> None:
        self._login = login or mythic.login
        self._execute = execute or _execute
        self._metadata_update = metadata_update or SendMythicRPCChatChannelMetadataUpdate

    async def _login_token(self, token: str, *, server_name: str = "") -> Any:
        host = os.getenv("NGINX_HOST", "").strip() or str(server_name or "").strip()
        if not host:
            raise WatcherControlBoundaryError("NGINX_HOST is not configured")
        return await self._login(
            server_ip=host,
            server_port=_positive_port(),
            apitoken=token,
            ssl=_ssl_enabled(),
        )

    async def client_for_request(self, request: ChatRequest) -> Any:
        provider = await ChatAPITokenProvider.from_request(request)
        return await self._login_token(await provider.get_token())

    async def inspect_request_channel(self, request: ChatRequest) -> WatcherChannel:
        client = await self.client_for_request(request)
        return await self.inspect_channel(
            client, channel_id=int(request.ChannelID), operation_id=int(request.OperationID)
        )

    async def inspect_channel(
        self, client: Any, *, channel_id: int, operation_id: int
    ) -> WatcherChannel:
        result = await self._execute(
            client,
            _CHANNEL_QUERY,
            {"channel_id": int(channel_id), "operation_id": int(operation_id)},
        )
        rows = result.get("chat_channel")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise WatcherControlBoundaryError("exact Watcher request channel was not returned")
        channel = _channel(rows[0])
        if channel.channel_id != int(channel_id) or channel.operation_id != int(operation_id):
            raise WatcherControlBoundaryError("Watcher request channel binding changed")
        return channel

    async def active_profile_from_onstart(
        self,
        operation_id: int,
        *,
        bootstrap_token: str,
        server_name: str,
    ) -> tuple[str, WatcherChannel | None]:
        client = await self._login_token(str(bootstrap_token), server_name=server_name)
        result = await self._execute(
            client, _ACTIVE_WATCHERS_QUERY, {"operation_id": int(operation_id)}
        )
        rows = result.get("chat_channel")
        if not isinstance(rows, list):
            raise WatcherControlBoundaryError("Mythic returned malformed active Watcher rows")
        if len(rows) >= 1024:
            return "conflict", None
        candidates = [_channel(row) for row in rows if isinstance(row, Mapping)]
        if not candidates:
            return "unconfigured", None
        active = [row for row in candidates if not row.archived]
        active_marked = [row for row in active if row.has_applied_marker]
        historical_marked = [row for row in candidates if row.archived and row.has_applied_marker]
        if not active_marked:
            if historical_marked:
                return "controller-missing", None
            return "unconfigured", None
        applied = [
            row
            for row in active_marked
            if row.valid_owner_candidate and row.applied_marker is not None
        ]
        if len(active_marked) != 1 or len(applied) != 1:
            return "conflict", None
        return "selected", applied[0]

    async def publish_profile_metadata(
        self,
        *,
        operation_id: int,
        channel_id: int,
        generation: int,
        lifecycle_state: str,
        provider: str,
        model: str,
        config_sources: Mapping[str, str],
        profile_binding_sha256: str,
        interval_seconds: int,
        paused: bool,
    ) -> None:
        response = await self._metadata_update(
            MythicRPCChatChannelMetadataUpdateMessage(
                OperationID=int(operation_id),
                ChannelID=int(channel_id),
                ContainerName="sage",
                ChannelMetadata={
                    "watcher": {
                        "generation": int(generation),
                        "state": str(lifecycle_state),
                        "provider": str(provider),
                        "model": str(model),
                        "config_sources": dict(config_sources),
                        "profile_binding_sha256": str(profile_binding_sha256),
                        "interval_seconds": int(interval_seconds),
                        "paused": bool(paused),
                    }
                },
            )
        )
        if getattr(response, "Success", False) is not True:
            raise WatcherControlBoundaryError(
                f"Watcher channel metadata update failed: {getattr(response, 'Error', '')}"
            )
