import sys
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage

from ai.langgraph.model import (  # noqa: E402
    _DigestToolUsesEdit,
    _digest_cleared_tool_content,
)


def _count_chars(messages):
    return sum(len(str(getattr(m, "content", ""))) for m in messages)


def _tool_pair(tid: str, name: str, content):
    return [
        AIMessage(content="", tool_calls=[{"id": tid, "name": name, "args": {}}]),
        ToolMessage(content=content, tool_call_id=tid, name=name),
    ]


def _messages(*pairs):
    messages = []
    for pair in pairs:
        messages.extend(pair)
    return messages


def test_forced_trigger_replaces_old_tool_results_with_bounded_digest_and_keeps_recent():
    old = "alpha preview text " + ("x" * 300)
    middle = "bravo preview text " + ("y" * 300)
    recent = "charlie kept result " + ("z" * 300)
    messages = _messages(
        _tool_pair("call-1", "alpha_tool", old),
        _tool_pair("call-2", "bravo_tool", middle),
        _tool_pair("call-3", "charlie_tool", recent),
    )
    edit = _DigestToolUsesEdit(trigger=1, keep=1, clear_tool_inputs=False, placeholder="TEST PLACEHOLDER")

    edit.apply(messages, count_tokens=_count_chars)

    first, second, kept = messages[1], messages[3], messages[5]
    assert "alpha_tool" in first.content
    assert "alpha preview text" in first.content
    assert first.content != edit.placeholder
    assert len(first.content) <= 180
    assert "bravo_tool" in second.content
    assert "bravo preview text" in second.content
    assert second.content != edit.placeholder
    assert len(second.content) <= 180
    assert kept.content == recent


def test_cleared_message_is_marked_in_response_metadata():
    messages = _messages(
        _tool_pair("call-1", "old_tool", "old result " * 50),
        _tool_pair("call-2", "kept_tool", "kept result " * 50),
    )
    edit = _DigestToolUsesEdit(trigger=1, keep=1, clear_tool_inputs=False, placeholder="TEST PLACEHOLDER")

    edit.apply(messages, count_tokens=_count_chars)

    metadata = messages[1].response_metadata["context_editing"]
    assert metadata["cleared"] is True
    assert metadata["strategy"] == "clear_tool_uses"
    assert messages[1].content != edit.placeholder


def test_below_trigger_leaves_all_message_content_unchanged():
    original_contents = ["", "old result " * 10, "", "new result " * 10]
    messages = _messages(
        _tool_pair("call-1", "old_tool", original_contents[1]),
        _tool_pair("call-2", "new_tool", original_contents[3]),
    )
    edit = _DigestToolUsesEdit(
        trigger=1_000_000,
        keep=1,
        clear_tool_inputs=False,
        placeholder="TEST PLACEHOLDER",
    )

    edit.apply(messages, count_tokens=_count_chars)

    assert [m.content for m in messages] == original_contents
    assert all(m.content != edit.placeholder for m in messages)


def test_list_of_text_blocks_digest_uses_text_block_preview_without_raising():
    content = [
        {"type": "text", "text": "first text block with preview"},
        {"type": "image", "data": "ignored"},
        {"type": "text", "text": "second text block"},
    ]
    messages = _messages(
        _tool_pair("call-1", "block_tool", content),
        _tool_pair("call-2", "kept_tool", "kept result " * 20),
    )
    edit = _DigestToolUsesEdit(trigger=1, keep=1, clear_tool_inputs=False, placeholder="TEST PLACEHOLDER")

    edit.apply(messages, count_tokens=_count_chars)

    assert "block_tool" in messages[1].content
    assert "first text block with preview second text block" in messages[1].content
    assert messages[1].content != edit.placeholder
    assert len(messages[1].content) <= 180


def test_reclear_leaves_already_digested_content_stable():
    messages = _messages(
        _tool_pair("call-1", "old_tool", "stable preview " + ("x" * 300)),
        _tool_pair("call-2", "kept_tool", "kept result " * 50),
    )
    edit = _DigestToolUsesEdit(trigger=1, keep=1, clear_tool_inputs=False, placeholder="TEST PLACEHOLDER")

    edit.apply(messages, count_tokens=_count_chars)
    once = messages[1].content
    edit.apply(messages, count_tokens=_count_chars)

    assert messages[1].content == once
    assert messages[1].content.count("[cleared old_tool result") == 1
    assert messages[1].content != edit.placeholder


def test_digest_helper_bounds_str_content_and_stringifies_weird_content():
    content = "  alpha\n beta\tgamma " + ("x" * 300)
    result = _digest_cleared_tool_content("sample_tool", content)
    weird = _digest_cleared_tool_content(None, 123)

    assert result.startswith(f"[cleared sample_tool result · {len(content)} chars] alpha beta gamma")
    assert len(result) <= 180
    assert result.endswith("…")
    assert weird == "[cleared tool result · 3 chars] 123…"
