"""Conversation offers its model only the tools its request lane can execute.

The Conversation contract refuses guarded tools, Mythic file staging and guarded MCP tools. Before
this change the real graph still handed those tools to the provider (seven to Mythic_Operator, two
to Mythic_Payload, one to Sandbox), so a model could pick one, receive a denial and try a sibling.
The menu is now projected in code before every model call; the denial gates stay behind it.

These tests drive the real graph: `MythicTools.get_tools` registration, the agents
`_rebuild_graph` builds, their full middleware stacks and the real `ChatOpenAI` client. Only
external I/O is inert. The provider HTTP transport is an `httpx.MockTransport` that records each
request body and returns scripted replies, sockets refuse to connect, the Mythic SDK records any
call instead of making it, and the MCP registry is a fixed fake. Assertions read the JSON body the
provider would have received.
"""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool

from ai.langgraph import mcp_tool_policy
from ai.langgraph import model as mm
from ai.langgraph import mythic_tools as mt
from ai.langgraph.mythic_tools import CONTROL_PLANE_STAGING_TOOLS, GUARDED_TOOLS, MythicTools
from ai.langgraph.request_contract import build_request_contract

# Independent oracle: the execution classifications read from their source sets, not from the
# projection under test.
EXECUTION_TOOLS = GUARDED_TOOLS | CONTROL_PLANE_STAGING_TOOLS
AGENTS = (
    "Supervisor", "Mythic_Operator", "Mythic_Payload", "Sandbox", "BloodHound", "MCP_Manager", "Generalist",
)
OPERATOR_EXECUTION_TOOLS = [
    "add_credential", "download_tool", "ensure_tool_uploaded", "execute_capability",
    "ingest_collection", "issue_task_and_waitfor_task_output", "upload_file_by_file_uuid",
]
BLOODHOUND_READS = ("domain_info", "user_info")
READ_ONLY_POLICY = {
    "default": "guarded",
    "servers": {"BloodHound": {"default": "guarded", "tools": {name: "read_only" for name in BLOODHOUND_READS}}},
}
EXECUTION_LANES = (("supervised", False), ("supervised", True), ("auto", False))
FORGED_ISSUE = {"command": "whoami", "callback_display_id": 2, "parameters": ""}


class ProviderWire:
    """Records each provider request body and answers with the next scripted assistant message."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        message = self.replies.pop(0) if self.replies else {"role": "assistant", "content": "Inert final answer."}
        return httpx.Response(200, json={
            "id": f"inert-{len(self.requests)}",
            "object": "chat.completion",
            "created": 0,
            "model": "inert-wire",
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


class MythicSdkRecorder:
    """Stands in for the Mythic SDK module: records every call and makes none."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        async def record(*_args, **_kwargs):
            self.calls.append(name)
            raise ConnectionError(f"inert Mythic SDK: {name}")

        return record


def _bloodhound_read(query: str = "") -> str:
    """Inert BloodHound read."""
    return json.dumps({"query": query, "rows": []})


def _bloodhound_tools(names) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(func=_bloodhound_read, name=name, description=f"BloodHound {name} read")
        for name in names
    ]


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "engagement"))
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(tmp_path / "sage.db"))

    def refuse(*_args, **_kwargs):
        raise AssertionError("no socket may connect in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    wire = ProviderWire()
    real_init = mm.init_chat_model

    def inert_init(*args, **kwargs):
        kwargs.update(
            base_url="http://provider.invalid/v1",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(wire.handle)),
            http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
        )
        return real_init(*args, **kwargs)

    monkeypatch.setattr(mm, "init_chat_model", inert_init)

    agents: dict[str, tuple[Any, dict[str, Any]]] = {}
    real_create = mm.create_agent

    def capture(*args, **kwargs):
        agent = real_create(*args, **kwargs)
        agents[kwargs["name"]] = (agent, kwargs)
        return agent

    monkeypatch.setattr(mm, "create_agent", capture)
    sdk = MythicSdkRecorder()
    monkeypatch.setattr(mt, "mythic", sdk)

    registry = {"BloodHound": _bloodhound_tools(BLOODHOUND_READS)}
    manager = mm.MCPManager
    monkeypatch.setattr(manager, "get_connected_servers", lambda: list(registry))
    monkeypatch.setattr(manager, "is_bloodhound_server", lambda server: server == "BloodHound")
    monkeypatch.setattr(manager, "get_tools_by_server", lambda server: list(registry.get(server, ())))
    monkeypatch.setattr(mcp_tool_policy, "_loaded_policy", json.loads(json.dumps(READ_ONLY_POLICY)))
    return SimpleNamespace(wire=wire, agents=agents, sdk=sdk, registry=registry)


def _install(model, mode: str, autonomous_solve: bool, request_id: str) -> None:
    model.install_request_contract(build_request_contract(
        request_id=request_id, channel_id="offline", operation_id="offline",
        mode=mode, autonomous_solve=autonomous_solve,
    ))


