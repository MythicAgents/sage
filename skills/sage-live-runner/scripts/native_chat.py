#!/usr/bin/env python3
"""Create and run one-shot Sage requests through Mythic v4 native chat."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from uuid import uuid4

from mythic import mythic


DEFAULT_SERVER = "127.0.0.1"
DEFAULT_USER = "mythic_admin"
DEFAULT_OBJECTIVE = "From the current foothold, achieve administrative control of essos.local."
DEFAULT_PREPARED_CHANNEL_NAME = "Sage GOAD Ready"
PREPARED_CHANNEL_MARKER = "sage-goad-one-shot"
DEFAULT_BHUSA_DEMO_CHANNEL_NAME = "BHUSA Demo"
BHUSA_DEMO_METADATA_DISPLAY = "expanded; max=15"
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_ENV_PATHS = (
    WORKSPACE_ROOT / "mythic_v4" / ".env",
    WORKSPACE_ROOT / "mythic" / ".env",
)
SAGE_ENV_PATH = REPO_ROOT / "Payload_Type" / "sage" / ".env"
READINESS_CONTRACT_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "readiness_contract.py"
BOOTSTRAP_PATH = REPO_ROOT / "skills" / "sage-callback-bootstrap" / "scripts" / "bootstrap_payloads.py"
TERMINAL_STATUSES = {"completed", "complete", "error", "failed", "cancelled", "canceled"}
APPROVABLE_INPUT_REQUEST_STATUS = "streaming"
REQUIRED_TOKEN_SCOPES = {"apitoken.write", "chat-ai.write"}
AUTONOMOUS_TOKEN_SCOPES = {"*"}

READINESS_QUERY = """
query SageChatReadiness {
  consuming_container(
    where: {
      name: {_eq: "sage"}
      type: {_eq: "chat"}
      deleted: {_eq: false}
    }
    order_by: {id: desc}
  ) {
    id
    name
    type
    container_running
    deleted
    updated_at
  }
  apitokens(
    where: {
      active: {_eq: true}
      deleted: {_eq: false}
      token_type: {_eq: "api"}
    }
    order_by: {id: desc}
  ) {
    id
    name
    active
    deleted
    scopes
    operator_id
  }
}
"""

PREPARED_CHANNEL_QUERY = """
query PreparedSageChannels {
  chat_channel(
    where: {
      channel_type: {_eq: "ai"}
      archived: {_eq: false}
      locked: {_eq: true}
      last_message_id: {_is_null: true}
      chat_container: {
        name: {_eq: "sage"}
        deleted: {_eq: false}
      }
    }
    order_by: {id: desc}
  ) {
    id
    name
    description
    channel_type
    archived
    locked
    last_message_id
    chat_container_id
    apitokens_id
    ai_metadata
  }
}
"""

CREATE_CHANNEL_MUTATION = """
mutation CreateSageChannel(
  $name: String!
  $description: String
  $containerId: Int!
  $model: String!
  $tokenId: Int!
  $metadata: jsonb
) {
  chatCreateChannel(
    name: $name
    description: $description
    channel_type: "ai"
    chat_container_id: $containerId
    chat_model: $model
    locked: true
    ai_metadata: $metadata
    apitokens_id: $tokenId
  ) {
    status
    error
    id
    channel_id
  }
}
"""

CREATE_TOKEN_MUTATION = """
mutation CreateSageChatToken($operatorId: Int, $name: String!, $scopes: [String!]) {
  createAPIToken(operator_id: $operatorId, name: $name, scopes: $scopes) {
    id
    name
    scopes
    token_type
    status
    error
    operator_id
  }
}
"""

OPERATION_BOT_QUERY = """
query SageOperationBot($operationId: Int!) {
  operator(
    where: {
      account_type: {_eq: "bot"}
      current_operation_id: {_eq: $operationId}
      active: {_eq: true}
      deleted: {_eq: false}
    }
    order_by: {id: asc}
  ) {
    id
    username
    account_type
    active
    deleted
  }
}
"""

CREATE_MESSAGE_MUTATION = """
mutation CreateSageMessage($channelId: Int!, $message: String!) {
  chatCreateMessage(
    channel_id: $channelId
    message: $message
    system_message: false
    all_operations: false
  ) {
    status
    error
    message_id
    request_id
  }
}
"""

CHAT_INPUT_RESPONSE_MUTATION = """
mutation SageChatInputResponse($messageId: Int!, $action: String!, $response: String, $choiceId: String) {
  chatInputResponse(message_id: $messageId, action: $action, response: $response, choice_id: $choiceId) {
    status
    error
    message_id
    request_id
  }
}
"""

REQUEST_QUERY = """
query SageChatRequest($requestId: Int!) {
  chat_request(where: {id: {_eq: $requestId}}, limit: 1) {
    id
    channel_id
    request_message_id
    status
    error
    created_by
    updated_at
  }
  chat_message(
    where: {chat_request_id: {_eq: $requestId}}
    order_by: {id: asc}
  ) {
    id
    channel_id
    chat_request_id
    chat_response_key
    author_type
    sender_display_name
    message
    metadata
    edited
    deleted
    status
    created_at
    updated_at
  }
}
"""

SAGE_CHANNEL_IDS_QUERY = """
query SageChannelIds {
  chat_channel(
    where: {
      channel_type: {_eq: "ai"}
      archived: {_eq: false}
      chat_container: {
        name: {_eq: "sage"}
        type: {_eq: "chat"}
        deleted: {_eq: false}
      }
    }
    order_by: {id: desc}
  ) {
    id
    name
  }
}
"""

LATEST_REQUEST_QUERY = """
query LatestSageChatRequest($channelIds: [Int!]!) {
  chat_request(
    where: {channel_id: {_in: $channelIds}}
    order_by: {id: desc}
    limit: 1
  ) {
    id
    channel_id
    request_message_id
    status
    error
    created_by
    updated_at
  }
}
"""


def resolve_env_path(explicit: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    configured = os.environ.get("MYTHIC_ENV_PATH")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(DEFAULT_ENV_PATHS)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else DEFAULT_ENV_PATHS[0]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load helper module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_password(env_path: str | Path | None = None) -> str:
    value = os.environ.get("MYTHIC_ADMIN_PASSWORD")
    if value:
        return value
    path = resolve_env_path(env_path)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "MYTHIC_ADMIN_PASSWORD" and value.strip():
                return value.strip().strip("'\"")
    raise RuntimeError(
        "Set MYTHIC_ADMIN_PASSWORD or MYTHIC_ENV_PATH; checked "
        + ", ".join(str(path) for path in DEFAULT_ENV_PATHS)
    )


def _read_env_defaults(path: Path | None = None) -> dict[str, str]:
    path = path or SAGE_ENV_PATH
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[7:]
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def normalize_provider(value: str) -> str:
    """Return the canonical provider identifier used by Sage's Mythic choice field."""
    return str(value or "openai").strip().lower()


