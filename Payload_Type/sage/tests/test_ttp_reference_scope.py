"""TTP references preserve content without presenting prerequisites as observations."""

import asyncio
import json
import socket
from unittest.mock import AsyncMock

import pytest

from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph import ttp_library
from ai.mcp import MCPManager


@pytest.mark.parametrize("connection", ["disconnected", "connected", "unknown"])
def test_guidance_distinguishes_reference_from_runtime_metadata(monkeypatch, connection):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("reference probe must not open network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    def servers():
        if connection == "unknown":
            raise RuntimeError("registry unavailable")
        return ["bloodhound"] if connection == "connected" else []

    monkeypatch.setattr(MCPManager, "get_connected_servers", servers)
    sink = MythicTools(agent_task_id="reference-scope-test")
    monkeypatch.setattr(sink, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    tool = next(t for t in sink.get_tools(["get_ttp_guidance"])
                if t.name == "get_ttp_guidance")
    result = json.loads(asyncio.run(tool.ainvoke({
        "goal": "run SharpHound to collect AD data for BloodHound ingestion",
        "callback_display_id": 7,
    })))
    frontmatter, body = ttp_library.load_ttp(result["slug"])
    assert result["frontmatter"] == frontmatter
    assert result["guidance"] == ttp_library.guidance_body(body)
    assert result["payload_type"] == "apollo"
    scope = result["reference_scope"]
    assert set(scope["reference_fields"]) == {"frontmatter", "guidance", "execution_on_agent"}
    assert scope["prerequisites_evaluated"] is False
    assert set(scope["metadata_fields"]) == {"payload_type", "recommendation.connected"}
    assert "not observed target state" in tool.description
    if connection == "connected":
        assert "recommendation" not in result
    else:
        assert result["recommendation"]["connected"] is (False if connection == "disconnected" else None)
