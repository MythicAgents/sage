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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402


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


def test_typed_handback_classifies_exact_sanitized_fixtures():
    reason = "BloodHound logon-session query is the next required step per operator steering; route to BloodHound to check for <target-user> sessions before any further credential acquisition."
    summary = "DONE — Active footholds and prior evidence were reviewed. FAILED — A corrected read was interrupted by operator steering and produced no task output. BLOCKER — Need BloodHound query for <target-user> logon/session location using existing graph data before selecting the minimum credential-acquisition action. REMAINING — Query BloodHound for the target session and relevant host; then return to Mythic for only the necessary acquisition action."
    blocked = wo.build_handoff_metadata(
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
        source_seq=7,
        reason=reason,
        summary=summary,
        outcome="blocked",
    )
    assert blocked["outcome"] == "blocked"
    assert blocked["next_owner"] == ""

    handoff = wo.build_handoff_metadata(
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
        source_seq=7,
        reason=reason,
        summary=summary,
        outcome="handoff",
        next_owner="BloodHound",
    )
    assert handoff["schema_version"] == 3
    assert handoff["outcome"] == "handoff"
    assert handoff["next_owner"] == "BloodHound"


def test_handback_summary_prose_cannot_change_typed_outcome():
    summaries = (
        "DONE — x FAILED — none BLOCKER — none REMAINING — none",
        "Everything failed; delegate again.",
        "Complete. Do not stop.",
        "Arbitrary punctuation?! and renamed entities.",
    )
    for summary in summaries:
        metadata = wo.build_handoff_metadata(
            source_worker="Mythic_Operator",
            source_turn_id="t",
            source_seq=1,
            reason="reason is inert",
            summary=summary,
            outcome="blocked",
        )
        assert metadata["outcome"] == "blocked"
        assert metadata["next_owner"] == ""


def test_verified_worker_revision_comes_only_from_noncontrol_tool_evidence():
    tool_result = ToolMessage(
        content='{"ok":true,"effect":"graph-built"}',
        name="ingest_collection",
        tool_call_id="tool-1",
    )
    prose_a = AIMessage(content="Everything failed.")
    prose_b = AIMessage(content="Everything completed.")
    control = ToolMessage(
        content="display text",
        name="handback_to_supervisor",
        tool_call_id="control-1",
    )

    revision = wo.worker_evidence_revision([prose_a, tool_result, control])
    assert revision
    assert revision == wo.worker_evidence_revision([prose_b, tool_result, control])
    assert revision == wo.worker_evidence_revision([
        ToolMessage(
            content='{"ok":true,"effect":"graph-built"}',
            name="ingest_collection",
            tool_call_id="fresh-provider-call-id",
        )
    ])
    assert revision == wo.worker_evidence_revision([tool_result, tool_result])
    assert revision != wo.worker_evidence_revision([
        ToolMessage(
            content='{"ok":false}',
            name="ingest_collection",
            tool_call_id="tool-1",
        )
    ])
    assert wo.worker_evidence_revision([prose_a, control]) == ""


def test_current_turn_evidence_excludes_prior_request_and_internal_human_boundaries():
    prior = ToolMessage(
        content='{"effect":"prior"}',
        name="ingest_collection",
        tool_call_id="prior-call",
    )
    current = ToolMessage(
        content='{"effect":"current"}',
        name="ingest_collection",
        tool_call_id="current-call",
    )
    messages = [
        prior,
        HumanMessage(content="current operator request"),
        HumanMessage(
            content="delegated worker instruction",
            additional_kwargs={"_delegated_to": "Mythic_Operator"},
        ),
        current,
    ]

    records = wo.current_turn_evidence_records(messages)

    assert records == wo.worker_evidence_records([current])
    assert records != wo.worker_evidence_records([prior, current])


