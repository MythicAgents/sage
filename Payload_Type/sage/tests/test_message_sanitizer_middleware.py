"""Regression: provider-agnostic empty/blank-block guard on create_agent's internal react loop (2026-07-10).

Extends the bug-2 fix. The reported `ValidationException: system: text content blocks must be non-empty` fired
on a native `ChatBedrock` (InvokeModel) call INSIDE create_agent's react loop (Phoenix trace: Sage → Supervisor
→ model → ChatBedrock). Neither prior defense reached that path:
  - `_sanitize_messages` only cleans the channel lists the graph passes in, not create_agent's internal messages;
  - the langchain_openai `_convert_message_to_dict` monkeypatch only affects the ChatOpenAI/proxy provider — it
    is a no-op for langchain-aws AND every other native provider (ollama, anthropic, google_genai).

`_MessageSanitizerMiddleware` fires at `wrap_model_call`, which wraps the model invocation for ALL providers, and
normalizes the outgoing request. This test drives it with a fake ModelRequest/handler (no live provider needed),
which is exactly why it is provider-agnostic: the middleware never inspects the provider/model class.

Run: cd Payload_Type/sage && ../../.venv/bin/python -m pytest tests/test_message_sanitizer_middleware.py -q
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph.model import (  # noqa: E402
    Model, _MessageSanitizerMiddleware, _sanitize_model_messages, _DEFAULT_SYSTEM_PROMPT,
)
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage  # noqa: E402


class _FakeReq:
    """Stand-in for langchain's create_agent ModelRequest: exposes system_prompt/messages + override()."""
    def __init__(self, system_prompt=None, messages=None):
        self.system_prompt = system_prompt
        self.messages = list(messages or [])

    def override(self, **kw):
        new = _FakeReq(self.system_prompt, list(self.messages))
        for k, v in kw.items():
            setattr(new, k, v)
        return new


def _mw() -> _MessageSanitizerMiddleware:
    return _MessageSanitizerMiddleware(object.__new__(Model))


def _drive(req):
    captured = {}

    def handler(r):
        captured["req"] = r
        return "OK"

    result = _mw().wrap_model_call(req, handler)
    return captured["req"], result


# ---- system_prompt guard (the reported crash class) --------------------------------------------
def test_blank_system_prompt_is_replaced_on_every_call():
    got, _ = _drive(_FakeReq(system_prompt="   ", messages=[HumanMessage(content="hi")]))
    assert got.system_prompt == _DEFAULT_SYSTEM_PROMPT


def test_empty_string_system_prompt_is_replaced():
    got, _ = _drive(_FakeReq(system_prompt="", messages=[HumanMessage(content="hi")]))
    assert got.system_prompt == _DEFAULT_SYSTEM_PROMPT


def test_real_system_prompt_is_left_alone_and_request_passes_through_unchanged():
    req = _FakeReq(system_prompt="You are Sage.", messages=[HumanMessage(content="hi")])
    got, result = _drive(req)
    assert got is req  # no override when nothing needs sanitizing
    assert result == "OK"


# ---- messages: empty system / empty assistant inside the loop ----------------------------------
def test_empty_system_message_in_loop_is_dropped():
    req = _FakeReq(system_prompt="ok", messages=[SystemMessage(content=""), HumanMessage(content="hi")])
    got, _ = _drive(req)
    assert [m for m in got.messages if isinstance(m, SystemMessage)] == []


def test_empty_assistant_message_is_backfilled():
    req = _FakeReq(system_prompt="ok", messages=[HumanMessage(content="hi"), AIMessage(content="")])
    got, _ = _drive(req)
    ai = [m for m in got.messages if isinstance(m, AIMessage)][0]
    assert ai.content == "."


def test_assistant_with_tool_use_block_is_preserved():
    """A tool_use block with an immediate result IS content and must not be clobbered."""
    tool_block = [{"type": "tool_use", "id": "t1", "name": "run", "input": {}}]
    req = _FakeReq(
        system_prompt="ok",
        messages=[AIMessage(content=tool_block), ToolMessage(content="ok", tool_call_id="t1", name="run")],
    )
    got, _ = _drive(req)
    ai = [m for m in got.messages if isinstance(m, AIMessage)][0]
    assert ai.content == tool_block


