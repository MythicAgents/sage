"""Compare one supervised task's result across Mythic, Sage's two cards and the model's follow-up.

A supervised task result reaches the operator twice. Sage's Mythic client waits on the SDK, decodes what it returns
(the raw value) and shows it on the task's own lifecycle card, ``mythic-task:<callback>:<task>``, in full as lazy
output. Separately the tool prefixes the raw value with the task's identity and state and may append its own notes (the
formatted value); that becomes the model call's ToolMessage, its own card, and the tool result in the model's next
request. These readers compare each stage with the one before it, by identity, never by searching other cards.

- ``transport_holds``: Mythic's own response rows for the task, concatenated in response-id order and decoded as Sage
  decodes the SDK's return, equal the lifecycle card's full stored output.
- ``presentation_holds``: the formatted value the follow-up carried for call C equals C's own card in the same request,
  inline up to 4,000 characters, lazy above (``ai/langgraph/model.py`` ``on_tool_end``).
- ``annotation_holds``: the formatted value is the identity prefix for that task, then the raw value exactly, then
  nothing or only Sage's known note blocks (``ai/langgraph/mythic_tools.py`` ``_registered_issue_task`` and
  ``issue_task_and_waitfor_task_output``).

Rows are Mythic's stored ``chat_message`` rows of one request: Mythic moves a card's ``tool_use.output`` into
``tool_output`` and keeps ``result_preview`` in metadata. The same functions read recorded and live rows.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any

# The normal card's storage rule (ai/langgraph/model.py on_tool_end): inline up to this many characters, lazy above.
INLINE_CAP = 4000
OUTPUT_CAP = 1_000_000
TERMINAL_STATUS = "completed"
_LIFECYCLE_ID = re.compile(r"mythic-task:(\d+):(\d+)")
# Every block issue_task_and_waitfor_task_output may append after the raw value, each after a blank line.
KNOWN_NOTE_TAGS = ("SAGE RESULT", "SAGE LOOP-GUARD", "SAGE REPAIR", "SAGE HINT", "SAGE OPSEC", "engagement-ledger")
_NOTE = re.compile(r"\n\n\[(" + "|".join(re.escape(tag) for tag in KNOWN_NOTE_TAGS) + r")\] ")


def digest(text: str) -> dict[str, Any]:
    data = text.encode("utf-8")
    return {"bytes": len(data), "chars": len(text), "sha256": hashlib.sha256(data).hexdigest()}


def tool_card(row: dict[str, Any]) -> dict[str, Any] | None:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    card = metadata.get("tool_use") if metadata.get("special_type") == "tool_use" else None
    return card if isinstance(card, dict) else None


def _cards(rows: dict[str, Any], call_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    found = []
    for row in rows.get("messages") or []:
        card = tool_card(row)
        if card is not None and str(card.get("tool_call_id") or "") == call_id:
            found.append((row, card))
    return found


def decode_task_rows(responses: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]], str]:
    """Mythic's response rows in id order, each base64-decoded, joined as bytes and decoded once.

    The SDK joins the decoded rows as bytes and Sage decodes the whole blob as UTF-8 with replacement
    (``mythic_tools._task_output_text``); decoding row by row would differ on a character split across rows.
    """
    rows = []
    for response in responses or []:
        response_id = response.get("id")
        if type(response_id) is not int:
            return None, [], "a response row has no id"
        try:
            data = base64.b64decode(str(response.get("response_text") or ""), validate=True)
        except (binascii.Error, ValueError):
            return None, [], f"response {response_id} is not base64"
        rows.append((response_id, data))
    ids = [response_id for response_id, _data in rows]
    if len(set(ids)) != len(ids):
        return None, [], "duplicate response ids"
    rows.sort(key=lambda item: item[0])
    evidence = [{"id": response_id, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for response_id, data in rows]
    return b"".join(data for _id, data in rows).decode("utf-8", "replace"), evidence, ""


def lifecycle_identity(card: dict[str, Any]) -> tuple[int, int] | None:
    match = _LIFECYCLE_ID.fullmatch(str(card.get("tool_call_id") or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _arguments(card: dict[str, Any]) -> dict[str, Any]:
    value = card.get("arguments")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def transport_holds(task: dict[str, Any], rows: dict[str, Any], *, callback: int) -> dict[str, Any]:
    """Comparison 1: the task's Mythic rows equal its lifecycle card's full output in this request."""
    reasons: list[str] = []
    task_id = task.get("display_id")
    task_callback = (task.get("callback") or {}).get("display_id")
    status = str(task.get("status") or "")
    if type(task_id) is not int:
        return {"holds": False, "reasons": ["task has no display id"]}
    if task_callback != callback:
        reasons.append(f"task ran on callback {task_callback}, not {callback}")
    if task.get("completed") is not True:
        reasons.append("Mythic does not show the task completed")
    if "error" in status.casefold():
        reasons.append(f"Mythic task status is {status!r}")
    subtasks = task.get("subtask_count")
    if type(subtasks) is not int:
        reasons.append("subtasks were not read")
    elif subtasks:
        reasons.append(f"the task has {subtasks} subtask(s); the live row expects none")
    expected, evidence, decode_error = decode_task_rows(task.get("responses") or [])
    if decode_error:
        reasons.append(decode_error)
    elif not expected:
        reasons.append("Mythic holds no output for the task")

    lifecycle_id = f"mythic-task:{callback}:{task_id}"
    cards = _cards(rows, lifecycle_id)
    result: dict[str, Any] = {"lifecycle_id": lifecycle_id, "rows": evidence, "lifecycle_cards": len(cards)}
    if expected is not None:
        result["expected"] = digest(expected)
    if len(cards) != 1:
        reasons.append(f"{len(cards)} lifecycle card(s) for {lifecycle_id} in this request")
    else:
        row, card = cards[0]
        arguments = _arguments(card)
        if arguments.get("task_id") != task_id or arguments.get("callback_id") != callback:
            reasons.append("the lifecycle card's arguments name another task or callback")
        if card.get("status") != TERMINAL_STATUS:
            reasons.append(f"the lifecycle card's status is {card.get('status')!r}")
        output = row.get("tool_output")
        output = output if isinstance(output, str) else ""
        result["card_output"] = digest(output)
        # T1: result_preview may carry a classification note ahead of the raw value; it is recorded, not compared.
        result["card_preview_equal"] = card.get("result_preview") == output
        if not output:
            reasons.append("the lifecycle card has no stored output")
        elif expected is not None and output != expected:
            reasons.append("the lifecycle card's output differs from Mythic's rows")
    result.update(holds=not reasons, reasons=reasons)
    return result


def card_value_holds(row: dict[str, Any], card: dict[str, Any], formatted: str) -> str:
    """The normal card's representation of ``formatted``, per on_tool_end's storage rule. Empty string when it holds."""
    preview = card.get("result_preview")
    preview = preview if isinstance(preview, str) else ""
    lazy = row.get("tool_output")
    lazy = lazy if isinstance(lazy, str) else ""
    size = len(formatted)
    if not formatted:
        return "the formatted value is empty"
    if size <= INLINE_CAP:
        if lazy:
            return "an inline-sized result also has lazy output"
        return "" if preview == formatted else "the card's inline result differs from the formatted value"
    expected_preview = (formatted[:INLINE_CAP]
                        + f"\n…[{size:,} chars total — open “View output” for the full result]")
    expected_lazy = formatted if size <= OUTPUT_CAP else (
        formatted[:OUTPUT_CAP] + f"\n…[truncated {size - OUTPUT_CAP:,} of {size:,} chars]")
    if lazy != expected_lazy:
        return "the card's lazy output differs from the formatted value"
    if preview != expected_preview:
        return "the card's preview is not the formatted value's first 4,000 characters and marker"
    return ""


def presentation_holds(formatted: str | None, rows: dict[str, Any], call_id: str) -> dict[str, Any]:
    """Comparison 2: the follow-up's formatted value for ``call_id`` equals that call's own card in this request."""
    reasons: list[str] = []
    cards = _cards(rows, call_id) if call_id else []
    result: dict[str, Any] = {"call_id": call_id, "cards": len(cards)}
    if not isinstance(formatted, str):
        reasons.append("no formatted value was read for the call")
        formatted = ""
    else:
        result["formatted"] = digest(formatted)
    if not call_id:
        reasons.append("no call id")
    elif len(cards) != 1:
        reasons.append(f"{len(cards)} card(s) for call {call_id} in this request")
    else:
        row, card = cards[0]
        if card.get("status") != TERMINAL_STATUS:
            reasons.append(f"the call's card status is {card.get('status')!r}")
        failure = card_value_holds(row, card, formatted)
        if failure:
            reasons.append(failure)
        result["storage"] = "inline" if 0 < len(formatted) <= INLINE_CAP else "lazy"
    result.update(holds=not reasons, reasons=reasons)
    return result


def expected_prefix(task_id: int, callback: int, command: str, completed: Any, status: Any) -> str:
    """The identity and state lines ``_registered_issue_task`` puts ahead of the tool's result."""
    native_completed = str(completed).lower() if type(completed) is bool else "null"
    native_status = json.dumps(status) if type(status) is str and status else "null"
    return ("[Mythic task issuance identity] "
            f"task_display_id={task_id} callback_display_id={callback} requested_command={command}\n"
            "[Mythic task state] "
            f"task_completed={native_completed} task_status={native_status} observation=observed\n\n")


