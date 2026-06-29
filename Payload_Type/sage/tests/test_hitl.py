"""Tests for Phase-2 HITL approve/deny gating.

Uses Model.__new__ to exercise middleware/default-deny units without the heavy __init__
(sqlite + chat model).
Run: cd Payload_Type/sage && python3 -m pytest tests/test_hitl.py -q
"""
import sys
import asyncio
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph.model import (  # noqa: E402
    Model,
    _ControllerCollectionRequest,
    _ControllerHitlPause,
    _hitl_is_approved,
)
from ai.langgraph.mythic_tools import GUARDED_TOOLS  # noqa: E402
from langchain.agents.middleware import HumanInTheLoopMiddleware  # noqa: E402


def _bare_model(mode: str) -> Model:
    m = Model.__new__(Model)
    m.mode = mode
    m.command_name = "chat"
    m._autonomous_solve = False
    m.verbose = False
    m._controller_hitl_pending = None
    m._controller_hitl_approved_key = ""
    m._controller_hitl_objective = ""
    m.llm = None
    m._get_base_chat_model = lambda: None
    return m


def test_guarded_tools_are_state_changing_only():
    expected = {
        "issue_task_and_waitfor_task_output",
        "upload_file_by_file_uuid",
        "materialize_capability_inputs",
        "execute_capability",
        "create_payload",
        "download_tool",
        "ingest_collection",
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


def _controller_hitl_model():
    m = _bare_model("supervised")
    m._autonomous_solve = True
    m.command_name = "chat"
    m._controller_verbose_stream_tail = None
    return m


def _capability_pending(m, *, name="gpo-controlled-system-exec", target="lab.local", callback_id="3"):
    return m._controller_hitl_capability_request(
        {
            "name": name,
            "target": target,
            "preconditions": ["graph-built:lab.local"],
            "effects": ["da:lab.local"],
        },
        {"callback_id": callback_id},
        "obtain administrative control of lab.local",
    )


def test_controller_hitl_capability_pauses_before_execution_and_surfaces_context():
    m = _controller_hitl_model()
    sent = []

    async def _stream(msg):
        sent.append(msg)
        return True

    m._stream_message_to_mythic = _stream
    pending = _capability_pending(m)

    with pytest.raises(_ControllerHitlPause):
        _run(m._require_controller_hitl_approval(pending))

    assert m._controller_hitl_pending["key"] == pending["key"]
    assert len(sent) == 1
    assert "Approval required" in sent[0]
    assert "gpo-controlled-system-exec" in sent[0]
    assert "lab.local" in sent[0]
    assert "graph-built:lab.local" in sent[0]
    assert "da:lab.local" in sent[0]
    assert "callback: `3`" in sent[0]


def test_controller_hitl_collection_pauses_with_scope_and_reason():
    m = _controller_hitl_model()
    sent = []

    async def _stream(msg):
        sent.append(msg)
        return True

    m._stream_message_to_mythic = _stream
    request = _ControllerCollectionRequest(
        foothold=SimpleNamespace(callback_id="7", host="workstation01", agent="merlin"),
        scope_domain="child.lab.local",
        reason="objective-scope-expansion",
        collection_key="collection:7:child.lab.local",
        support="objective domain child.lab.local is trusted and uncollected",
    )
    pending = m._controller_hitl_collection_request(request, "obtain administrative control of child.lab.local")

    with pytest.raises(_ControllerHitlPause):
        _run(m._require_controller_hitl_approval(pending))

    assert m._controller_hitl_pending["key"] == pending["key"]
    assert "collect_graph" in sent[0]
    assert "child.lab.local" in sent[0]
    assert "objective-scope-expansion" in sent[0]
    assert "workstation01" in sent[0]
    assert "merlin" in sent[0]


def test_controller_hitl_approve_resumes_exact_pending_move():
    m = _controller_hitl_model()
    pending = _capability_pending(m)
    m._controller_hitl_pending = pending
    audit = []
    seen = {}

    m._write_hitl_audit = lambda tool, args, decision: audit.append((tool, args, decision))
    m._seed_autonomous_objective = lambda objective: seen.setdefault("seeded", objective)

    async def _run_controller(objective):
        seen["objective"] = objective
        seen["approved_key"] = m._controller_hitl_approved_key
        return "resumed"

    m._run_autonomous_controller = _run_controller

    assert _run(m.handle_controller_hitl_resume("approve")) == "resumed"
    assert audit == [("execute_capability", pending["args"], "approve")]
    assert seen["seeded"] == "obtain administrative control of lab.local"
    assert seen["objective"] == "obtain administrative control of lab.local"
    assert seen["approved_key"] == pending["key"]
    assert m._controller_hitl_pending is None


def test_invoke_routes_controller_approval_before_seeding_new_objective():
    m = _controller_hitl_model()
    m.graph = object()
    m.agent_task_id = "agent-task"
    m.task_id = 42
    m._controller_hitl_pending = _capability_pending(m)
    seen = {}

    async def _resume(response):
        seen["response"] = response
        return "resumed"

    m.handle_controller_hitl_resume = _resume
    m._seed_autonomous_objective = lambda objective: seen.setdefault("seeded", objective)

    assert _run(m.invoke("approve", is_interactive=True)) == "resumed"
    assert seen == {"response": "approve"}


def test_controller_hitl_default_deny_executes_nothing_and_halts():
    m = _controller_hitl_model()
    pending = _capability_pending(m)
    m._controller_hitl_pending = pending
    audit = []
    sent = []
    ran = {"controller": False}

    m._write_hitl_audit = lambda tool, args, decision: audit.append((tool, args, decision))

    async def _stream(msg):
        sent.append(msg)
        return True

    async def _run_controller(_objective):
        ran["controller"] = True
        return "should-not-run"

    m._stream_message_to_mythic = _stream
    m._run_autonomous_controller = _run_controller

    assert _run(m.handle_controller_hitl_resume("maybe")) == ""
    assert audit == [("execute_capability", pending["args"], "deny")]
    assert ran["controller"] is False
    assert m._controller_hitl_pending is None
    assert m._controller_hitl_approved_key == ""
    assert "Operator denied `execute_capability`" in sent[0]


def test_controller_hitl_stale_approval_never_authorizes_different_action():
    m = _controller_hitl_model()
    first = _capability_pending(m, name="collect-graph", target="lab.local", callback_id="3")
    second = _capability_pending(m, name="dcsync-krbtgt", target="lab.local", callback_id="3")
    m._controller_hitl_approved_key = first["key"]
    m._stream_message_to_mythic = lambda _msg: asyncio.sleep(0)

    with pytest.raises(_ControllerHitlPause):
        _run(m._require_controller_hitl_approval(second))

    assert m._controller_hitl_pending["key"] == second["key"]
    assert m._controller_hitl_approved_key == ""
