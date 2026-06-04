import asyncio
import json
import sys
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage

from ai.langgraph.model import (  # noqa: E402
    _COMPACTION_PROTECTED_TOOLS,
    _ToolResultCompactionMiddleware,
    _compact_tool_result_str,
    _transform_content,
)


def _request(name: str):
    return ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": "call-1"},
        tool=None,
        state={},
        runtime=None,
    )


def _large_json(rows: int = 250) -> str:
    return json.dumps([
        {"name": f"principal-{i}", "sid": f"S-1-5-21-{i}"}
        for i in range(rows)
    ])


def test_small_str_under_trigger_returned_unchanged():
    s = "small result"

    result = _compact_tool_result_str(s, trigger=100)

    assert result is s
    assert result == "small result"


def test_uniform_json_list_flat_dicts_becomes_toon_with_all_values_findable():
    data = [
        {"hostname": "workstation-01", "username": "alice", "score": 10},
        {"hostname": "server-02", "username": "bob", "score": 20},
        {"hostname": "domain-controller-03", "username": "carol", "score": 30},
    ]

    result = _compact_tool_result_str(json.dumps(data), trigger=1)

    assert result.startswith("⟦TOON rows=3 keys=hostname\tusername\tscore⟧")
    for row in data:
        for value in row.values():
            assert str(value) in result


def test_tab_or_newline_cell_falls_back_without_corruption():
    data = [
        {"id": "row-1", "note": "alpha\tbeta"},
        {"id": "row-2", "note": "gamma"},
    ]

    result = _compact_tool_result_str(json.dumps(data), trigger=1)

    assert not result.startswith("⟦TOON ")
    assert "alpha" in result
    assert "beta" in result
    assert "row-2" in result


def test_nested_or_mixed_json_uses_compact_json_without_raising():
    data = {"rows": [{"id": 1, "nested": {"dn": "CN=Alice"}}], "ok": True}

    result = _compact_tool_result_str(json.dumps(data, indent=2), trigger=1)

    assert result == json.dumps(data, separators=(",", ":"), sort_keys=True)
    assert not result.startswith("⟦TOON ")


def test_malformed_json_over_trigger_uses_char_cap_without_raising():
    s = "{" + ("not-json" * 20)

    result = _compact_tool_result_str(s, trigger=1, ceiling=25)

    assert result.startswith(s[:25])
    assert "[truncated: 25 of" in result


def test_result_over_ceiling_after_densify_truncates_and_preserves_head():
    data = [{"id": f"row-{i}", "value": f"value-{i}"} for i in range(50)]

    result = _compact_tool_result_str(json.dumps(data), trigger=1, ceiling=90)

    assert result.startswith("⟦TOON rows=50 keys=id\tvalue⟧")
    assert "row-0" in result
    assert "[truncated: showing" in result
    assert "of 50 rows" in result


def test_compaction_is_deterministic_for_identical_large_fixture():
    data = [
        {"first": "alpha", "second": "bravo"},
        {"second": "charlie", "third": "delta"},
        {"first": "echo", "third": "foxtrot"},
    ] * 30
    s = json.dumps(data)

    results = [_compact_tool_result_str(s, trigger=1, ceiling=500) for _ in range(3)]

    assert results[0] == results[1] == results[2]


def test_tuple_content_blocks_densifies_text_and_preserves_artifact_and_non_text():
    big_json = _large_json()
    non_text_block = {"type": "image", "data": "untouched"}
    content = [{"type": "text", "text": big_json}, non_text_block]

    result = _transform_content(content)

    assert result[0]["text"].startswith("⟦TOON ")
    assert "principal-0" in result[0]["text"]
    assert result[1] is non_text_block


def test_error_status_small_dict_string_unchanged():
    s = json.dumps({"status": "error", "error": "callback not found"})

    result = _compact_tool_result_str(s)

    assert result is s
    assert result == '{"status": "error", "error": "callback not found"}'


def test_middleware_awrap_caps_oversized_json_string_content():
    name = "cypher_query"
    mw = _ToolResultCompactionMiddleware(model=object())
    original = ToolMessage(content=_large_json(), tool_call_id="call-1", name=name)

    async def handler(request):
        return original

    result = asyncio.run(mw.awrap_tool_call(_request(name), handler))

    assert result.content.startswith("⟦TOON ") or "[truncated" in result.content
    assert result.tool_call_id == "call-1"
    assert result.name == "cypher_query"


def test_middleware_caps_text_block_in_list_content_preserves_non_text_by_identity():
    name = "cypher_query"
    mw = _ToolResultCompactionMiddleware(model=object())
    image_block = {"type": "image", "data": "untouched"}
    content = [{"type": "text", "text": _large_json()}, image_block]
    original = ToolMessage(content=content, tool_call_id="call-1", name=name)
    object.__setattr__(original, "content", content)

    async def handler(request):
        return original

    result = asyncio.run(mw.awrap_tool_call(_request(name), handler))

    assert result.content[0]["text"].startswith("⟦TOON ") or "[truncated" in result.content[0]["text"]
    assert result.content[1] is image_block


def test_middleware_skips_protected_tool_returns_identity():
    name = "respond_to_user"
    assert name in _COMPACTION_PROTECTED_TOOLS
    mw = _ToolResultCompactionMiddleware(model=object())
    original = ToolMessage(content=_large_json(), tool_call_id="call-1", name=name)

    def handler(request):
        return original

    result = mw.wrap_tool_call(_request(name), handler)

    assert result is original


def test_middleware_small_content_under_trigger_returned_identity():
    name = "cypher_query"
    mw = _ToolResultCompactionMiddleware(model=object())
    original = ToolMessage(content="small", tool_call_id="call-1", name=name)

    async def handler(request):
        return original

    result = asyncio.run(mw.awrap_tool_call(_request(name), handler))

    assert result is original


def test_middleware_passes_non_toolmessage_through_unchanged():
    name = "transfer_to_MCP_Manager"
    mw = _ToolResultCompactionMiddleware(model=object())
    original = Command(goto="MCP_Manager")

    def handler(request):
        return original

    result = mw.wrap_tool_call(_request(name), handler)

    assert result is original


def test_middleware_does_not_swallow_handler_exception():
    mw = _ToolResultCompactionMiddleware(model=object())

    async def async_handler(request):
        raise RuntimeError("boom")

    def sync_handler(request):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(mw.awrap_tool_call(_request("cypher_query"), async_handler))
    with pytest.raises(RuntimeError, match="boom"):
        mw.wrap_tool_call(_request("cypher_query"), sync_handler)
