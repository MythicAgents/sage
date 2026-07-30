"""Channel-keyed adapter over ``Model``'s session registry.

The session key is ``str(ChannelID)`` — the only stable per-conversation id (``RequestID``
changes per turn; PRD Section 7). This is a thin wrapper over the existing
``add_session``/``get_session``/``remove_session`` in ``ai.langgraph.model`` so the chat
container reuses the same registry the PayloadType path used, keyed by channel instead of task.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from mythic_container.ChatBase import ChatRequest

try:  # package vs. flat import, matching the rest of ai/langgraph
    from ai.langgraph.model import add_session, get_session, remove_session
except ImportError:  # pragma: no cover - exercised only under alternate import roots
    from ..ai.langgraph.model import add_session, get_session, remove_session  # type: ignore

if TYPE_CHECKING:
    from ai.langgraph.model import Model

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
