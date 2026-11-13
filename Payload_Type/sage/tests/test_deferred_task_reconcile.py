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


# --- Mythic's completion-function window -------------------------------------------------------------------------
# Mythic sets a task's completed flag before it runs the agent's completion function, and shows a settling status
# until that function returns. A handback read in that window must keep re-reading, not label the task completed.

from ai.langgraph.task_evidence import (  # noqa: E402
    MYTHIC_COMPLETION_FUNCTION_ERROR,
    MYTHIC_COMPLETION_FUNCTION_RUNNING,
    MYTHIC_SETTLING_STATES,
)

_RUNNING = MYTHIC_COMPLETION_FUNCTION_RUNNING
_SETTLING_OUTCOME = "completed_completion_function_running"


def test_settling_read_then_completion_function_error_is_reconciled_as_agent_error():
    """Case S1: the first read lands in the completion-function window; the next shows the function failed."""
    reader = AsyncMock(side_effect=[
        _rows("[*] replicated account output", status=_RUNNING),
        _rows("[*] replicated account output", status=MYTHIC_COMPLETION_FUNCTION_ERROR),
    ])
    result, tools = _handback("settling-then-agent-error", reader)
    assert isinstance(result, Command) and result.goto == "Supervisor"
    assert tools._deferred_task_receipt is None
    message = _reconciled(result)[0]
    assert message.additional_kwargs["_deferred_task_reconciled"] == {
        "task_id": 4242, "outcome": "completed_with_agent_error",
    }
    content = str(message.content)
    assert "completion function failed" in content and "replicated account output" in content
    assert reader.await_count >= 2


def test_settling_then_agent_error_through_the_real_row_reader(monkeypatch):
    """Case S1 through the real MythicTools._task_output_rows; only the SDK row fetch is faked."""
    import base64

    from ai.langgraph import mythic_tools

    def production_rows(status):
        return [{
            "id": 7, "timestamp": "2026-11-12T00:00:00Z",
            "response_text": base64.b64encode(b"[*] replicated account output").decode(),
            "task": {"id": 4242, "operation_id": 1, "display_id": 4242, "status": status, "completed": True,
                     "agent_task_id": "a-4242", "command_name": "whoami", "timestamp": "2026-11-12T00:00:00Z",
                     "callback": {"display_id": 5}},
        }]

    statuses = iter([_RUNNING, MYTHIC_COMPLETION_FUNCTION_ERROR])
    fetches = []

    async def fake_fetch(**kwargs):
        fetches.append(kwargs["task_display_id"])
        return production_rows(next(statuses))

    monkeypatch.setattr(mythic_tools.mythic, "get_all_task_output_by_id", fake_fetch)

    async def run():
        model, tools = _handback_model("chat:deferred-reconcile:request:settling-real-rows")
        tools.get_all_task_output_by_task_id = AsyncMock(side_effect=AssertionError("model-facing read used"))
        wrapped = _operator_wrapped(model, "Issued task 4242; output was deferred.")
        state = dict(model.state)
        state["mythic_operator_messages"] = [AIMessage(content="Issued task 4242; output was deferred.")]
        return await wrapped(state, {}), tools

    result, tools = asyncio.run(run())
    assert isinstance(result, Command) and result.goto == "Supervisor"
    message = _reconciled(result)[0]
    assert message.additional_kwargs["_deferred_task_reconciled"]["outcome"] == "completed_with_agent_error"
    assert "replicated account output" in str(message.content)
    assert fetches == [4242, 4242]


def test_task_still_settling_for_the_whole_window_continues_with_its_own_outcome():
    """Case S2: no checkpoint, the receipt is cleared, and the handback names the status and the caveat."""
    reader = AsyncMock(return_value=_rows("[*] replicated account output", status=_RUNNING))
    result, tools = _handback("settling-whole-window", reader)
    assert isinstance(result, Command) and result.goto == "Supervisor"
    assert not _forced_end(result)
    assert tools._deferred_task_receipt is None
    message = _reconciled(result)[0]
    assert message.additional_kwargs["_deferred_task_reconciled"] == {
        "task_id": 4242, "outcome": _SETTLING_OUTCOME,
    }
    content = str(message.content)
    assert json.dumps(_RUNNING.casefold()) in content
    assert "may not be in Mythic yet" in content
    assert "replicated account output" in content
    assert "not reached a terminal state" not in content
    assert reader.await_count == 1 + len(model_module._DEFERRED_RECONCILE_RETRY_DELAYS)


