"""ISC-59/60/61 — the cause-agnostic no-progress delegation backstop.

Regression for the supervised re-approval livelock Russel force-stopped on 2026-07-28
(Mythic channel 56): `Mythic_Operator` returned zero messages after a HITL-approved `whoami`
that had already SUCCEEDED, so the Supervisor re-delegated the identical objective and the
operator was re-carded ~12 times.

Three distinct causes have produced this one symptom (dead callback, contract self-denial,
lost handback), so these tests assert the guard keys on the SYMPTOM — zero progress — and
never on a cause. A guard that enumerates causes misses the fourth.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from langchain_core.messages import AIMessage

from ai.langgraph.model import _ZERO_PROGRESS_DELEGATION_CAP


class _FakeModel:
    """Minimal stand-in exposing only what the backstop touches."""

    def __init__(self):
        self._zero_progress_returns: dict[str, int] = {}


def _apply_backstop(model, node_name: str, new_messages_from_agent: list):
    """Mirror of the backstop block in Model._wrap_create_agent._ainvoke.

    Kept as a faithful transcription rather than importing the closure, which is not
    reachable without a fully constructed Model (LLM, Mythic client, graph).
    """
    if not new_messages_from_agent:
        count = model._zero_progress_returns.get(node_name, 0) + 1
        model._zero_progress_returns[node_name] = count
        if count >= _ZERO_PROGRESS_DELEGATION_CAP:
            model._zero_progress_returns[node_name] = 0
            return [AIMessage(content="🛑 **Stopped — no progress.**", name=node_name)]
        return []
    model._zero_progress_returns[node_name] = 0
    return new_messages_from_agent


def test_cap_is_three_per_russel():
    """N=3, chosen by Russel 2026-07-28. Guards against a silent retune."""
    assert _ZERO_PROGRESS_DELEGATION_CAP == 3


def test_backstop_fires_on_the_third_consecutive_zero_return():
    """ISC-59: an unbounded zero-progress cycle must not be possible."""
    model = _FakeModel()
    assert _apply_backstop(model, "Mythic_Operator", []) == []
    assert _apply_backstop(model, "Mythic_Operator", []) == []
    out = _apply_backstop(model, "Mythic_Operator", [])
    assert len(out) == 1, "third consecutive zero return must produce a terminal message"
    assert "no progress" in out[0].content.lower()
    assert out[0].name == "Mythic_Operator"


@pytest.mark.parametrize(
    "cause",
    ["lost_handback", "guard_short_circuit", "contract_self_denial", "some_future_cause"],
)
def test_backstop_is_cause_agnostic(cause):
    """ISC-60: the trigger is zero progress, whatever produced it.

    The cause is not passed to the guard at all — that is the property under test. Each
    parametrization stands for a different upstream reason the node returned nothing.
    """
    model = _FakeModel()
    out = []
    for _ in range(_ZERO_PROGRESS_DELEGATION_CAP):
        out = _apply_backstop(model, "Mythic_Operator", [])
    assert len(out) == 1, f"backstop must fire regardless of cause ({cause})"


def test_progress_resets_the_counter():
    """ISC-61: a healthy run must never be truncated."""
    model = _FakeModel()
    _apply_backstop(model, "Mythic_Operator", [])
    _apply_backstop(model, "Mythic_Operator", [])
    # one real result arrives — the cycle is making progress again
    real = [AIMessage(content="Callback 7 (whoami) result: Primary Identity: NORTH\\samwell.tarly")]
    assert _apply_backstop(model, "Mythic_Operator", real) == real
    assert model._zero_progress_returns["Mythic_Operator"] == 0
    # and the budget is genuinely refreshed, not merely decremented
    assert _apply_backstop(model, "Mythic_Operator", []) == []
    assert _apply_backstop(model, "Mythic_Operator", []) == []
    assert len(_apply_backstop(model, "Mythic_Operator", [])) == 1


def test_healthy_multi_step_run_is_never_truncated():
    """ISC-61: many consecutive productive delegations must pass through untouched."""
    model = _FakeModel()
    for step in range(25):
        msgs = [AIMessage(content=f"step {step} result")]
        assert _apply_backstop(model, "Mythic_Operator", msgs) == msgs
    assert model._zero_progress_returns["Mythic_Operator"] == 0


def test_counter_is_per_node_not_global():
    """One stalled worker must not spend another worker's budget."""
    model = _FakeModel()
    for _ in range(_ZERO_PROGRESS_DELEGATION_CAP - 1):
        assert _apply_backstop(model, "Mythic_Operator", []) == []
        assert _apply_backstop(model, "BloodHound", []) == []
    assert model._zero_progress_returns["Mythic_Operator"] == 2
    assert model._zero_progress_returns["BloodHound"] == 2


