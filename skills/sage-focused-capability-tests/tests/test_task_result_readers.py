"""Offline checks for the task-result readers (no live Mythic, no provider).

Inputs, labelled where used:
- Recorded: ``fixtures/task_result_request85.json``, request 85's two card emissions and the same call's tool message
  in the next provider request, reduced (see its ``provenance``). Mythic's storage step is applied as Mythic applies it
  (``tool_use.output`` moves to ``tool_output``; ``rabbitmq/recv_chat_response.go`` ``ExtractToolOutput``).
- Synthetic: the task's Mythic response rows. The capture has no server bodies, so the rows are the recorded raw value
  split in two (30 bytes and the rest). Every mutation of a recorded shape is synthetic too.
- Real producers: Sage's ``MessageCaptureCallback.on_tool_end`` and ``Model._emit_execution_event`` through
  ``_emit_tool_use_card`` and ``ChatStreamEmitter.emit_tool_use`` into ``HeadlessSageChat``.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/task_result_readers.py"
SPEC = importlib.util.spec_from_file_location("task_result_readers", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
readers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readers)
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))

RECORDED = json.loads((ROOT / "skills/sage-focused-capability-tests/tests/fixtures/task_result_request85.json")
                      .read_text())
CALLBACK = RECORDED["callback"]
TASK = RECORDED["task"]
CALL = RECORDED["call_id"]
LIFECYCLE_ID = f"mythic-task:{CALLBACK}:{TASK}"
NOTE = "\n\n[SAGE OPSEC] footprint total=4 axes={'new_beacon': 0, 'disk_artifact': 0}."


def _stored(emission: dict) -> dict:
    """One card emission as Mythic stores it: ``tool_use.output`` moved to ``tool_output``."""
    metadata = copy.deepcopy(emission["metadata"])
    output = metadata["tool_use"].pop("output", "")
    return {"author_type": "ai", "message": emission.get("content", ""), "metadata": metadata,
            "tool_output": output or ""}


def _rows() -> dict:
    return {"status": "complete", "messages": [_stored(card) for card in RECORDED["cards"]]}


def _lifecycle(rows: dict) -> dict:
    return next(row for row in rows["messages"] if row["metadata"]["tool_use"]["tool_call_id"] == LIFECYCLE_ID)


def _call_card(rows: dict) -> dict:
    return next(row for row in rows["messages"] if row["metadata"]["tool_use"]["tool_call_id"] == CALL)


RAW = _lifecycle(_rows())["tool_output"]
FORMATTED = RECORDED["follow_up"]["content"]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _task(raw: str = RAW, **changes) -> dict:
    """Synthetic: Mythic's rows for the task, the recorded raw value split after 30 bytes."""
    data = raw.encode()
    task = {"display_id": TASK, "completed": True, "status": "success", "callback": {"display_id": CALLBACK},
            "subtask_count": 0,
            "responses": [{"id": 101, "response_text": _b64(data[:30])}, {"id": 102, "response_text": _b64(data[30:])}]}
    task.update(changes)
    return task


# ---------------------------------------------------------------------------------------------------------------
# The recorded relation
# ---------------------------------------------------------------------------------------------------------------

def test_the_recorded_stages_keep_their_relation():
    rows = _rows()
    assert _call_card(rows)["metadata"]["tool_use"]["result_preview"] == FORMATTED
    assert _call_card(rows)["tool_output"] == ""
    assert _lifecycle(rows)["metadata"]["tool_use"]["result_preview"] == RAW
    assert FORMATTED.endswith(RAW) and RAW
    assert RECORDED["follow_up"]["tool_call_id"] == CALL
    assert RECORDED["original"]["13540.result_preview"] == RECORDED["original"]["13588.content"]


# ---------------------------------------------------------------------------------------------------------------
# Comparison 1: transport
# ---------------------------------------------------------------------------------------------------------------

def test_transport_holds_for_the_recorded_lifecycle_card_and_its_rows():
    result = readers.transport_holds(_task(), _rows(), callback=CALLBACK)
    assert result["holds"], result["reasons"]
    assert [row["id"] for row in result["rows"]] == [101, 102]
    assert result["expected"] == result["card_output"]
    assert result["card_preview_equal"] is True


def _set_lifecycle_output(value):
    def mutate(task, rows):
        _lifecycle(rows)["tool_output"] = value
    return mutate


def _second_lifecycle(task, rows):
    rows["messages"].append(copy.deepcopy(_lifecycle(rows)))


