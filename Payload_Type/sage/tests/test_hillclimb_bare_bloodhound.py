"""Offline tests for the dynamic BloodHound MCP loader (the live MCP connection is validated on the lab).
Pins: schema conversion from a discovered tool, graceful no-op when unconfigured, dispatch fall-through.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import bare_bloodhound as bb  # noqa: E402


class _StubTool:
    args_schema = None

    def __init__(self, name, desc, args):
        self.name = name
        self.description = desc
        self._args = args

    @property
    def args(self):
        return self._args


def test_tool_spec_from_discovered_tool():
    spec = bb.tool_spec(_StubTool("cypher_query", "Run a raw Cypher query", {"query": {"type": "string"}}))
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "cypher_query"
    assert spec["function"]["description"].startswith("Run a raw Cypher")
    assert "query" in spec["function"]["parameters"]["properties"]


def test_load_is_graceful_noop_when_dir_missing(monkeypatch):
    # Point at a guaranteed-nonexistent dir -> _connections() returns None -> graceful empty load.
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", "/nonexistent/bloodhound/xyz")
    specs, registry = bb.load_bloodhound_mcp_tools()
    assert specs == [] and registry == {}


def test_env_var_overrides_hardcoded_default(monkeypatch):
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", "/override/dir")
    monkeypatch.setattr(bb, "_DEFAULT_BLOODHOUND_MCP_DIR", "/default/dir")
    monkeypatch.setattr(bb.os.path, "isdir", lambda p: True)
    conns = bb._connections()
    assert conns[bb.BLOODHOUND_SERVER]["args"] == ["--directory", "/override/dir", "run", "main.py"]


def test_hardcoded_default_used_when_env_absent(monkeypatch):
    monkeypatch.delenv("SAGE_BLOODHOUND_MCP_DIR", raising=False)
    monkeypatch.setattr(bb, "_DEFAULT_BLOODHOUND_MCP_DIR", "/default/dir")
    monkeypatch.setattr(bb.os.path, "isdir", lambda p: True)
    conns = bb._connections()
    assert conns[bb.BLOODHOUND_SERVER]["cwd"] == "/default/dir"


def test_dispatcher_falls_through_for_non_bloodhound_tool():
    # Returns None so a combined dispatcher can route Mythic/unknown tools elsewhere.
    assert bb.make_bloodhound_dispatcher({})({"tool": "issue_command", "args": {}}) is None
