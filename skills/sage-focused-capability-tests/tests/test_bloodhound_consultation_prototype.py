"""Offline assembled proof for the opt-in agent-as-tool consultation prototype."""

import asyncio
import aiosqlite
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Payload_Type" / "sage"))
from ai.langgraph.request_contract import build_request_contract  # noqa: E402
from tests.test_scripted_handoff import _Harness, _ScriptedModel  # noqa: E402

SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/bloodhound_consultation_prototype.py"
spec = importlib.util.spec_from_file_location("bloodhound_consultation_prototype", SCRIPT)
prototype = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prototype)


def _call(name, args, id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args,
                                             "id": id, "type": "tool_call"}])


def test_two_consultations_share_supervisor_conversation_and_stop(monkeypatch):
    from ai.mcp import MCPManager
    from ai.langgraph import mcp_tool_policy
    monkeypatch.setattr(mcp_tool_policy, "_loaded_policy", {"servers": {"offline-graph": {"default": "read_only"}}})
    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))

    @tool
    def domain_info() -> str:
        """Inert graph metadata."""
        return "offline graph"

    @tool
    def foreign_tool() -> str:
        """Unrelated MCP tool that BloodHound must not receive."""
        pytest.fail("foreign tool executed")

    monkeypatch.setattr(MCPManager, "get_tools_by_server",
                        staticmethod(lambda _: [domain_info, foreign_tool]))

    script = [
        _call("transfer_to_BloodHound", {"handoff_title": "Graph", "handoff_instruction": "Assess user A"}, "q1"),
        AIMessage(content="First answer: user A has no verified path."),
        _call("transfer_to_BloodHound", {"handoff_title": "Correct", "handoff_instruction": "Check missing path"}, "q2"),
        AIMessage(content="Second answer: path still unverified."),
    ]

    async def run():
        harness = _Harness(script)
        model = harness._build(script)
        model.mythic_client.get_tools.return_value = []
        model.mode = "supervised"
        model._bloodhound_server_is_locally_pinned = lambda server: server == "offline-graph"
        contract = build_request_contract(
            request_id="chat:1:request:1", channel_id="1", operation_id="operation-1",
            mode="supervised", autonomous_solve=False,
        )
        model.install_request_contract(contract)
        operator = HumanMessage(
            content="Assess graph paths for user A.",
            additional_kwargs={"_operator_input": True, "_request_id": contract.request_id},
        )
        model.state["supervisor_messages"].append(operator)
        model.state["messages"] = [operator]
        graph = prototype.build_offline_graph(model, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "consultation-pair"}, "recursion_limit": 64}
        result = await graph.ainvoke(model.state, config)
        return result, model

    result, model = asyncio.run(run())
    assert [tool.name for tool in model._bloodhound_tools_for_turn()] == ["domain_info"]
    messages = result["supervisor_messages"]
    assert len([m for m in messages if isinstance(m, ToolMessage)
                and m.name == "transfer_to_BloodHound"]) == 2
    assert any("First answer" in str(m.content) for m in messages)
    assert any("Second answer" in str(m.content) for m in messages)
    assert result["bloodhound_messages"] == model.state["bloodhound_messages"] == []
    assert result["messages"][-1].additional_kwargs["_supervised_stop_reason"] == "specialist_return_limit"