def annotation_holds(formatted: str | None, rows: dict[str, Any], task: dict[str, Any], *,
                     callback: int) -> dict[str, Any]:
    """The formatted value is the task's identity prefix, its lifecycle card's raw value, then only known notes."""
    reasons: list[str] = []
    task_id = task.get("display_id")
    cards = _cards(rows, f"mythic-task:{callback}:{task_id}")
    result: dict[str, Any] = {"notes": []}
    if not isinstance(formatted, str) or not formatted:
        return {**result, "holds": False, "reasons": ["no formatted value was read for the call"]}
    if len(cards) != 1:
        return {**result, "holds": False, "reasons": [f"{len(cards)} lifecycle card(s) for the task"]}
    row, card = cards[0]
    raw = row.get("tool_output") if isinstance(row.get("tool_output"), str) else ""
    prefix = expected_prefix(task_id, callback, str(card.get("tool_name") or ""), task.get("completed"),
                             task.get("status"))
    if not raw:
        reasons.append("the lifecycle card has no stored output")
    if not formatted.startswith(prefix):
        reasons.append("the formatted value does not start with this task's identity and state")
    else:
        rest = formatted[len(prefix):]
        if not raw or not rest.startswith(raw):
            reasons.append("the raw value does not follow the identity prefix exactly")
        else:
            tail = rest[len(raw):]
            if tail and not _NOTE.match(tail):
                reasons.append("text after the raw value is not one of Sage's known notes")
            result["notes"] = _NOTE.findall(tail)
    result.update(holds=not reasons, reasons=reasons)
    return result
