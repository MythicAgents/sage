"""Conservative classification of a completed model response as a refusal.

Provider-owned refusal fields take precedence over tool calls. Prose is considered only
for a terminal response with no tool call; quoted or historical text is never inspected.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_DIRECT_REFUSAL = re.compile(
    r"^(?:(?:i(?:'m| am)\s+sorry,?\s*(?:but\s+)?|sorry,?\s*)?"
    r"i\s+(?:cannot|can't|can not|won't|will not|am unable to)\s+"
    r"(?:(?:help|assist)(?:\s+you)?\s+with|comply\s+with|"
    r"provide\s+(?:instructions|guidance|assistance)\s+(?:on|for|with))\b)",
    re.IGNORECASE,
)


def refusal_from_message(message: Any) -> tuple[str, str] | None:
    """Return (source, text) for an explicit refusal in one completed AI message."""
    additional = getattr(message, "additional_kwargs", None)
    metadata = getattr(message, "response_metadata", None)
    content = getattr(message, "content", None)
    if isinstance(additional, Mapping):
        refusal = additional.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            return "additional_kwargs.refusal", refusal.strip()
    if isinstance(metadata, Mapping):
        refusal = metadata.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            return "response_metadata.refusal", refusal.strip()
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and str(block.get("type") or "").casefold() == "refusal":
                value = block.get("refusal") or block.get("text")
                if isinstance(value, str) and value.strip():
                    return "content.refusal", value.strip()
    # A tool call with refusal-like prose is an actionable response, not a prose refusal.
    if (getattr(message, "tool_calls", None) or getattr(message, "invalid_tool_calls", None)
            or (isinstance(additional, Mapping) and any(
                additional.get(key) for key in ("tool_calls", "function_call")
            ))):
        return None
    if isinstance(content, str) and _DIRECT_REFUSAL.match(content.strip()):
        return "content.text", content.strip()
    return None
