"""Tests for Phase-2 HITL approve/deny gating.

Uses Model.__new__ to exercise middleware/default-deny units without the heavy __init__
(sqlite + chat model).
Run: cd Payload_Type/sage && python3 -m pytest tests/test_hitl.py -q
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph.model import Model, _hitl_is_approved  # noqa: E402
from ai.langgraph.mythic_tools import GUARDED_TOOLS  # noqa: E402
from langchain.agents.middleware import HumanInTheLoopMiddleware  # noqa: E402


def _bare_model(mode: str) -> Model:
    m = Model.__new__(Model)
    m.mode = mode
    m.llm = None
    m._get_base_chat_model = lambda: None
    return m


def test_guarded_tools_are_state_changing_only():
    expected = {
        "issue_task_and_waitfor_task_output",
        "upload_file_by_file_uuid",
        "create_payload",
        "download_tool",
        "stage_file_to_disk",
        "sandbox_exec",
        "file_upload",
    }
    assert expected.issubset(GUARDED_TOOLS)
    assert not any(t.startswith("get_") for t in GUARDED_TOOLS)
    assert "get_all_task_output_by_task_id" not in GUARDED_TOOLS
    assert "download_file" not in GUARDED_TOOLS
    assert "ensure_tool_uploaded" not in GUARDED_TOOLS


def test_auto_mode_adds_no_hitl_middleware():
    mw = _bare_model("auto")._context_middleware()
    hitl = [m for m in mw if isinstance(m, HumanInTheLoopMiddleware)]
    assert hitl == []


def test_supervised_mode_adds_exactly_one_hitl_middleware():
    mw = _bare_model("supervised")._context_middleware()
    hitl = [m for m in mw if isinstance(m, HumanInTheLoopMiddleware)]
    assert len(hitl) == 1


def test_hitl_operator_text_default_deny():
    for text in ("approve", "yes", "y", "ok", "go", "proceed", "APPROVE "):
        assert _hitl_is_approved(text) is True

    for text in ("", "no", "deny", "stop", "later", "nah don't", "maybe", "garbage"):
        assert _hitl_is_approved(text) is False


class _FakeInterrupt:
    def __init__(self, value):
        self.value = value


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_surface_hitl_interrupt_detects_and_prompts():
    """An __interrupt__ event must produce a user-facing approve/deny prompt naming the tool+args
    (the task-595 fix: previously the run halted on the raw tool-call with no prompt)."""
    m = _bare_model("supervised")
    sent = []
    async def _stub(msg):
        sent.append(msg)
    m._stream_message_to_mythic = _stub
    val = {"action_requests": [{"name": "issue_task_and_waitfor_task_output",
                                "args": {"command": "inline_assembly", "callback_display_id": 16,
                                         "parameters": {"assembly_name": "Rubeus.exe"}}}]}
    event = {"__interrupt__": (_FakeInterrupt(val),)}
    assert _run(m._surface_hitl_interrupt(event)) is True
    assert len(sent) == 1
    assert "Approval required" in sent[0]
    assert "issue_task_and_waitfor_task_output" in sent[0]
    assert "Rubeus.exe" in sent[0]


def test_surface_hitl_interrupt_ignores_normal_event():
    m = _bare_model("supervised")
    sent = []
    async def _stub(msg):
        sent.append(msg)
    m._stream_message_to_mythic = _stub
    assert _run(m._surface_hitl_interrupt({"Supervisor": {"messages": []}})) is False
    assert sent == []


class _FakeTask:
    def __init__(self, interrupts):
        self.interrupts = interrupts


class _FakeSnapshot:
    def __init__(self, interrupts=(), tasks=()):
        self.interrupts = interrupts
        self.tasks = tasks


def _itr(value, iid):
    o = _FakeInterrupt(value)
    o.id = iid
    return o


def test_collect_action_requests_no_double_count():
    """task-598 regression: the SAME interrupt in both snapshot.interrupts AND a task's interrupts
    must be counted ONCE — else 2 decisions for 1 hanging tool call -> middleware ValueError."""
    from ai.langgraph.model import _collect_hitl_action_requests
    itr = _itr({"action_requests": [{"name": "issue_task_and_waitfor_task_output", "args": {}}]}, "int-1")
    snap = _FakeSnapshot(interrupts=(itr,), tasks=(_FakeTask((itr,)),))
    assert len(_collect_hitl_action_requests(snap)) == 1


def test_collect_action_requests_fallback_to_tasks():
    from ai.langgraph.model import _collect_hitl_action_requests
    itr = _itr({"action_requests": [{"name": "sandbox_exec", "args": {}}]}, "int-2")
    snap = _FakeSnapshot(interrupts=(), tasks=(_FakeTask((itr,)),))
    assert len(_collect_hitl_action_requests(snap)) == 1


def test_collect_action_requests_two_distinct_calls_kept():
    """A single interrupt that genuinely reviews two tool calls must yield two decisions."""
    from ai.langgraph.model import _collect_hitl_action_requests
    itr = _itr({"action_requests": [{"name": "a", "args": {}}, {"name": "b", "args": {}}]}, "int-3")
    snap = _FakeSnapshot(interrupts=(itr,), tasks=(_FakeTask((itr,)),))
    assert len(_collect_hitl_action_requests(snap)) == 2
