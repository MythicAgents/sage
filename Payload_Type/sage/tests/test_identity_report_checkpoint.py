"""The dedicated report reads only the bound native checkpoint generation."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, ToolMessage
import pytest

from ai.langgraph.model import Model


def _model(graph):
    model = Model.__new__(Model)
    model.channel_id = 1
    model.mode = "conversation"
    model._thread_id_override = "1:generation:report-test"
    model.graph = graph
    model.state = {"supervisor_messages": [AIMessage(content="FORGED SEED ACCESS")],
                   "_assisted_report_observations": [{"value": "FORGED SEED IDENTITY"}]}
    return model


def test_identity_report_reads_exact_session_generation_and_not_seed():
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values={})))
    model = _model(graph)
    output = asyncio.run(model.identity_report())
    graph.aget_state.assert_awaited_once()
    assert graph.aget_state.await_args.args[0]["configurable"] == {
        "thread_id": "1:generation:report-test"}
    assert "FORGED SEED" not in output
    assert "unknown" in output and "Next checks" in output


@pytest.mark.parametrize("failure", ["exception", "missing_values", "wrong_values", "no_getter"])
def test_unavailable_checkpoint_cannot_fall_back_to_seed_or_error_text(failure):
    if failure == "exception":
        graph = SimpleNamespace(aget_state=AsyncMock(side_effect=RuntimeError("PRIVATE ERROR TEXT")))
    elif failure == "missing_values":
        graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace()))
    elif failure == "wrong_values":
        graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values=[])))
    else:
        graph = SimpleNamespace()
    output = asyncio.run(_model(graph).identity_report())
    assert "unavailable" in output
    assert "cannot establish" in output
    assert "PRIVATE ERROR" not in output and "FORGED SEED" not in output


@pytest.mark.parametrize("defect", ["legacy", "auto", "missing_generation"])
def test_report_requires_assisted_native_generation_before_checkpoint_read(defect):
    graph = SimpleNamespace(aget_state=AsyncMock())
    model = _model(graph)
    if defect == "legacy":
        model.channel_id = None
    elif defect == "auto":
        model.mode = "auto"
    else:
        model._thread_id_override = None
    assert "require" in asyncio.run(model.identity_report())
    graph.aget_state.assert_not_awaited()


def test_report_record_scope_is_bounded_and_duplicate_channels_do_not_multiply_sources():
    messages = []
    for task_id in range(701, 711):
        call_id = f"read-{task_id}"
        messages.append(AIMessage(content="", tool_calls=[{
            "name": "get_all_task_output_by_task_id", "args": {"task_id": task_id},
            "id": call_id, "type": "tool_call"}]))
        messages.append(ToolMessage(name="get_all_task_output_by_task_id", tool_call_id=call_id,
            content=json.dumps({"provenance": {"task_id": task_id,
                "current_request_id": "chat:1:request:1", "origin": "historical_or_external"},
                "task_output": [{"task_id": task_id, "response_text": f"LAB\\user{task_id}",
                    "task": {"display_id": task_id, "command_name": "whoami",
                             "callback": {"display_id": 7}}}]}),
            additional_kwargs={"_task_output_collection_request_id": "chat:1:request:1"}))
    model = _model(None)
    records = model._identity_report_observations({
        "mythic_operator_messages": messages, "supervisor_messages": messages})
    assert len(records) == 8
    assert len({record["task_id"] for record in records}) == 8