@pytest.mark.parametrize("prior_kind,expected_reads", [
    ("current_header", 1),
    ("old_header", 2),
    ("current_tool", 1),
    ("old_tool", 2),
])
def test_prior_return_is_charged_only_to_current_request(
    monkeypatch, prior_kind, expected_reads,
):
    from ai.mcp import MCPManager
    from ai.langgraph import mcp_tool_policy
    monkeypatch.setattr(mcp_tool_policy, "_loaded_policy", {"servers": {"offline-graph": {"default": "read_only"}}})
    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))

    @tool
    def domain_info() -> str:
        """Inert domain information."""
        return "offline"
    monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _: [domain_info]))

    script = [
        _call("transfer_to_BloodHound", {"handoff_instruction": "Question 1"}, "q1"),
        AIMessage(content="First consultation"),
    ]
    if expected_reads == 2:
        script += [
            _call("transfer_to_BloodHound", {"handoff_instruction": "Question 2"}, "q2"),
            AIMessage(content="Second consultation"),
        ]

    async def run():
        harness = _Harness(script)
        model = harness._build(script)
        model.mythic_client.get_tools.return_value = []
        model.mode = "supervised"
        model._bloodhound_server_is_locally_pinned = lambda server: server == "offline-graph"
        contract = build_request_contract(
            request_id="chat:1:request:1", channel_id="1", operation_id="operation-1",
            mode="supervised", autonomous_solve=False,
        )
        model.install_request_contract(contract)
        operator = HumanMessage(content="Assess graph.", additional_kwargs={
            "_operator_input": True, "_request_id": contract.request_id,
        })
        if prior_kind.endswith("header"):
            header_request = (contract.request_id if prior_kind == "current_header"
                              else "chat:old:request:1")
            model.state["supervisor_messages"].extend([
                AIMessage(content="Earlier return", additional_kwargs={
                    "_is_completion_header": True, "_request_id": header_request}), operator,
            ])
        else:
            prior_call = _call("transfer_to_BloodHound",
                               {"handoff_instruction": "Prior question"}, "prior-q")
            prior_result = ToolMessage(content="Prior paired answer",
                                       name="transfer_to_BloodHound", tool_call_id="prior-q")
            model.state["supervisor_messages"].extend(
                [operator, prior_call, prior_result] if prior_kind == "current_tool"
                else [prior_call, prior_result, operator]
            )
        model.state["messages"] = [operator]
        graph = prototype.build_offline_graph(model, checkpointer=InMemorySaver())
        return await graph.ainvoke(model.state, {
            "configurable": {"thread_id": "request-budget"}, "recursion_limit": 64,
        })

    result = asyncio.run(run())
    replies = [m for m in result["supervisor_messages"] if isinstance(m, ToolMessage)
               and m.name == "transfer_to_BloodHound"
               and "<untrusted_bloodhound_consultation>" in str(m.content)]
    assert len(replies) == expected_reads
    assert sum("<untrusted_bloodhound_consultation>" in str(m.content) for m in replies) == expected_reads
    assert result["messages"][-1].additional_kwargs["_supervised_stop_reason"] == "specialist_return_limit"


def test_parallel_consultations_are_rejected_before_worker_dispatch(monkeypatch):
    from ai.mcp import MCPManager
    from ai.langgraph import mcp_tool_policy
    monkeypatch.setattr(mcp_tool_policy, "_loaded_policy", {"servers": {"offline-graph": {"default": "read_only"}}})
    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))

    @tool
    def domain_info() -> str:
        """Inert graph metadata."""
        pytest.fail("parallel consultation dispatched")

    monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _: [domain_info]))
    parallel = AIMessage(content="", tool_calls=[
        {"name": "transfer_to_BloodHound", "args": {"handoff_instruction": "Question 1"},
         "id": "parallel-1", "type": "tool_call"},
        {"name": "transfer_to_BloodHound", "args": {"handoff_instruction": "Question 2"},
         "id": "parallel-2", "type": "tool_call"},
    ])
    script = [parallel, AIMessage(content="Parallel graph questions were not executed.")]

    async def run():
        harness = _Harness(script)
        model = harness._build(script)
        model.mythic_client.get_tools.return_value = []
        model.mode = "supervised"
        model._bloodhound_server_is_locally_pinned = lambda server: server == "offline-graph"
        contract = build_request_contract(
            request_id="chat:1:request:1", channel_id="1", operation_id="operation-1",
            mode="supervised", autonomous_solve=False,
        )
        model.install_request_contract(contract)
        operator = HumanMessage(content="Assess graph.", additional_kwargs={
            "_operator_input": True, "_request_id": contract.request_id,
        })
        model.state["supervisor_messages"].append(operator)
        model.state["messages"] = [operator]
        graph = prototype.build_offline_graph(model, checkpointer=InMemorySaver())
        return await graph.ainvoke(model.state, {
            "configurable": {"thread_id": "parallel-reject"}, "recursion_limit": 64,
        })

    result = asyncio.run(run())
    assert any("Parallel BloodHound consultations were not run" in str(m.content)
               for m in result["supervisor_messages"])
    assert not any("<untrusted_bloodhound_consultation>" in str(m.content)
                   for m in result["supervisor_messages"])


