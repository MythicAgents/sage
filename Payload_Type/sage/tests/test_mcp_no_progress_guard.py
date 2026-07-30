import json
import sys
from pathlib import Path

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage

from ai.langgraph.model import (  # noqa: E402
    _MCP_EMPTY_VARIANT_LIMIT,
    _MCP_NO_PROGRESS_LIMIT,
    _MCPManagerNoProgressStopMiddleware,
    _tool_messages_as_text,
)


class _FakeModel:
    def __init__(self):
        self.delegation_id = "mcp_manager:1"

    def current_delegation_id(self, agent_name: str):
        assert agent_name == "MCP_Manager"
        return self.delegation_id


def _request(call_id: str, *, query: str = "") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": "search-document-content",
            "args": {"search_query": query},
            "id": call_id,
        },
        tool=None,
        state={},
        runtime=None,
    )


def test_mcp_no_progress_guard_ends_after_repeated_identical_empty_results():
    mw = _MCPManagerNoProgressStopMiddleware(_FakeModel())
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return ToolMessage(
            content="[[], null]",
            name="search-document-content",
            tool_call_id=request.tool_call["id"],
        )

    result = None
    for idx in range(_MCP_NO_PROGRESS_LIMIT + 1):
        result = mw.wrap_tool_call(_request(f"call-{idx}", query="same-query"), handler)

    assert calls == _MCP_NO_PROGRESS_LIMIT + 1
    assert isinstance(result, ToolMessage)
    payload = json.loads(result.content)
    assert payload["capability"] == "mcp-no-progress-boundary"
    assert payload["next_action"] == "summarize_and_handback"
    assert mw.before_model({"messages": []}, runtime=None) == {"jump_to": "end"}


def test_mcp_no_progress_guard_allows_distinct_empty_variants_until_empty_budget():
    mw = _MCPManagerNoProgressStopMiddleware(_FakeModel(), limit=2, empty_limit=4)

    def handler(request):
        return ToolMessage(
            content="[[], null]",
            name="search-document-content",
            tool_call_id=request.tool_call["id"],
        )

    for idx in range(3):
        result = mw.wrap_tool_call(_request(f"call-{idx}", query=f"q-{idx}"), handler)
        assert result.content == "[[], null]"
        assert mw.before_model({"messages": []}, runtime=None) is None

    blocked = mw.wrap_tool_call(_request("call-4", query="q-4"), handler)
    payload = json.loads(blocked.content)
    assert payload["capability"] == "mcp-no-progress-boundary"
    assert "empty observations across query variants" in payload["reason"]
    assert mw.before_model({"messages": []}, runtime=None) == {"jump_to": "end"}


def test_mcp_no_progress_guard_resets_after_new_nonempty_result():
    mw = _MCPManagerNoProgressStopMiddleware(_FakeModel())

    def empty_handler(request):
        return ToolMessage(
            content="[]",
            name="search-document-content",
            tool_call_id=request.tool_call["id"],
        )

    def evidence_handler(request):
        return ToolMessage(
            content=json.dumps({"object_id": "artifact-1", "path": "SYSVOL/secret.ps1"}),
            name="get-file-details",
            tool_call_id=request.tool_call["id"],
        )

    mw.wrap_tool_call(_request("call-1", query="password"), empty_handler)
    mw.wrap_tool_call(_request("call-2", query="secret"), empty_handler)
    assert mw._no_progress_streak == 2

    evidence_request = ToolCallRequest(
        tool_call={"name": "get-file-details", "args": {"object_id": "artifact-1"}, "id": "call-3"},
        tool=None,
        state={},
        runtime=None,
    )
    mw.wrap_tool_call(evidence_request, evidence_handler)
    assert mw._no_progress_streak == 0
    assert mw.before_model({"messages": []}, runtime=None) is None