@pytest.mark.parametrize("settling", sorted(MYTHIC_SETTLING_STATES))
def test_every_settling_status_keeps_re_reading_and_a_later_success_is_completed(settling):
    reader = AsyncMock(side_effect=[
        _rows("[*] task output", status=settling),
        _rows("[*] task output", status="success"),
    ])
    result, _ = _handback(f"settling-{settling[:12]}", reader)
    assert isinstance(result, Command) and result.goto == "Supervisor"
    message = _reconciled(result)[0]
    assert message.additional_kwargs["_deferred_task_reconciled"]["outcome"] == "completed"
    assert reader.await_count == 2


@pytest.mark.parametrize("settling", sorted(MYTHIC_SETTLING_STATES))
def test_settling_status_is_classified_as_its_own_outcome(settling):
    assert model_module._reconciled_task_outcome(settling) == _SETTLING_OUTCOME


def test_worker_read_during_the_settling_window_does_not_freeze_the_handback(monkeypatch):
    """A worker read while Mythic still finishes the task must not cache the settling rows for the handback.

    The completed-task output cache treats a task's rows as immutable once Mythic's completed flag is true. In
    the completion-function window that flag is already true, so caching there would serve every later handback
    read the settling status and hide the completion function's result.
    """
    import base64

    from ai.langgraph import mythic_tools

    current = {"status": _RUNNING}

    def production_rows():
        return [{
            "id": 7, "timestamp": "2026-11-12T00:00:00Z",
            "response_text": base64.b64encode(b"[*] replicated account output").decode(),
            "task": {"id": 4242, "operation_id": 1, "display_id": 4242, "status": current["status"],
                     "completed": True, "agent_task_id": "a-4242", "command_name": "whoami",
                     "timestamp": "2026-11-12T00:00:00Z", "callback": {"display_id": 5}},
        }]

    async def fake_fetch(**kwargs):
        return production_rows()

    async def fake_query(*args, **kwargs):
        return {"task": [{"display_id": 4242, "completed": True, "status": current["status"]}]}

    monkeypatch.setattr(mythic_tools.mythic, "get_all_task_output_by_id", fake_fetch)
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", fake_query)

    async def run():
        model, tools = _handback_model("chat:deferred-reconcile:request:settling-worker-read")
        # The worker reads the deferred task while Mythic runs the completion function.
        worker_view = json.loads(await tools.get_all_task_output_by_task_id(4242))
        assert worker_view["task_output"][0]["task"]["status"] == _RUNNING
        # The completion function then fails before the handback reads the task.
        current["status"] = MYTHIC_COMPLETION_FUNCTION_ERROR
        wrapped = _operator_wrapped(model, "Issued task 4242; output was deferred.")
        state = dict(model.state)
        state["mythic_operator_messages"] = [AIMessage(content="Issued task 4242; output was deferred.")]
        return await wrapped(state, {}), tools

    result, tools = asyncio.run(run())
    assert isinstance(result, Command) and result.goto == "Supervisor"
    message = _reconciled(result)[0]
    assert message.additional_kwargs["_deferred_task_reconciled"]["outcome"] == "completed_with_agent_error"
    assert 4242 not in tools._task_output_record_cache


def test_settled_task_output_is_still_cached(monkeypatch):
    """Near match: a task Mythic has finished (final status) keeps the completed-task cache."""
    import base64

    from ai.langgraph import mythic_tools

    rows = [{
        "id": 7, "response_text": base64.b64encode(b"[*] task output").decode(),
        "task": {"id": 4242, "display_id": 4242, "status": "success", "completed": True,
                 "callback": {"display_id": 5}, "command_name": "whoami"},
    }]
    fetches = []

    async def fake_fetch(**kwargs):
        fetches.append(kwargs["task_display_id"])
        return [dict(r) for r in rows]

    async def fake_query(*args, **kwargs):
        return {"task": [{"display_id": 4242, "completed": True, "status": "success"}]}

    monkeypatch.setattr(mythic_tools.mythic, "get_all_task_output_by_id", fake_fetch)
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", fake_query)

    async def run():
        _, tools = _handback_model("chat:deferred-reconcile:request:settled-cache")
        await tools.get_all_task_output_by_task_id(4242)
        await tools.get_all_task_output_by_task_id(4242)
        return tools

    tools = asyncio.run(run())
    assert 4242 in tools._task_output_record_cache
    assert fetches == [4242]