def test_local_handback_and_tool_read_return_together(monkeypatch):
    from ai.mcp import MCPManager
    from ai.langgraph import mcp_tool_policy
    monkeypatch.setattr(mcp_tool_policy, "_loaded_policy", {"servers": {"offline-graph": {"default": "read_only"}}})
    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))

    @tool
    def domain_info() -> str:
        """Inert graph metadata."""
        return "offline domain count: 1"
    monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _: [domain_info]))

    script = [
        _call("transfer_to_BloodHound", {"handoff_instruction": "Read graph"}, "q1"),
        _call("domain_info", {}, "read-1"),
        _call("summarize_and_handback", {"progress_summary": "One domain read",
                                               "tasks_remaining": "Check paths",
                                               "key_findings": "One domain"}, "handback-1"),
        _call("respond_to_user", {"final_response": "Graph unavailable; paths remain unverified."}, "done"),
    ]

    async def run():
        harness = _Harness(script)
        model = harness._build(script)
        model.mythic_client.get_tools.return_value = []
        model.mode = "supervised"
        model._bloodhound_server_is_locally_pinned = lambda server: server == "offline-graph"
        contract = build_request_contract(
            request_id="chat:1:request:1", channel_id="1", operation_id="operation-1",
            mode="supervised", autonomous_solve=False,
        )
        model.install_request_contract(contract)
        operator = HumanMessage(content="Read the graph and report remaining work.",
                                additional_kwargs={"_operator_input": True,
                                                   "_request_id": contract.request_id})
        model.state["supervisor_messages"].append(operator)
        model.state["messages"] = [operator]
        graph = prototype.build_offline_graph(model, checkpointer=InMemorySaver())
        return await graph.ainvoke(model.state, {
            "configurable": {"thread_id": "handback"}, "recursion_limit": 64,
        })

    result = asyncio.run(run())
    paired = [m for m in result["supervisor_messages"] if isinstance(m, ToolMessage)
              and m.name == "transfer_to_BloodHound"]
    assert len(paired) == 1
    assert "offline domain count: 1" in paired[0].content
    assert "One domain read" in paired[0].content
    assert "Check paths" in paired[0].content
    payload = json.loads(paired[0].content.splitlines()[1])
    assert payload["answer"] == ""
    assert result["supervisor_messages"][-1].additional_kwargs["_is_final_report"] is True
    assert "Graph unavailable" in result["supervisor_messages"][-1].content


def test_completion_errors_allow_explicit_synthesis_retry(monkeypatch):
    from ai.mcp import MCPManager
    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))

    @tool
    def domain_info() -> str:
        """Inert graph metadata."""
        return "offline"

    monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _: [domain_info]))
    script = [
        _call("respond_to_user", {"reuse_latest_worker_answer": True}, "reuse"),
        _call("respond_to_user", {"final_response": ""}, "missing"),
        _call("respond_to_user", {"final_response": "Graph unavailable; no result verified."}, "valid"),
    ]

    async def run():
        harness = _Harness(script)
        model = harness._build(script)
        model.mythic_client.get_tools.return_value = []
        model.mode = "supervised"
        model._bloodhound_server_is_locally_pinned = lambda server: server == "offline-graph"
        contract = build_request_contract(
            request_id="chat:1:request:1", channel_id="1", operation_id="operation-1",
            mode="supervised", autonomous_solve=False,
        )
        model.install_request_contract(contract)
        operator = HumanMessage(content="Report current graph state.", additional_kwargs={
            "_operator_input": True, "_request_id": contract.request_id,
        })
        model.state["supervisor_messages"].append(operator)
        model.state["messages"] = [operator]
        graph = prototype.build_offline_graph(model, checkpointer=InMemorySaver())
        return await graph.ainvoke(model.state, {
            "configurable": {"thread_id": "completion-errors"}, "recursion_limit": 64,
        })

    result = asyncio.run(run())
    completion_results = [m for m in result["supervisor_messages"]
                          if isinstance(m, ToolMessage) and m.name == "respond_to_user"]
    assert [m.status for m in completion_results] == ["error", "error", "success"]
    assert result["supervisor_messages"][-1].additional_kwargs["_is_final_report"] is True
    assert result["supervisor_messages"][-1].content == "Graph unavailable; no result verified."


