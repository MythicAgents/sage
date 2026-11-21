"""`/model`: show or change this chat's model through Mythic's own channel setting.

The chat's **Model** setting in Mythic stays the single source of the model: `/model <name>` writes that setting
(``chatUpdateChannel``, which replaces ``ai_metadata`` whole, so every other key is written back unchanged), and
the next request carries it into the ordinary settings-change path. The displayed and invoked model are therefore
the same value. Mythic's own checks (the channel token's scope, channel creator or operation admin) apply.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from mythic import mythic

USAGE = (
    "Usage: `/model` shows this chat's model · `/model <name>` changes it, for example `/model gpt-5.6-sol`. "
    "The name is one value with no spaces (Bedrock ARNs and Ollama tags are fine)."
)
# One token: no whitespace or control characters. `:`, `/`, `.` and `@` are allowed.
_MODEL_VALUE = re.compile(r"[^\s\x00-\x1f\x7f]+")

CHANNEL_SETTINGS_QUERY = """
query SageChannelSettings($channel_id: Int!, $operation_id: Int!) {
  chat_channel(where: {id: {_eq: $channel_id}, operation_id: {_eq: $operation_id}}, limit: 2) {
    id
    ai_metadata
  }
}
"""

UPDATE_CHANNEL_SETTINGS = """
mutation SageSetChannelModel($channel_id: Int!, $ai_metadata: jsonb!) {
  chatUpdateChannel(channel_id: $channel_id, ai_metadata: $ai_metadata) {
    status
    error
  }
}
"""


def parse_model_argument(argument: Any) -> tuple[str, str]:
    """``(value, error)``: an empty value with no error means "show the current model"."""
    text = str(argument or "").strip()
    if not text:
        return "", ""
    if not _MODEL_VALUE.fullmatch(text):
        return "", USAGE
    return text, ""


async def write_channel_model(client: Any, operation_id: int, channel_id: int, value: str) -> str:
    """Set only ``config.model`` in the channel's stored settings; ``""`` on success, else Mythic's reason."""
    result = await mythic.execute_custom_query(
        client, CHANNEL_SETTINGS_QUERY, {"channel_id": int(channel_id), "operation_id": int(operation_id)}
    )
    rows = (result or {}).get("chat_channel") or []
    if len(rows) != 1:
        return "Mythic did not return exactly one row for this chat channel"
    metadata = rows[0].get("ai_metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return "this chat channel's stored settings are not valid JSON"
    updated = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
    config = updated.get("config") if isinstance(updated.get("config"), dict) else {}
    updated["config"] = {**config, "model": value}
    response = await mythic.execute_custom_query(
        client, UPDATE_CHANNEL_SETTINGS, {"channel_id": int(channel_id), "ai_metadata": updated}
    )
    outcome = (response or {}).get("chatUpdateChannel") or {}
    if outcome.get("status") != "success":
        return str(outcome.get("error") or "Mythic did not confirm the change")
    return ""