def test_blank_text_block_stripped_but_tool_use_kept():
    content = [{"type": "text", "text": ""}, {"type": "tool_use", "id": "t1", "name": "run", "input": {}}]
    req = _FakeReq(
        system_prompt="ok",
        messages=[AIMessage(content=content), ToolMessage(content="ok", tool_call_id="t1", name="run")],
    )
    got, _ = _drive(req)
    ai = [m for m in got.messages if isinstance(m, AIMessage)][0]
    assert ai.content == [{"type": "tool_use", "id": "t1", "name": "run", "input": {}}]


def test_dangling_tool_call_is_stripped_before_model_invocation():
    req = _FakeReq(
        system_prompt="ok",
        messages=[
            HumanMessage(content="hi"),
            AIMessage(
                content="I should call a tool.",
                tool_calls=[{"id": "t1", "name": "run", "args": {}, "type": "tool_call"}],
            ),
            HumanMessage(content="intervening user message"),
        ],
    )
    got, _ = _drive(req)
    ai = [m for m in got.messages if isinstance(m, AIMessage)][0]
    assert ai.content == "I should call a tool."
    assert ai.tool_calls == []


def test_valid_parallel_tool_results_are_preserved():
    ai = AIMessage(
        content="",
        tool_calls=[
            {"id": "t1", "name": "run", "args": {}, "type": "tool_call"},
            {"id": "t2", "name": "run", "args": {}, "type": "tool_call"},
        ],
    )
    req = _FakeReq(
        system_prompt="ok",
        messages=[
            HumanMessage(content="hi"),
            ai,
            ToolMessage(content="one", tool_call_id="t1", name="run"),
            ToolMessage(content="two", tool_call_id="t2", name="run"),
            HumanMessage(content="continue"),
        ],
    )
    got, _ = _drive(req)
    preserved_ai = [m for m in got.messages if isinstance(m, AIMessage)][0]
    preserved_tools = [m for m in got.messages if isinstance(m, ToolMessage)]
    assert preserved_ai.tool_calls == ai.tool_calls
    assert [m.tool_call_id for m in preserved_tools] == ["t1", "t2"]


def test_orphan_tool_result_is_dropped_after_dangling_tool_call_repair():
    req = _FakeReq(
        system_prompt="ok",
        messages=[
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                tool_calls=[{"id": "t1", "name": "run", "args": {}, "type": "tool_call"}],
            ),
            HumanMessage(content="intervening user message"),
            ToolMessage(content="late", tool_call_id="t1", name="run"),
        ],
    )
    got, _ = _drive(req)
    assert [m for m in got.messages if isinstance(m, AIMessage)] == []
    assert [m for m in got.messages if isinstance(m, ToolMessage)] == []


# ---- fail-open + async ---------------------------------------------------------------------------
def test_fail_open_when_override_raises():
    class _BadReq(_FakeReq):
        def override(self, **kw):
            raise RuntimeError("boom")
    bad = _BadReq(system_prompt="", messages=[HumanMessage(content="hi")])
    got, result = _drive(bad)
    assert got is bad  # sanitizer swallowed the error and passed the original request through
    assert result == "OK"


def test_async_wrap_model_call_sanitizes():
    req = _FakeReq(system_prompt="", messages=[SystemMessage(content=""), HumanMessage(content="hi")])
    captured = {}

    async def handler(r):
        captured["req"] = r
        return "OK"

    asyncio.run(_mw().awrap_model_call(req, handler))
    got = captured["req"]
    assert got.system_prompt == _DEFAULT_SYSTEM_PROMPT
    assert [m for m in got.messages if isinstance(m, SystemMessage)] == []


# ---- shared helper units ------------------------------------------------------------------------
def test_sanitize_model_messages_helper():
    msgs = [SystemMessage(content=""), HumanMessage(content="hi"), AIMessage(content="  ")]
    out, changed = _sanitize_model_messages(msgs)
    assert changed is True
    assert [m for m in out if isinstance(m, SystemMessage)] == []
    assert [m for m in out if isinstance(m, AIMessage)][0].content == "."
    # clean input -> unchanged
    clean = [SystemMessage(content="s"), HumanMessage(content="hi")]
    out2, changed2 = _sanitize_model_messages(clean)
    assert changed2 is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all message-sanitizer-middleware tests passed")