async def _model(harness, mode: str, autonomous_solve: bool, request_id: str = "tool-surface"):
    """A native-chat Model on the real graph; it must be built inside the running event loop."""
    harness.agents.clear()
    model = mm.Model(
        provider="openai", model="inert-wire", system_prompt="conversation tool-surface regression",
        config={"configurable": {"api_key": "sk-inert"}}, task_id=0,
        agent_task_id="conversation-tool-surface", mode=mode,
    )
    model.mythic_client = MythicTools()
    model._native_chat_explicit_hitl = True
    _install(model, mode, autonomous_solve, request_id)
    model._rebuild_graph()
    return model


def _name(tool: dict[str, Any]) -> str:
    return str(tool.get("function", {}).get("name") or tool.get("name") or "")


def _names(tools) -> list[str]:
    return [_name(tool) for tool in tools]


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}},
    ]}


def _tool_result(body: dict[str, Any], call_id: str) -> str:
    return next(
        str(message.get("content"))
        for message in body["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id
    )


async def _invoke(harness, agent_name: str) -> dict[str, Any]:
    agent, _ = harness.agents[agent_name]
    return await asyncio.wait_for(
        agent.ainvoke({"messages": [HumanMessage(content="Which tools can you use here?")]}), 30,
    )


async def _first_menu(harness, agent_name: str) -> list[dict[str, Any]]:
    """The tools in this agent's first provider request."""
    harness.wire.requests.clear()
    harness.wire.replies.clear()
    await _invoke(harness, agent_name)
    assert harness.wire.requests, f"{agent_name} made no provider request"
    return list(harness.wire.requests[0].get("tools") or [])


async def _menus(harness, mode: str, autonomous_solve: bool) -> dict[str, list[dict[str, Any]]]:
    await _model(harness, mode, autonomous_solve)
    return {name: await _first_menu(harness, name) for name in AGENTS}


def _run(scenario):
    return asyncio.run(scenario())


@pytest.mark.parametrize("autonomous_solve", [False, True])
def test_conversation_provider_menus_carry_only_executable_tools(harness, autonomous_solve):
    async def scenario():
        return await _menus(harness, "supervised", False), await _menus(harness, "conversation", autonomous_solve)

    reference, conversation = _run(scenario)
    removed = {}
    for name in AGENTS:
        offered = _names(conversation[name])
        assert not set(offered) & EXECUTION_TOOLS, (name, sorted(set(offered) & EXECUTION_TOOLS))
        # Every permitted tool keeps its position and its exact provider schema.
        assert conversation[name] == [tool for tool in reference[name] if _name(tool) not in EXECUTION_TOOLS], name
        removed[name] = sorted(set(_names(reference[name])) - set(offered))

    assert removed == {
        "Supervisor": [],
        "Mythic_Operator": OPERATOR_EXECUTION_TOOLS,
        "Mythic_Payload": ["create_payload", "delete_payload"],
        "Sandbox": ["sandbox_exec"],
        "BloodHound": [],
        "MCP_Manager": [],
        "Generalist": [],
    }
    # Reads, handoffs and finish tools remain selectable.
    assert {
        "list_callbacks", "get_all_task_output_by_task_id", "get_task_history_for_callback",
        "read_credentials", "get_ttp_guidance", "summarize_and_handback", "handback_to_supervisor",
        "transfer_to_Mythic_Payload",
    } <= set(_names(conversation["Mythic_Operator"]))
    assert {"transfer_to_Mythic_Operator", "respond_to_user", "request_continuation"} <= set(
        _names(conversation["Supervisor"])
    )
    assert set(BLOODHOUND_READS) <= set(_names(conversation["BloodHound"]))
    assert _names(conversation["Sandbox"]) == ["summarize_and_handback"]


@pytest.mark.parametrize("mode,autonomous_solve", EXECUTION_LANES)
def test_execution_lanes_keep_their_registered_menus(harness, mode, autonomous_solve):
    menus = _run(lambda: _menus(harness, mode, autonomous_solve))
    for name in AGENTS:
        registered = [mm._offered_tool_name(tool) for tool in harness.agents[name][1]["tools"]]
        assert _names(menus[name]) == registered, name
    assert set(OPERATOR_EXECUTION_TOOLS) <= set(_names(menus["Mythic_Operator"]))
    assert {"create_payload", "delete_payload"} <= set(_names(menus["Mythic_Payload"]))
    assert "sandbox_exec" in _names(menus["Sandbox"])


def test_conversation_read_and_finish_dispatch_and_forged_omitted_call_is_denied_before_mythic(harness):
    wire = harness.wire

    async def scenario():
        await _model(harness, "conversation", False)

        # A permitted read runs, and its real result reaches the next provider call.
        wire.requests.clear()
        wire.replies[:] = [_tool_call("list_ttp_categories", {}, "read-1")]
        await _invoke(harness, "Mythic_Operator")
        assert len(wire.requests) == 2
        read_result = _tool_result(wire.requests[1], "read-1")
        assert '"acl-abuse"' in read_result and '"verdict": "blocked"' not in read_result

        # The finish tool dispatches: its handback ends the worker turn in the parent graph.
        wire.requests.clear()
        wire.replies[:] = [_tool_call(
            "summarize_and_handback",
            {"progress_summary": "Read the categories.", "tasks_remaining": "None."},
            "finish-1",
        )]
        with pytest.raises(Exception) as finished:
            await _invoke(harness, "Mythic_Operator")
        assert type(finished.value).__name__ == "ParentCommand"
        assert finished.value.args[0].goto == "__end__"
        assert len(wire.requests) == 1

        # A model that names an omitted execution tool anyway is still refused, and Mythic never sees it:
        # the post-response gate strips the call and records the denial before any tool runs.
        wire.requests.clear()
        wire.replies[:] = [_tool_call("issue_task_and_waitfor_task_output", FORGED_ISSUE, "forged-1")]
        result = await _invoke(harness, "Mythic_Operator")
        assert "issue_task_and_waitfor_task_output" not in _names(wire.requests[0].get("tools") or [])
        assert len(wire.requests) == 1
        assert harness.sdk.calls == []
        forged = result["messages"][-1]
        assert forged.tool_calls == []
        assert "issue_task_and_waitfor_task_output: conversational request denies guarded tool execution" in str(
            forged.content
        )
        assert not [m for m in result["messages"] if getattr(m, "tool_call_id", None) == "forged-1"]

    _run(scenario)


def test_schema_dictionary_tools_are_projected_like_tool_objects(harness):
    """An enrolled executable reaches the menu as a strict schema dictionary, not a tool object."""
    enrolled = {"type": "object", "required": ["command"], "properties": {
        "command": {"type": "string"}, "callback_display_id": {"type": "integer"}, "parameters": {"type": "string"},
    }}

    async def operator_menu(mode):
        model = await _model(harness, mode, False)
        model.mythic_client._enrolled_issue_selection = {"issue_schema": enrolled}
        return await _first_menu(harness, "Mythic_Operator")

    async def scenario():
        return await operator_menu("supervised"), await operator_menu("conversation")

    supervised, conversation = _run(scenario)
    issue = [tool for tool in supervised if _name(tool) == "issue_task_and_waitfor_task_output"]
    assert len(issue) == 1 and issue[0]["function"]["strict"] is True
    assert issue[0]["function"]["parameters"]["properties"] == enrolled["properties"]
    assert not set(_names(conversation)) & EXECUTION_TOOLS
    assert conversation == [tool for tool in supervised if _name(tool) not in EXECUTION_TOOLS]


def test_conversation_menu_follows_current_mcp_policy_and_registry(harness, monkeypatch):
    async def scenario():
        model = await _model(harness, "conversation", False)
        assert set(BLOODHOUND_READS) <= set(_names(await _first_menu(harness, "BloodHound")))

        # A policy change applies on the next call without a rebuild, in agreement with execution denial.
        guarded = json.loads(json.dumps(READ_ONLY_POLICY))
        guarded["servers"]["BloodHound"]["tools"]["domain_info"] = "guarded"
        monkeypatch.setattr(mcp_tool_policy, "_loaded_policy", guarded)
        offered = _names(await _first_menu(harness, "BloodHound"))
        assert "domain_info" not in offered and "user_info" in offered
        assert model._request_contract_block_reason("domain_info") == (
            "conversational request denies guarded MCP tool execution"
        )
        assert model._request_contract_block_reason("user_info") == ""

        # A registry change reaches a rebuilt graph, and that graph's menu is projected too.
        harness.registry["BloodHound"] = _bloodhound_tools(("domain_info", "user_info", "group_info"))
        guarded["servers"]["BloodHound"]["tools"]["group_info"] = "read_only"
        model._rebuild_graph()
        offered = _names(await _first_menu(harness, "BloodHound"))
        assert "group_info" in offered and "user_info" in offered and "domain_info" not in offered

    _run(scenario)


def test_menu_follows_the_installed_lane_on_a_reused_and_rebuilt_model(harness):
    async def operator_menu():
        return set(_names(await _first_menu(harness, "Mythic_Operator")))

    async def scenario():
        model = await _model(harness, "supervised", False, request_id="lane-1")
        assert set(OPERATOR_EXECUTION_TOOLS) <= await operator_menu()

        _install(model, "conversation", False, "lane-2")
        assert not await operator_menu() & EXECUTION_TOOLS

        model._rebuild_graph()
        assert not await operator_menu() & EXECUTION_TOOLS

        _install(model, "supervised", True, "lane-3")
        assert set(OPERATOR_EXECUTION_TOOLS) <= await operator_menu()

    _run(scenario)
