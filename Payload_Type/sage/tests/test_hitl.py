"""Tests for Phase-2 HITL approve/deny gating.

Uses Model.__new__ to exercise middleware/default-deny units without the heavy __init__
(sqlite + chat model).
Run: cd Payload_Type/sage && python3 -m pytest tests/test_hitl.py -q
"""
import sys
import asyncio
import copy
from types import SimpleNamespace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph.model import (  # noqa: E402
    Model,
    _ControllerCollectionRequest,
    _ControllerHitlPause,
    _hitl_is_approved,
)
from ai.langgraph.mythic_tools import GUARDED_TOOLS  # noqa: E402
from langchain.agents.middleware import HumanInTheLoopMiddleware  # noqa: E402
from sage_chat.hitl import approval_action_digest, approval_action_fingerprint, build_approval_request  # noqa: E402


def _bare_model(mode: str) -> Model:
    m = Model.__new__(Model)
    m.mode = mode
    m.command_name = "chat"
    m._autonomous_solve = False
    m.verbose = False
    m._controller_hitl_pending = None
    m._controller_hitl_approved_key = ""
    m._controller_hitl_approved_pending = None
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
        "ensure_tool_uploaded",
        "ingest_collection",
        "sandbox_exec",
        "file_upload",
    }
    assert expected.issubset(GUARDED_TOOLS)
    assert not any(t.startswith("get_") for t in GUARDED_TOOLS)
    assert "get_all_task_output_by_task_id" not in GUARDED_TOOLS
    assert "download_file" not in GUARDED_TOOLS
    assert "ensure_tool_uploaded" in GUARDED_TOOLS


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


@pytest.mark.parametrize(
    "action_requests",
    (
        ["not-an-action"],
        [{}],
        [{"name": "drop_database", "args": {}}],
        [{"name": "execute_capability", "args": "{}"}],
        [{"name": 7, "args": {}}],
        [{"name": " execute_capability ", "args": {}}],
        [{"name": "execute_capability", "args": {"targets": ("a", "b")}}],
        [{"name": "execute_capability", "args": {1: "target"}}],
    ),
)
def test_approval_cards_reject_malformed_unknown_or_nonexact_actions(action_requests):
    with pytest.raises(ValueError):
        build_approval_request(action_requests)


def test_action_fingerprint_is_exact_over_tool_and_json_native_args_only():
    base = {"name": "execute_capability", "args": {"b": [1, True, None], "a": {"x": "y"}}}
    assert approval_action_fingerprint(base) == approval_action_fingerprint({
        **base,
        "id": "fresh-call",
        "display_name": "different label",
        "args": {"a": {"x": "y"}, "b": [1, True, None]},
    })
    assert approval_action_fingerprint(base) == approval_action_fingerprint({
        "name": "execute_capability",
        "args": {"b": [1.0, True, None], "a": {"x": "y"}},
    })
    for other in (
        {"name": "execute_capability", "args": {"b": [True, 1, None], "a": {"x": "y"}}},
        {"name": "execute_capability", "args": {"b": [1, True, "null"], "a": {"x": "y"}}},
        {"name": "execute_capability", "args": {"b": [1, False, None], "a": {"x": "y"}}},
        {"name": "execute_capability", "args": {"b": [1, True, None], "a": {"x": "z"}}},
    ):
        assert approval_action_fingerprint(base) != approval_action_fingerprint(other)
    assert approval_action_fingerprint({
        "name": "execute_capability",
        "args": {"outer": {"b": 2, "a": {"y": 1, "x": [3, 4]}}},
    }) == approval_action_fingerprint({
        "name": "execute_capability",
        "args": {"outer": {"a": {"x": [3, 4], "y": 1}, "b": 2}},
    })
    for malformed in (
        {"name": " execute_capability ", "args": {}},
        {"name": "Execute_Capability", "args": {}},
        {"name": "execute_capability", "args": {"x": float("inf")}},
        {"name": "execute_capability", "args": {"x": ("a", "b")}},
        {"name": "execute_capability", "args": {1: "x"}},
    ):
        with pytest.raises(ValueError):
            approval_action_fingerprint(malformed)