def _lifecycle_arguments(task, rows):
    _lifecycle(rows)["metadata"]["tool_use"]["arguments"] = json.dumps(
        {"callback_id": CALLBACK, "parameters": "", "task_id": TASK + 1})


TRANSPORT_FALSIFIERS = {
    "dropped row": lambda task, rows: task.update(responses=task["responses"][:1]),
    "reordered rows": lambda task, rows: task.update(responses=[
        {"id": 101, "response_text": task["responses"][1]["response_text"]},
        {"id": 102, "response_text": task["responses"][0]["response_text"]}]),
    "padded server value": lambda task, rows: task["responses"].append({"id": 103, "response_text": _b64(b"\n")}),
    "stripped card value": _set_lifecycle_output(RAW[:-1]),
    "partial card from a timed-out wait": lambda task, rows: _lifecycle(rows).update(
        tool_output=base64.b64decode(task["responses"][0]["response_text"]).decode()),
    "another task": lambda task, rows: task.update(display_id=TASK + 1),
    "another callback": lambda task, rows: task.update(callback={"display_id": CALLBACK + 1}),
    "card arguments name another task": _lifecycle_arguments,
    "task not completed": lambda task, rows: task.update(completed=False),
    "task errored": lambda task, rows: task.update(status="error: access denied"),
    "subtasks unread": lambda task, rows: task.pop("subtask_count"),
    "a subtask": lambda task, rows: task.update(subtask_count=1),
    "empty output": lambda task, rows: task.update(responses=[]),
    "row without id": lambda task, rows: task["responses"][0].pop("id"),
    "duplicate ids": lambda task, rows: task["responses"][1].update(id=101),
    "row not base64": lambda task, rows: task["responses"][0].update(response_text="not base64!"),
    "card without stored output": _set_lifecycle_output(""),
    "two lifecycle cards": _second_lifecycle,
    "card not completed": lambda task, rows: _lifecycle(rows)["metadata"]["tool_use"].update(status="started"),
}


@pytest.mark.parametrize("name", sorted(TRANSPORT_FALSIFIERS))
def test_each_transport_near_match_is_rejected(name):
    task, rows = _task(), _rows()
    TRANSPORT_FALSIFIERS[name](task, rows)
    result = readers.transport_holds(task, rows, callback=CALLBACK)
    assert result["holds"] is False and result["reasons"], name


def test_rows_listed_out_of_id_order_are_read_in_id_order():
    task = _task()
    task["responses"].reverse()
    result = readers.transport_holds(task, _rows(), callback=CALLBACK)
    assert result["holds"], result["reasons"]
    assert [row["id"] for row in result["rows"]] == [101, 102]


def test_a_classification_note_on_the_lifecycle_preview_is_recorded_not_compared():
    """T1: Sage prefixes result_preview when its classifier disagrees with Mythic; tool_output stays raw."""
    rows = _rows()
    _lifecycle(rows)["metadata"]["tool_use"]["result_preview"] = "Mythic task completed successfully; … " + RAW
    result = readers.transport_holds(_task(), rows, callback=CALLBACK)
    assert result["holds"] and result["card_preview_equal"] is False


def test_rows_decode_as_sage_decodes_the_sdk_return_across_a_split_character():
    from ai.langgraph.mythic_tools import _task_output_text

    data = "résumé — ✓".encode()
    split = data.index("✓".encode()) + 1  # inside the three-byte character
    responses = [{"id": 2, "response_text": _b64(data[split:])}, {"id": 1, "response_text": _b64(data[:split])}]
    text, evidence, error = readers.decode_task_rows(responses)
    assert not error and [row["id"] for row in evidence] == [1, 2]
    assert text == _task_output_text(data) == "résumé — ✓"
    per_row = "".join(chunk.decode("utf-8", "replace") for chunk in (data[:split], data[split:]))
    assert per_row != text


# ---------------------------------------------------------------------------------------------------------------
# Comparison 2: presentation and the annotation
# ---------------------------------------------------------------------------------------------------------------

def test_presentation_holds_for_the_recorded_follow_up_and_its_card():
    result = readers.presentation_holds(FORMATTED, _rows(), CALL)
    assert result["holds"], result["reasons"]
    assert result["storage"] == "inline" and result["cards"] == 1


def _sibling_with_the_value(formatted, rows):
    sibling = copy.deepcopy(_call_card(rows))
    sibling["metadata"]["tool_use"]["tool_call_id"] = "call_sibling"
    rows["messages"].append(sibling)
    _call_card(rows)["metadata"]["tool_use"]["result_preview"] = "Tool finished."
    return formatted


