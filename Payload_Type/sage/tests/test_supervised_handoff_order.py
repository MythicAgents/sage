"""Offline production-path probes for assisted specialist return ordering."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ai.langgraph.model import (
    Model, _assisted_worker_observation_context, _create_handoff_tool,
    _supervised_completed_return_context,
)
from ai.langgraph.turn_authority import TurnAuthority


REQUEST = "chat:1:request:1"
EXACT = "Use callback 7 as reference, then propose upload."


def _operator_input(text: str = EXACT, request_id: str = REQUEST,
                    revision: int = 0, seq: int = 1) -> HumanMessage:
    kwargs = {"_request_id": request_id, "_operator_input": True,
              "_operator_input_revision": revision, "_seq": seq}
    if revision:
        kwargs["_operator_steer"] = True
    return HumanMessage(content=text, additional_kwargs=kwargs)


def _delegated(text: str = EXACT, *, seq: int = 2,
               request_id: str = REQUEST) -> HumanMessage:
    return HumanMessage(content=text, additional_kwargs={
        "_delegated_to": "Mythic_Operator", "_handoff_request_id": request_id,
        "_seq": seq,
    })


def _return_state() -> tuple[dict, list]:
    opening = _delegated()
    header = AIMessage(content="[Mythic_Payload completed task]",
                       name="Mythic_Payload",
                       additional_kwargs={"_is_completion_header": True,
                                          "_request_id": REQUEST, "_seq": 8})
    build = AIMessage(content=".", name="Mythic_Payload", tool_calls=[
        {"name": "create_payload", "args": {}, "id": "build-1", "type": "tool_call"},
    ])
    result = ToolMessage(content='{"agent_file_id":"uuid"}', tool_call_id="build-1",
                         name="create_payload")
    summary = AIMessage(content="Apollo payload exists; file id UUID.",
                        name="Mythic_Payload")
    evidence = AIMessage(
        content="<untrusted_mythic_task_observations>\n{}\n</untrusted_mythic_task_observations>",
        name="Mythic_Payload",
        additional_kwargs={"_worker_task_evidence": True, "_request_id": REQUEST},
    )
    state = {"_request_id": REQUEST, "_request_lane": "supervised_workflow",
             "messages": [], "supervisor_messages": [_operator_input(), header, summary, evidence],
             "mythic_operator_messages": [opening, header, build, result, summary]}
    return state, [opening, header, build, result, summary, evidence]


def test_assembled_operator_input_prioritizes_same_request_completed_return():
    state, parts = _return_state()
    tool = _create_handoff_tool(agent_name="Mythic_Operator")
    command = tool.func(SimpleNamespace(state=state, tool_call_id="resume-operator"),
                        "Model-authored wording must not replace authority.", "Resume")
    delegated = command.update["mythic_operator_messages"][-1]
    assert delegated.content == EXACT
    state["mythic_operator_messages"].append(delegated)

    model = Model.__new__(Model)
    model._autonomous_solve = False
    model._message_seq = 10
    model.state = {"_message_seq": 10}
    model.llm = None
    model.mythic_client = None
    model._turn_authority = TurnAuthority(mode="observe")
    seen = []

    class Agent:
        async def ainvoke(self, payload, config=None):
            seen.append(list(payload["messages"]))
            return {"messages": list(payload["messages"]) + [AIMessage(content="done")]}

    wrapped = model._wrap_create_agent(Agent(), "mythic_operator_messages", "Mythic_Operator")
    asyncio.run(wrapped(state, {}))
    assert len(seen) == 1
    provider_input = seen[0]
    assignments = [m for m in provider_input
                   if m.additional_kwargs.get("_assisted_assignment_context")]
    assert len(assignments) == 1
    assert "Model-authored wording must not replace authority." in assignments[0].content
    without_assignment = [m for m in provider_input if m not in assignments]
    assert without_assignment[:-1] == [parts[0], delegated] + parts[1:]
    assert isinstance(provider_input[-1], HumanMessage)
    assert provider_input[-1].additional_kwargs["_synthetic_nudge"] == (
        "provider_requires_user_turn"
    )
    assert provider_input[-1].content == (
        "Internal conversation continuation. The operator's request and authorization are unchanged."
    )
    assert provider_input[3].tool_calls[0]["id"] == provider_input[4].tool_call_id
    human_contents = [message.content for message in provider_input
                      if isinstance(message, HumanMessage)
                      and not message.additional_kwargs.get("_synthetic_nudge")]
    assert human_contents == [EXACT, EXACT]
    assert state["mythic_operator_messages"][-1] is delegated  # persisted order is unchanged


@pytest.mark.parametrize("change", [
    "new_request", "accepted_steer", "missing_earlier_exact", "wrong_latest_input",
    "foreign_header", "conversation_lane", "intervening_human", "wrong_handoff_identity",
    "old_identical_text", "missing_authenticated_sequence",
])
def test_return_order_near_matches_keep_original_input_order(change):
    state, _ = _return_state()
    channel = state["mythic_operator_messages"] + [_delegated(seq=11)]
    if change == "new_request":
        state["_request_id"] = "chat:1:request:2"
        state["supervisor_messages"].append(
            _operator_input(request_id=state["_request_id"], seq=10)
        )
        channel[-1] = _delegated(seq=11, request_id=state["_request_id"])
    elif change == "accepted_steer":
        state["supervisor_messages"].append(_operator_input("Only report.", revision=1,
                                                           seq=10))
        channel[-1] = _delegated("Only report.", seq=11)
    elif change == "missing_earlier_exact":
        channel[0] = _delegated("Different opening")
    elif change == "wrong_latest_input":
        channel[-1] = _delegated("Different final instruction", seq=11)
    elif change == "foreign_header":
        channel[1] = channel[1].model_copy(update={
            "additional_kwargs": {"_is_completion_header": True, "_request_id": "older"},
        })
    elif change == "conversation_lane":
        state["_request_lane"] = "conversational"
    elif change == "intervening_human":
        channel.insert(-1, HumanMessage(content="Another turn"))
    elif change == "wrong_handoff_identity":
        channel[-1].additional_kwargs["_handoff_request_id"] = "other"
    elif change == "old_identical_text":
        state["supervisor_messages"][0] = _operator_input(seq=5)
    elif change == "missing_authenticated_sequence":
        state["supervisor_messages"][0].additional_kwargs.pop("_seq")
    assert _supervised_completed_return_context(channel, state, "Mythic_Operator") is channel


def test_observations_project_without_changing_operator_authority():
    state, _ = _return_state()
    channel = state["mythic_operator_messages"] + [_delegated(seq=11)]
    observed = _assisted_worker_observation_context(state["supervisor_messages"], channel)
    assert len(observed) == 1
    reordered = _supervised_completed_return_context(channel[:-1] + observed + channel[-1:],
                                                     state, "Mythic_Operator")
    assert reordered[1].content == EXACT
    assert reordered[-1] is observed[0]
    assert [message for message in reordered if isinstance(message, HumanMessage)] == (
        [channel[0], channel[-1]]
    )