def test_approval_identity_and_card_bytes_normalize_nested_integral_floats():
    float_actions = [{
        "name": "execute_capability",
        "args": {
            "callback_id": "1",
            "policy_decision": {
                "model_branch_coverage": 1.0,
                "nested": [-0.0, {"count": 3.0}],
            },
        },
    }]
    integer_actions = [{
        "name": "execute_capability",
        "args": {
            "callback_id": "1",
            "policy_decision": {
                "model_branch_coverage": 1,
                "nested": [0, {"count": 3}],
            },
        },
    }]

    assert approval_action_digest(float_actions) == approval_action_digest(integer_actions)
    float_card = build_approval_request(float_actions)
    integer_card = build_approval_request(integer_actions)
    assert float_card == integer_card
    assert float_card["data"]["arguments"]["policy_decision"] == {
        "model_branch_coverage": 1,
        "nested": [0, {"count": 3}],
    }


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


def test_continuation_loop_surfaces_hitl_interrupt_and_stops_consuming_events():
    m = _bare_model("supervised")
    m.state = {
        "messages": [],
        "supervisor_messages": [],
        "recursion_summary_requested": True,
        "recursion_handback": True,
        "_message_seq": 1,
    }
    m._message_seq = 1
    m._thread_id_override = "chat:generation:continuation-hitl"
    m._stop_requested = False
    m._hitl_card_pending = False
    m._graph_run_config = lambda thread_id: {"configurable": {"thread_id": thread_id}}
    m._format_message_for_streaming = lambda _message, agent_name=None: ""
    processed = []
    surfaced = []

    async def _classify(_response):
        return "CONTINUE"

    async def _surface(event):
        surfaced.append(event)
        m._hitl_card_pending = True
        return True

    async def _process(event):
        processed.append(event)

    class _Events:
        def __init__(self):
            self._events = iter((
                {"__interrupt__": (_FakeInterrupt({"action_requests": []}),)},
                {"Supervisor": {"messages": [AIMessage(content="must not run")] }},
            ))

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration:
                raise StopAsyncIteration

    class _Graph:
        def astream(self, _state, _config):
            return _Events()

    m._classify_continuation_intent = _classify
    m._surface_hitl_interrupt = _surface
    m._process_stream_event = _process
    m.graph = _Graph()

    assert _run(m.handle_continuation_response("continue")) == ""
    assert len(surfaced) == 1
    assert processed == []
    assert m._hitl_card_pending is True


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


def test_hitl_resume_rejects_malformed_checkpoint_before_command_resume():
    class _Graph:
        resumed = False

        async def aget_state(self, _config):
            itr = _itr({"action_requests": [{"name": "drop_database", "args": {}}]}, "bad")
            return _FakeSnapshot(interrupts=(itr,))

        async def astream(self, *_args, **_kwargs):
            self.resumed = True
            if False:
                yield None

    model = _bare_model("supervised")
    model.graph = _Graph()

    with pytest.raises(RuntimeError, match="exact guarded request is unavailable and the session must be replaced"):
        _run(model.handle_hitl_resume("approve", "thread-1", expected_action_digest="attacker"))
    assert model.graph.resumed is False


@pytest.mark.parametrize(("response", "operator_message"), (("deny", ""), ("deny", "use a different read")))
def test_hitl_resume_missing_checkpoint_fails_closed_before_graph_resume(response, operator_message):
    class _Graph:
        resumed = False

        async def aget_state(self, _config):
            return _FakeSnapshot(interrupts=())

        async def astream(self, *_args, **_kwargs):
            self.resumed = True
            if False:
                yield None

    model = _bare_model("supervised")
    model.graph = _Graph()
    with pytest.raises(RuntimeError, match="exact guarded request is unavailable and the session must be replaced"):
        _run(model.handle_hitl_resume(response, "thread-1", operator_message=operator_message))
    assert model.graph.resumed is False


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


def test_controller_hitl_collection_policy_decision_changes_exact_approval_key():
    m = _controller_hitl_model()
    request = _ControllerCollectionRequest(
        foothold=SimpleNamespace(callback_id="7", host="workstation01", agent="merlin"),
        scope_domain="child.lab.local",
        reason="objective-scope-expansion",
        collection_key="collection:7:child.lab.local",
        support="objective domain child.lab.local is trusted and uncollected",
    )
    decision = {
        "decision_id": "decision-original",
        "policy_mode": "llm",
        "effective_backend": "runtime-provider:runtime-model",
    }

    without_decision = m._controller_hitl_collection_request(
        request,
        "obtain administrative control of child.lab.local",
    )
    with_decision = m._controller_hitl_collection_request(
        request,
        "obtain administrative control of child.lab.local",
        decision,
    )

    assert with_decision["key"] != without_decision["key"]
    assert with_decision["args"]["policy_decision"] == decision
    assert m._controller_pending_policy_decision(with_decision) == decision


