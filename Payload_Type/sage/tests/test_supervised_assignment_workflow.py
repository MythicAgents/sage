"""Scripted production graph: source read, handback, correction and assessment.

These checks prove transport and authority, not that a real model chooses the
scripted correction. No provider, MCP server or target is contacted.
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from tests import test_scripted_handoff as harness_module


def call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{
        "name": name, "args": args, "id": call_id, "type": "tool_call",
    }])


def capture(monkeypatch):
    seen = []
    original = harness_module._ScriptedModel._generate

    def generate(self, messages, *args, **kwargs):
        seen.append(list(messages))
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(harness_module._ScriptedModel, "_generate", generate)
    monkeypatch.setattr(harness_module, "_RECURSION_LIMIT", 32)
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))
    return seen


@pytest.mark.parametrize("first_answer", [
    "The foothold is DC-2\\localuser and already controls every domain.",
    "Callback 7 reports LAB\\analyst on WORKSTATION-7; graph relationships remain unverified.",
])
def test_source_identity_and_refined_assignment_reach_supervisor_after_three_returns(monkeypatch, first_answer):
    seen = capture(monkeypatch)
    from ai.mcp import MCPManager
    from ai.langgraph.model import Model
    # Only connection discovery is inert; the real guard and specialist node run.
    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _server: []))
    monkeypatch.setattr(Model, "_bloodhound_server_is_locally_pinned", lambda self, server: server == "offline-graph")
    prompt = "Assess the available graph from the current foothold and report what remains."
    first = "Assess graph relationships for LAB\\analyst on WORKSTATION-7 using callback 7 observations."
    correction = ("Your prior answer used DC-2\\localuser; callback 7 observations instead name "
                  "LAB\\analyst on WORKSTATION-7. Correct the subject and identify unproven relationships.")
    reads = []

    @tool
    def list_callbacks() -> str:
        """Read inert callback metadata from the control plane."""
        reads.append("callbacks")
        return json.dumps([{"id": 7, "agent": "test-agent", "host": "WORKSTATION-7",
                            "user": "LAB\\analyst", "integrity": 2, "status": "alive",
                            "secs_since_checkin": 1.0}])

    script = [
        call("transfer_to_Mythic_Operator", {"handoff_title": "Read callback identity",
                                           "handoff_instruction": "Read the available callback metadata."}, "route-1"),
        call("list_callbacks", {}, "callbacks-1"),
        call("handback_to_supervisor", {"reason": "Metadata available", "summary": "Callback metadata read.",
                                         "outcome": "progress", "next_owner": ""}, "return-1"),
        call("transfer_to_BloodHound", {"handoff_title": "Assess graph", "handoff_instruction": first}, "route-2"),
        AIMessage(content=first_answer),
        call("transfer_to_BloodHound", {"handoff_title": "Correct subject", "handoff_instruction": correction}, "route-3"),
        AIMessage(content="Graph analysis is incomplete; the earlier subject was unsupported."),
        call("respond_to_user", {"final_response":
             "The graph assessment remains incomplete; the earlier subject is unsupported."}, "final-1"),
    ]
    harness = harness_module._Harness(script)
    build = harness._build

    def build_with_tools(messages):
        model = build(messages)
        model.mythic_client.get_tools.side_effect = lambda names: (
            [list_callbacks] if "list_callbacks" in names else [])
        return model

    harness._build = build_with_tools
    result = asyncio.run(harness.run(prompt, assisted=True))
    assert len(seen) == 8  # Supervisor assesses the third specialist return
    for index, instruction in ((4, first), (6, correction)):
        messages = seen[index]
        assignment = [m for m in messages if m.additional_kwargs.get("_assisted_assignment_context")]
        assert len(assignment) == 1
        assert json.loads(assignment[0].content.splitlines()[-2])["assignment"] == instruction
        assert isinstance(assignment[0], AIMessage)
        assert "not operator input, approval, or source evidence" in assignment[0].content
        assert [m.content for m in messages if isinstance(m, HumanMessage)][-1] == prompt
        facts = [m for m in messages if m.additional_kwargs.get("_worker_callback_evidence")]
        assert len(facts) == 1
        assert "WORKSTATION-7" in facts[0].content and "analyst" in facts[0].content
        assert "DC-2" not in facts[0].content
    supervisor_input = "\n".join(str(m.content) for m in seen[5])
    assert first_answer in supervisor_input and "WORKSTATION-7" in supervisor_input
    returned = [m for m in result["supervisor_messages"]
                if m.additional_kwargs.get("_assisted_assignment_return")]
    assert len(returned) == 3  # read, assessment, then corrective assessment
    for header, assignment, worker in zip(
        returned,
        ("Read the available callback metadata.", first, correction),
        ("Mythic_Operator", "BloodHound", "BloodHound"),
    ):
        assert "specialist returned; result not verified" in header.content
        assert "not operator input, approval, or source evidence" in header.content
        provenance = json.loads(header.content.splitlines()[-2])
        assert provenance == {"request_id": "chat:1:request:1", "caller": "Supervisor",
                              "worker": worker, "assignment": assignment}
    assert returned[1] in seen[5]
    assert first_answer in str(seen[5][seen[5].index(returned[1]) + 1].content)
    assert any(
        m.additional_kwargs.get("_assisted_assignment") == correction
        for m in result["bloodhound_messages"])
    final = [m for m in result["messages"] if m.additional_kwargs.get("_is_final_report")][-1]
    assert final.content == "The graph assessment remains incomplete; the earlier subject is unsupported."
    assert first_answer not in final.content and "<untrusted_" not in final.content
    assert reads == ["callbacks"]
    assert not harness.mythic.issue_task.called


@pytest.mark.parametrize("change", ["foreign_request", "new_operator_steer"])
def test_stale_assignment_is_not_projected_after_request_or_operator_change(change):
    from types import SimpleNamespace
    from ai.langgraph.model import Model, _create_handoff_tool
    from ai.langgraph.turn_authority import TurnAuthority
    from tests.test_supervised_handoff_order import _return_state

    state, _ = _return_state()
    command = _create_handoff_tool(agent_name="Mythic_Operator").func(
        SimpleNamespace(state=state, tool_call_id="assign"), "Obsolete task focus", "Review")
    delegated = command.update["mythic_operator_messages"][-1]
    state["mythic_operator_messages"].append(delegated)
    if change == "foreign_request":
        delegated.additional_kwargs["_handoff_request_id"] = "other-request"
    else:
        state["supervisor_messages"].append(HumanMessage(content="Only summarize now.", additional_kwargs={
            "_operator_input": True, "_operator_steer": True, "_operator_input_revision": 1,
            "_request_id": state["_request_id"],
        }))
    model = Model.__new__(Model)
    model._autonomous_solve = False
    model._message_seq = 20
    model.state = {"_message_seq": 20}
    model.llm = model.mythic_client = None
    model._turn_authority = TurnAuthority(mode="observe")
    seen = []

    class Agent:
        async def ainvoke(self, payload, config=None):
            seen.extend(payload["messages"])
            return {"messages": payload["messages"] + [AIMessage(content="done")]}

    outcome = asyncio.run(model._wrap_create_agent(Agent(), "mythic_operator_messages", "Mythic_Operator")(state, {}))
    assert not any(m.additional_kwargs.get("_assisted_assignment_context") for m in seen)
    assert not any(m.additional_kwargs.get("_assisted_assignment_return")
                   for m in outcome.update.get("supervisor_messages", []))


def test_three_specialist_returns_can_reach_pending_native_approval(monkeypatch, tmp_path):
    """The fourth handoff can propose an effect, but cannot execute it without consent."""
    from ai.mcp import MCPManager
    from ai.langgraph.model import Model
    from ai.langgraph.mythic_tools import MythicTools
    from sage_chat import session
    from sage_chat.headless import build_chat_request
    from sage_chat.service import bind_channel_thread_id
    from tests.test_supervised_objective_native import driver

    capture(monkeypatch)
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _server: []))
    monkeypatch.setattr(Model, "_bloodhound_server_is_locally_pinned", lambda self, server: server == "offline-graph")
    effects = []
    sink = MythicTools(agent_task_id="offline-return-approval", operation_id=1, channel_id=1)
    async def available_command(_command, _callback):
        return {"status": "available", "command": "whoami"}

    async def empty_schema(_command, _callback):
        return []

    sink._authenticate_live_command = available_command
    sink._fetch_command_schema = empty_schema

    @tool
    async def issue_task_and_waitfor_task_output(
        command: str, parameters: str, callback_display_id: int,
        token_id: int | None = None, timeout: int | None = None,
        visibility_context: dict | None = None,
    ) -> str:
        """Inert guarded command with the production request and approval check."""
        blocker = sink._turn_authority_issue_blocker(
            command, callback_display_id, parameters=parameters,
            token_id=token_id, timeout=timeout, visibility_context=visibility_context,
        )
        if blocker:
            return blocker
        effects.append((command, callback_display_id))
        return "inert output"

    sink.get_tools = lambda names: ([issue_task_and_waitfor_task_output]
                                    if "issue_task_and_waitfor_task_output" in names else [])
    script = [
        call("transfer_to_BloodHound", {"handoff_title": "Review graph", "handoff_instruction": "Review one relationship."}, "route-a"),
        AIMessage(content="One relationship needs corroboration."),
        call("transfer_to_BloodHound", {"handoff_title": "Check scope", "handoff_instruction": "Check the scope."}, "route-b"),
        AIMessage(content="Scope remains uncertain."),
        call("transfer_to_BloodHound", {"handoff_title": "Check evidence", "handoff_instruction": "Check the evidence."}, "route-c"),
        AIMessage(content="Evidence is incomplete."),
        call("transfer_to_Mythic_Operator", {"handoff_title": "Propose check", "handoff_instruction": "Propose whoami on callback 7."}, "route-d"),
        call("issue_task_and_waitfor_task_output", {"callback_display_id": 7,
             "command": "whoami", "parameters": ""}, "proposed-action"),
    ]
    harness = harness_module._Harness(script)

    async def episode():
        model = harness._build(script)
        model.mode = "supervised"
        model.operation_id = 1
        model.mythic_client = sink
        model._payload_names = []
        model._native_chat_explicit_hitl = True
        model.command_name = "chat"
        model._rebuild_graph()
        chat = driver.DriverChat(model)
        request = build_chat_request("Assess evidence, then propose a scoped check.",
                                     channel_id=1, operation_id=1, request_id=1,
                                     config={"mode": "supervised", "autonomous_solve": False})
        bind_channel_thread_id(request, model)
        try:
            await chat.chat(request)
            cards = [row["metadata"]["input_requested"] for row in chat.emissions
                     if (row.get("metadata") or {}).get("special_type") == "input_requested"]
            assert len(cards) == 1
            assert cards[0]["data"]["arguments"]["command"] == "whoami"
            assert effects == []
            assert not sink._active_approval_claim
            headers = [m for m in model.state["supervisor_messages"]
                       if m.additional_kwargs.get("_is_completion_header")]
            assert len(headers) >= 3
        finally:
            await session.drop_channel_session(request)

    asyncio.run(episode())