PRESENTATION_FALSIFIERS = {
    "no formatted value": lambda formatted, rows: None,
    "empty formatted value": lambda formatted, rows: "",
    "invented suffix on the card": lambda formatted, rows: (
        _call_card(rows)["metadata"]["tool_use"].update(result_preview=formatted + " (and more)"), formatted)[1],
    "card missing content": lambda formatted, rows: (
        _call_card(rows)["metadata"]["tool_use"].pop("result_preview"), formatted)[1],
    "inline value also lazy": lambda formatted, rows: (_call_card(rows).update(tool_output=formatted), formatted)[1],
    "card of another call": lambda formatted, rows: (
        _call_card(rows)["metadata"]["tool_use"].update(tool_call_id="call_other"), formatted)[1],
    "card of another request": lambda formatted, rows: (rows.update(messages=[_lifecycle(rows)]), formatted)[1],
    "identical content on a sibling card": _sibling_with_the_value,
    "two cards for the call": lambda formatted, rows: (
        rows["messages"].append(copy.deepcopy(_call_card(rows))), formatted)[1],
    "card errored": lambda formatted, rows: (
        _call_card(rows)["metadata"]["tool_use"].update(status="error"), formatted)[1],
    "follow-up carried a different value": lambda formatted, rows: formatted.replace("lab.user", "lab.admin"),
}


@pytest.mark.parametrize("name", sorted(PRESENTATION_FALSIFIERS))
def test_each_presentation_near_match_is_rejected(name):
    rows = _rows()
    formatted = PRESENTATION_FALSIFIERS[name](FORMATTED, rows)
    result = readers.presentation_holds(formatted, rows, CALL)
    assert result["holds"] is False and result["reasons"], name


def test_the_annotation_is_the_tasks_prefix_then_the_raw_value():
    result = readers.annotation_holds(FORMATTED, _rows(), _task(), callback=CALLBACK)
    assert result["holds"], result["reasons"]
    assert result["notes"] == []
    assert FORMATTED == readers.expected_prefix(TASK, CALLBACK, "whoami", True, "success") + RAW


def test_a_known_note_after_the_raw_value_is_accepted_and_recorded():
    result = readers.annotation_holds(FORMATTED + NOTE, _rows(), _task(), callback=CALLBACK)
    assert result["holds"] and result["notes"] == ["SAGE OPSEC"]


ANNOTATION_FALSIFIERS = {
    "prefix names another task": lambda: (FORMATTED.replace(f"task_display_id={TASK}", f"task_display_id={TASK + 1}"),
                                          _task()),
    "prefix names another callback": lambda: (
        FORMATTED.replace(f"callback_display_id={CALLBACK}", f"callback_display_id={CALLBACK + 1}"), _task()),
    "prefix state differs from Mythic": lambda: (FORMATTED, _task(status="completed")),
    "raw value changed": lambda: (FORMATTED.replace("lab.user", "lab.admin"), _task()),
    "raw value truncated": lambda: (FORMATTED[:-5], _task()),
    "invented trailing text": lambda: (FORMATTED + "\n\nAll good.", _task()),
    "unknown note tag": lambda: (FORMATTED + "\n\n[SAGE VERDICT] done", _task()),
    "no prefix": lambda: (RAW, _task()),
}


@pytest.mark.parametrize("name", sorted(ANNOTATION_FALSIFIERS))
def test_each_annotation_near_match_is_rejected(name):
    formatted, task = ANNOTATION_FALSIFIERS[name]()
    result = readers.annotation_holds(formatted, _rows(), task, callback=CALLBACK)
    assert result["holds"] is False and result["reasons"], name


# ---------------------------------------------------------------------------------------------------------------
# Sage's real card producers, inline and lazy
# ---------------------------------------------------------------------------------------------------------------

REQUEST_ID = "request-85-replay"
DELEGATION = "mythic_operator:chat:50:request:85:1"


def _model_and_chat():
    from ai.langgraph import model as model_module
    from ai.langgraph.request_events import RequestEventLedger
    from sage_chat.headless import HeadlessSageChat, build_chat_request
    from sage_chat.streaming import ChatStreamEmitter

    chat = HeadlessSageChat()
    model = object.__new__(model_module.Model)
    model._request_event_ledger = RequestEventLedger(REQUEST_ID)
    model._response_emitter = ChatStreamEmitter(chat, build_chat_request("Run whoami on callback 2."))
    model._active_delegations = {}
    ledger = model._request_event_ledger
    ledger.record_once(event_id=f"{REQUEST_ID}:input", kind="operator_input", phase="received",
                       content="Run whoami on callback 2.", metadata={})
    ledger.record_once(event_id=f"{REQUEST_ID}:contract", kind="control_transition",
                       phase="request_installed", content="request contract installed", metadata={})
    return model_module, model, chat