def test_controller_hitl_uses_native_input_card_when_chat_emitter_is_bound():
    m = _controller_hitl_model()
    emitted = []
    streamed = []

    async def _emit(action_requests):
        emitted.append(action_requests)

    async def _stream(msg):
        streamed.append(msg)
        return True

    m._hitl_card_emitter = _emit
    m._hitl_card_pending = False
    m._stream_message_to_mythic = _stream
    pending = _capability_pending(m)

    with pytest.raises(_ControllerHitlPause):
        _run(m._require_controller_hitl_approval(pending))

    assert emitted == [[{
        "name": "execute_capability",
        "display_name": "gpo-controlled-system-exec",
        "args": pending["args"],
    }]]
    assert m._hitl_card_pending is True
    assert streamed == []


def test_controller_hitl_approve_resumes_exact_pending_move():
    m = _controller_hitl_model()
    pending = _capability_pending(m)
    m._controller_hitl_pending = pending
    seen = {}
    m._seed_autonomous_objective = lambda objective: seen.setdefault("seeded", objective)

    async def _run_controller(objective):
        seen["objective"] = objective
        seen["approved_key"] = m._controller_hitl_approved_key
        return "resumed"

    m._run_autonomous_controller = _run_controller

    assert _run(m.handle_controller_hitl_resume("approve")) == "resumed"
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
    sent = []
    ran = {"controller": False}

    async def _stream(msg):
        sent.append(msg)
        return True

    async def _run_controller(_objective):
        ran["controller"] = True
        return "should-not-run"

    m._stream_message_to_mythic = _stream
    m._run_autonomous_controller = _run_controller

    assert _run(m.handle_controller_hitl_resume("maybe")) == ""
    assert ran["controller"] is False
    assert m._controller_hitl_pending is None
    assert m._controller_hitl_approved_key == ""
    assert "Operator denied `gpo-controlled-system-exec`" in sent[0]


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


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("target", "domain=other.local"),
        ("callback", "4"),
        ("policy", "decision-mutated"),
        ("transaction", "transaction-mutated"),
    ),
)
def test_controller_hitl_mutated_authority_bytes_deny_before_resume(field, value):
    m = _controller_hitl_model()
    pending = m._controller_hitl_capability_request(
        {
            "name": "dcsync-account",
            "target": "domain=lab.local;account=alice",
            "intent": {
                "policy_decision": {"decision_id": "decision-original"},
                "transaction_id": "transaction-original",
            },
        },
        {
            "callback_id": "3",
            "policy_decision": {"decision_id": "decision-original"},
            "transaction_id": "transaction-original",
        },
        "obtain credentials for alice@lab.local",
    )
    action_requests = [{
        "name": pending["tool"],
        "display_name": pending["display_name"],
        "args": pending["args"],
    }]
    digest = approval_action_digest(action_requests)
    mutated = copy.deepcopy(pending)
    if field == "target":
        mutated["args"]["action"]["target"] = value
    elif field == "callback":
        mutated["args"]["inputs"]["callback_id"] = value
    elif field == "policy":
        mutated["args"]["inputs"]["policy_decision"]["decision_id"] = value
    else:
        mutated["args"]["action"]["intent"]["transaction_id"] = value
    m._controller_hitl_pending = mutated
    m._controller_observed_transactions = []
    m._run_autonomous_controller = lambda _objective: (_ for _ in ()).throw(
        AssertionError("mutated approval reached controller resume")
    )

    with pytest.raises(RuntimeError, match="changed after its approval card was created"):
        _run(m.handle_controller_hitl_resume("approve", expected_action_digest=digest))

    assert m._controller_observed_transactions == []


