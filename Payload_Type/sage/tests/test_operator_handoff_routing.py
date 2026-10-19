"""The assembled Sage graph must route an Operator transfer to one successor."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt

from ai.langgraph import model as model_module
from ai.langgraph.model import Model
from ai.langgraph.request_contract import build_request_contract
from tests.test_scripted_handoff import _Harness, _handoff_call
from tests.test_supervised_objective_native import driver


def _model():
    async def build():
        model = Model(
            provider="openai", model="gpt-4o-mini", system_prompt="offline routing",
            config={"configurable": {"api_key": "sk-offline",
                                     "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
            task_id=0, agent_task_id="offline-routing", mode="supervised",
        )
        model.memory = InMemorySaver()
        model.mythic_client = MagicMock()
        model.mythic_client.get_payload_names = AsyncMock(return_value=[])
        model._payload_names = []
        return model

    return asyncio.run(build())


def _assemble(model, operator, payload, supervisor):
    model._graph_start_node_for_turn = lambda: "Mythic_Operator"
    model._mythic_operator_agent = lambda: operator
    model._mythic_payload_agent = lambda: payload
    model._supervisor_agent = lambda: supervisor

    async def idle(_state):
        return {}

    model._generalist_agent = lambda: idle
    model._bloodhound_agent = lambda: idle
    model._mcp_manager_agent = lambda: idle
    model._sandbox_agent = lambda: idle
    model._autonomous_executor_node = idle
    model._rebuild_graph()
    return model.graph


def test_payload_handoff_pauses_without_concurrent_supervisor_and_resumes_once():
    model = _model()
    calls = []

    async def operator(_state):
        calls.append("operator")
        return Command(goto="Mythic_Payload")

    async def payload(_state):
        calls.append("payload")
        interrupt({"approval": "inert"})
        calls.append("effect")
        return {}

    async def supervisor(_state):
        calls.append("supervisor")
        return {}

    graph = _assemble(model, operator, payload, supervisor)

    async def exercise():
        config = {"configurable": {"thread_id": "operator-payload-approval"}}
        first = await graph.ainvoke(model.state, config)
        assert "__interrupt__" in first
        assert calls == ["operator", "payload"], calls
        await graph.ainvoke(Command(resume=True), config)

    asyncio.run(exercise())
    assert calls == ["operator", "payload", "payload", "effect", "supervisor"], calls


def test_normal_operator_completion_routes_to_supervisor_once():
    model = _model()
    calls = []

    class FinishedAgent:
        async def ainvoke(self, args, config=None):
            calls.append("operator")
            return {"messages": [*args["messages"], AIMessage(content="Done.")]}

    async def payload(_state):
        calls.append("payload")
        return {}

    async def supervisor(_state):
        calls.append("supervisor")
        return Command(goto="__end__")

    operator = model._wrap_create_agent(
        FinishedAgent(), "mythic_operator_messages", "Mythic_Operator"
    )
    graph = _assemble(model, operator, payload, supervisor)
    asyncio.run(graph.ainvoke(model.state, {"configurable": {"thread_id": "operator-normal"}}))
    assert calls == ["operator", "supervisor"], calls


def test_operator_self_caller_does_not_duplicate_its_result():
    model = _model()

    class FinishedAgent:
        async def ainvoke(self, args, config=None):
            return {"messages": [*args["messages"], AIMessage(content="Inert result.")]}

    async def idle(_state):
        return {}

    async def supervisor(_state):
        return Command(goto="__end__")

    operator = model._wrap_create_agent(
        FinishedAgent(), "mythic_operator_messages", "Mythic_Operator"
    )
    graph = _assemble(model, operator, idle, supervisor)
    model.state["mythic_operator_messages"].append(HumanMessage(
        content="Continue inert work.",
        additional_kwargs={
            "_delegated_to": "Mythic_Operator",
            "_handoff_caller": "Mythic_Operator",
            "_handoff_request_id": "",
        },
    ))
    result = asyncio.run(graph.ainvoke(
        model.state, {"configurable": {"thread_id": "operator-self-caller"}}
    ))
    assert sum(message.content == "Inert result."
               for message in result["mythic_operator_messages"]) == 1


def test_old_payload_handoff_does_not_copy_into_current_operator_request():
    model = _model()

    class FinishedAgent:
        async def ainvoke(self, args, config=None):
            return {"messages": [*args["messages"], AIMessage(content="Old handoff result.")]}

    model.state["_request_id"] = "current-request"
    model.state["mythic_payload_messages"] = [HumanMessage(
        content="Old inert task.",
        additional_kwargs={
            "_delegated_to": "Mythic_Payload",
            "_handoff_caller": "Mythic_Operator",
            "_handoff_request_id": "prior-request",
        },
    )]
    payload = model._wrap_create_agent(
        FinishedAgent(), "mythic_payload_messages", "Mythic_Payload"
    )
    update = asyncio.run(payload(model.state))
    assert "mythic_operator_messages" not in update


def test_explicit_operator_handback_routes_to_supervisor_once():
    model = _model()
    calls = []

    async def operator(_state):
        calls.append("operator")
        return Command(goto="Supervisor")

    async def payload(_state):
        calls.append("payload")
        return {}

    async def supervisor(_state):
        calls.append("supervisor")
        return Command(goto="__end__")

    graph = _assemble(model, operator, payload, supervisor)
    asyncio.run(graph.ainvoke(model.state, {"configurable": {"thread_id": "operator-handback"}}))
    assert calls == ["operator", "supervisor"], calls


def test_operator_terminal_return_skips_supervisor():
    model = _model()
    calls = []

    async def operator(_state):
        calls.append("operator")
        return Command(goto="__end__")

    async def payload(_state):
        calls.append("payload")
        return {}

    async def supervisor(_state):
        calls.append("supervisor")
        return {}

    graph = _assemble(model, operator, payload, supervisor)
    asyncio.run(graph.ainvoke(model.state, {"configurable": {"thread_id": "operator-terminal"}}))
    assert calls == ["operator"], calls


def test_operator_failure_handler_routes_to_supervisor_once():
    model = _model()
    calls = []

    async def operator(_state):
        calls.append("operator")
        raise RuntimeError("inert failure")

    async def payload(_state):
        calls.append("payload")
        return {}

    async def supervisor(_state):
        calls.append("supervisor")
        return Command(goto="__end__")

    graph = _assemble(model, operator, payload, supervisor)
    result = asyncio.run(graph.ainvoke(
        model.state, {"configurable": {"thread_id": "operator-failure"}}
    ))
    assert calls == ["operator", "supervisor"], calls
    assert any("inert failure" in message.content
               for message in result["supervisor_messages"])


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_real_agent_handoff_to_payload_waits_for_approval_before_supervisor(
    monkeypatch, decision,
):
    """Use the actual assembled agents and handoff tools with independent scripts."""
    calls = []
    resumed_context = []
    payload_outcome = (
        "Inert payload completed." if decision == "approve"
        else "Inert payload request rejected."
    )

    def operator_first(_messages):
        calls.append("Mythic_Operator:initial")
        return AIMessage(content="", tool_calls=[{
            "name": "transfer_to_Mythic_Payload",
            "args": {"handoff_title": "Prepare inert payload",
                     "handoff_instruction": "Describe an inert payload configuration."},
            "id": "operator-to-payload", "type": "tool_call",
        }])

    def operator_resumed(messages):
        calls.append("Mythic_Operator:resumed")
        resumed_context.extend(messages)
        return AIMessage(content=f"Observed: {payload_outcome}")

    scripts = {
        "Supervisor": driver.ScriptedModel(messages=iter([
            lambda _messages: (calls.append("Supervisor:initial"), _handoff_call())[1],
            lambda _messages: (calls.append("Supervisor:resume"), _handoff_call())[1],
            lambda _messages: (calls.append("Supervisor:final"),
                               AIMessage(content=f"Observed: {payload_outcome}"))[1],
        ])),
        "Mythic_Operator": driver.ScriptedModel(messages=iter([
            operator_first, operator_resumed,
        ])),
        "Mythic_Payload": driver.ScriptedModel(messages=iter([
            lambda _messages: (calls.append("Mythic_Payload:propose"), AIMessage(
                content="", tool_calls=[{
                    "name": "create_payload", "args": {"filename": "inert.bin"},
                    "id": "payload-approval", "type": "tool_call",
                }]))[1],
            lambda _messages: (calls.append("Mythic_Payload:complete"),
                               AIMessage(content=payload_outcome))[1],
        ])),
    }
    original = model_module.create_agent

    def create_scripted(*args, **kwargs):
        node = kwargs["name"]
        if node in scripts:
            kwargs["model"] = scripts[node]
        return original(*args, **kwargs)

    monkeypatch.setattr(model_module, "create_agent", create_scripted)
    harness = _Harness([])
    effects = []

    @tool
    async def create_payload(filename: str) -> str:
        """Record one inert payload action after a supervised approval."""
        effects.append(filename)
        return "inert payload created"

    async def exercise():
        model = harness._build([])
        harness.model = model
        model.mode = "supervised"
        contract = build_request_contract(
            request_id="chat:1:request:1", channel_id="1", operation_id="operation-1",
            mode="supervised", autonomous_solve=False,
        )
        model.install_request_contract(contract)
        model.mythic_client.get_tools.side_effect = lambda names: (
            [create_payload] if "create_payload" in names else []
        )
        model._rebuild_graph()
        model.state["mythic_operator_messages"].append(
            AIMessage(content="Prior inert Operator observation marker.")
        )
        config = model._graph_run_config("operator-payload-real-agents")
        config["recursion_limit"] = 30
        operator_input = HumanMessage(content="Describe a payload.", additional_kwargs={
            "_request_id": contract.request_id, "_operator_input": True,
        })
        model.state["messages"] = [operator_input]
        model.state["supervisor_messages"].append(operator_input)
        first = await model.graph.ainvoke(model.state, config)
        assert "__interrupt__" in first
        assert calls == ["Supervisor:initial", "Mythic_Operator:initial",
                         "Mythic_Payload:propose"], calls
        assert effects == []
        await model.graph.ainvoke(Command(resume={"decisions": [{"type": decision}]}), config)

    asyncio.run(exercise())
    assert effects == (["inert.bin"] if decision == "approve" else [])
    assert calls == ["Supervisor:initial", "Mythic_Operator:initial", "Mythic_Payload:propose",
                     "Mythic_Payload:complete",
                     "Supervisor:resume", "Mythic_Operator:resumed", "Supervisor:final"], calls
    assert sum(payload_outcome in str(message.content)
               for message in resumed_context) == 1
    assert any("Prior inert Operator observation marker." in str(message.content)
               for message in resumed_context)
    assert any("Describe a payload." in str(message.content)
               for message in resumed_context)


def test_supervisor_direct_payload_result_does_not_copy_into_operator(monkeypatch):
    """A Payload task from Supervisor must not alter an unrelated Operator channel."""
    calls = []
    operator_context = []

    def operator_reply(messages):
        calls.append("Mythic_Operator")
        operator_context.extend(messages)
        return AIMessage(content="Operator finished.")

    scripts = {
        "Supervisor": driver.ScriptedModel(messages=iter([
            lambda _messages: (calls.append("Supervisor:direct"), AIMessage(
                content="", tool_calls=[{
                    "name": "transfer_to_Mythic_Payload",
                    "args": {"handoff_title": "Inert direct task",
                             "handoff_instruction": "Describe an inert payload."},
                    "id": "supervisor-to-payload", "type": "tool_call",
                }]))[1],
            lambda _messages: (calls.append("Supervisor:operator"), _handoff_call())[1],
            lambda _messages: (calls.append("Supervisor:final"),
                               AIMessage(content="Operator response reviewed."))[1],
        ])),
        "Mythic_Payload": driver.ScriptedModel(messages=iter([
            lambda _messages: (calls.append("Mythic_Payload"),
                               AIMessage(content="Direct payload completion marker."))[1],
        ])),
        "Mythic_Operator": driver.ScriptedModel(messages=iter([operator_reply])),
    }
    original = model_module.create_agent

    def create_scripted(*args, **kwargs):
        if kwargs["name"] in scripts:
            kwargs["model"] = scripts[kwargs["name"]]
        return original(*args, **kwargs)

    monkeypatch.setattr(model_module, "create_agent", create_scripted)
    harness = _Harness([])

    async def exercise():
        model = harness._build([])
        model.mode = "supervised"
        contract = build_request_contract(
            request_id="chat:1:request:direct", channel_id="1", operation_id="operation-1",
            mode="supervised", autonomous_solve=False,
        )
        model.install_request_contract(contract)
        model._rebuild_graph()
        operator_input = HumanMessage(content="Describe a payload.", additional_kwargs={
            "_request_id": contract.request_id, "_operator_input": True,
        })
        model.state["messages"] = [operator_input]
        model.state["supervisor_messages"].append(operator_input)
        config = model._graph_run_config("supervisor-direct-payload")
        config["recursion_limit"] = 25
        return await model.graph.ainvoke(model.state, config)

    result = asyncio.run(exercise())
    assert calls == ["Supervisor:direct", "Mythic_Payload", "Supervisor:operator",
                     "Mythic_Operator", "Supervisor:final"]
    assert sum("Direct payload completion marker." in str(message.content)
               for message in operator_context) == 0
    assert sum("Direct payload completion marker." in str(message.content)
               for message in result["supervisor_messages"]) == 1
