"""Channel-keyed adapter over ``Model``'s session registry.

The session key is ``str(ChannelID)`` — the only stable per-conversation id (``RequestID``
changes per turn). This is a thin wrapper over the existing
``add_session``/``get_session``/``remove_session`` in ``ai.langgraph.model`` so the chat
container reuses the same registry the PayloadType path used, keyed by channel instead of task.
"""

from __future__ import annotations

import aiosqlite
import json
import sqlite3
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mythic_container.ChatBase import ChatRequest
from runtime_db import DatabaseUnavailable, database_path, unavailable_message

try:  # package vs. flat import, matching the rest of ai/langgraph
    from ai.langgraph.model import add_session, get_session, remove_session
except ImportError:  # pragma: no cover - exercised only under alternate import roots
    from ..ai.langgraph.model import add_session, get_session, remove_session  # type: ignore

if TYPE_CHECKING:
    from ai.langgraph.model import Model


# Model.initialize() uses this same runtime SQLite file for LangGraph checkpoints. A tiny
# channel marker survives process restarts without introducing a second state store.
_CHANNEL_MARKER_DB_PATH: str | None = None


async def claim_channel_marker(request: ChatRequest) -> bool:
    """Atomically mark a native channel as seen; return True only on its first claim.

    A fresh process has no channel Model registry. Existing markers let the chat service
    distinguish a restarted channel from a new channel even when Mythic sends empty
    Context. Database failures propagate and stop the request before Model invocation.
    """
    path = _CHANNEL_MARKER_DB_PATH or database_path()
    try:
        async with aiosqlite.connect(path, timeout=5) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS sage_chat_channel_markers ("
                "operation_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, "
                "PRIMARY KEY (operation_id, channel_id))"
            )
            cursor = await db.execute(
                "INSERT OR IGNORE INTO sage_chat_channel_markers "
                "(operation_id, channel_id) VALUES (?, ?)",
                (int(request.OperationID), int(request.ChannelID)),
            )
            await db.commit()
            return cursor.rowcount == 1
    except sqlite3.OperationalError as exc:
        # This is the first sage.db access on a chat turn; name the file in the operator-facing error.
        raise DatabaseUnavailable(unavailable_message(path, exc)) from exc

# Versioned per-channel restoration record, kept beside the marker in the same runtime file.
# The LangGraph checkpoint stays the owner of conversation history; this row holds only what the
# checkpoint does not: which generation the channel uses, the identity/config it was bound to, the
# runtime ``/mode`` override, the engagement binding, an outstanding approval card and its claim.
# It never holds secrets: config is a SHA-256 signature and actions are rebuilt from the checkpoint.
CHANNEL_SESSION_SCHEMA_VERSION = 1
CHANNEL_SESSION_ACTIVE = "active"
CHANNEL_SESSION_RETIRED = "retired"


class ChannelSessionRecordInvalid(ValueError):
    """The stored channel record is unreadable or has an unsupported schema."""


def channel_session_db_path() -> str:
    """The runtime file holding markers, channel records and LangGraph checkpoints."""
    return str(_CHANNEL_MARKER_DB_PATH or database_path())


async def _ensure_session_table(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS sage_chat_channel_sessions ("
        "operation_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, "
        "schema_version INTEGER NOT NULL, payload TEXT NOT NULL, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (operation_id, channel_id))"
    )


async def load_channel_session_record(operation_id: int, channel_id: int) -> dict | None:
    """Return the stored record, ``None`` when absent; an unreadable row raises."""
    path = channel_session_db_path()
    try:
        async with aiosqlite.connect(path, timeout=5) as db:
            await _ensure_session_table(db)
            cursor = await db.execute(
                "SELECT schema_version, payload FROM sage_chat_channel_sessions "
                "WHERE operation_id=? AND channel_id=?",
                (int(operation_id), int(channel_id)),
            )
            row = await cursor.fetchone()
            await db.commit()
    except sqlite3.OperationalError as exc:
        raise DatabaseUnavailable(unavailable_message(path, exc)) from exc
    if row is None:
        return None
    schema_version, payload = row
    if schema_version != CHANNEL_SESSION_SCHEMA_VERSION:
        raise ChannelSessionRecordInvalid(
            f"unsupported channel session schema {schema_version!r}"
        )
    try:
        record = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ChannelSessionRecordInvalid("channel session record is not valid JSON") from exc
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != CHANNEL_SESSION_SCHEMA_VERSION
        or record.get("operation_id") != int(operation_id)
        or record.get("channel_id") != int(channel_id)
        or record.get("status") not in {CHANNEL_SESSION_ACTIVE, CHANNEL_SESSION_RETIRED}
        or not isinstance(record.get("thread_id"), str)
    ):
        raise ChannelSessionRecordInvalid("channel session record has an invalid shape")
    return record


