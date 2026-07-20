import asyncio
import sys
from pathlib import Path

import pytest
from langchain_core.tools import StructuredTool


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai import mcp as mcpmod  # noqa: E402
from ai.mcp import MCPServerManager  # noqa: E402


def test_mcp_registry_wrapper_emits_raw_grouped_lifecycle_events():
    manager = MCPServerManager()
    events = []

    async def file_upload(action: str, file_bytes_base64: str, api_key: str):
        return {"status": "ok", "token": "server-secret"}

    tool = StructuredTool.from_function(
        coroutine=file_upload,
        name="file_upload",
        description="Upload a collection file.",
    )
    wrapped = manager._wrap_tool_for_visibility("BloodHound", tool)
    observer_token = manager.set_execution_observer(events.append)
    activity_token = manager.set_execution_activity({
        "id": "collection:1",
        "name": "Collection",
    })
    try:
        result = asyncio.run(wrapped.ainvoke({
            "action": "upload_bytes",
            "file_bytes_base64": "A" * 2048,
            "api_key": "operator-secret",
        }))
    finally:
        manager.reset_execution_activity(activity_token)
        manager.reset_execution_observer(observer_token)

    assert result == {"status": "ok", "token": "server-secret"}
    assert [event["status"] for event in events] == ["started", "completed"]
    assert events[0]["event_id"] == events[1]["event_id"]
    assert events[0]["source"] == "mcp"
    assert events[0]["tool_name"] == "file_upload"
    assert events[0]["arguments"]["file_bytes_base64"] == "A" * 2048
    assert events[0]["arguments"]["api_key"] == "operator-secret"
    assert events[0]["activity"] == {"id": "collection:1", "name": "Collection"}
    assert "server-secret" in events[1]["result_preview"]
    assert events[1]["output"] == events[1]["result_preview"]


def test_mcp_registry_wrapper_is_fail_open_when_observer_raises():
    manager = MCPServerManager()

    async def cypher_query(query: str):
        return {"rows": [query]}

    async def broken_observer(_event):
        raise RuntimeError("chat unavailable")

    tool = StructuredTool.from_function(
        coroutine=cypher_query,
        name="cypher_query",
        description="Run a graph query.",
    )
    wrapped = manager._wrap_tool_for_visibility("BloodHound", tool)
    token = manager.set_execution_observer(broken_observer)
    try:
        result = asyncio.run(wrapped.ainvoke({"query": "MATCH (n) RETURN n"}))
    finally:
        manager.reset_execution_observer(token)

    assert result == {"rows": ["MATCH (n) RETURN n"]}


def test_unclassified_mcp_server_is_denied_before_session_side_effect(monkeypatch):
    manager = MCPServerManager()
    called = []

    def _should_not_connect(_connection):
        called.append(True)
        raise AssertionError("session creation must not happen")

    monkeypatch.setattr(mcpmod, "create_session", _should_not_connect)
    config = mcpmod.create_stdio_config(
        name="unknown",
        command="python",
        args=[],
        env=None,
        cwd=None,
        encoding=None,
        encoding_error_handler=None,
        session_kwargs=None,
    )

    ok, error = asyncio.run(manager.connect_server(config))

    assert ok is False
    assert "unclassified" in str(error)
    assert called == []


def test_target_facing_mcp_server_is_denied_before_session_side_effect(monkeypatch):
    manager = MCPServerManager()
    called = []

    def _should_not_connect(_connection):
        called.append(True)
        raise AssertionError("session creation must not happen")

    monkeypatch.setattr(mcpmod, "create_session", _should_not_connect)
    config = mcpmod.create_stdio_config(
        name="ldap-sidecar",
        command="python",
        args=[],
        env=None,
        cwd=None,
        encoding=None,
        encoding_error_handler=None,
        session_kwargs=None,
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_TARGET_FACING,
    )

    ok, error = asyncio.run(manager.connect_server(config))

    assert ok is False
    assert "target_facing" in str(error)
    assert called == []


