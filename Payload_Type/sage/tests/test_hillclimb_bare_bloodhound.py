"""Offline tests for the parity-filtered BloodHound MCP loader.
Pins schema conversion, exact shipping allowlist parity, graceful no-op, and dispatch fall-through.
"""
import sys
import asyncio
import base64
import json
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


class _AsyncStubTool(_StubTool):
    def __init__(self, name, desc, args, responses):
        super().__init__(name, desc, args)
        self.responses = list(responses)
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self.responses.pop(0)


def test_tool_spec_from_discovered_tool():
    spec = bb.tool_spec(_StubTool("cypher_query", "Run a raw Cypher query", {"query": {"type": "string"}}))
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "cypher_query"
    assert spec["function"]["description"].startswith("Run a raw Cypher")
    assert "query" in spec["function"]["parameters"]["properties"]


def test_bare_bloodhound_admits_only_unique_exact_shipping_names():
    allowed = _StubTool("domain_info", "allowed", {})
    duplicate_one = _StubTool("user_info", "duplicate", {})
    duplicate_two = _StubTool("user_info", "duplicate", {})
    raw_cypher = _StubTool("cypher_query", "shipping withholds this", {})
    whitespace = _StubTool("group_info ", "malformed", {})

    admitted = bb.admitted_bloodhound_tools(
        [allowed, duplicate_one, duplicate_two, raw_cypher, whitespace]
    )
    assert [tool.name for tool in admitted] == ["domain_info"]
    assert {tool.name for tool in admitted} <= bb.SHIPPING_BLOODHOUND_TOOL_NAMES


def test_hidden_collection_adapter_requires_one_exact_compatible_tool():
    args = {
        "info_type": {"type": "string"},
        "job_id": {"type": "integer"},
        "file_name": {"type": "string"},
        "file_bytes_base64": {"type": "string"},
    }
    compatible = _StubTool("file_upload", "hidden", args)
    assert bb.admitted_collection_upload_tool([compatible]) is compatible
    assert bb.admitted_collection_upload_tool([compatible, compatible]) is None
    assert bb.admitted_collection_upload_tool([
        _StubTool("file_upload", "drifted", {"file_path": {"type": "string"}})
    ]) is None


def test_collection_ingestor_uploads_bytes_then_returns_raw_job_status():
    tool = _AsyncStubTool(
        "file_upload",
        "hidden",
        {},
        [json.dumps({"job_id": 17, "status": "upload_complete"}),
         {"job_id": 17, "status_message": "Ingesting"}],
    )
    result = asyncio.run(bb.make_collection_ingestor(tool)("forest.zip", b"zip-bytes"))
    assert result == {
        "upload": {"job_id": 17, "status": "upload_complete"},
        "job_status": {"job_id": 17, "status_message": "Ingesting"},
    }
    assert tool.calls[0]["info_type"] == "upload_bytes"
    assert tool.calls[0]["file_name"] == "forest.zip"
    assert base64.b64decode(tool.calls[0]["file_bytes_base64"]) == b"zip-bytes"
    assert tool.calls[1] == {"info_type": "status", "job_id": 17}


def test_collection_ingestor_preserves_upload_without_inventing_job_status():
    tool = _AsyncStubTool("file_upload", "hidden", {}, ["accepted-without-job-id"])
    result = asyncio.run(bb.make_collection_ingestor(tool)("forest.zip", b"zip"))
    assert result == {"upload": "accepted-without-job-id", "job_status": None}
    assert len(tool.calls) == 1


def test_load_is_graceful_noop_when_dir_missing(monkeypatch):
    # Point at a guaranteed-nonexistent dir -> _connections() returns None -> graceful empty load.
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", "/nonexistent/bloodhound/xyz")
    specs, registry = bb.load_bloodhound_mcp_tools()
    assert specs == [] and registry == {}


def test_explicit_env_var_builds_connection(monkeypatch):
    monkeypatch.setenv("SAGE_BLOODHOUND_MCP_DIR", "/override/dir")
    monkeypatch.setattr(bb.os.path, "isdir", lambda p: True)
    conns = bb._connections()
    assert conns[bb.BLOODHOUND_SERVER]["args"] == ["--directory", "/override/dir", "run", "main.py"]


def test_missing_env_fails_closed_without_checkout_guess(monkeypatch):
    monkeypatch.delenv("SAGE_BLOODHOUND_MCP_DIR", raising=False)
    monkeypatch.setattr(bb.os.path, "isdir", lambda p: True)
    assert bb._connections() is None


def test_dispatcher_falls_through_for_non_bloodhound_tool():
    # Returns None so a combined dispatcher can route Mythic/unknown tools elsewhere.
    assert bb.make_bloodhound_dispatcher({})({"tool": "issue_command", "args": {}}) is None
