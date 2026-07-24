import asyncio
import sys
from pathlib import Path

import anyio
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


def test_mcp_registry_wrapper_evicts_server_after_closed_transport():
    manager = MCPServerManager()
    events = []
    manager.sessions["Nemesis"] = object()
    manager.connections["Nemesis"] = {"transport": "sse"}
    manager.configs["Nemesis"] = mcpmod.create_sse_config(
        name="Nemesis",
        url="https://nemesis.local/mcp/sse",
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
    )
    manager._session_contexts["Nemesis"] = object()
    manager.tools["Nemesis"] = []

    async def get_file_details(object_id: str):
        raise anyio.ClosedResourceError()

    tool = StructuredTool.from_function(
        coroutine=get_file_details,
        name="get_file_details",
        description="Read a file record.",
    )
    wrapped = manager._wrap_tool_for_visibility("Nemesis", tool)
    token = manager.set_execution_observer(events.append)
    try:
        with pytest.raises(anyio.ClosedResourceError):
            asyncio.run(wrapped.ainvoke({"object_id": "abc"}))
    finally:
        manager.reset_execution_observer(token)

    assert [event["status"] for event in events] == ["started", "error"]
    assert manager.get_connected_servers() == []
    assert manager.get_tools_by_server("Nemesis") == []
    assert "Nemesis" not in manager.connections
    assert "Nemesis" not in manager.configs
    assert "Nemesis" not in manager._session_contexts


def test_mcp_registry_wrapper_keeps_server_after_non_transport_tool_error():
    manager = MCPServerManager()
    manager.sessions["Nemesis"] = object()
    manager.connections["Nemesis"] = {"transport": "sse"}
    manager.configs["Nemesis"] = mcpmod.create_sse_config(
        name="Nemesis",
        url="https://nemesis.local/mcp/sse",
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
    )
    manager._session_contexts["Nemesis"] = object()
    manager.tools["Nemesis"] = []

    async def get_file_details(object_id: str):
        raise RuntimeError("application-level failure")

    tool = StructuredTool.from_function(
        coroutine=get_file_details,
        name="get_file_details",
        description="Read a file record.",
    )
    wrapped = manager._wrap_tool_for_visibility("Nemesis", tool)

    with pytest.raises(RuntimeError, match="application-level failure"):
        asyncio.run(wrapped.ainvoke({"object_id": "abc"}))

    assert manager.get_connected_servers() == ["Nemesis"]
    assert "Nemesis" in manager.connections
    assert "Nemesis" in manager.configs
    assert "Nemesis" in manager._session_contexts


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


def test_offensive_runtime_allows_canonical_bloodhound_control_plane(monkeypatch):
    manager = MCPServerManager()
    calls = []
    directory = "/srv/bloodhound-mcp"
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", directory)
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