def test_non_target_control_plane_is_the_canonical_allowed_mcp_class():
    config = mcpmod.create_stdio_config(
        name="control-plane",
        command="python",
        args=[],
        env=None,
        cwd=None,
        encoding=None,
        encoding_error_handler=None,
        session_kwargs=None,
        sage_execution_class="control_plane",
    )

    assert mcpmod.MCP_EXECUTION_CLASS_CONTROL_PLANE == "non_target_control_plane"
    assert config.sage_execution_class == mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE
    assert mcpmod.execution_class_allowed(config.sage_execution_class) is True


def test_offensive_runtime_denies_generic_mcp_before_outbound_coroutine():
    manager = MCPServerManager()
    calls = []

    async def fetch_external(resource: str):
        calls.append(resource)
        return {"resource": resource}

    manager.configs["control-plane"] = mcpmod.create_stdio_config(
        name="control-plane",
        command="python",
        args=[],
        env=None,
        cwd=None,
        encoding=None,
        encoding_error_handler=None,
        session_kwargs=None,
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
    )
    tool = StructuredTool.from_function(
        coroutine=fetch_external,
        name="fetch_external",
        description="Fetch from a non-target control plane.",
    )
    wrapped = manager._wrap_tool_for_visibility("control-plane", tool)
    token = manager.set_execution_context(mcpmod.MCP_EXECUTION_CONTEXT_OFFENSIVE_RUNTIME)
    try:
        with pytest.raises(PermissionError, match="permits only the canonical BloodHound"):
            asyncio.run(wrapped.ainvoke({"resource": "status"}))
    finally:
        manager.reset_execution_context(token)

    assert calls == []


def test_offensive_runtime_allows_canonical_bloodhound_control_plane():
    manager = MCPServerManager()
    calls = []
    directory = "/srv/bloodhound-mcp"
    manager.configs["BloodHound"] = mcpmod.create_stdio_config(
        name="BloodHound",
        command="uv",
        args=["--directory", directory, "run", "main.py"],
        env={},
        cwd=directory,
        encoding=None,
        encoding_error_handler=None,
        session_kwargs=None,
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_BLOODHOUND_CONTROL_PLANE,
    )

    async def cypher_query(query: str):
        calls.append(query)
        return {"rows": []}

    tool = StructuredTool.from_function(
        coroutine=cypher_query,
        name="cypher_query",
        description="Query the BloodHound graph.",
    )
    wrapped = manager._wrap_tool_for_visibility("BloodHound", tool)
    token = manager.set_execution_context(mcpmod.MCP_EXECUTION_CONTEXT_OFFENSIVE_RUNTIME)
    try:
        result = asyncio.run(wrapped.ainvoke({"query": "MATCH (n) RETURN n LIMIT 1"}))
    finally:
        manager.reset_execution_context(token)

    assert result == {"rows": []}
    assert calls == ["MATCH (n) RETURN n LIMIT 1"]


def test_caller_mislabeled_bloodhound_server_is_denied_before_session_side_effect(monkeypatch):
    manager = MCPServerManager()
    called = []

    def _should_not_connect(_connection):
        called.append(True)
        raise AssertionError("session creation must not happen")

    monkeypatch.setattr(mcpmod, "create_session", _should_not_connect)
    config = mcpmod.create_stdio_config(
        name="ldap-sidecar",
        command="uv",
        args=["--directory", "/srv/not-bloodhound", "run", "main.py"],
        env={},
        cwd="/srv/not-bloodhound",
        encoding=None,
        encoding_error_handler=None,
        session_kwargs=None,
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_BLOODHOUND_CONTROL_PLANE,
    )

    ok, error = asyncio.run(manager.connect_server(config))

    assert ok is False
    assert "canonical BloodHound stdio configuration" in str(error)
    assert called == []