def test_steer_after_first_return_does_not_reset_second_return_limit(monkeypatch, tmp_path):
    from ai.mcp import MCPManager
    from ai.langgraph import mcp_tool_policy
    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))
    policy = tmp_path / "policy.json"
    policy.write_text('{"servers":{"offline-graph":{"tools":{"domain_info":"guarded"}}}}')
    mcp_tool_policy.load_policy(str(policy))
    effects = []

    @tool
    def domain_info() -> str:
        """Read inert graph metadata after approval."""
        effects.append("read")
        return "offline"

    monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _: [domain_info]))
    first_script = [
        _call("transfer_to_BloodHound", {"handoff_instruction": "First graph question"}, "q1"),
        AIMessage(content="First answer"),
        _call("transfer_to_BloodHound", {"handoff_instruction": "Second graph question"}, "q2"),
        _call("domain_info", {}, "read-2"),
    ]
    third = _call("transfer_to_BloodHound", {"handoff_instruction": "Third graph question"}, "q3")
    resumed_script = [AIMessage(content="Second answer after denial"), third]

    async def run():
        contract = build_request_contract(
            request_id="chat:1:request:1", channel_id="1", operation_id="operation-1",
            mode="supervised", autonomous_solve=False,
        )
        operator = HumanMessage(content="Assess graph paths.", additional_kwargs={
            "_operator_input": True, "_request_id": contract.request_id,
        })
        first_harness = _Harness(first_script)
        first = first_harness._build(first_script)
        first.mythic_client.get_tools.return_value = []
        first.mode = "supervised"
        first._bloodhound_server_is_locally_pinned = lambda server: server == "offline-graph"
        first.install_request_contract(contract)
        first.state["supervisor_messages"].append(operator)
        first.state["messages"] = [operator]
        database = tmp_path / "checkpoint.db"
        first_connection = aiosqlite.connect(database, check_same_thread=False)
        first.graph = prototype.build_offline_graph(
            first, checkpointer=AsyncSqliteSaver(first_connection))
        config = {"configurable": {"thread_id": "steer-two-returns"}, "recursion_limit": 64}
        async for _ in first.graph.astream(first.state, config):
            pass
        paused = await first.graph.aget_state(config)
        await first_connection.close()

        resumed_harness = _Harness(resumed_script)
        resumed = resumed_harness._build(resumed_script)
        resumed.mythic_client.get_tools.return_value = []
        resumed.mode = "supervised"
        resumed._bloodhound_server_is_locally_pinned = lambda server: server == "offline-graph"
        resumed.install_request_contract(contract)
        resumed.state["supervisor_messages"].append(operator)
        resumed.state["messages"] = [operator]
        resumed_connection = aiosqlite.connect(database, check_same_thread=False)
        resumed.graph = prototype.build_offline_graph(
            resumed, checkpointer=AsyncSqliteSaver(resumed_connection))
        await resumed.handle_hitl_resume(
            "deny", "steer-two-returns",
            operator_message="Stop graph reads and report what is established.",
        )
        finished = await resumed.graph.aget_state(config)
        remaining_script = list(resumed._get_base_chat_model().messages)
        await resumed_connection.close()
        return paused, finished, remaining_script

    try:
        paused, finished, remaining_script = asyncio.run(run())
    finally:
        mcp_tool_policy.load_policy()
    assert paused.interrupts
    assert effects == []
    assert finished.next == ()
    assert len([m for m in finished.values["supervisor_messages"]
                if isinstance(m, ToolMessage) and m.name == "transfer_to_BloodHound"]) == 2
    assert finished.values["messages"][-1].additional_kwargs["_supervised_stop_reason"] == "specialist_return_limit"
    assert remaining_script == [third]