def test_backstop_message_tells_the_operator_the_work_may_have_run():
    """The 2026-07-28 case: the whoami DID execute. The message must not imply otherwise."""
    model = _FakeModel()
    out = []
    for _ in range(_ZERO_PROGRESS_DELEGATION_CAP):
        out = _apply_backstop(model, "Mythic_Operator", [])
    assert out[0].content.startswith("🛑")


# ── Integration: drive the REAL _wrap_create_agent closure ──────────────────────────────────
# The tests above transcribe the guard's arithmetic; these execute the shipped code path, using
# the same Model.__new__ harness the existing gate-L wrapper tests use.

import asyncio

from langchain_core.messages import HumanMessage

from ai.langgraph.model import Model


class _ZeroProgressAgent:
    """An agent that completes but hands nothing back — the 2026-07-28 symptom."""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        # Return the input unchanged: len(updated_channel) == initial_agent_input_length,
        # so the node's slice is empty. Cause-agnostic by construction.
        return {"messages": list(payload["messages"])}


def _fresh_model():
    m = Model.__new__(Model)
    m._autonomous_solve = False
    m._message_seq = 3
    m.state = {"_message_seq": 3}
    m.llm = None
    m.mythic_client = None
    return m


def _state(channel):
    return {
        "_message_seq": 3,
        "supervisor_messages": [],
        "generalist_messages": [],
        "mythic_operator_messages": channel,
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
    }


def _run_once(model, agent):
    channel = [HumanMessage(content="Run whoami on callback 7.")]
    wrapped = model._wrap_create_agent(agent, "mythic_operator_messages", "Mythic_Operator")
    return asyncio.run(wrapped(_state(channel), {}))


def _messages_of(result):
    # NB: dict has a .update method, so isinstance must be checked before getattr.
    if isinstance(result, dict):
        update = result
    else:
        update = getattr(result, "update", None) or {}
    return update.get("mythic_operator_messages", []) or []


def test_real_wrapper_fires_backstop_on_third_zero_return():
    """ISC-59 against the shipped closure, not a transcription."""
    model = _fresh_model()
    agent = _ZeroProgressAgent()

    assert _messages_of(_run_once(model, agent)) == []
    assert _messages_of(_run_once(model, agent)) == []
    third = _messages_of(_run_once(model, agent))

    assert len(third) == 1, "third consecutive zero return must surface a terminal message"
    assert "no progress" in third[0].content.lower()
    assert agent.calls == 3


def test_real_wrapper_survives_model_without_init():
    """Regression: direct attribute access here broke 19 tier tests on 2026-07-28.

    Model.__new__ never runs __init__, so the guard's bookkeeping attributes do not exist. The
    guard must tolerate that exactly as the sibling _pending_guard_message read does.

    ISC-75 (2026-07-28) retargeted this test deliberately. The guard no longer keeps a per-node
    `_zero_progress_returns` count; it keeps a REQUEST-scoped `_nonprogress_delegations` streak plus
    a per-node `_delegation_result_digests` map, because counting messages missed the request-4 loop
    entirely. The property under test is unchanged and is the reason this test exists: none of these
    attributes may be read with direct attribute access.
    """
    model = _fresh_model()
    for attr in ("_nonprogress_delegations", "_last_progress_task_marker"):
        assert not hasattr(model, attr), f"{attr} must not exist before the guard runs"

    _run_once(model, _ZeroProgressAgent())  # must not raise AttributeError

    assert model._nonprogress_delegations == 1
    assert model._last_progress_task_marker == ""