def test_offensive_runtime_rechecks_bloodhound_pin_after_registry_swap(monkeypatch):
    manager = MCPServerManager()
    calls = []
    trusted = "/srv/bloodhound-mcp"
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", trusted)

    def _config(directory):
        return mcpmod.create_stdio_config(
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

    manager.configs["BloodHound"] = _config(trusted)

    async def cypher_query(query: str):
        calls.append(query)
        return {"rows": []}

    wrapped = manager._wrap_tool_for_visibility(
        "BloodHound",
        StructuredTool.from_function(
            coroutine=cypher_query,
            name="cypher_query",
            description="Query the BloodHound graph.",
        ),
    )
    manager.configs["BloodHound"] = _config("/tmp/operator-controlled")
    token = manager.set_execution_context(mcpmod.MCP_EXECUTION_CONTEXT_OFFENSIVE_RUNTIME)
    try:
        with pytest.raises(PermissionError, match="source connection is no longer active"):
            asyncio.run(wrapped.ainvoke({"query": "MATCH (n) RETURN n"}))
    finally:
        manager.reset_execution_context(token)

    assert calls == []


def test_bloodhound_identity_rejects_caller_supplied_process_environment(monkeypatch):
    manager = MCPServerManager()
    directory = "/srv/bloodhound-mcp"
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", directory)
    config = mcpmod.create_stdio_config(
        name="BloodHound",
        command="uv",
        args=["--directory", directory, "run", "main.py"],
        env={"PATH": "/tmp/operator-controlled", "PYTHONPATH": "/tmp/operator-controlled"},
        cwd=directory,
        encoding=None,
        encoding_error_handler=None,
        session_kwargs=None,
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_BLOODHOUND_CONTROL_PLANE,
    )

    assert manager._is_canonical_bloodhound_config(config) is False


def test_same_name_mcp_connects_are_serialized_through_tool_registration():
    manager = MCPServerManager()
    active = 0
    max_active = 0

    async def _connect_locked(_config):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return True, None

    manager._connect_server_locked = _connect_locked
    first = mcpmod.create_sse_config(
        name="BloodHound",
        url="https://first.invalid/mcp",
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
    )
    second = mcpmod.create_sse_config(
        name="BloodHound",
        url="https://second.invalid/mcp",
        sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
    )

    async def _race():
        return await asyncio.gather(
            manager.connect_server(first),
            manager.connect_server(second),
        )

    assert asyncio.run(_race()) == [(True, None), (True, None)]
    assert max_active == 1


def test_refresh_cannot_commit_stale_tools_across_same_name_connect(monkeypatch):
    directory = "/srv/bloodhound-mcp"
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", directory)
    started_refresh = asyncio.Event()
    release_refresh = asyncio.Event()
    calls = []
    retained_b_wrappers = []

    class _Session:
        def __init__(self, label):
            self.label = label

        async def initialize(self):
            return None

    class _SessionContext:
        def __init__(self, label):
            self.session = _Session(label)

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return None

    def _create_session(connection):
        label = "A" if connection.get("transport") == "stdio" else "B"
        return _SessionContext(label)

    async def _load_tools(session, *, connection):
        del connection
        if session.label == "B":
            started_refresh.set()
            await release_refresh.wait()

        async def _cypher_query(query: str):
            calls.append(session.label)
            return session.label

        return [StructuredTool.from_function(
            coroutine=_cypher_query,
            name="cypher_query",
            description=f"source-{session.label}",
        )]

    monkeypatch.setattr(mcpmod, "create_session", _create_session)
    monkeypatch.setattr(mcpmod, "load_mcp_tools", _load_tools)

    async def _scenario():
        manager = MCPServerManager()
        b_config = mcpmod.create_sse_config(
            name="BloodHound",
            url="https://operator-controlled.invalid/mcp",
            sage_execution_class=mcpmod.MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
        )
        b_session = _Session("B")
        b_connection = {"transport": "sse", "url": b_config.url}
        manager.configs["BloodHound"] = b_config
        manager.sessions["BloodHound"] = b_session
        manager.connections["BloodHound"] = b_connection
        manager._session_contexts["BloodHound"] = _SessionContext("B")

        original_wrap = manager._wrap_tool_for_visibility

        def _capture_wrap(server_name, tool, **kwargs):
            wrapped = original_wrap(server_name, tool, **kwargs)
            if tool.description == "source-B" and wrapped is not None:
                retained_b_wrappers.append(wrapped)
            return wrapped

        manager._wrap_tool_for_visibility = _capture_wrap

        refresh_task = asyncio.create_task(manager.refresh_tools("BloodHound"))
        await started_refresh.wait()
        a_config = mcpmod.create_stdio_config(
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
        connect_task = asyncio.create_task(manager.connect_server(a_config))
        await asyncio.sleep(0.01)
        assert connect_task.done() is False

        release_refresh.set()
        await refresh_task
        assert await connect_task == (True, None)
        assert manager.is_bloodhound_server("BloodHound") is True
        current = manager.get_tools_by_server("BloodHound")
        assert len(current) == 1
        assert current[0].description == "source-A"
        assert len(retained_b_wrappers) == 1

        token = manager.set_execution_context(mcpmod.MCP_EXECUTION_CONTEXT_OFFENSIVE_RUNTIME)
        try:
            assert await current[0].ainvoke({"query": "MATCH (n) RETURN n"}) == "A"
            with pytest.raises(PermissionError, match="source connection is no longer active"):
                await retained_b_wrappers[0].ainvoke({"query": "MATCH (n) DETACH DELETE n"})
        finally:
            manager.reset_execution_context(token)

    asyncio.run(_scenario())
    assert calls == ["A"]


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
