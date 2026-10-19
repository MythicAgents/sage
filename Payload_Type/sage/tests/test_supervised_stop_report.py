"""A specialist return never bypasses the Supervisor's model assessment."""

import asyncio

import pytest
from langchain_core.messages import AIMessage

from ai.langgraph import model as model_module
from ai.langgraph.model import Model
from ai.langgraph.request_contract import build_request_contract


REQUEST_ID = "chat:bounded-supervision:request:2"


class _ReachedModelBoundary(Exception):
    """The wrapper reached its model invocation."""


def _header(request_id=REQUEST_ID):
    return AIMessage(
        content="[specialist completed task]",
        name="BloodHound",
        additional_kwargs={"_is_completion_header": True, "_request_id": request_id},
    )


def _invoke(monkeypatch, channel):
    model = object.__new__(Model)
    model._active_delegations = {}
    model._message_seq = 10
    model.state = {"_message_seq": 10}
    model._request_contract = build_request_contract(
        request_id=REQUEST_ID,
        channel_id="bounded-supervision",
        operation_id="offline-regression",
        mode="supervised",
        autonomous_solve=False,
    )

    def reached_model(*_args, **_kwargs):
        raise _ReachedModelBoundary

    monkeypatch.setattr(model_module, "MessageCaptureCallback", reached_model)
    wrapped = model._wrap_create_agent(None, "supervisor_messages", "Supervisor")
    return asyncio.run(wrapped({"_request_id": REQUEST_ID,
                                "supervisor_messages": channel}, {}))


@pytest.mark.parametrize("channel", [
    [],
    [_header()],
    [_header(), _header()],
    [_header(), _header(), _header()],
    [_header("chat:bounded-supervision:request:1"), _header()],
    [_header("chat:bounded-supervision:request:1"),
     _header("chat:bounded-supervision:request:1")],
])
def test_supervisor_reaches_model_with_any_number_of_returns(monkeypatch, channel):
    with pytest.raises(_ReachedModelBoundary):
        _invoke(monkeypatch, channel)


@pytest.mark.parametrize("trailing", [
    [],
    [AIMessage(content="We achieved domain administration.", name="BloodHound")],
    [AIMessage(content="<untrusted_mythic_task_observations>secret/raw task output"
                       "</untrusted_mythic_task_observations>", name="Mythic_Operator",
               additional_kwargs={"_worker_task_evidence": True})],
    [AIMessage(content="task 12: sensitive reference", name="Mythic_Operator",
               additional_kwargs={"_worker_task_references": True})],
    [AIMessage(content="", name="BloodHound", tool_calls=[{
        "name": "transfer_to_BloodHound", "args": {"task": "repeat"},
        "id": "attempt-3", "type": "tool_call",
    }])],
    [AIMessage(content="old request claimed success", name="BloodHound",
               additional_kwargs={"_request_id": "chat:bounded-supervision:request:1"})],
])
def test_two_returns_and_trailing_evidence_reach_model(monkeypatch, trailing):
    with pytest.raises(_ReachedModelBoundary):
        _invoke(monkeypatch, [_header(), _header(), *trailing])
