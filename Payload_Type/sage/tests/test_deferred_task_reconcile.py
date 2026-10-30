"""A deferred task that finished by handback continues the request with an accurate summary.

In the supervised run, a DCSync task had completed with its full output, but Apollo's completion function
failed to register the credential. The handback's single output read saw no rows yet (they landed in the
same second), so the request ended at a "has not reached a terminal state" checkpoint. Re-reading within a
short window, taking completion from the task record, and naming a failed completion function as its own
outcome keep the request going with a summary that says what happened. Uses no real credential material.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END
from langgraph.types import Command

from ai.langgraph import model as model_module
from tests.test_supervised_pending_handoff import _handback_model, _operator_wrapped


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(model_module, "_DEFERRED_RECONCILE_RETRY_DELAYS", (0.0, 0.0, 0.0))


def _rows(text, *, status="success", completed=True):
    return json.dumps([{
        "id": 7, "response_text": text, "task": {
            "id": 4242, "display_id": 4242, "completed": completed, "status": status,
            "callback": {"display_id": 5}, "command_name": "whoami",
        },
    }])


_NO_ROWS = json.dumps([])
_AGENT_ERROR = "Error: completion function - click cog to check stderr"


def _handback(request_suffix, reader, record=None):
    async def run():
        model, tools = _handback_model(f"chat:deferred-reconcile:request:{request_suffix}")
        tools.get_all_task_output_by_task_id = AsyncMock(side_effect=AssertionError("model-facing read used"))

        async def rows(task_id):
            # The internal reader returns decoded rows, the shape the real helper produces.
            return json.loads(await reader(task_id=task_id)), False

        tools._task_output_rows = rows
        if record is not None:
            tools._task_record_state = record
        wrapped = _operator_wrapped(model, "Issued task 4242; output was deferred.")
        state = dict(model.state)
        state["mythic_operator_messages"] = [AIMessage(content="Issued task 4242; output was deferred.")]
        return await wrapped(state, {}), tools

    return asyncio.run(run())


def _reconciled(result):
    return [m for m in (result.update or {}).get("supervisor_messages", [])
            if getattr(m, "additional_kwargs", {}).get("_deferred_task_reconciled")]


def _forced_end(result):
    return [m for m in (result.update or {}).get("supervisor_messages", [])
            if getattr(m, "additional_kwargs", {}).get("_pending_task_handoff")]


def test_rows_that_land_after_the_first_read_are_reconciled_and_the_turn_continues():
    reader = AsyncMock(side_effect=[_NO_ROWS, _rows("replicated account output")])
    record = AsyncMock(return_value={"status": "success", "completed": True})
    result, tools = _handback("late-rows", reader, record)
    assert isinstance(result, Command) and result.goto == "Supervisor"
    assert tools._deferred_task_receipt is None
    content = str(_reconciled(result)[0].content)
    assert "completed" in content and "replicated account output" in content
    assert reader.await_count == 2


def test_record_says_finished_with_no_rows_in_the_window_still_continues():
    reader = AsyncMock(return_value=_NO_ROWS)
    record = AsyncMock(return_value={"status": "success", "completed": True})
    result, tools = _handback("record-only", reader, record)
    assert isinstance(result, Command) and result.goto == "Supervisor"
    assert "returned no output text" in str(_reconciled(result)[0].content)
    assert reader.await_count == 1 + len(model_module._DEFERRED_RECONCILE_RETRY_DELAYS)


def test_failed_completion_function_is_its_own_outcome_and_the_turn_continues():
    reader = AsyncMock(return_value=_rows("[*] dumped account secrets for one principal", status=_AGENT_ERROR))
    result, tools = _handback("agent-error", reader)
    assert isinstance(result, Command) and result.goto == "Supervisor"
    message = _reconciled(result)[0]
    assert message.additional_kwargs["_deferred_task_reconciled"] == {
        "task_id": 4242, "outcome": "completed_with_agent_error",
    }
    content = str(message.content)
    assert "completion function failed" in content
    assert "credentials or files" in content and "may be missing from Mythic" in content
    assert "dumped account secrets for one principal" in content
    assert "FAILED" not in content and "not reached a terminal state" not in content
    reader.assert_awaited_once()


@pytest.mark.parametrize("status, outcome", [
    (_AGENT_ERROR, "completed_with_agent_error"),
    ("error: completion function", "completed_with_agent_error"),
    ("error", "failed"),
    ("Error: task function failed", "failed"),
    ("timeout", "failed"),
    ("success", "completed"),
    ("completed", "completed"),
    ("errored output parsed", "completed"),  # near match: not an "error:" status
])
def test_finished_task_status_classification(status, outcome):
    assert model_module._reconciled_task_outcome(status) == outcome


def test_other_error_status_on_a_finished_task_is_reported_as_failed():
    reader = AsyncMock(return_value=_rows("access denied", status="Error: task function failed"))
    result, _ = _handback("other-error", reader)
    assert isinstance(result, Command) and result.goto == "Supervisor"
    assert "FAILED" in str(_reconciled(result)[0].content)


def test_genuinely_unfinished_task_with_no_rows_keeps_the_checkpoint():
    reader = AsyncMock(return_value=_NO_ROWS)
    record = AsyncMock(return_value={"status": "processing", "completed": False})
    result, tools = _handback("unfinished", reader, record)
    assert isinstance(result, Command) and result.goto == END
    assert tools._deferred_task_receipt is not None
    message = _forced_end(result)[0]
    assert message.additional_kwargs.get("_turn_outcome") == "checkpoint"
    content = str(message.content)
    assert "not reached a terminal state" in content
    assert 'Mythic reported status "processing" with no output rows' in content


def test_unreadable_record_with_no_rows_keeps_the_checkpoint():
    reader = AsyncMock(return_value=_NO_ROWS)
    record = AsyncMock(side_effect=RuntimeError("transport down"))
    result, tools = _handback("record-down", reader, record)
    assert isinstance(result, Command) and result.goto == END
    assert tools._deferred_task_receipt is not None
    assert "could not confirm its state" in str(_forced_end(result)[0].content)


def test_record_read_is_scoped_to_the_exact_task_display_id():
    async def run():
        _, tools = _handback_model("chat:deferred-reconcile:request:record-query")
        captured = {}

        async def query(*, mythic, query, variables):
            captured.update(query=query, variables=variables)
            return {"task": [{"display_id": 4242, "status": _AGENT_ERROR, "completed": True}]}

        from ai.langgraph import mythic_tools
        mythic_tools.mythic.execute_custom_query, saved = query, mythic_tools.mythic.execute_custom_query
        try:
            state = await tools._task_record_state(4242)
            invalid = await tools._task_record_state("42; drop")
        finally:
            mythic_tools.mythic.execute_custom_query = saved
        return state, invalid, captured

    state, invalid, captured = asyncio.run(run())
    assert state == {"status": _AGENT_ERROR.casefold(), "completed": True}
    assert captured["variables"] == {"display_id": 4242}
    assert "display_id: {_eq: $display_id}" in captured["query"]
    assert invalid is None


def test_public_output_tool_envelope_is_unwrapped_when_only_it_is_available():
    """The model-facing tool wraps rows as {"provenance", "task_output"}; the fallback reads through it."""
    from ai.langgraph.model import Model

    envelope = json.dumps({"provenance": {"task_id": 4242}, "task_output": json.loads(
        _rows("[*] replicated account output")
    )})

    class _Client:
        get_all_task_output_by_task_id = AsyncMock(return_value=envelope)

    model = object.__new__(Model)
    model.mythic_client = _Client()
    observed = asyncio.run(model._reconcile_deferred_task_receipt({"task_display_id": 4242}))
    assert observed["completed"] is True and observed["status"] == "success"
    assert "replicated account output" in observed["excerpt"]


def test_production_shape_through_the_real_tools_reaches_the_excerpt(monkeypatch):
    """Drive the real MythicTools readers against a fake Mythic row source, as production does."""
    import base64

    from ai.langgraph import mythic_tools

    raw_rows = [{
        "id": 7, "response_text": base64.b64encode(b"Error: failed to parse arguments").decode(),
        "task": {"id": 4242, "display_id": 4242, "completed": True, "status": "error",
                 "callback": {"display_id": 5}, "command_name": "whoami"},
    }]

    async def fake_rows(**kwargs):
        return [dict(r) for r in raw_rows]

    monkeypatch.setattr(mythic_tools.mythic, "get_all_task_output_by_id", fake_rows)

    async def run():
        model, tools = _handback_model("chat:deferred-reconcile:request:real-shape")
        observed = await model._reconcile_deferred_task_receipt({"task_display_id": 4242})
        return observed, tools

    observed, tools = asyncio.run(run())
    assert observed["completed"] is True
    assert "failed to parse arguments" in observed["excerpt"]
    # Internal reads never feed the model-facing re-read clamp, so the worker still sees the output.
    assert tools._failed_read_counts.get(4242) is None
