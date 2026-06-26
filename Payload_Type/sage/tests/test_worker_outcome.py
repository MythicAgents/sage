"""Offline tests for the typed worker-outcome control contract (P0). No live range; pure decision logic.

The load-bearing tests are the two the Codex/Grok reviews demanded:
  - after the same blocker repeats at the same state, the supervisor must NOT re-delegate (kills the 1116 loop);
  - a fingerprint must NOT over-suppress a legitimate retry after observable state changed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import worker_outcome as wo  # noqa: E402
from worker_outcome import WorkerOutcome, Blocker, Outcome, Decision  # noqa: E402


def _blocked(fp="adcs-certificate-auth:needs-ca-key", rev="r1", route_cap="", owner=""):
    return WorkerOutcome(
        outcome=Outcome.BLOCKED, state_revision=rev,
        blocker=Blocker(fp, "an enrolled cert / CA private key for the target exists"),
        next_capability=route_cap, next_owner=owner,
    )


def test_progress_and_complete_proceed():
    assert wo.supervisor_decision([], WorkerOutcome(Outcome.PROGRESS, "r1")) == Decision.PROCEED
    assert wo.supervisor_decision([], WorkerOutcome(Outcome.COMPLETE, "r1")) == Decision.PROCEED


def test_first_blocker_with_prerequisite_routes_not_redelegates():
    # 1116: worker blocked on cert-auth, prerequisite is adcs-ca-private-key-export -> ROUTE, don't re-delegate.
    out = _blocked(route_cap="adcs-ca-private-key-export")
    assert wo.supervisor_decision([], out) == Decision.ROUTE_TO_OWNER


def test_same_blocker_same_state_repeats_must_stop_not_redelegate():
    # THE 1116 KILL: the identical blocker at the identical state -> STOP. No 48 re-delegations.
    out = _blocked(route_cap="adcs-ca-private-key-export")
    history = [out]                       # we already routed once on this exact (blocker, state)
    assert wo.supervisor_decision(history, out) == Decision.STOP


def test_blocker_with_changed_state_is_not_over_suppressed():
    # OVER-SUPPRESSION GUARD (Codex §4): same blocker fingerprint, but the observed state advanced (r1 -> r2,
    # e.g. the CA key was exported) -> this is a legitimate fresh attempt, NOT a loop.
    first = _blocked(rev="r1", route_cap="adcs-ca-private-key-export")
    later = _blocked(rev="r2", route_cap="adcs-ca-private-key-export")
    assert wo.supervisor_decision([first], later) != Decision.STOP
    assert wo.supervisor_decision([first], later) == Decision.ROUTE_TO_OWNER


def test_first_blocker_no_route_retries_once_then_stops():
    out = _blocked(fp="lone-blocker", route_cap="", owner="")
    assert wo.supervisor_decision([], out) == Decision.RETRY_WORKER        # first sight, one attempt
    assert wo.supervisor_decision([out], out) == Decision.STOP             # repeated, same state -> stop


def test_1116_replay_produces_one_stop_not_a_tail():
    # Replay the worker returning the SAME blocker at the same state 49 times (the 1116 shape). The supervisor
    # routes/retries at most once, then every subsequent identical outcome is STOP — no redelegation tail.
    out = _blocked(route_cap="adcs-ca-private-key-export")
    history: list[WorkerOutcome] = []
    decisions = []
    for _ in range(49):
        decisions.append(wo.supervisor_decision(history, out))
        history.append(out)
    assert decisions[0] == Decision.ROUTE_TO_OWNER
    assert all(d == Decision.STOP for d in decisions[1:])  # the 461K-token tail is structurally impossible


def test_bloodhound_routing_failure_advances_to_owner():
    # Active-run failure: worker says "graph analysis is required next" -> route to the named owner/capability,
    # not a re-delegation of the broad objective (which caused 4 identical SharpHound ingests).
    out = WorkerOutcome(
        outcome=Outcome.HANDOFF, state_revision="g1",
        next_capability="bloodhound-graph-analysis", next_owner="Mythic_Operator",
        note="BloodHound graph analysis is required next",
    )
    assert wo.supervisor_decision([], out) == Decision.ROUTE_TO_OWNER


def test_action_fingerprint_revision_model():
    a = wo.action_fingerprint("dcsync north.sevenkingdoms.local krbtgt", "rev1", "v1")
    assert a == wo.action_fingerprint("dcsync   north.sevenkingdoms.local  krbtgt", "rev1", "v1")  # normalized
    assert a != wo.action_fingerprint("dcsync north.sevenkingdoms.local krbtgt", "rev2", "v1")     # new state
    assert a != wo.action_fingerprint("dcsync north.sevenkingdoms.local krbtgt", "rev1", "v2")     # new impl


def test_outcome_serialization_roundtrip():
    out = _blocked(route_cap="adcs-ca-private-key-export", owner="Mythic_Operator")
    d = out.to_dict()
    assert d["contract_version"] == wo.CONTRACT_VERSION
    assert WorkerOutcome.from_dict(d) == out


# --- capability-result mapping + the archived 1116 replay (completion-criterion #1) ---

def test_outcome_from_capability_result_maps_ok_and_blocked():
    assert wo.outcome_from_capability_result("x", {"ok": True}, "0").outcome == Outcome.PROGRESS
    blk = wo.outcome_from_capability_result(
        "adcs-certificate-auth", {"ok": False, "reason": "needs CA key"}, "0")
    assert blk.outcome == Outcome.BLOCKED
    assert blk.blocker.fingerprint == "adcs-certificate-auth::needs ca key"   # keyed on capability+reason
    # a STRUCTURED prerequisite becomes the route target; free-text reasons are not parsed
    routed = wo.outcome_from_capability_result(
        "adcs-certificate-auth", {"ok": False, "reason": "x", "run_first": "adcs-ca-private-key-export"}, "0")
    assert routed.next_capability == "adcs-ca-private-key-export"


def test_blocker_fingerprint_invariant_to_supervisor_paraphrasing():
    # The worker's (capability, reason) is the key — the supervisor's wording never enters it, so 48 reworded
    # re-delegations of the same blocked action collapse to ONE fingerprint (what the old detector missed).
    a = wo.outcome_from_capability_result("adcs-certificate-auth", {"ok": False, "reason": "missing CA key"}, "0")
    b = wo.outcome_from_capability_result("adcs-certificate-auth", {"ok": False, "reason": "missing CA key"}, "0")
    assert a.blocker.fingerprint == b.blocker.fingerprint


def test_archived_1116_capability_replay_terminates():
    # The real 1116 shape: the worker returns the SAME blocked execute_capability result every re-delegation
    # with no progress. The control-state decision must yield ONE attempt then terminal STOPs — never a tail.
    blocked = {"ok": False, "capability": "adcs-certificate-auth",
               "reason": "missing enrolled certificate / CA private key; run adcs-ca-private-key-export first"}
    history: list[WorkerOutcome] = []
    decisions = []
    for _ in range(49):
        outcome, decision = wo.decide_capability_outcome(history, "adcs-certificate-auth", blocked, 0)
        decisions.append(decision)
        history.append(outcome)
    assert decisions[0] == Decision.RETRY_WORKER
    assert all(d == Decision.STOP for d in decisions[1:])   # 48-redelegation tail is structurally impossible


def test_progress_between_blockers_is_not_over_suppressed():
    # blocked@epoch0 -> a real success (epoch advances) -> the same blocker@epoch1 is a NEW state, not a loop.
    blocked = {"ok": False, "capability": "dcsync", "reason": "access denied"}
    history: list[WorkerOutcome] = []
    o0, d0 = wo.decide_capability_outcome(history, "dcsync", blocked, 0); history.append(o0)
    o1, d1 = wo.decide_capability_outcome(history, "dcsync", {"ok": True}, 1); history.append(o1)
    assert d1 == Decision.PROCEED
    o2, d2 = wo.decide_capability_outcome(history, "dcsync", blocked, 1); history.append(o2)
    assert d2 != Decision.STOP    # different epoch -> legitimate fresh attempt, not over-suppressed


# --- LoopBreakerState: per-solve state machine (the Model glue; covers the two Forge-caught edges) ---

def _blocked_result():
    return {"ok": False, "capability": "adcs-certificate-auth", "reason": "needs CA key"}


def test_loop_breaker_halts_on_repeated_blocker_with_fresh_turn_keys():
    st = wo.LoopBreakerState()
    halts = [wo.observe_capability_outcome(st, "adcs-certificate-auth", _blocked_result(), f"call-{i}")
             for i in range(49)]
    assert halts[0] is False        # first delegation is not a halt
    assert halts[1] is True         # the first genuine re-delegation of the same blocker halts
    assert all(halts[1:])           # and every subsequent one — no 48-redelegation tail


def test_loop_breaker_ignores_empty_turn_key():
    # Forge finding 2: an unkeyed observation must never be counted (else a same-turn double-fire false-halts).
    st = wo.LoopBreakerState()
    assert wo.observe_capability_outcome(st, "x", {"ok": False, "reason": "r"}, "") is False
    assert wo.observe_capability_outcome(st, "x", {"ok": False, "reason": "r"}, "") is False
    assert st.outcomes == []        # nothing counted


def test_loop_breaker_dedups_same_turn_key():
    st = wo.LoopBreakerState()
    assert wo.observe_capability_outcome(st, "x", {"ok": False, "reason": "r"}, "call-1") is False
    assert wo.observe_capability_outcome(st, "x", {"ok": False, "reason": "r"}, "call-1") is False  # re-fire ignored
    assert len(st.outcomes) == 1


def test_loop_breaker_fresh_state_has_no_cross_solve_leak():
    # Forge finding 1: a new solve gets a fresh LoopBreakerState, so the first block in solve 2 must NOT halt.
    st1 = wo.LoopBreakerState()
    wo.observe_capability_outcome(st1, "x", _blocked_result(), "a")
    assert wo.observe_capability_outcome(st1, "x", _blocked_result(), "b") is True   # halts in solve 1
    st2 = wo.LoopBreakerState()                                                      # new solve
    assert wo.observe_capability_outcome(st2, "x", _blocked_result(), "a") is False  # clean — no leak


def test_loop_breaker_progress_resets_staleness():
    st = wo.LoopBreakerState()
    blk = {"ok": False, "capability": "dcsync", "reason": "denied"}
    assert wo.observe_capability_outcome(st, "dcsync", blk, "t1") is False
    assert wo.observe_capability_outcome(st, "dcsync", {"ok": True, "capability": "dcsync"}, "t2") is False  # progress
    assert wo.observe_capability_outcome(st, "dcsync", blk, "t3") is False   # new epoch -> not over-suppressed
