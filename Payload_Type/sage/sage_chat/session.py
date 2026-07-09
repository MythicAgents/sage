"""Channel-keyed adapter over ``Model``'s session registry.

The session key is ``str(ChannelID)`` — the only stable per-conversation id (``RequestID``
changes per turn; PRD Section 7). This is a thin wrapper over the existing
``add_session``/``get_session``/``remove_session`` in ``ai.langgraph.model`` so the chat
container reuses the same registry the PayloadType path used, keyed by channel instead of task.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mythic_container.ChatBase import ChatRequest

try:  # package vs. flat import, matching the rest of ai/langgraph
    from ai.langgraph.model import add_session, get_session, remove_session
except ImportError:  # pragma: no cover - exercised only under alternate import roots
    from ..ai.langgraph.model import add_session, get_session, remove_session  # type: ignore

if TYPE_CHECKING:
    from ai.langgraph.model import Model


def channel_session_key(request: ChatRequest) -> str:
    return str(request.ChannelID)


async def get_channel_session(request: ChatRequest) -> "Model | None":
    return await get_session(channel_session_key(request))


async def put_channel_session(request: ChatRequest, model: "Model") -> None:
    await add_session(channel_session_key(request), model)


async def drop_channel_session(request: ChatRequest) -> None:
    await remove_session(channel_session_key(request))