def test_controller_hitl_key_binds_policy_provenance_and_action():
    m = _controller_hitl_model()
    payload = {
        "name": "dcsync-account",
        "target": "domain=lab.local;account=alice",
        "preconditions": ["ds-replication-rights:lab.local"],
        "effects": ["creds:alice@lab.local"],
        "intent": {"policy_decision": {"episode_id": "one", "decision_id": "first"}},
    }
    first = m._controller_hitl_capability_request(
        payload,
        {"callback_id": "3", "policy_decision": {"timestamp": "one"}},
        "obtain credentials for alice@lab.local",
    )
    replay = m._controller_hitl_capability_request(
        payload,
        {"callback_id": "3", "policy_decision": {"timestamp": "one"}},
        "obtain credentials for alice@lab.local",
    )
    payload["intent"]["policy_decision"] = {"episode_id": "two", "decision_id": "second"}
    second = m._controller_hitl_capability_request(
        payload,
        {"callback_id": "3", "policy_decision": {"timestamp": "two"}},
        "obtain credentials for alice@lab.local",
    )
    changed = m._controller_hitl_capability_request(
        {**payload, "target": "domain=lab.local;account=bob"},
        {"callback_id": "3", "policy_decision": {"timestamp": "two"}},
        "obtain credentials for alice@lab.local",
    )

    assert first["key"] == replay["key"]
    assert first["key"] != second["key"]
    assert first["key"] != changed["key"]


# --- Minimal loop-breaker (denial-routing spec, staged) -----------------------------------------

def test_reproposal_after_effect_denial_terminalises_blocked_and_does_not_recard():
    """When the just-approved supervised action was refused at the effect boundary, the resume loop must
    surface the denial reason and terminalise `blocked` instead of re-carding the denied action (the
    livelock). Red-before: `_handle_reproposal_after_denial` did not exist, so the interrupt was always
    re-surfaced as a fresh approval card."""
    m = _bare_model("supervised")
    emitted, terminal, closed = [], [], []

    async def _emit(msg):
        emitted.append(msg)

    async def _close(status="stopped"):
        closed.append(status)

    m._stream_message_to_mythic = _emit
    m.record_request_terminal = lambda status="complete": terminal.append(status)
    m._close_all_request_lifecycles = _close
    m.mythic_client = SimpleNamespace(
        _last_effect_denial={"reason": "STOP — callback 5 is not taskable: dead; no checkin"}
    )

    handled = _run(m._handle_reproposal_after_denial())

    assert handled is True
    assert terminal == ["blocked"]          # terminalised blocked
    assert closed == ["blocked"]            # lifecycle closed → seals the 49R-16 decision record
    assert emitted and "Blocked" in emitted[0] and "not taskable" in emitted[0]
    assert m.mythic_client._last_effect_denial is None   # flag cleared, no re-trigger next cycle


def test_no_denial_recorded_leaves_reproposal_path_untouched():
    """Legitimate path: an approved action that SUCCEEDED records no denial, so the loop-breaker does NOT
    fire and normal approval-card surfacing is preserved. This is the non-regression the guard must keep."""
    m = _bare_model("supervised")
    terminal = []
    m.record_request_terminal = lambda status="complete": terminal.append(status)
    m.mythic_client = SimpleNamespace(_last_effect_denial=None)

    handled = _run(m._handle_reproposal_after_denial())

    assert handled is False
    assert terminal == []


def test_note_effect_denial_records_only_for_supervised_approved_effect():
    """The refusal source records a denial only when the request is a supervised lane WITH an active
    approval claim (an approved action refused), and returns the blocker string unchanged."""
    from ai.langgraph.mythic_tools import MythicTools
    from ai.langgraph.request_contract import RequestLane

    supervised = SimpleNamespace(
        _active_approval_claim={"approval_id": "x"},
        _request_contract=SimpleNamespace(lane=RequestLane.SUPERVISED_WORKFLOW),
    )
    out = MythicTools._note_effect_denial(supervised, "STOP — callback 5 is not taskable: dead")
    assert out == "STOP — callback 5 is not taskable: dead"
    assert supervised._last_effect_denial and "not taskable" in supervised._last_effect_denial["reason"]

    # no active approval claim → not an approved-then-refused effect → do not record
    no_claim = SimpleNamespace(
        _active_approval_claim=None,
        _request_contract=SimpleNamespace(lane=RequestLane.SUPERVISED_WORKFLOW),
    )
    MythicTools._note_effect_denial(no_claim, "STOP — something")
    assert getattr(no_claim, "_last_effect_denial", None) is None
