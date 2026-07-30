"""The ``response_emitter`` that bridges ``Model``'s egress to the Mythic chat queue.

``Model`` streams at *message* granularity: its single egress ``_stream_message_to_mythic``
is called once per complete ``AIMessage``/``ToolMessage`` with a pre-formatted blob (see
fork [F2]). This emitter maps each such blob to one visible
Mythic response block with its own ``response_key`` — satisfying the response_key discipline
(one key per assistant block) without pretending to token-stream. True token streaming is a
follow-up that adds an ``on_llm_new_token`` hook feeding ``send_delta`` on a stable key.

The service finalizes the last visible assistant block with the turn's single terminal status,
avoiding a separate empty completion row in the Mythic UI.
"""

from __future__ import annotations

from typing import Any

from mythic_container.ChatBase import ChatRequest


class ChatStreamEmitter:
    """Callable matching ``Model``'s ``response_emitter: Callable[[str], Awaitable[bool]]``.

    ``chat`` is the ``SageChat`` (or any object exposing the ``Chat.send_text`` coroutine);
    ``request`` is the live ``ChatRequest`` for this turn. One emitter is bound per turn.
    """

    def __init__(self, chat: Any, request: ChatRequest):
        self._chat = chat
        self._request = request
        self._block = 0
        self._agent_text_block = 0
        self.last_response_key = ""
        self.last_content = ""

    async def __call__(self, formatted_message: str) -> bool:
        # Mirror the RPC path's empty-guard: a blank block would create an empty UI part.
        if not formatted_message:
            return False
        self._block += 1
        response_key = f"assistant:{self._request.RequestID}:{self._block}"
        try:
            await self._chat.send_text(self._request, response_key, content=formatted_message)
            self.last_response_key = response_key
            self.last_content = formatted_message
            return True
        except Exception:
            # The single-egress caller (_stream_message_to_mythic) already logs; return False so it
            # records a failed stream without raising into the graph's astream loop.
            return False

    async def emit_final_response(
        self,
        *,
        event_id: str,
        content: str,
        control_transitions: list[dict[str, str]] | None = None,
    ) -> bool:
        """Project a lifecycle-owned terminal text block with its evidence ID."""
        if not event_id or not content:
            return False
        response_key = f"event:{event_id}"
        try:
            await self._chat.send_text(
                self._request,
                response_key,
                content=content,
                metadata={
                    "event_id": event_id,
                    **(
                        {"control_transitions": control_transitions}
                        if control_transitions
                        else {}
                    ),
                },
            )
            self.last_response_key = response_key
            self.last_content = content
            return True
        except Exception:
            return False

    async def emit_tool_use(
        self,
        *,
        event_id: str = "",
        tool_call_id: str,
        tool_name: str,
        tool_source: str,
        status: str,
        content: str,
        complete: bool,
        arguments_present: bool = False,
        arguments: str | None = None,
        result_preview: str | None = None,
        output: str | None = None,
        delegation_id: str | None = None,
        delegation_name: str | None = None,
    ) -> bool:
        """Emit one tool-use card ChatResponse.

        Reuses the same response_key across the started and finished emissions so the Mythic React
        UI updates the card in place. Separate from assistant text keys and the turn terminal, so it
        never touches the always-terminal invariant. Fail-soft: returns False on send errors.
        """
        response_key = (
            f"event:{event_id}"
            if event_id
            else f"tool_use:{tool_call_id}:{tool_name}"
        )
        tool_use: dict[str, Any] = {
            "status": status,
            "tool_name": tool_name,
            "tool_source": tool_source,
            "arguments_present": bool(arguments_present),
        }
        if tool_call_id:
            tool_use["tool_call_id"] = tool_call_id
        if arguments:
            tool_use["arguments"] = arguments
        if result_preview:
            tool_use["result_preview"] = result_preview
        if output:
            # Full raw result → Mythic stores it in chat_message.tool_output, strips it from message
            # metadata, and serves it lazily via "View output" (never in normal subscriptions), so a big
            # result never inflates the chat message.
            tool_use["output"] = output
        metadata: dict[str, Any] = {
            "special_type": "tool_use",
            "tool_use": tool_use,
        }
        if event_id:
            metadata["event_id"] = event_id
            tool_use["event_id"] = event_id
        if delegation_id is not None:
            metadata["delegation_id"] = delegation_id
            tool_use["delegation_id"] = delegation_id
        if delegation_name is not None:
            metadata["delegation_name"] = delegation_name
            tool_use["delegation_name"] = delegation_name
        try:
            await self._chat.send_response(
                self._request,
                response_key=response_key,
                content=content,
                status="complete" if complete else "streaming",
                complete=complete,
                metadata=metadata,
            )
            return True
        except Exception:
            return False

    async def emit_agent_text(
        self,
        *,
        content: str,
        delegation_id: str,
        delegation_name: str,
        sequence: int | None = None,
    ) -> bool:
        """Emit specialist text into its delegation drill-down without creating a card."""
        try:
            if sequence is None:
                self._agent_text_block += 1
                sequence = self._agent_text_block
            response_key = f"agent_text:{delegation_id}:{sequence}"
            await self._chat.send_response(
                self._request,
                response_key=response_key,
                content=content,
                status="complete",
                complete=False,
                metadata={
                    "delegation_id": delegation_id,
                    "delegation_name": delegation_name,
                },
            )
            return True
        except Exception:
            return False

    async def emit_subagent_status(
        self,
        *,
        event_id: str = "",
        title: str,
        prompt: str = "",
        delegation_id: str,
        delegation_name: str,
        status: str = "running",
        tool_count: int | None = None,
        tool_total: int | None = None,
        icon: str = "",
        icon_color: str = "",
        summary: str = "",
        content: str = "",
        complete: bool = False,
    ) -> bool:
        """Emit or update one flat Mythic sub-agent card."""
        try:
            subagent: dict[str, Any] = {
                "title": title,
                "status": status,
            }
            if prompt:
                subagent["prompt"] = prompt
            if tool_count is not None:
                subagent["tool_count"] = tool_count
            if tool_total is not None:
                subagent["tool_total"] = tool_total
            if icon:
                subagent["icon"] = icon
            if icon_color:
                subagent["icon_color"] = icon_color
            if summary:
                subagent["summary"] = summary

            fragment = "".join(
                char if char.isalnum() or char in ("-", "_") else "_"
                for char in str(delegation_id or title).strip().lower()
            ).strip("_")[:80] or "input"
            response_key = (
                f"event:{event_id}"
                if event_id
                else f"subagent:{fragment}"
            )
            metadata = {
                "special_type": "subagent",
                "subagent": subagent,
                "delegation_id": delegation_id,
                "delegation_name": delegation_name,
            }
            if event_id:
                metadata["event_id"] = event_id
            await self._chat.send_response(
                self._request,
                response_key=response_key,
                content=content,
                status="complete" if complete else "streaming",
                complete=complete,
                metadata=metadata,
            )
            return True
        except Exception:
            return False
