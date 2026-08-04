"""ISC-75 — a delegation that TRIED to act and failed is a stall; analysis is not.

Regression for the live rejection loop: one ordinary operator rejection made the Supervisor
re-delegate 8-9 times, every cycle returned messages, and only the global step limit ended it —
leaving the operator `status=error` with no explanation.

Two earlier signals were refuted and both refutations are pinned here:

1. **Message counting** (original ISC-59) — the loop emits a message every cycle, so the streak reset
   forever. `test_paraphrasing_refusal_loop_is_caught` fails under it.
2. **Content digest** (ISC-75 attempt 1) — the model paraphrases its own refusal
   (`[turn-authority] issue_task…` / `Let me issue the ticket_cache_list…` /
   `[turn-authority] mode is supervised_action…`), so the digest never stabilises. Refuted live: the
   guard reached 2/3 and the request still hit the step limit. Content is prose, and prose cannot
   carry control state.

The signal is typed: progress is the Mythic task display id moving, and a delegation only counts
against the streak when it actually ATTEMPTED a guarded action (a blocked guarded call or a surfaced
approval card set `_guarded_attempt_pending`). Analysis-only delegations are NEUTRAL — attempt 2
truncated them, which was a false-positive class this now removes.

Every test runs the SHIPPED closure via `Model._wrap_create_agent`. A transcription mirror would pass
while production drifted, which is how the old counter looked healthy while the request looped.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage

from ai.langgraph.model import _ZERO_PROGRESS_DELEGATION_CAP, Model


class _FakeMythicClient:
    def __init__(self, task_id=None):
        self._last_issued_task_display_id = task_id


class _BlockedActionAgent:
    """Attempts a guarded action that never lands, phrasing the refusal differently every cycle.

    Mirrors production: the guarded call is blocked, so `_guarded_attempt_pending` is set, and the
    agent hands back a refusal whose wording changes — the shape that defeated attempt 1.
    """

    def __init__(self, model):
        self.calls = 0
        self.model = model

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        self.model._guarded_attempt_pending = True
        return {
            "messages": list(payload["messages"])
            + [AIMessage(content=f"[turn-authority] refusal phrased differently #{self.calls}")]
        }


class _AnalysisAgent:
    """Legitimate non-tasking work: graph queries, schema reads. Never attempts a guarded action."""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        return {
            "messages": list(payload["messages"])
            + [AIMessage(content=f"BloodHound finding {self.calls}")]
        }


class _TaskingAgent:
    """Crosses the effect boundary every cycle: the Mythic task display id advances."""

    def __init__(self, client, model):
        self.calls = 0
        self.client = client
        self.model = model

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        self.model._guarded_attempt_pending = True
        self.client._last_issued_task_display_id = self.calls
        return {
            "messages": list(payload["messages"])
            + [AIMessage(content=f"issued task {self.calls}")]
        }


def _fresh_model(client=None):
    m = Model.__new__(Model)
    m._autonomous_solve = False
    m._message_seq = 3
    m.state = {"_message_seq": 3}
    m.llm = None
    m.mythic_client = client
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


def _run_once(model, agent, node="Mythic_Operator", key="mythic_operator_messages"):
    channel = [HumanMessage(content="List the Kerberos ticket cache on callback 1 only.")]
    wrapped = model._wrap_create_agent(agent, key, node)
    return asyncio.run(wrapped(_state(channel), {}))


def _messages_of(result, key="mythic_operator_messages"):
    update = result if isinstance(result, dict) else (getattr(result, "update", None) or {})
    return update.get(key, []) or []


def _stopped(result, key="mythic_operator_messages"):
    msgs = _messages_of(result, key)
    if len(msgs) != 1:
        return False
    content = msgs[0].content.lower()
    return "no progress" in content or "weren't approved" in content


# ── A1: an attempted-and-blocked loop is caught even when its wording changes ─────────────────

def test_paraphrasing_refusal_loop_is_caught():
    """The exact shape that refuted attempt 1. Preconditions asserted so it cannot pass vacuously."""
    model = _fresh_model(_FakeMythicClient())
    agent = _BlockedActionAgent(model)

    seen, result = [], None
    for _ in range(_ZERO_PROGRESS_DELEGATION_CAP):
        result = _run_once(model, agent)
        msgs = _messages_of(result)
        assert msgs, "precondition: the agent DOES return a message every cycle"
        seen.append(msgs[0].content)

    assert len(set(seen[:2])) == 2, "precondition: wording really does change between cycles"
    assert _stopped(result), "an attempted-and-blocked loop must terminate at the cap"


def test_zero_return_loop_still_caught():
    """ISC-59's original case must not regress."""

    class _Silent:
        def __init__(self, model):
            self.model = model

        async def ainvoke(self, payload, config=None):
            self.model._guarded_attempt_pending = True
            return {"messages": list(payload["messages"])}

    model = _fresh_model(_FakeMythicClient())
    agent = _Silent(model)
    out = [_run_once(model, agent) for _ in range(_ZERO_PROGRESS_DELEGATION_CAP)]
    assert _stopped(out[-1])


