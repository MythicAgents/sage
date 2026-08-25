"""Operation-scoped bot authentication and findings delivery in Mythic.

The normal Mythic chat channel is the authoritative human-visible surface.
Event-log and optional Slack notices are deliberately generic nudges.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Awaitable, Callable, Mapping

from mythic import mythic

from .findings_slack import (
    SLACK_FINDINGS_WEBHOOK_ENV,
    emit_configured_findings_change_notice,
)
from .operation_findings import (
    PendingFindingDelivery,
    list_pending_finding_deliveries,
    record_finding_delivery_attempt,
)
from .operation_memory import OperationMemoryStore, _required_text
from .operation_memory_runtime import render_findings_markdown


WATCHER_TOKEN_ENV = "SAGE_WATCHER_APITOKEN"
FINDINGS_CHANNEL_NAME = "sage-findings"
FINDINGS_CHANNEL_DESCRIPTION = "Sage operation findings (managed by sage-findings-watcher-v1)."
GENERIC_MYTHIC_NOTICE = "Sage findings changed. Open #sage-findings to review."
REQUIRED_WATCHER_SCOPES = frozenset(
    {
        "callback.read",
        "chat-ai.read",
        "chat.write",
        "credential.read",
        "eventlog.write",
        "file.read",
        "operation.read",
        "response.read",
        "task.read",
    }
)
REQUIRED_WATCHER_EFFECTIVE_SCOPES = REQUIRED_WATCHER_SCOPES | {
    "chat.read",
    "eventlog.read",
}
REQUIRED_BOOTSTRAP_SCOPES = frozenset({"chat.write", "chat-ai.write"})

_WHOAMI_QUERY = """
query SageWatcherWhoami {
    whoami {
    status error user_id username account_type active deleted
    current_operation_id auth_method scopes effective_scopes
  }
}
"""

_FINDINGS_CHANNEL_QUERY = """
query SageFindingsChannels($name: String!) {
  chat_channel(
    where: {
      name: {_eq: $name}
      channel_type: {_eq: "standard"}
      archived: {_eq: false}
    }
    order_by: {id: asc}
  ) { id name description channel_type archived }
}
"""

_CREATE_FINDINGS_CHANNEL = """
mutation CreateSageFindingsChannel($name: String!, $description: String!) {
  chatCreateChannel(
    name: $name
    description: $description
    channel_type: "standard"
    locked: false
  ) { status error id channel_id }
}
"""

_CREATE_FINDINGS_MESSAGE = """
mutation CreateSageFindingsMessage($channelId: Int!, $message: String!) {
  chatCreateMessage(
    channel_id: $channelId
    message: $message
    system_message: false
    all_operations: false
  ) { status error message_id request_id }
}
"""


class WatcherConfigurationError(RuntimeError):
    """Persistent bot authentication is absent or violates the operation boundary."""


class FindingsDeliveryError(RuntimeError):
    """A configured authoritative delivery sink did not accept the notice."""


@dataclass(frozen=True)
class WatcherBotIdentity:
    operation_id: str
    operator_id: int
    username: str
    effective_scopes: tuple[str, ...]


@dataclass(frozen=True)
class WatcherMythicSession:
    client: Any
    identity: WatcherBotIdentity


def _positive_port(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise WatcherConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise WatcherConfigurationError(f"{name} must be a positive integer")
    return value


def _ssl_enabled() -> bool:
    return os.getenv("NGINX_SSL", "true").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _exact_scope_set(value: Any, *, field: str) -> frozenset[str]:
    if not isinstance(value, list) or any(
        not isinstance(scope, str) or not scope.strip() for scope in value
    ):
        raise WatcherConfigurationError(f"watcher whoami {field} are malformed")
    normalized = frozenset(scope.strip().casefold() for scope in value)
    if len(normalized) != len(value):
        raise WatcherConfigurationError(f"watcher whoami {field} are malformed")
    return normalized


async def _execute(client: Any, query: str, variables: Mapping[str, Any]) -> Mapping[str, Any]:
    result = await mythic.execute_custom_query(client, query, variables=dict(variables))
    if not isinstance(result, Mapping):
        raise FindingsDeliveryError("Mythic returned a non-object GraphQL result")
    return result


class MythicFindingsDelivery:
    """Authenticate one persistent operation bot and drain durable sink outboxes."""

    def __init__(
        self,
        *,
        login: Callable[..., Awaitable[Any]] | None = None,
        execute: Callable[[Any, str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None,
        eventlog: Callable[..., Awaitable[Any]] | None = None,
        slack_notice: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._login = login or mythic.login
        self._execute = execute or _execute
        self._eventlog = eventlog or mythic.send_event_log_message
        self._slack_notice = slack_notice
        self._channels: dict[str, int] = {}

    async def connect(
        self, operation_id: Any, *, server_name: str = ""
    ) -> WatcherMythicSession:
        operation = _required_text(operation_id, "operation_id")
        token = os.getenv(WATCHER_TOKEN_ENV, "").strip()
        if not token:
            raise WatcherConfigurationError(
                f"{WATCHER_TOKEN_ENV} is not configured for operation {operation}"
            )
        host = os.getenv("NGINX_HOST", "").strip() or str(server_name or "").strip()
        if not host:
            raise WatcherConfigurationError("NGINX_HOST is not configured")
        try:
            client = await self._login(
                server_ip=host,
                server_port=_positive_port("NGINX_PORT", 7443),
                apitoken=token,
                ssl=_ssl_enabled(),
            )
            result = await self._execute(client, _WHOAMI_QUERY, {})
        except WatcherConfigurationError:
            raise
        except Exception as exc:
            raise WatcherConfigurationError(
                "the persistent Sage watcher bot could not authenticate"
            ) from exc
        row = result.get("whoami") if isinstance(result.get("whoami"), Mapping) else {}
        try:
            observed_operation = int(row.get("current_operation_id"))
            operator_id = int(row.get("user_id"))
        except (TypeError, ValueError) as exc:
            raise WatcherConfigurationError("watcher whoami identity is incomplete") from exc
        username = str(row.get("username") or "").strip()
        if (
            str(row.get("status") or "").casefold() != "success"
            or str(row.get("account_type") or "").casefold() != "bot"
            or row.get("active") is not True
            or row.get("deleted") is True
            or observed_operation != int(operation)
            or operator_id <= 0
            or not username
        ):
            raise WatcherConfigurationError(
                "watcher token is not an active bot in the requested operation"
            )
        granted_scopes = _exact_scope_set(row.get("scopes"), field="stored grants")
        effective_scopes = _exact_scope_set(
            row.get("effective_scopes"), field="effective scopes"
        )
        if granted_scopes != REQUIRED_WATCHER_SCOPES:
            missing = REQUIRED_WATCHER_SCOPES - granted_scopes
            excess = granted_scopes - REQUIRED_WATCHER_SCOPES
            raise WatcherConfigurationError(
                "watcher bot stored grants must exactly match the frozen runtime class"
                + (": missing " + ", ".join(sorted(missing)) if missing else "")
                + ("; excess " + ", ".join(sorted(excess)) if excess else "")
            )
        if effective_scopes != REQUIRED_WATCHER_EFFECTIVE_SCOPES:
            missing = REQUIRED_WATCHER_EFFECTIVE_SCOPES - effective_scopes
            excess = effective_scopes - REQUIRED_WATCHER_EFFECTIVE_SCOPES
            raise WatcherConfigurationError(
                "watcher bot effective scopes must exactly match the frozen Mythic closure"
                + (": missing " + ", ".join(sorted(missing)) if missing else "")
                + ("; excess " + ", ".join(sorted(excess)) if excess else "")
            )
        return WatcherMythicSession(
            client=client,
            identity=WatcherBotIdentity(
                operation_id=operation,
                operator_id=operator_id,
                username=username,
                effective_scopes=tuple(sorted(effective_scopes)),
            ),
        )

    async def bootstrap_channel(
        self,
        operation_id: Any,
        *,
        bootstrap_token: Any,
        server_name: str = "",
    ) -> int:
        """Create/reuse the standard channel with one on-start token, then forget it."""
        operation = _required_text(operation_id, "operation_id")
        token = str(bootstrap_token or "").strip()
        if not token:
            raise WatcherConfigurationError("the on-start bootstrap token is missing")
        host = os.getenv("NGINX_HOST", "").strip() or str(server_name or "").strip()
        if not host:
            raise WatcherConfigurationError("NGINX_HOST is not configured")
        try:
            client = await self._login(
                server_ip=host,
                server_port=_positive_port("NGINX_PORT", 7443),
                apitoken=token,
                ssl=_ssl_enabled(),
            )
            result = await self._execute(client, _WHOAMI_QUERY, {})
        except WatcherConfigurationError:
            raise
        except Exception as exc:
            raise WatcherConfigurationError(
                "the on-start channel bootstrap could not authenticate"
            ) from exc
        row = result.get("whoami") if isinstance(result.get("whoami"), Mapping) else {}
        scopes = {
            str(scope).strip().casefold()
            for scope in row.get("effective_scopes", [])
            if isinstance(scope, str) and scope.strip()
        }
        try:
            observed_operation = int(row.get("current_operation_id"))
            operator_id = int(row.get("user_id"))
        except (TypeError, ValueError) as exc:
            raise WatcherConfigurationError(
                "on-start bootstrap whoami identity is incomplete"
            ) from exc
        username = str(row.get("username") or "").strip()
        if (
            str(row.get("status") or "").casefold() != "success"
            or str(row.get("auth_method") or "").casefold() != "on_start"
            or str(row.get("account_type") or "").casefold() != "bot"
            or row.get("active") is not True
            or row.get("deleted") is True
            or observed_operation != int(operation)
            or operator_id <= 0
            or not username
        ):
            raise WatcherConfigurationError(
                "on-start bootstrap is not an active bot in the requested operation"
            )
        if not REQUIRED_BOOTSTRAP_SCOPES.issubset(scopes):
            raise WatcherConfigurationError(
                "on-start bootstrap is missing required channel scopes"
            )
        if "*" in scopes or "task.write" in scopes:
            raise WatcherConfigurationError(
                "on-start bootstrap has forbidden persistent authority"
            )
        session = WatcherMythicSession(
            client=client,
            identity=WatcherBotIdentity(
                operation_id=operation,
                operator_id=operator_id,
                username=username,
                effective_scopes=tuple(sorted(scopes)),
            ),
        )
        return await self._findings_channel(session)

    @staticmethod
    def _require_action(result: Mapping[str, Any], field: str) -> Mapping[str, Any]:
        row = result.get(field)
        if not isinstance(row, Mapping) or str(row.get("status") or "").casefold() != "success":
            raise FindingsDeliveryError(f"Mythic {field} did not report success")
        return row

    async def _findings_channel(self, session: WatcherMythicSession) -> int:
        operation = session.identity.operation_id
        if operation in self._channels:
            return self._channels[operation]
        result = await self._execute(
            session.client,
            _FINDINGS_CHANNEL_QUERY,
            {
                "name": FINDINGS_CHANNEL_NAME,
            },
        )
        rows = result.get("chat_channel")
        if not isinstance(rows, list):
            raise FindingsDeliveryError("Mythic findings-channel query was malformed")
        if len(rows) > 1:
            raise FindingsDeliveryError("multiple managed Sage findings channels exist")
        if rows:
            channel_id = int(rows[0]["id"])
        else:
            created = self._require_action(
                await self._execute(
                    session.client,
                    _CREATE_FINDINGS_CHANNEL,
                    {
                        "name": FINDINGS_CHANNEL_NAME,
                        "description": FINDINGS_CHANNEL_DESCRIPTION,
                    },
                ),
                "chatCreateChannel",
            )
            channel_id = int(created.get("channel_id") or created.get("id"))
        if channel_id <= 0:
            raise FindingsDeliveryError("Mythic returned an invalid findings channel id")
        self._channels[operation] = channel_id
        return channel_id

    async def ensure_channel(self, session: WatcherMythicSession) -> int:
        """Create or reuse the managed standard channel before the first scan."""
        return await self._findings_channel(session)

    async def _deliver_sink(
        self,
        pending: PendingFindingDelivery,
        session: WatcherMythicSession,
        markdown: str,
    ) -> None:
        if pending.sink == "mythic_chat":
            channel_id = await self.ensure_channel(session)
            self._require_action(
                await self._execute(
                    session.client,
                    _CREATE_FINDINGS_MESSAGE,
                    {"channelId": channel_id, "message": markdown},
                ),
                "chatCreateMessage",
            )
            return
        if pending.sink == "mythic_eventlog":
            result = await self._eventlog(
                session.client,
                message=GENERIC_MYTHIC_NOTICE,
                level="info",
                source="sage-findings-watcher",
                warning=True,
            )
            if isinstance(result, Mapping):
                self._require_action(result, "createOperationEventLog")
            return
        if pending.sink == "slack":
            if not os.getenv(SLACK_FINDINGS_WEBHOOK_ENV, "").strip():
                return
            delivered = (
                await self._slack_notice()
                if self._slack_notice is not None
                else await emit_configured_findings_change_notice()
            )
            if not delivered:
                raise FindingsDeliveryError("configured Slack notice failed")
            return
        raise FindingsDeliveryError("unknown findings delivery sink")

    async def drain(
        self,
        store: OperationMemoryStore,
        operation_id: Any,
        session: WatcherMythicSession,
        *,
        view: tuple[Any, ...],
        snapshot: Mapping[str, Any],
        admission_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> int:
        operation = _required_text(operation_id, "operation_id")
        if session.identity.operation_id != operation:
            raise FindingsDeliveryError("delivery session operation mismatch")
        markdown = render_findings_markdown(view, snapshot)
        delivered_count = 0
        for pending in await list_pending_finding_deliveries(store, operation):
            # Do not rely on a caller-level ownership snapshot.  Revalidate at
            # each external sink boundary, outside the delivery-error handler so
            # an authority failure neither emits nor mutates retry accounting.
            if admission_guard is not None:
                await admission_guard()
            try:
                await self._deliver_sink(pending, session, markdown)
            except Exception as exc:
                await record_finding_delivery_attempt(
                    store,
                    operation,
                    pending.notification.ledger_id,
                    pending.sink,
                    delivered=False,
                    error=type(exc).__name__,
                )
                continue
            await record_finding_delivery_attempt(
                store,
                operation,
                pending.notification.ledger_id,
                pending.sink,
                delivered=True,
            )
            delivered_count += 1
        return delivered_count