def test_mcp_no_progress_guard_treats_zero_count_results_as_empty_observations():
    mw = _MCPManagerNoProgressStopMiddleware(_FakeModel(), limit=2, empty_limit=2)

    def handler(request):
        return ToolMessage(
            content=json.dumps({"file_count": 0}),
            name="count-files",
            tool_call_id=request.tool_call["id"],
        )

    first = mw.wrap_tool_call(_request("call-1", query="one"), handler)
    second = mw.wrap_tool_call(_request("call-2", query="two"), handler)

    assert json.loads(first.content)["file_count"] == 0
    assert json.loads(second.content)["capability"] == "mcp-no-progress-boundary"


def test_mcp_no_progress_guard_trips_on_repeated_nonempty_result():
    mw = _MCPManagerNoProgressStopMiddleware(_FakeModel(), limit=2)

    def handler(request):
        return ToolMessage(
            content=json.dumps({"object_id": "artifact-1", "path": "SYSVOL/secret.ps1"}),
            name="search-files",
            tool_call_id=request.tool_call["id"],
        )

    first = mw.wrap_tool_call(_request("call-1", query="secret"), handler)
    second = mw.wrap_tool_call(_request("call-2", query="secret"), handler)
    third = mw.wrap_tool_call(_request("call-3", query="secret"), handler)

    assert json.loads(first.content)["object_id"] == "artifact-1"
    assert json.loads(second.content)["object_id"] == "artifact-1"
    payload = json.loads(third.content)
    assert payload["capability"] == "mcp-no-progress-boundary"
    assert mw.before_model({"messages": []}, runtime=None) == {"jump_to": "end"}


def test_mcp_no_progress_guard_resets_on_new_delegation():
    model = _FakeModel()
    mw = _MCPManagerNoProgressStopMiddleware(model, limit=2)

    def handler(request):
        return ToolMessage(
            content="[]",
            name="search-document-content",
            tool_call_id=request.tool_call["id"],
        )

    mw.wrap_tool_call(_request("call-1", query="secret"), handler)
    mw.wrap_tool_call(_request("call-2", query="secret"), handler)
    mw.wrap_tool_call(_request("call-3", query="secret"), handler)
    assert mw.before_model({"messages": []}, runtime=None) == {"jump_to": "end"}

    model.delegation_id = "mcp_manager:2"
    assert mw.before_model({"messages": []}, runtime=None) is None
    assert mw._no_progress_streak == 0


def test_mcp_no_progress_guard_is_opt_in_for_mcp_manager_only():
    from ai.langgraph.model import Model

    model = Model.__new__(Model)
    model.mode = "auto"
    model._get_base_chat_model = lambda: None

    default_names = [type(item).__name__ for item in model._context_middleware()]
    mcp_names = [
        type(item).__name__
        for item in model._context_middleware(mcp_no_progress_stop=True)
    ]

    assert "_MCPManagerNoProgressStopMiddleware" not in default_names
    assert "_MCPManagerNoProgressStopMiddleware" in mcp_names


def test_mcp_no_progress_guard_empty_variant_budget_exceeds_duplicate_budget():
    assert _MCP_EMPTY_VARIANT_LIMIT > _MCP_NO_PROGRESS_LIMIT


def test_tool_messages_as_text_preserves_structured_mcp_results_and_boundary_payload():
    structured = ToolMessage(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "object_id": "artifact-1",
                        "path": "SYSVOL/north.sevenkingdoms.local/scripts/secret.ps1",
                    }
                ),
            }
        ],
        name="search-enrichments-by-module",
        tool_call_id="call-1",
    )
    blocked = ToolMessage(
        content=json.dumps(
            {
                "capability": "mcp-no-progress-boundary",
                "next_action": "summarize_and_handback",
            }
        ),
        name="search-files",
        tool_call_id="call-2",
    )

    text = _tool_messages_as_text([structured, blocked])

    assert "artifact-1" in text
    assert "secret.ps1" in text
    assert "mcp-no-progress-boundary" in text