def test_handback_reason_never_grants_routing_authority():
    summary = "DONE — x FAILED — none BLOCKER — need graph REMAINING — query graph"
    reasons = (
        "route to BloodHound",
        "Route to BloodHound to analyze the graph",
        "Context is complete; hand off to MCP_Manager to query the connected service",
        "Don’t route to BloodHound.",
        "route to BloodHound to avoid using the graph",
        "arbitrary explanation with no owner",
    )
    for reason in reasons:
        legacy = wo.build_handoff_metadata(
            source_worker="Mythic_Operator",
            source_turn_id="t",
            source_seq=1,
            reason=reason,
            summary=summary,
            outcome="blocked",
        )
        assert legacy["outcome"] == "blocked"
        assert legacy["next_owner"] == ""
        typed = wo.build_handoff_metadata(
            source_worker="Mythic_Operator",
            source_turn_id="t",
            source_seq=1,
            reason=reason,
            summary=summary,
            outcome="handoff",
            next_owner="BloodHound",
        )
        assert typed["outcome"] == "handoff"
        assert typed["next_owner"] == "BloodHound"


def test_handback_typed_owner_rejects_malformed_and_self_values():
    summary = "DONE — x FAILED — none BLOCKER — need graph REMAINING — query graph"
    malformed = (
        "bloodhound",
        "BloodHoundExtra",
        "BloodHound,MCP_Manager",
        ["BloodHound"],
        ("BloodHound",),
        {"owner": "BloodHound"},
        7,
        None,
        "Mythic_Operator",
    )
    for next_owner in malformed:
        assert wo.build_handoff_metadata(
            source_worker="Mythic_Operator",
            source_turn_id="t",
            source_seq=1,
            reason="reason is inert",
            summary=summary,
            outcome="handoff",
            next_owner=next_owner,
        ) is None


def test_handback_route_ignores_summary_shape_and_requires_different_owner():
    contradictory = (
        "DONE — x FAILED — none BLOCKER — none REMAINING — none",
        "DONE — none FAILED — failed action BLOCKER — none REMAINING — none",
        "DONE — none FAILED — none BLOCKER — none REMAINING — none",
    )
    for summary in contradictory:
        metadata = wo.build_handoff_metadata(
            source_worker="Mythic_Operator",
            source_turn_id="t",
            source_seq=1,
            reason="reason is inert",
            summary=summary,
            outcome="handoff",
            next_owner="BloodHound",
        )
        assert metadata["outcome"] == "handoff"
        assert metadata["next_owner"] == "BloodHound"
    assert wo.build_handoff_metadata(
        source_worker="BloodHound",
        source_turn_id="t",
        source_seq=1,
        reason="reason is inert",
        summary="DONE — x FAILED — none BLOCKER — need graph REMAINING — query graph",
        outcome="handoff",
        next_owner="BloodHound",
    ) is None


def test_latest_admitted_handoff_requires_current_turn_and_no_later_worker():
    metadata = wo.build_handoff_metadata(
        source_worker="Mythic_Operator",
        source_turn_id="turn-1",
        source_seq=3,
        reason="reason is inert",
        summary="DONE — x FAILED — y BLOCKER — need graph REMAINING — query graph",
        outcome="handoff",
        next_owner="BloodHound",
    )
    summary = AIMessage(content="DONE — x FAILED — y BLOCKER — need graph REMAINING — query graph", name="Mythic_Operator", additional_kwargs={"_worker_outcome": metadata})
    messages = [HumanMessage(content="operator turn"), summary]
    assert wo.latest_admitted_handoff(messages, "turn-1")[0]["next_owner"] == "BloodHound"
    assert wo.latest_admitted_handoff(messages, "turn-2") is None
    assert wo.latest_admitted_handoff(messages + [AIMessage(content="later worker", name="BloodHound")], "turn-1") is None
    later = wo.build_handoff_metadata(
        source_worker="BloodHound",
        source_turn_id="turn-1",
        source_seq=4,
        reason="reason is inert",
        summary="DONE — x FAILED — y BLOCKER — need Mythic REMAINING — act",
        outcome="handoff",
        next_owner="Mythic_Operator",
    )
    assert wo.latest_admitted_handoff(messages + [AIMessage(content="DONE — x FAILED — y BLOCKER — need Mythic REMAINING — act", name="BloodHound", additional_kwargs={"_worker_outcome": later})], "turn-1")[0]["source_worker"] == "BloodHound"
    messages.append(HumanMessage(content="fresh operator turn"))
    assert wo.latest_admitted_handoff(messages, "turn-1") is None