# ── The item-3 correction: analysis is neutral ────────────────────────────────────────────────

def test_analysis_only_delegations_are_neutral():
    """A BloodHound-style request issues no Mythic task and is NOT a stall.

    Attempt 2 asserted the opposite — that any three non-tasking delegations should be surfaced as
    "no progress" — and that was a false-positive class, not a requirement. A delegation that never
    attempted a guarded action neither advances nor clears the streak.
    """
    model = _fresh_model(_FakeMythicClient())
    agent = _AnalysisAgent()

    for _ in range(_ZERO_PROGRESS_DELEGATION_CAP * 3):
        assert not _stopped(_run_once(model, agent)), "analysis must never trip the backstop"
    assert getattr(model, "_nonprogress_delegations", 0) == 0


def test_analysis_does_not_clear_a_real_stall():
    """Neutral means neutral: interleaved analysis must not reset a genuine stall streak."""
    model = _fresh_model(_FakeMythicClient())
    blocked = _BlockedActionAgent(model)
    analysis = _AnalysisAgent()

    _run_once(model, blocked)
    assert model._nonprogress_delegations == 1
    _run_once(model, analysis)
    assert model._nonprogress_delegations == 1, "analysis must not clear the streak"
    _run_once(model, blocked)
    assert model._nonprogress_delegations == 2


# ── A2/A4: effect crossings reset; tasking runs are never truncated ───────────────────────────

def test_effect_boundary_crossing_resets_the_streak():
    """ISC-61: the same command issued repeatedly, each one executing, is NOT a loop."""
    client = _FakeMythicClient()
    model = _fresh_model(client)
    agent = _TaskingAgent(client, model)

    for _ in range(_ZERO_PROGRESS_DELEGATION_CAP * 3):
        assert not _stopped(_run_once(model, agent)), "an executed action is progress"


def test_streak_resets_after_progress_interrupts_a_stall():
    """A2: two stalled cycles then a real effect must clear the streak."""
    client = _FakeMythicClient()
    model = _fresh_model(client)
    blocked = _BlockedActionAgent(model)
    tasking = _TaskingAgent(client, model)

    _run_once(model, blocked)
    _run_once(model, blocked)
    assert model._nonprogress_delegations == 2
    _run_once(model, tasking)
    assert model._nonprogress_delegations == 0


# ── A3: termination is a halt with a reason ───────────────────────────────────────────────────

def test_cap_halts_the_request_and_explains_why():
    """A3: injecting text is not enough — the live Supervisor re-delegated straight past it."""
    model = _fresh_model(_FakeMythicClient())
    agent = _BlockedActionAgent(model)

    assert not getattr(model, "_stop_requested", False)
    result = None
    for _ in range(_ZERO_PROGRESS_DELEGATION_CAP):
        result = _run_once(model, agent)

    assert _stopped(result)
    assert model._stop_requested is True, "the graph must halt, not just emit a message"

    text = _messages_of(result)[0].content.lower()
    assert "start a new request" in text or "how to proceed" in text


# ── A5: request-scoped, not per node ──────────────────────────────────────────────────────────

def test_streak_is_request_scoped_not_per_node():
    """A5: a loop that rotates nodes is still a loop."""
    model = _fresh_model(_FakeMythicClient())

    _run_once(model, _BlockedActionAgent(model), "Mythic_Operator", "mythic_operator_messages")
    assert model._nonprogress_delegations == 1
    _run_once(model, _BlockedActionAgent(model), "Generalist", "generalist_messages")
    assert model._nonprogress_delegations == 2, "the streak must accumulate ACROSS nodes"


def test_streak_does_not_leak_between_requests():
    """A5: a fresh Model (new request) starts clean."""
    model_a = _fresh_model(_FakeMythicClient())
    agent_a = _BlockedActionAgent(model_a)
    for _ in range(_ZERO_PROGRESS_DELEGATION_CAP):
        _run_once(model_a, agent_a)

    model_b = _fresh_model(_FakeMythicClient())
    assert not hasattr(model_b, "_nonprogress_delegations")
    assert not _stopped(_run_once(model_b, _BlockedActionAgent(model_b)))
