"""Headless driver for evals / trajectory (PRD Section 8, deliverable #2).

Removing the PayloadType deletes the interactive ``chat`` command the eval harness drove. This
replaces it with an in-process driver: a ``SageChat`` subclass whose ``send_*`` surface records
emissions instead of publishing to Mythic's ``chat_response`` queue, plus a helper that builds a
``ChatRequest`` and runs one turn. ``evals/`` and ``ai/hillclimb``/``ai/trajectory`` call this
instead of tasking a live container.

The recorded ``emissions`` list preserves order and captures the response_key discipline and the
single terminal status, so tests and evals can assert on both.
"""

from __future__ import annotations

from typing import Any

from mythic_container.ChatBase import ChatRequest

from .service import SageChat


class HeadlessSageChat(SageChat):
    """A ``SageChat`` that records every response instead of sending it to Mythic.

    Overriding the ``send_*`` surface captures both the ``ChatTurnContext`` route (deltas, complete,
    error) and the ``ChatStreamEmitter`` route (``send_text`` per message block), because both call
    back through these methods on the ``Chat`` instance.
    """

    def __init__(self) -> None:
        self.emissions: list[dict[str, Any]] = []
        self.channel_metadata_updates: list[dict[str, Any]] = []

    def _record(self, kind: str, response_key: str, **fields: Any) -> None:
        self.emissions.append({"kind": kind, "response_key": response_key, **fields})

    async def update_channel_metadata(self, request, channel_metadata):
        # Capture instead of firing the real Mythic RPC (the base version would hang with no Mythic).
        # The ChatTurnContext.update_channel_metadata delegates here via self.chat.update_channel_metadata.
        self.channel_metadata_updates.append(channel_metadata)
        return None

    async def send_streaming(self, request, response_key, content="", metadata=None):
        self._record("streaming", response_key, content=content, metadata=metadata)

    async def send_delta(self, request, response_key, content="", metadata=None):
        self._record("delta", response_key, content=content, metadata=metadata)

    async def send_text(self, request, response_key, content="", metadata=None):
        self._record("text", response_key, content=content, metadata=metadata)

    async def send_response(
        self,
        request,
        response_key,
        content="",
        is_delta=False,
        complete=False,
        complete_request=False,
        status="",
        error="",
        metadata=None,
    ):
        self._record(
            "response",
            response_key,
            content=content,
            status=status,
            complete=complete,
            complete_request=complete_request,
            metadata=metadata,
        )

    async def send_complete(self, request, response_key, metadata=None, content="", complete_request=False):
        self._record("complete", response_key, content=content, metadata=metadata, complete_request=complete_request)

    async def send_error(self, request, response_key, error="", metadata=None, complete_request=True):
        self._record("error", response_key, error=error, metadata=metadata, complete_request=complete_request)

    # NOTE: the input-request surface (send_input_request / send_approval_request / send_single_choice_request)
    # is intentionally NOT overridden — the base implementations route through send_complete (which we DO
    # override), so an approval request records faithfully as a complete_request=False `input_requested`
    # block (the channel-release), not a terminal. Keeps the recorder aligned with the real SDK semantics.

    @property
    def terminal_emissions(self) -> list[dict[str, Any]]:
        """The emissions that release the channel — exactly one per well-formed turn."""
        return [e for e in self.emissions if e.get("complete_request")]


def build_chat_request(
    prompt: str,
    *,
    channel_id: int = 1,
    operation_id: int = 1,
    request_id: int = 1,
    model: str = "Sage",
    config: dict | None = None,
    secrets: dict | None = None,
) -> ChatRequest:
    """Construct a ``ChatRequest`` for a headless turn (snake_case kwargs, PascalCase attrs)."""
    return ChatRequest(
        container_name="sage",
        operation_id=operation_id,
        channel_id=channel_id,
        request_id=request_id,
        model=model,
        prompt=prompt,
        config=config or {},
        secrets=secrets or {},
    )


async def run_headless_turn(
    prompt: str,
    *,
    chat: HeadlessSageChat | None = None,
    **request_kwargs: Any,
) -> HeadlessSageChat:
    """Run one chat turn headlessly and return the driver (inspect ``.emissions``).

    Pass a shared ``chat`` across calls to keep channel continuity within one eval session; omit it
    for a one-shot turn. Uses the same ``SageChat.chat()`` path a live request would.
    """
    driver = chat or HeadlessSageChat()
    request = build_chat_request(prompt, **request_kwargs)
    await driver.chat(request)
    return driver