def _emit_call_card(formatted: str, call_id: str = CALL) -> dict:
    """The ToolMessage for ``call_id`` through on_tool_end into the real emitter; returns Mythic's stored rows."""
    from langchain_core.messages import ToolMessage

    model_module, model, chat = _model_and_chat()
    callback = model_module.MessageCaptureCallback(agent_name="Mythic_Operator",
                                                   tool_use_func=model._emit_tool_use_card,
                                                   delegation_id=DELEGATION, delegation_name="Mythic_Operator")
    message = ToolMessage(content=formatted, tool_call_id=call_id, name="issue_task_and_waitfor_task_output")
    asyncio.run(callback.on_tool_end(message, run_id=uuid.uuid4()))
    return {"status": "complete", "messages": [_stored(emission) for emission in chat.emissions
                                               if (emission.get("metadata") or {}).get("special_type") == "tool_use"]}


@pytest.mark.parametrize("size", [len(FORMATTED), readers.INLINE_CAP, readers.INLINE_CAP + 1, 20_000])
def test_the_real_call_card_holds_the_formatted_value_inline_or_lazy(size):
    formatted = (FORMATTED * (size // len(FORMATTED) + 1))[:size]
    rows = _emit_call_card(formatted)
    result = readers.presentation_holds(formatted, rows, CALL)
    assert result["holds"], result["reasons"]
    assert result["storage"] == ("inline" if size <= readers.INLINE_CAP else "lazy")


@pytest.mark.parametrize("size", [readers.INLINE_CAP, readers.INLINE_CAP + 1])
def test_a_real_card_for_another_call_never_holds_the_value(size):
    formatted = (FORMATTED * (size // len(FORMATTED) + 1))[:size]
    rows = _emit_call_card(formatted, call_id="call_another")
    assert readers.presentation_holds(formatted, rows, CALL)["holds"] is False


LAZY_FALSIFIERS = {
    "lazy value is a prefix": lambda formatted, row: row.update(tool_output=formatted[:-1]),
    "truncated preview stands in for the value": lambda formatted, row: row.update(tool_output=""),
    "invented suffix on the lazy value": lambda formatted, row: row.update(tool_output=formatted + " (and more)"),
    "preview is not the value's first 4,000 characters": lambda formatted, row: row["metadata"]["tool_use"].update(
        result_preview=formatted[1:readers.INLINE_CAP + 1]),
}


@pytest.mark.parametrize("name", sorted(LAZY_FALSIFIERS))
def test_each_lazy_near_match_is_rejected(name):
    """Synthetic mutations of the real producer's 4,001-character card."""
    formatted = (FORMATTED * (readers.INLINE_CAP // len(FORMATTED) + 2))[:readers.INLINE_CAP + 1]
    rows = _emit_call_card(formatted)
    LAZY_FALSIFIERS[name](formatted, _call_card(rows))
    result = readers.presentation_holds(formatted, rows, CALL)
    assert result["holds"] is False and result["reasons"], name


def test_the_real_lifecycle_card_holds_the_raw_value_for_its_task():
    model_module, model, chat = _model_and_chat()
    model._controller_update_transaction_task_lineage = lambda event: None
    model._observe_supervised_execution_event = lambda event: None
    event = {"event_id": f"mythic-task:{CALLBACK}:{TASK}", "source": "mythic", "tool_name": "whoami",
             "status": "completed", "task_completed": True, "task_status": "success", "callback_id": CALLBACK,
             "task_id": TASK, "parameters": "", "result_preview": RAW, "output": RAW,
             "activity": {"id": DELEGATION, "name": "Mythic_Operator"}}
    asyncio.run(model._emit_execution_event(event))
    rows = {"status": "complete", "messages": [_stored(emission) for emission in chat.emissions
                                               if (emission.get("metadata") or {}).get("special_type") == "tool_use"]}
    assert readers.transport_holds(_task(), rows, callback=CALLBACK)["holds"]
    assert readers.annotation_holds(FORMATTED, rows, _task(), callback=CALLBACK)["holds"]
    assert readers.transport_holds(_task(display_id=TASK + 1), rows, callback=CALLBACK)["holds"] is False