@pytest.mark.parametrize("decision", ["approve", "deny", "steer"])
def test_nested_guarded_read_interrupt_reaches_native_checkpoint(monkeypatch, tmp_path, decision):
    """Probe whether a nested agent pause is visible to the native resume handler."""
    from ai.mcp import MCPManager
    from ai.langgraph import mcp_tool_policy
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("network"))
    monkeypatch.setattr("socket.socket.connect_ex", lambda *_: pytest.fail("network"))
    monkeypatch.setattr(MCPManager, "get_connected_servers", staticmethod(lambda: ["offline-graph"]))
    policy = tmp_path / "policy.json"
    policy.write_text('{"servers":{"offline-graph":{"tools":{"domain_info":"guarded"}}}}')
    mcp_tool_policy.load_policy(str(policy))
    provider_inputs = []
    original_generate = _ScriptedModel._generate

    def capture_generate(self, messages, *args, **kwargs):
        provider_inputs.append(list(messages))
        return original_generate(self, messages, *args, **kwargs)

    monkeypatch.setattr(_ScriptedModel, "_generate", capture_generate)

    effects = []

    @tool
    def domain_info() -> str:
        """Read inert graph metadata after native approval."""
        effects.append("read")
        return "offline graph"

    monkeypatch.setattr(MCPManager, "get_tools_by_server", staticmethod(lambda _: [domain_info]))
    script = [
        _call("transfer_to_BloodHound", {"handoff_title": "Graph", "handoff_instruction": "Read domains"}, "q1"),
        _call("domain_info", {}, "read-1"),
        AIMessage(content="The graph was read."),
        _call("respond_to_user", {"final_response": "The graph was read."}, "done"),
    ]

    async def run():
        harness = _Harness(script)
        model = harness._build(script)
        model.mythic_client.get_tools.return_value = []
        model.mode = "supervised"
        model._bloodhound_server_is_locally_pinned = lambda server: server == "offline-graph"
        contract = build_request_contract(
            request_id="chat:1:request:1", channel_id="1", operation_id="operation-1",
            mode="supervised", autonomous_solve=False,
        )
        model.install_request_contract(contract)
        operator = HumanMessage(content="Read graph domains.", additional_kwargs={
            "_operator_input": True, "_request_id": contract.request_id,
        })
        model.state["supervisor_messages"].append(operator)
        model.state["messages"] = [operator]
        database = tmp_path / "checkpoint.db"
        first_connection = aiosqlite.connect(database, check_same_thread=False)
        graph = prototype.build_offline_graph(
            model, checkpointer=AsyncSqliteSaver(first_connection))
        model.graph = graph
        config = {"configurable": {"thread_id": "nested-approval"}, "recursion_limit": 64}
        events = []
        async for event in graph.astream(model.state, config):
            events.append(event)
        snapshot = await graph.aget_state(config)
        before_effects = list(effects)
        await first_connection.close()
        resumed_harness = _Harness(script[2:])
        resumed_model = resumed_harness._build(script[2:])
        resumed_model.mythic_client.get_tools.return_value = []
        resumed_model.mode = "supervised"
        resumed_model._bloodhound_server_is_locally_pinned = lambda server: server == "offline-graph"
        resumed_model.install_request_contract(contract)
        resumed_model.state["supervisor_messages"].append(operator)
        resumed_model.state["messages"] = [operator]
        reopened_connection = aiosqlite.connect(database, check_same_thread=False)
        resumed_graph = prototype.build_offline_graph(
            resumed_model, checkpointer=AsyncSqliteSaver(reopened_connection))
        resumed_model.graph = resumed_graph
        if snapshot.interrupts:
            with pytest.raises(RuntimeError, match="pending guarded-action batch changed"):
                await resumed_model.handle_hitl_resume(
                    "approve", "nested-approval", expected_action_digest="wrong-card-digest")
            assert effects == []
            await resumed_model.handle_hitl_resume(
                "deny" if decision == "steer" else decision,
                "nested-approval",
                operator_message="Stop graph reads and report the current evidence."
                if decision == "steer" else "",
            )
        finished = await resumed_graph.aget_state(config)
        if decision == "approve":
            with pytest.raises(RuntimeError, match="guarded|approval|pending"):
                await resumed_model.handle_hitl_resume("approve", "nested-approval")
        await reopened_connection.close()
        return model, events, snapshot, finished, before_effects

    try:
        model, events, snapshot, finished, before_effects = asyncio.run(run())
    finally:
        mcp_tool_policy.load_policy()
    assert before_effects == []
    assert any("__interrupt__" in event for event in events)
    assert snapshot.interrupts
    assert finished.next == ()
    assert effects == (["read"] if decision == "approve" else [])
    if decision == "steer":
        assert any(isinstance(message, HumanMessage)
                   and message.content == "Stop graph reads and report the current evidence."
                   for message in finished.values["supervisor_messages"])
        assert any(any(isinstance(message, HumanMessage)
                       and message.content == "Stop graph reads and report the current evidence."
                       for message in inputs)
                   for inputs in provider_inputs)
