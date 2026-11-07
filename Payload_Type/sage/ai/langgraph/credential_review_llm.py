"""The narrow model reviewer for credential review: one isolated, untraced, tool-free call that returns a citation.

``credential_review`` decides; this module only makes the call. The reviewer is not the task-running agent: it gets the
review rule and a bounded excerpt of one task's output, no tools, no conversation, and no parent run. The call runs in
an empty ``contextvars.Context`` (so no LangGraph run config, message stream, parent callback or trace context is
inherited) and inside OpenInference's ``suppress_tracing``, so neither the excerpt it reads nor the citation it
returns reaches a span or the chat stream. When that boundary cannot be guaranteed the reviewer refuses to call and the
task's row stays pending. Errors are reported by exception type only.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
from typing import Any, Iterable

try:
    from . import credential_review
except ImportError:  # pragma: no cover - flat import for tests that put ai/langgraph on sys.path
    import credential_review  # type: ignore

DEFAULT_TIMEOUT_S = 90.0
_ENV_TRACING = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "LANGSMITH_TRACING_V2")


class ReviewerUnavailable(RuntimeError):
    """The isolated, untraced review call cannot be made; the message names the missing boundary (no values)."""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _suppress_tracing():
    try:
        from openinference.instrumentation import suppress_tracing
    except ImportError as exc:  # pragma: no cover - the project venv ships it
        raise ReviewerUnavailable("OpenInference tracing suppression is not importable") from exc
    return suppress_tracing()


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content)
    return str(content or "")


class CredentialReviewer:
    """Locate a credential in a bounded excerpt with the configured chat model, isolated from tracing and the graph."""

    prompt_version = credential_review.LLM_PROMPT_VERSION

    def __init__(self, chat_model: Any, *, model_id: str, timeout_s: float = DEFAULT_TIMEOUT_S):
        self._model = chat_model
        self.model_id = str(model_id or "")
        self._timeout_s = float(timeout_s)

    def check_boundary(self) -> None:
        """Raise ReviewerUnavailable unless the call can run untraced."""
        for name in _ENV_TRACING:
            if _truthy(os.environ.get(name)):
                raise ReviewerUnavailable(f"LangSmith tracing is enabled by {name}, so the review call could be traced")
        if os.environ.get("SAGE_OBSERVATION_DIRECTORY", ""):
            raise ReviewerUnavailable("observation capture (SAGE_OBSERVATION_DIRECTORY) records provider request bodies")
        if str(os.environ.get("OPENAI_LOG") or "").strip().casefold() == "debug":
            raise ReviewerUnavailable("OPENAI_LOG=debug logs provider request bodies")
        _suppress_tracing()  # importable; entered around the call itself

    async def locate(self, messages: Iterable[tuple[str, str]]) -> str:
        """The model's raw reply to the review prompt. The caller parses it and never stores it."""
        self.check_boundary()
        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = [SystemMessage(content=content) if role == "system" else HumanMessage(content=content)
                  for role, content in messages]

        async def call() -> str:
            with _suppress_tracing():
                reply = await self._model.ainvoke(prompt, config={"run_name": "credential_review"})
            return _text(getattr(reply, "content", reply))

        # An empty context: no parent LangGraph run config (message stream, callbacks) and no inherited trace context.
        task = asyncio.get_running_loop().create_task(call(), context=contextvars.Context())
        return await asyncio.wait_for(task, timeout=self._timeout_s)
