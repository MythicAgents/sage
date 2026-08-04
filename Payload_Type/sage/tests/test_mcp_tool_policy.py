"""MCP tool policy: deny-by-default for MCP server tools with user-editable overrides.

Tests the policy loader, classifier, and integration with tool_safety_of().
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.mcp_tool_policy import (
    classify_mcp_tool,
    effective_policy_summary,
    is_mcp_tool_guarded,
    load_policy,
    _loaded_policy,
)
from ai.langgraph.mythic_tools import (
    TOOL_SAFETY_GUARDED,
    TOOL_SAFETY_READ_ONLY,
    tool_safety_of,
)


@pytest.fixture(autouse=True)
def _reset_policy():
    """Reset the global policy state between tests."""
    import ai.langgraph.mcp_tool_policy as mod
    mod._loaded_policy = None
    mod._policy_path = ""
    yield
    mod._loaded_policy = None
    mod._policy_path = ""


def _load_inline(policy: dict) -> None:
    import ai.langgraph.mcp_tool_policy as mod
    mod._loaded_policy = policy
    mod._policy_path = "<inline>"


# ── Classifier ──────────────────────────────────────────────────────────────────

def test_unclassified_tool_defaults_to_guarded():
    _load_inline({})
    assert classify_mcp_tool("some-server", "unknown_tool") == TOOL_SAFETY_GUARDED


def test_global_default_applies():
    _load_inline({"default": "read_only"})
    assert classify_mcp_tool("any-server", "any-tool") == TOOL_SAFETY_READ_ONLY


def test_server_default_overrides_global():
    _load_inline({
        "default": "read_only",
        "servers": {"my-server": {"default": "guarded"}},
    })
    assert classify_mcp_tool("my-server", "some-tool") == TOOL_SAFETY_GUARDED
    assert classify_mcp_tool("other-server", "some-tool") == TOOL_SAFETY_READ_ONLY


def test_tool_override_wins_over_server_default():
    _load_inline({
        "servers": {
            "bh": {
                "default": "read_only",
                "tools": {"file_upload": "guarded"},
            },
        },
    })
    assert classify_mcp_tool("bh", "domain_info") == TOOL_SAFETY_READ_ONLY
    assert classify_mcp_tool("bh", "file_upload") == TOOL_SAFETY_GUARDED


def test_invalid_classification_falls_back_to_guarded():
    _load_inline({"default": "yolo"})
    assert classify_mcp_tool("s", "t") == TOOL_SAFETY_GUARDED


def test_is_mcp_tool_guarded_convenience():
    _load_inline({
        "servers": {"bh": {"default": "read_only", "tools": {"file_upload": "guarded"}}},
    })
    assert not is_mcp_tool_guarded("bh", "domain_info")
    assert is_mcp_tool_guarded("bh", "file_upload")
    assert is_mcp_tool_guarded("unknown-server", "any-tool")


# ── tool_safety_of integration ──────────────────────────────────────────────────

def test_tool_safety_of_with_mcp_server():
    _load_inline({
        "servers": {"bh": {"default": "read_only", "tools": {"file_upload": "guarded"}}},
    })
    assert tool_safety_of("domain_info", mcp_server="bh") == TOOL_SAFETY_READ_ONLY
    assert tool_safety_of("file_upload", mcp_server="bh") == TOOL_SAFETY_GUARDED
    assert tool_safety_of("unknown", mcp_server="unknown-server") == TOOL_SAFETY_GUARDED


# ── Loader ──────────────────────────────────────────────────────────────────────

def test_load_from_file(tmp_path):
    policy = {"default": "read_only", "servers": {"test": {"default": "guarded"}}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    result = load_policy(str(path))
    assert result == policy
    assert classify_mcp_tool("test", "any") == TOOL_SAFETY_GUARDED
    assert classify_mcp_tool("other", "any") == TOOL_SAFETY_READ_ONLY


def test_load_missing_file_defaults_to_guarded(tmp_path):
    result = load_policy(str(tmp_path / "nonexistent.json"))
    assert result == {}
    assert classify_mcp_tool("any", "any") == TOOL_SAFETY_GUARDED


def test_load_malformed_json_defaults_to_guarded(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json!!")
    result = load_policy(str(path))
    assert result == {}
    assert classify_mcp_tool("any", "any") == TOOL_SAFETY_GUARDED


def test_load_non_dict_defaults_to_guarded(tmp_path):
    path = tmp_path / "array.json"
    path.write_text("[]")
    result = load_policy(str(path))
    assert result == {}


# ── Policy summary ──────────────────────────────────────────────────────────────

def test_effective_policy_summary():
    _load_inline({
        "default": "guarded",
        "servers": {
            "bh": {"default": "read_only", "tools": {"file_upload": "guarded"}},
        },
    })
    summary = effective_policy_summary()
    assert len(summary) == 2
    assert summary[0]["scope"] == "global"
    assert summary[0]["default"] == "guarded"
    assert summary[1]["scope"] == "server:bh"
    assert summary[1]["default"] == "read_only"
    assert summary[1]["tools"]["file_upload"] == "guarded"


# ── Shipped BloodHound policy ───────────────────────────────────────────────────

def test_shipped_bloodhound_policy():
    """The default mcp_tool_policy.json correctly classifies BloodHound CE tools."""
    policy_path = Path(__file__).resolve().parents[1] / "mcp_tool_policy.json"
    if not policy_path.is_file():
        pytest.skip("mcp_tool_policy.json not found at expected location")
    load_policy(str(policy_path))
    bh_read_only = [
        "domain_info", "user_info", "group_info", "computer_info",
        "ou_info", "gpo_info", "graph_analysis", "adcs_info",
        "data_quality", "cypher_query",
    ]
    for tool in bh_read_only:
        assert classify_mcp_tool("bloodhound-ce", tool) == TOOL_SAFETY_READ_ONLY, (
            f"{tool} should be read_only in bloodhound-ce"
        )
    assert classify_mcp_tool("bloodhound-ce", "file_upload") == TOOL_SAFETY_GUARDED
    assert classify_mcp_tool("bloodhound-ce", "unknown_new_tool") == TOOL_SAFETY_GUARDED
