"""Channel-keyed adapter over ``Model``'s session registry.

The session key is ``str(ChannelID)`` — the only stable per-conversation id (``RequestID``
changes per turn; PRD Section 7). This is a thin wrapper over the existing
``add_session``/``get_session``/``remove_session`` in ``ai.langgraph.model`` so the chat
container reuses the same registry the PayloadType path used, keyed by channel instead of task.
"""

from __future__ import annotations

import aiosqlite
from typing import TYPE_CHECKING
from uuid import uuid4

from mythic_container.ChatBase import ChatRequest
from runtime_db import database_path

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
    async with aiosqlite.connect(_CHANNEL_MARKER_DB_PATH or database_path(), timeout=5) as db:
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


async def drop_channel_session(request: ChatRequest, *, expected_model: "Model | None" = None) -> bool:
    if expected_model is not None:
        current = await get_channel_session(request)
        if current is not expected_model:
            return False
    await remove_session(channel_session_key(request))
    return True