def default_ai_metadata(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    file_env = _read_env_defaults()

    def value(name: str, default: str = "") -> str:
        return os.environ.get(name) or file_env.get(name) or default

    config: dict[str, Any] = {
        "provider": normalize_provider(value("provider", "openai")),
        "model": value("model"),
        "mode": "auto",
        "autonomous_solve": True,
        "max_steps": int(value("max_steps", "0")),
    }
    for key in (
        "API_ENDPOINT",
        "API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
    ):
        resolved = value(key)
        if resolved:
            config[key] = resolved
    metadata = {"config": config}
    if extra:
        metadata.update(extra)
    return metadata


def canary_ai_metadata(
    *, max_steps: int, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build isolated supervised metadata without changing normal Auto defaults."""

    if max_steps < 1:
        raise ValueError("canary max_steps must be at least 1")
    metadata = default_ai_metadata()
    config = dict(metadata.get("config") or {})
    config.update(
        {
            "mode": "supervised",
            "autonomous_solve": False,
            "max_steps": int(max_steps),
        }
    )
    metadata["config"] = config
    if extra:
        extra_copy = dict(extra)
        extra_config = extra_copy.pop("config", None)
        metadata.update(extra_copy)
        if isinstance(extra_config, dict):
            config.update(extra_config)
            config["mode"] = "supervised"
            config["autonomous_solve"] = False
            config["max_steps"] = int(max_steps)
    return metadata


def bhusa_demo_ai_metadata(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the exact supervised BHUSA demo channel metadata."""
    metadata = default_ai_metadata()
    config = dict(metadata.get("config") or {})
    config.update(
        {
            "mode": "supervised",
            "autonomous_solve": False,
            "policy_mode": "hybrid",
            "max_steps": 200,
        }
    )
    metadata["config"] = config
    metadata["channel_metadata_display"] = {"display": BHUSA_DEMO_METADATA_DISPLAY}
    if extra:
        extra_copy = dict(extra)
        extra_config = extra_copy.pop("config", None)
        metadata.update(extra_copy)
        if isinstance(extra_config, dict):
            config.update(extra_config)
        config["mode"] = "supervised"
        config["autonomous_solve"] = False
        config["policy_mode"] = "hybrid"
        config["max_steps"] = 200
        metadata["channel_metadata_display"] = {"display": BHUSA_DEMO_METADATA_DISPLAY}
    return metadata


def _readiness_contract_module():
    return _load_module("sage_readiness_contract_for_native_chat", READINESS_CONTRACT_PATH)


def _chat_runtime_identity_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, dict) else {}
    config = metadata.get("config")
    if not isinstance(config, dict):
        config = {}
    contract = _readiness_contract_module()
    return {
        "provider": normalize_provider(config.get("provider") or ""),
        "model": str(config.get("model") or "").strip(),
        "route": contract._route_summary(config.get("API_ENDPOINT") or ""),
    }


async def login(
    *,
    server: str = DEFAULT_SERVER,
    user: str = DEFAULT_USER,
    password: str | None = None,
    env_path: str | Path | None = None,
) -> Any:
    return await mythic.login(
        server_ip=server,
        username=user,
        password=password or resolve_password(env_path),
    )


def _scopes(token: dict[str, Any]) -> set[str]:
    raw = token.get("scopes") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [item.strip() for item in raw.split(",")]
    return {str(item).strip() for item in raw if str(item).strip()}


def select_chat_resources(
    readiness: dict[str, Any],
    *,
    api_token_id: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    containers = [
        row
        for row in readiness.get("consuming_container", [])
        if row.get("container_running") and not row.get("deleted")
    ]
    if not containers:
        raise RuntimeError("Sage chat container is not running in Mythic.")

    tokens = readiness.get("apitokens", [])
    if api_token_id is not None:
        tokens = [row for row in tokens if int(row.get("id", -1)) == int(api_token_id)]
        if not tokens:
            raise RuntimeError(f"Active Mythic API token {api_token_id} was not found.")
    usable = [
        row
        for row in tokens
        if AUTONOMOUS_TOKEN_SCOPES.issubset(_scopes(row))
    ]
    if not usable:
        raise RuntimeError(
            "No active Mythic API token has the wildcard scope required for autonomous Sage operations."
        )
    return containers[0], usable[0]


async def inspect_readiness(
    client: Any,
    *,
    api_token_id: int | None = None,
    runtime_dbs_archived: bool = False,
) -> dict[str, Any]:
    bootstrap = _load_module("sage_callback_bootstrap_for_native_chat", BOOTSTRAP_PATH)
    return await bootstrap.readiness(
        client,
        api_token_id=api_token_id,
        runtime_dbs_archived=runtime_dbs_archived,
    )


def select_operation_bot(observed: dict[str, Any]) -> dict[str, Any]:
    matches = [
        row
        for row in observed.get("operator", [])
        if row.get("account_type") == "bot"
        and row.get("active") is True
        and row.get("deleted") is False
    ]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one active Mythic bot operator for the current operation.")
    _require_exact_int(matches[0].get("id"), "Current operation bot operator id")
    return matches[0]


async def resolve_operation_bot(client: Any) -> dict[str, Any]:
    operation_id = _require_exact_int(
        getattr(client, "current_operation_id", None),
        "Current Mythic operation id",
    )
    if operation_id <= 0:
        raise RuntimeError("Current Mythic operation id must be positive.")
    return select_operation_bot(
        await mythic.execute_custom_query(
            client,
            OPERATION_BOT_QUERY,
            variables={"operationId": operation_id},
        )
    )


async def ensure_api_token(
    client: Any,
    *,
    name: str = "Sage native chat",
    operator_id: int | None = None,
) -> dict[str, Any]:
    expected_operator_id = (
        _require_exact_int(operator_id, "Requested API token operator id")
        if operator_id is not None
        else None
    )
    observed = await mythic.execute_custom_query(client, READINESS_QUERY)
    usable = [
        row
        for row in observed.get("apitokens", [])
        if (
            (expected_operator_id is None or row.get("operator_id") == expected_operator_id)
            and AUTONOMOUS_TOKEN_SCOPES.issubset(_scopes(row))
        )
    ]
    if usable:
        return {"created": False, "api_token": usable[0]}
    result = await mythic.execute_custom_query(
        client,
        CREATE_TOKEN_MUTATION,
        variables={
            "operatorId": expected_operator_id,
            "name": name,
            "scopes": sorted(AUTONOMOUS_TOKEN_SCOPES),
        },
    )
    token = _require_success("API token creation", result.get("createAPIToken") or {})
    if expected_operator_id is not None and token.get("operator_id") != expected_operator_id:
        raise RuntimeError("Mythic API token creation returned the wrong operator_id.")
    return {"created": True, "api_token": token}


def _require_success(operation: str, value: dict[str, Any]) -> dict[str, Any]:
    if str(value.get("status") or "").casefold() != "success":
        raise RuntimeError(f"Mythic {operation} failed: {value.get('error') or value}")
    return value


async def create_locked_channel(
    client: Any,
    *,
    name: str | None = None,
    description: str = "Sage one-shot GOAD evaluation",
    model: str = "Sage",
    api_token_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observed = await mythic.execute_custom_query(client, READINESS_QUERY)
    container, token = select_chat_resources(observed, api_token_id=api_token_id)
    channel_name = name or (
        f"sage-one-shot-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    )
    built_metadata = default_ai_metadata(metadata)
    result = await mythic.execute_custom_query(
        client,
        CREATE_CHANNEL_MUTATION,
        variables={
            "name": channel_name,
            "description": description,
            "containerId": int(container["id"]),
            "model": model,
            "tokenId": int(token["id"]),
            "metadata": built_metadata,
        },
    )
    created = _require_success("chat channel creation", result.get("chatCreateChannel") or {})
    channel_id = created.get("channel_id") or created.get("id")
    if channel_id is None:
        raise RuntimeError(f"Mythic chat channel creation returned no channel ID: {created}")
    return {
        "chat_channel_id": int(channel_id),
        "chat_channel_name": channel_name,
        "chat_container_id": int(container["id"]),
        "api_token_id": int(token["id"]),
        "chat_runtime_identity": _chat_runtime_identity_from_metadata(built_metadata),
    }


def _prepared_channel_result(channel: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    metadata = channel.get("ai_metadata")
    config = metadata.get("config") if isinstance(metadata, dict) else {}
    if not isinstance(config, dict):
        config = {}
    return {
        "chat_channel_id": int(channel["id"]),
        "chat_channel_name": str(channel["name"]),
        "chat_container_id": int(channel["chat_container_id"]),
        "api_token_id": int(channel["apitokens_id"]),
        "prepared": True,
        "reused": reused,
        "chat_runtime_identity": _chat_runtime_identity_from_metadata(channel.get("ai_metadata")),
        "prepared_policy": {
            "mode": str(config.get("mode") or "").strip().casefold(),
            "autonomous_solve": config.get("autonomous_solve"),
        },
    }


def _prepared_channel_matches_auto(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    config = metadata.get("config")
    if not isinstance(config, dict):
        return False
    return (
        str(config.get("mode") or "").strip().casefold() == "auto"
        and config.get("autonomous_solve") is True
    )


async def find_prepared_channel(client: Any) -> dict[str, Any] | None:
    result = await mythic.execute_custom_query(client, PREPARED_CHANNEL_QUERY)
    for channel in result.get("chat_channel") or []:
        metadata = channel.get("ai_metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        if (
            channel.get("name") == DEFAULT_PREPARED_CHANNEL_NAME
            or metadata.get("prepared_for") == PREPARED_CHANNEL_MARKER
        ) and _prepared_channel_matches_auto(metadata):
            return _prepared_channel_result(channel, reused=True)
    return None


async def prepare_locked_channel(
    client: Any,
    *,
    name: str = DEFAULT_PREPARED_CHANNEL_NAME,
    api_token_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = await find_prepared_channel(client)
    if existing:
        return existing
    prepared_metadata = {"prepared_for": PREPARED_CHANNEL_MARKER}
    if metadata:
        prepared_metadata.update(metadata)
    created = await create_locked_channel(
        client,
        name=name,
        description="Prepared Sage one-shot GOAD evaluation",
        api_token_id=api_token_id,
        metadata=prepared_metadata,
    )
    return {**created, "prepared": True, "reused": False}


async def prepare_bhusa_demo_channel(
    client: Any,
    *,
    channel_name: str = DEFAULT_BHUSA_DEMO_CHANNEL_NAME,
    token_name: str = "Sage BHUSA demo",
) -> dict[str, Any]:
    operation_bot = await resolve_operation_bot(client)
    token = await ensure_api_token(
        client,
        name=token_name,
        operator_id=int(operation_bot["id"]),
    )
    channel = await create_locked_channel(
        client,
        name=channel_name,
        description="BHUSA demo supervised channel",
        api_token_id=int(token["api_token"]["id"]),
        metadata=bhusa_demo_ai_metadata(),
    )
    return {
        "operation_bot": operation_bot,
        "api_token": token,
        "chat_channel": channel,
    }


async def create_message(client: Any, channel_id: int, prompt: str) -> dict[str, Any]:
    result = await mythic.execute_custom_query(
        client,
        CREATE_MESSAGE_MUTATION,
        variables={"channelId": int(channel_id), "message": prompt},
    )
    created = _require_success("chat message creation", result.get("chatCreateMessage") or {})
    if created.get("request_id") is None:
        raise RuntimeError(f"Mythic chat message creation returned no request ID: {created}")
    return {
        "chat_message_id": created.get("message_id"),
        "chat_request_id": int(created["request_id"]),
    }


_SAFE_PROGRESS_TEXT = re.compile(r"^[A-Za-z0-9_.:/ -]{1,120}$")


def _safe_progress_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not _SAFE_PROGRESS_TEXT.fullmatch(text):
        return None
    return text


def _extract_progress_metadata(messages: list[dict[str, Any]]) -> dict[str, Any]:
    operation: str | None = None
    tool_name: str | None = None
    retry_count: int | None = None
    for message in reversed(messages or []):
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            continue
        containers = [metadata]
        container_metadata = metadata.get("container_metadata")
        if isinstance(container_metadata, dict):
            containers.append(container_metadata)
        for container in containers:
            tool_use = container.get("tool_use")
            if isinstance(tool_use, dict):
                tool_name = tool_name or _safe_progress_text(tool_use.get("tool_name"))
                if retry_count is None and isinstance(tool_use.get("retry_count"), int):
                    retry_count = int(tool_use["retry_count"])
            runtime_telemetry = container.get("runtime_telemetry")
            if isinstance(runtime_telemetry, dict):
                operation = operation or _safe_progress_text(
                    runtime_telemetry.get("current_operation")
                    or runtime_telemetry.get("operation")
                )
                tool_name = tool_name or _safe_progress_text(runtime_telemetry.get("tool_name"))
                if retry_count is None and isinstance(runtime_telemetry.get("retry_count"), int):
                    retry_count = int(runtime_telemetry["retry_count"])
        if operation is not None and tool_name is not None and retry_count is not None:
            break
    progress: dict[str, Any] = {}
    if operation is not None:
        progress["current_operation"] = operation
    if tool_name is not None:
        progress["tool_name"] = tool_name
    if retry_count is not None:
        progress["retry_count"] = retry_count
    return progress


def _metadata_containers(message: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return []
    containers = [metadata]
    nested = metadata.get("container_metadata")
    if isinstance(nested, dict):
        containers.append(nested)
    return containers


def _has_input_requested(messages: list[dict[str, Any]]) -> bool:
    return bool(_pending_input_requested_messages(messages))


def _pending_input_requested_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for message in messages or []:
        if message.get("deleted") is True:
            continue
        for metadata in _metadata_containers(message):
            snapshot = metadata.get("input_requested")
            if metadata.get("special_type") != "input_requested" or not isinstance(snapshot, dict):
                continue
            if snapshot.get("status") == "pending":
                pending.append(message)
                break
    return pending


def _require_exact_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise RuntimeError(f"{label} must be an exact integer.")
    return value


async def fetch_request_snapshot(
    client: Any, request_id: int
) -> dict[str, Any]:
    expected_request_id = _require_exact_int(request_id, "Mythic chat request id")
    result = await mythic.execute_custom_query(
        client,
        REQUEST_QUERY,
        variables={"requestId": expected_request_id},
    )
    rows = result.get("chat_request") or []
    if not rows:
        raise RuntimeError(f"Mythic chat request {request_id} was not found.")
    request = rows[0]
    observed_request_id = _require_exact_int(
        request.get("id"), "Returned Mythic chat request id"
    )
    if observed_request_id != expected_request_id:
        raise RuntimeError(
            "Returned Mythic chat request id does not match the requested id."
        )
    request_channel_id = _require_exact_int(
        request.get("channel_id"), "Returned Mythic chat request channel id"
    )
    message_rows = result.get("chat_message") or []
    for message in message_rows:
        _require_exact_int(message.get("id"), "Mythic chat message id")
        message_request_id = _require_exact_int(
            message.get("chat_request_id"), "Mythic chat message request id"
        )
        if message_request_id != expected_request_id:
            raise RuntimeError(
                "Returned Mythic chat message request id does not match the requested id."
            )
        message_channel_id = _require_exact_int(
            message.get("channel_id"), "Mythic chat message channel id"
        )
        if message_channel_id != request_channel_id:
            raise RuntimeError(
                "Returned Mythic chat message channel id does not match the request channel."
            )
    messages = sorted(
        message_rows,
        key=lambda row: row["id"],
    )
    return {"request": request, "messages": messages}


async def approve_pending_input_card(client: Any, request_id: int) -> dict[str, Any]:
    snapshot = await fetch_request_snapshot(client, request_id)
    request = snapshot["request"]
    if request.get("status") != APPROVABLE_INPUT_REQUEST_STATUS:
        raise RuntimeError(
            "Selected Mythic chat request is not in the exact active streaming status."
        )
    pending = _pending_input_requested_messages(snapshot["messages"])
    if len(pending) != 1:
        raise RuntimeError(
            "Expected exactly one unresolved input_requested card for the selected request."
        )
    message_id = _require_exact_int(
        pending[0].get("id"), "Pending input_requested message id"
    )
    result = await mythic.execute_custom_query(
        client,
        CHAT_INPUT_RESPONSE_MUTATION,
        variables={
            "messageId": message_id,
            "action": "accept",
            "response": None,
            "choiceId": None,
        },
    )
    submitted = _require_success(
        "chat input approval", result.get("chatInputResponse") or {}
    )
    returned_message_id = _require_exact_int(
        submitted.get("message_id"), "Approved input_requested message id"
    )
    if returned_message_id != message_id:
        raise RuntimeError("Mythic chat input approval returned the wrong message_id.")
    returned_request_id = _require_exact_int(
        submitted.get("request_id"), "Approved Mythic chat request id"
    )
    selected_request_id = _require_exact_int(
        request.get("id"), "Selected Mythic chat request id"
    )
    if returned_request_id != selected_request_id:
        raise RuntimeError("Mythic chat input approval returned the wrong request_id.")
    return {
        "chat_request_id": selected_request_id,
        "input_request_message_id": message_id,
        "action": "accept",
        "response": submitted,
    }


async def resolve_request_selector(
    client: Any,
    *,
    request_id: int | None = None,
    latest: bool = False,
    channel_id: int | None = None,
) -> int:
    if (request_id is None) == (not latest):
        raise ValueError("select exactly one of request_id or latest")
    if request_id is not None:
        if channel_id is not None:
            raise ValueError("channel_id is valid only with latest")
        return _require_exact_int(request_id, "Selected Mythic chat request id")

    channel_result = await mythic.execute_custom_query(
        client, SAGE_CHANNEL_IDS_QUERY
    )
    sage_channel_ids = [
        _require_exact_int(
            row.get("id"), "Active Sage chat channel id"
        )
        for row in channel_result.get("chat_channel") or []
    ]
    if channel_id is not None:
        selected_channel_id = _require_exact_int(
            channel_id, "Selected Sage chat channel id"
        )
        if selected_channel_id not in sage_channel_ids:
            raise RuntimeError(
                f"Mythic chat channel {channel_id} is not an active Sage AI channel."
            )
        sage_channel_ids = [selected_channel_id]
    if not sage_channel_ids:
        raise RuntimeError("No active Sage chat channels were found.")
    request_result = await mythic.execute_custom_query(
        client,
        LATEST_REQUEST_QUERY,
        variables={"channelIds": sage_channel_ids},
    )
    requests = request_result.get("chat_request") or []
    if not requests:
        scope = (
            f"channel {channel_id}"
            if channel_id is not None
            else "active Sage channels"
        )
        raise RuntimeError(f"No Mythic chat requests were found for {scope}.")
    return _require_exact_int(
        requests[0].get("id"), "Latest Mythic chat request id"
    )


async def wait_for_request(
    client: Any,
    request_id: int,
    *,
    timeout_seconds: int = 1800,
    poll_interval_seconds: float = 5.0,
    progress_sink: Any | None = None,
    heartbeat_interval_seconds: float = 60.0,
    stop_on_input_requested: bool = False,
) -> dict[str, Any]:
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    last: dict[str, Any] | None = None
    last_progress_signature: tuple[str, int, str | None, str | None, int | None] | None = None
    last_progress_emit_at = started_at
    while time.monotonic() < deadline:
        try:
            snapshot = await fetch_request_snapshot(client, request_id)
        except RuntimeError:
            snapshot = None
        if snapshot is not None:
            last = snapshot["request"]
            status = str(last.get("status") or "").casefold()
            messages = snapshot["messages"]
            progress_metadata = _extract_progress_metadata(messages)
            signature = (
                status,
                len(messages),
                progress_metadata.get("current_operation"),
                progress_metadata.get("tool_name"),
                progress_metadata.get("retry_count"),
            )
            now = time.monotonic()
            elapsed_seconds = max(0, int(now - started_at))
            remaining_seconds = max(0, int(deadline - now))
            emit_event = None
            if signature != last_progress_signature:
                emit_event = "request_progress"
            elif progress_sink is not None and (now - last_progress_emit_at) >= heartbeat_interval_seconds:
                emit_event = "request_heartbeat"
            if progress_sink is not None and emit_event is not None:
                progress_sink({
                    "event": emit_event,
                    "chat_request_id": int(request_id),
                    "status": status,
                    "message_count": len(messages),
                    "updated_at": last.get("updated_at"),
                    "elapsed_seconds": elapsed_seconds,
                    "remaining_seconds": remaining_seconds,
                    **progress_metadata,
                })
                last_progress_signature = signature
                last_progress_emit_at = now
            if status in TERMINAL_STATUSES:
                return {"request": last, "messages": messages}
            if stop_on_input_requested and _has_input_requested(messages):
                return {
                    "request": last,
                    "messages": messages,
                    "halt_reason": "operator_input_requested",
                }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval_seconds, remaining))
    raise TimeoutError(
        f"Mythic chat request {request_id} did not finish within {timeout_seconds}s; "
        f"last status={None if last is None else last.get('status')!r}"
    )


def request_snapshot_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    request = snapshot.get("request") or {}
    messages = snapshot.get("messages") or []
    result = {
        "chat_channel_id": request.get("channel_id"),
        "chat_request_id": request.get("id"),
        "status": request.get("status"),
        "error": request.get("error"),
        "messages": messages,
        "runtime_telemetry": extract_runtime_telemetry(messages),
        "progress": _extract_progress_metadata(messages),
    }
    if snapshot.get("halt_reason"):
        result["halt_reason"] = snapshot["halt_reason"]
    return result


def _validate_transcript_identity(
    request: dict[str, Any], messages: list[dict[str, Any]]
) -> None:
    request_id = _require_exact_int(
        request.get("id"), "Transcript request id"
    )
    channel_id = _require_exact_int(
        request.get("channel_id"), "Transcript request channel id"
    )
    for message in messages:
        _require_exact_int(message.get("id"), "Transcript message id")
        message_request_id = _require_exact_int(
            message.get("chat_request_id"),
            "Transcript message chat_request_id",
        )
        message_channel_id = _require_exact_int(
            message.get("channel_id"), "Transcript message channel_id"
        )
        if message_request_id != request_id:
            raise RuntimeError(
                "Transcript export message chat_request_id does not match request id."
            )
        if message_channel_id != channel_id:
            raise RuntimeError(
                "Transcript export message channel_id does not match request channel_id."
            )


def build_transcript_export(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    request = dict(snapshot.get("request") or {})
    messages = sorted(
        [dict(row) for row in snapshot.get("messages") or []],
        key=lambda row: (
            row.get("id")
            if type(row.get("id")) is int
            else -1
        ),
    )
    _validate_transcript_identity(request, messages)
    result = request_snapshot_result(
        {**snapshot, "request": request, "messages": messages}
    )
    return {
        "schema": "sage-native-chat-transcript-v1",
        "chat_channel_id": request.get("channel_id"),
        "chat_request_id": request.get("id"),
        "status": request.get("status"),
        "error": request.get("error"),
        "request": request,
        "messages": messages,
        "runtime_telemetry": result["runtime_telemetry"],
        "progress": result["progress"],
        **(
            {"halt_reason": snapshot["halt_reason"]}
            if snapshot.get("halt_reason")
            else {}
        ),
    }


def write_transcript_export(
    path: str | Path, transcript: dict[str, Any]
) -> Path:
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    temporary.write_text(
        json.dumps(transcript, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(resolved)
    return resolved


def extract_runtime_telemetry(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return Sage's observed runtime telemetry from the terminal assistant message."""
    for message in reversed(messages or []):
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            continue
        telemetry = metadata.get("runtime_telemetry")
        if not isinstance(telemetry, dict):
            container_metadata = metadata.get("container_metadata")
            if isinstance(container_metadata, dict):
                telemetry = container_metadata.get("runtime_telemetry")
        if isinstance(telemetry, dict):
            return dict(telemetry)
    return {}


def evaluator_result_view(result: dict[str, Any]) -> dict[str, Any]:
    """Return the allowlisted evaluator view; the normal full result remains untouched."""
    import sys

    sage_root = REPO_ROOT / "Payload_Type" / "sage"
    if str(sage_root) not in sys.path:
        sys.path.insert(0, str(sage_root))
    from ai.hillclimb.evaluator_evidence import project_identifier, project_runtime_telemetry

    return {
        "schema": "native-chat-evaluator-result-v1",
        "chat_channel_id": project_identifier(result.get("chat_channel_id")),
        "chat_request_id": project_identifier(result.get("chat_request_id")),
        "status": project_identifier(result.get("status"), casefold=True),
        "error_present": bool(result.get("error")),
        "evaluator_evidence": project_runtime_telemetry(result.get("runtime_telemetry")),
    }


def emit_jsonl_event(event: dict[str, Any]) -> None:
    print(json.dumps(event, sort_keys=True, default=str), file=sys.stderr, flush=True)


def build_demo_manifest(
    result: dict[str, Any],
    *,
    run_status: str = "clean",
    runtime_identity: dict[str, Any] | None = None,
    startup_identity: dict[str, Any] | None = None,
    range_state: dict[str, Any] | None = None,
    snapshot: str | None = None,
    callback: dict[str, Any] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    proofs: list[dict[str, Any]] | None = None,
    artifact_paths: list[str | Path] | None = None,
    readiness_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = _load_module("sage_readiness_contract_for_manifest", READINESS_CONTRACT_PATH)
    artifacts = [contract.hash_file(item) for item in artifact_paths or []]
    telemetry = result.get("runtime_telemetry") or {}
    semantic_transactions = telemetry.get("transactions") or []
    flattened_tasks: list[dict[str, Any]] = []
    for transaction in semantic_transactions:
        if not isinstance(transaction, dict):
            continue
        child_tasks = transaction.get("child_tasks") or []
        for child_task in child_tasks:
            if isinstance(child_task, dict):
                flattened_tasks.append(dict(child_task))
    effective_runtime_identity = (
        result.get("chat_runtime_identity")
        or runtime_identity
        or contract.startup_identity_from_env()
    )
    process_identity = startup_identity or contract.startup_identity_from_env()
    readiness_snapshot = readiness_snapshot or {}
    manifest = {
        "schema": "sage-native-chat-demo-manifest-v1",
        "run_status": str(run_status or "clean"),
        "run_status_evidence": {
            "value": str(run_status or "clean"),
            "readiness_ready": bool(readiness_snapshot.get("ready")),
        },
        "runtime_identity": effective_runtime_identity,
        "startup_identity": process_identity,
        "range": {
            "identity": (range_state or {}).get("identity"),
            "state": (range_state or {}).get("state"),
            "range_number": (range_state or {}).get("range_number"),
            "range_state": (range_state or {}).get("range_state"),
            "snapshot": snapshot,
        },
        "callback": callback or {},
        "chat": {
            "channel_id": result.get("chat_channel_id"),
            "channel_name": result.get("chat_channel_name"),
            "request_id": result.get("chat_request_id"),
            "status": result.get("status"),
        },
        "semantic_transactions": semantic_transactions,
        "tasks": tasks if tasks is not None else flattened_tasks,
        "proofs": proofs if proofs is not None else (telemetry.get("proof_lineage") or []),
        "artifacts": artifacts,
        "readiness": readiness_snapshot,
    }
    return contract.redact_structure(manifest)


def write_demo_manifest(path: str | Path, manifest: dict[str, Any]) -> Path:
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return resolved


async def run_native_chat_turn(
    client: Any,
    prompt: str,
    *,
    timeout_seconds: int = 1800,
    poll_interval_seconds: float = 5.0,
    channel_name: str | None = None,
    api_token_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    use_prepared_channel: bool = True,
    progress_sink: Any | None = None,
    manifest_path: str | Path | None = None,
    manifest_context: dict[str, Any] | None = None,
    runtime_dbs_archived: bool = False,
    stop_on_input_requested: bool = False,
) -> dict[str, Any]:
    if manifest_path and not runtime_dbs_archived:
        raise RuntimeError("--manifest-path requires --runtime-dbs-archived.")

    readiness_snapshot: dict[str, Any] | None = None
    if manifest_path:
        readiness_snapshot = await inspect_readiness(
            client,
            api_token_id=api_token_id,
            runtime_dbs_archived=runtime_dbs_archived,
        )
        if not readiness_snapshot.get("ready"):
            raise RuntimeError("Demo manifest preflight failed: shared readiness contract is not ready.")

    channel = None
    if use_prepared_channel and channel_name is None:
        channel = await find_prepared_channel(client)
        identity = (
            channel.get("prepared_policy")
            if isinstance(channel, dict)
            else None
        )
        if (
            not isinstance(identity, dict)
            or identity.get("mode") != "auto"
            or identity.get("autonomous_solve") is not True
        ):
            channel = None
    if channel is None:
        channel = await create_locked_channel(
            client,
            name=channel_name,
            api_token_id=api_token_id,
            metadata=metadata,
        )
    message = await create_message(client, channel["chat_channel_id"], prompt)
    if progress_sink is not None:
        progress_sink({
            "event": "request_started",
            "chat_channel_id": int(channel["chat_channel_id"]),
            "chat_channel_name": str(channel["chat_channel_name"]),
            "chat_request_id": int(message["chat_request_id"]),
        })
    completed = await wait_for_request(
        client,
        message["chat_request_id"],
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        progress_sink=progress_sink,
        stop_on_input_requested=stop_on_input_requested,
    )
    request = completed["request"]
    messages = completed["messages"]
    result = {
        **channel,
        **message,
        "status": request.get("status"),
        "error": request.get("error"),
        "messages": messages,
        "runtime_telemetry": extract_runtime_telemetry(messages),
    }
    if completed.get("halt_reason"):
        result["halt_reason"] = completed["halt_reason"]
    if progress_sink is not None:
        progress_sink({
            "event": (
                "request_paused"
                if completed.get("halt_reason") == "operator_input_requested"
                else "request_terminal"
            ),
            "chat_channel_id": int(channel["chat_channel_id"]),
            "chat_request_id": int(message["chat_request_id"]),
            "status": str(result.get("status") or ""),
            **(
                {"halt_reason": completed["halt_reason"]}
                if completed.get("halt_reason")
                else {}
            ),
        })
    if manifest_path:
        context = dict(manifest_context or {})
        if readiness_snapshot is not None:
            contract = _readiness_contract_module()
            context.setdefault("startup_identity", readiness_snapshot.get("runtime_identity") or {})
            foothold = readiness_snapshot.get("foothold") or {}
            selected_foothold_id = foothold.get("selected_foothold_cb")
            selected_callback = next(
                (
                    dict(item)
                    for item in foothold.get("callbacks") or []
                    if isinstance(item, dict) and item.get("display_id") == selected_foothold_id
                ),
                {},
            )
            context.setdefault(
                "range_state",
                {
                    "range_number": (readiness_snapshot.get("ludus") or {}).get("range_number"),
                    "range_state": (readiness_snapshot.get("ludus") or {}).get("range_state"),
                },
            )
            context.setdefault("callback", selected_callback)
            context.setdefault(
                "readiness_snapshot",
                contract.redact_structure(readiness_snapshot),
            )
        manifest = build_demo_manifest(result, **context)
        written = write_demo_manifest(manifest_path, manifest)
        result["demo_manifest"] = {
            "path": str(written),
            "sha256": _load_module(
                "sage_readiness_contract_for_manifest_hash", READINESS_CONTRACT_PATH
            ).hash_file(written)["sha256"],
        }
    return result


async def _run(args: argparse.Namespace) -> int:
    client = await login(
        server=args.server,
        user=args.user,
        password=args.password,
        env_path=args.env_path,
    )
    if args.command == "inspect":
        result = await inspect_readiness(
            client,
            api_token_id=args.api_token_id,
            runtime_dbs_archived=args.runtime_dbs_archived,
        )
    elif args.command == "ensure-token":
        result = await ensure_api_token(client, name=args.name)
    elif args.command == "prepare":
        token = await ensure_api_token(client, name=args.token_name)
        result = {
            "api_token": token,
            "prepared_channel": await prepare_locked_channel(
                client,
                name=args.channel_name,
                api_token_id=int(token["api_token"]["id"]),
            ),
        }
    elif args.command == "demo-prepare":
        result = await prepare_bhusa_demo_channel(
            client,
            channel_name=args.channel_name,
            token_name=args.token_name,
        )
    elif args.command in {"status", "follow", "transcript"}:
        request_id = await resolve_request_selector(
            client,
            request_id=args.request_id,
            latest=args.latest,
            channel_id=args.channel_id,
        )
        if args.command == "follow":
            snapshot = await wait_for_request(
                client,
                request_id,
                timeout_seconds=args.timeout,
                poll_interval_seconds=args.poll_interval,
                progress_sink=emit_jsonl_event,
                stop_on_input_requested=True,
            )
        else:
            snapshot = await fetch_request_snapshot(client, request_id)
        if args.command == "transcript":
            result = build_transcript_export(snapshot)
            if args.output:
                written = write_transcript_export(args.output, result)
                result["export_path"] = str(written)
        else:
            result = request_snapshot_result(snapshot)
            if args.output_mode == "eval":
                result = evaluator_result_view(result)
    elif args.command == "canary":
        result = await run_native_chat_turn(
            client,
            args.prompt,
            timeout_seconds=args.timeout,
            poll_interval_seconds=args.poll_interval,
            channel_name=args.channel_name,
            api_token_id=args.api_token_id,
            metadata=canary_ai_metadata(max_steps=args.max_steps),
            use_prepared_channel=False,
            progress_sink=emit_jsonl_event,
            stop_on_input_requested=True,
        )
    elif args.command == "approve-pending":
        result = await approve_pending_input_card(client, args.request_id)
    else:
        result = await run_native_chat_turn(
            client,
            args.prompt,
            timeout_seconds=args.timeout,
            poll_interval_seconds=args.poll_interval,
            channel_name=args.channel_name,
            api_token_id=args.api_token_id,
            use_prepared_channel=not args.new_channel,
            progress_sink=emit_jsonl_event,
            manifest_path=args.manifest_path,
            runtime_dbs_archived=args.runtime_dbs_archived,
            manifest_context={
                "run_status": args.run_status,
                "snapshot": args.snapshot,
                "artifact_paths": args.artifact_path,
            },
        )
        if args.output_mode == "eval":
            result = evaluator_result_view(result)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if args.command == "inspect" and not result.get("ready"):
        return 1
    if (
        args.command == "canary"
        and result.get("halt_reason") != "operator_input_requested"
    ):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password")
    parser.add_argument("--env-path")
    parser.add_argument("--api-token-id", type=int)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument(
        "--runtime-dbs-archived",
        "--operator-db-cleanup-confirmed",
        dest="runtime_dbs_archived",
        action="store_true",
    )
    ensure_token = sub.add_parser("ensure-token")
    ensure_token.add_argument("--name", default="Sage native chat")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--channel-name", default=DEFAULT_PREPARED_CHANNEL_NAME)
    prepare.add_argument("--token-name", default="Sage native chat")
    demo_prepare = sub.add_parser("demo-prepare")
    demo_prepare.add_argument("--channel-name", default=DEFAULT_BHUSA_DEMO_CHANNEL_NAME)
    demo_prepare.add_argument("--token-name", default="Sage BHUSA demo")

    def add_request_selector(command_parser: argparse.ArgumentParser) -> None:
        selector = command_parser.add_mutually_exclusive_group(required=True)
        selector.add_argument("--request-id", type=int)
        selector.add_argument("--latest", action="store_true")
        command_parser.add_argument(
            "--channel-id",
            type=int,
            help="Limit --latest to one active Sage AI channel.",
        )

    status = sub.add_parser("status")
    add_request_selector(status)
    status.add_argument(
        "--output-mode", choices=("full", "eval"), default="full"
    )
    follow = sub.add_parser("follow")
    add_request_selector(follow)
    follow.add_argument("--timeout", type=int, default=1800)
    follow.add_argument("--poll-interval", type=float, default=5.0)
    follow.add_argument(
        "--output-mode", choices=("full", "eval"), default="full"
    )
    transcript = sub.add_parser("transcript")
    add_request_selector(transcript)
    transcript.add_argument("--output")
    canary = sub.add_parser("canary")
    canary.add_argument("--prompt", required=True)
    canary.add_argument("--timeout", type=int, default=300)
    canary.add_argument("--poll-interval", type=float, default=2.0)
    canary.add_argument("--channel-name")
    canary.add_argument("--max-steps", type=int, default=20)
    approve_pending = sub.add_parser("approve-pending")
    approve_pending.add_argument("--request-id", type=int, required=True)

    run = sub.add_parser("run")
    run.add_argument("--prompt", default=DEFAULT_OBJECTIVE)
    run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--poll-interval", type=float, default=5.0)
    run.add_argument("--channel-name")
    run.add_argument(
        "--output-mode",
        choices=("full", "eval"),
        default="full",
        help="Return full operator-visible messages (default) or only allowlisted evaluator evidence.",
    )
    run.add_argument(
        "--new-channel",
        action="store_true",
        help="Ignore an empty prepared Sage channel and create a new channel.",
    )
    run.add_argument("--manifest-path")
    run.add_argument(
        "--runtime-dbs-archived",
        "--operator-db-cleanup-confirmed",
        dest="runtime_dbs_archived",
        action="store_true",
    )
    run.add_argument("--artifact-path", action="append", default=[])
    run.add_argument("--run-status", choices=("clean", "resumed"), default="clean")
    run.add_argument("--snapshot")
    return parser


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