async def save_channel_session_record(record: dict) -> None:
    """Replace the channel's record in one transaction."""
    payload = dict(record)
    payload["schema_version"] = CHANNEL_SESSION_SCHEMA_VERSION
    operation_id = int(payload["operation_id"])
    channel_id = int(payload["channel_id"])
    payload["operation_id"] = operation_id
    payload["channel_id"] = channel_id
    path = channel_session_db_path()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    try:
        async with aiosqlite.connect(path, timeout=5) as db:
            await _ensure_session_table(db)
            await db.execute(
                "INSERT INTO sage_chat_channel_sessions "
                "(operation_id, channel_id, schema_version, payload, updated_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(operation_id, channel_id) DO UPDATE SET "
                "schema_version=excluded.schema_version, payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (operation_id, channel_id, CHANNEL_SESSION_SCHEMA_VERSION, encoded),
            )
            await db.commit()
    except sqlite3.OperationalError as exc:
        raise DatabaseUnavailable(unavailable_message(path, exc)) from exc


async def update_channel_session_record(
    operation_id: int,
    channel_id: int,
    *,
    expected_thread_id: str | None = None,
    **fields: Any,
) -> bool:
    """Merge fields into an existing record; False when absent or bound to another generation."""
    record = await load_channel_session_record(operation_id, channel_id)
    if record is None:
        return False
    if expected_thread_id is not None and record.get("thread_id") != expected_thread_id:
        return False
    record.update(fields)
    await save_channel_session_record(record)
    return True


async def retire_channel_session_record(
    operation_id: int,
    channel_id: int,
    *,
    thread_id: str,
    reason: str,
) -> bool:
    """Mark an intentionally dropped session so a later restart keeps today's refusal."""
    try:
        return await update_channel_session_record(
            operation_id,
            channel_id,
            expected_thread_id=thread_id,
            status=CHANNEL_SESSION_RETIRED,
            retired_reason=str(reason or "dropped"),
            pending=None,
        )
    except ChannelSessionRecordInvalid:
        return False


def channel_session_key(request: ChatRequest) -> str:
    return str(request.ChannelID)


def next_channel_thread_id(request: ChatRequest) -> str:
    key = channel_session_key(request)
    # This identifier is also a durable LangGraph checkpoint key. A process-local counter would
    # restart at 1 after every Sage restart and could reattach a new Model to an old pending HITL
    # checkpoint. A random generation keeps replacement/restart sessions collision-resistant.
    return f"{key}:generation:{uuid4().hex}"


def bind_channel_thread_id(request: ChatRequest, model: "Model") -> str:
    thread_id = str(getattr(model, "_thread_id_override", "") or "").strip()
    if thread_id:
        return thread_id
    thread_id = next_channel_thread_id(request)
    model._thread_id_override = thread_id
    return thread_id


async def get_channel_session(request: ChatRequest) -> "Model | None":
    return await get_session(channel_session_key(request))


async def put_channel_session(request: ChatRequest, model: "Model") -> None:
    await add_session(channel_session_key(request), model)


async def drop_channel_session(
    request: ChatRequest,
    *,
    expected_model: "Model | None" = None,
    reason: str = "dropped",
) -> bool:
    current = await get_channel_session(request)
    if expected_model is not None and current is not expected_model:
        return False
    await remove_session(channel_session_key(request))
    # An intentional drop (stop, cancel, runtime error, rotation) keeps today's "start a new chat"
    # outcome after a restart too. Only a session still registered when the process died is restored.
    thread_id = str(getattr(current, "_thread_id_override", "") or "")
    if thread_id:
        try:
            await retire_channel_session_record(
                int(request.OperationID),
                int(request.ChannelID),
                thread_id=thread_id,
                reason=reason,
            )
        except DatabaseUnavailable:
            # The in-memory drop already happened; a later restore then sees an active row and
            # validates it like any other, so this degrades to restoration, never to new authority.
            pass
    return True
