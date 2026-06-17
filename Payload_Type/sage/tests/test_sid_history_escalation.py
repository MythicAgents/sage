"""Child→parent (forest-root) SID-history escalation: classifier + gate model.

Regression for the gate bug exposed by the essos-DA solve (2026-06-07): a SID-history / ExtraSIDs golden
ticket forged from the CHILD krbtgt to reach the forest ROOT was classified identically to a plain child
golden ticket (technique golden-ticket, target=child, effect da:child) — so once the child golden ticket
was achieved, the gate SKIPped the parent climb, silently blocking it. Fix: a distinct
`sid-history-escalation` technique keyed by the `/sids` flag, effect da:{parent}, precondition
krbtgt-hash:{child}.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state as es  # noqa: E402
import intent_classifier as ic  # noqa: E402

GD = es.GateDecision
CHILD = "north.sevenkingdoms.local"
ROOT = "sevenkingdoms.local"
TS = "2026-06-07T00:00:00+00:00"


# --- classifier --------------------------------------------------------------

def test_classifier_extrasids_golden_is_sid_history_escalation():
    tech, target = ic.classify_tool_call(
        "execute_assembly",
        {"Assembly": "Rubeus.exe",
         "Arguments": (f"golden /aes256:abc /user:Administrator /domain:{CHILD} "
                       f"/sid:S-1-5-21-CHILD /sids:S-1-5-21-ROOT-519 /ptt")},
    )
    assert (tech, target) == ("sid-history-escalation", CHILD)


def test_classifier_plain_golden_unchanged():
    tech, target = ic.classify_tool_call(
        "execute_assembly",
        {"Assembly": "Rubeus.exe", "Arguments": f"golden /aes256:abc /user:Administrator /domain:{CHILD} /ptt"},
    )
    assert (tech, target) == ("golden-ticket", CHILD)


def test_classifier_mimikatz_extrasids_form():
    tech, target = ic.classify_tool_call(
        "execute_pe",
        {"Commands": [f"kerberos::golden /user:administrator /domain:{CHILD} "
                      f"/sid:S-1-5-21-CHILD /sids:S-1-5-21-ROOT-519 /krbtgt:hash /ptt"]},
    )
    assert tech == "sid-history-escalation" and target == CHILD


def test_classifier_handles_quoted_mimikatz_extrasids_form():
    tech, target = ic.classify_tool_call(
        "mimikatz",
        {"arguments": (f'"kerberos::golden /user:administrator /domain:{CHILD} '
                       f'/sid:S-1-5-21-CHILD /sids:S-1-5-21-ROOT-519 /krbtgt:hash"')},
    )
    assert tech == "sid-history-escalation" and target == CHILD


# --- model effect / precondition --------------------------------------------

def test_effect_is_parent_domain():
    assert es._technique_effect("sid-history-escalation", CHILD) == f"da:{ROOT}"


def test_precondition_is_child_krbtgt():
    assert es._technique_preconditions("sid-history-escalation", CHILD) == [f"krbtgt-hash:{CHILD}"]


def test_distinct_effect_from_child_golden_ticket():
    # The crux: the SID-history climb's effect must NOT collapse onto the child golden ticket's effect.
    assert es._technique_effect("golden-ticket", CHILD) == f"da:{CHILD}"
    assert es._technique_effect("sid-history-escalation", CHILD) == f"da:{ROOT}"


def test_parent_domain_helper_two_label_root_is_self():
    assert es._parent_domain(ROOT) == ROOT
    assert es._parent_domain(CHILD) == ROOT


# --- gate behavior: the actual bug ------------------------------------------

def _state_after_child_da():
    """Child golden ticket already achieved (da:child) + child krbtgt in hand (krbtgt-hash:child)."""
    s = es.EngagementState(objective="t")
    s = es.record_hop_result(s, "dcsync", CHILD, "achieved",
                             {"source": "issue_task", "provenance": "run"}, TS)
    s = es.record_hop_result(s, "golden-ticket", CHILD, "achieved",
                             {"source": "issue_task", "provenance": "run"}, TS)
    return s


def test_parent_climb_proceeds_even_after_child_golden_ticket():
    # The regression: this used to SKIP because it collapsed to golden-ticket:child / da:child.
    s = _state_after_child_da()
    d, reason = es.gate_decision("sid-history-escalation", CHILD, s)
    assert d == GD.PROCEED, reason


def test_parent_climb_skips_once_achieved():
    s = _state_after_child_da()
    s = es.record_hop_result(s, "sid-history-escalation", CHILD, "achieved",
                             {"source": "issue_task", "provenance": "run"}, TS)
    d, reason = es.gate_decision("sid-history-escalation", CHILD, s)
    assert d == GD.SKIP and f"da:{ROOT}" in reason


def test_parent_climb_defers_without_child_krbtgt():
    s = es.EngagementState(objective="t")  # nothing achieved
    d, reason = es.gate_decision("sid-history-escalation", CHILD, s)
    assert d == GD.DEFER and f"krbtgt-hash:{CHILD}" in reason


def test_child_golden_ticket_still_skips_when_already_done():
    # The fix must not regress the plain child golden ticket's own idempotent SKIP.
    s = _state_after_child_da()
    d, reason = es.gate_decision("golden-ticket", CHILD, s)
    assert d == GD.SKIP and f"da:{CHILD}" in reason


# --- gate: da:{domain} needs a live Kerberos context before remote DCSync of that domain --------------

def _castelblack():
    return es.Foothold(callback_id="50", agent="apollo", host="castelblack",
                       forest=CHILD, identity="NORTH\\samwell.tarly", integrity="high",
                       alive=True, source="reconcile", timestamp=TS)


def test_da_with_live_callback_context_implies_replication_rights():
    s = es.EngagementState(objective="t", footholds=[_castelblack()])
    s = es.record_effect_result(
        s, "sid-history-escalation", CHILD, f"da:{ROOT}", "achieved",
        {"source": "issue_task", "provenance": "run", "callback_id": "50"}, TS,
        preconditions=[f"krbtgt-hash:{CHILD}"],
        satisfied_effects=[f"da:{ROOT}", f"kerberos-context:{ROOT}@callback:50"],
    )
    assert s.satisfies_predicate(f"ds-replication-rights:{ROOT}")


def test_da_without_live_callback_context_does_not_imply_replication_rights():
    s = es.EngagementState(objective="t", footholds=[_castelblack()])
    s = es.record_hop_result(s, "sid-history-escalation", CHILD, "achieved",
                             {"source": "issue_task", "provenance": "run", "callback_id": "11"}, TS)
    assert not s.satisfies_predicate(f"ds-replication-rights:{ROOT}")


def test_da_unlocks_remote_parent_dcsync():
    # After the climb (da:ROOT) with a live callback-scoped Kerberos context, DCSync of the PARENT
    # can proceed remotely from the NORTH foothold.
    s = es.EngagementState(objective="t", footholds=[_castelblack()])
    s = es.record_effect_result(
        s, "sid-history-escalation", CHILD, f"da:{ROOT}", "achieved",
        {"source": "issue_task", "provenance": "run", "callback_id": "50"}, TS,
        preconditions=[f"krbtgt-hash:{CHILD}"],
        satisfied_effects=[f"da:{ROOT}", f"kerberos-context:{ROOT}@callback:50"],
    )
    d, reason = es.gate_decision("dcsync", ROOT, s)
    assert d == GD.PROCEED, reason


def test_dcsync_still_defers_without_any_foothold():
    # DCSync still needs a live foothold somewhere (network position) — with none, DEFER.
    s = es.EngagementState(objective="t", footholds=[])
    s = es.record_effect_result(
        s, "sid-history-escalation", CHILD, f"da:{ROOT}", "achieved",
        {"source": "issue_task", "provenance": "run", "callback_id": "50"}, TS,
        preconditions=[f"krbtgt-hash:{CHILD}"],
        satisfied_effects=[f"da:{ROOT}", f"kerberos-context:{ROOT}@callback:50"],
    )
    d, reason = es.gate_decision("dcsync", ROOT, s)
    assert d == GD.DEFER and "live-foothold" in reason


def test_dcsync_precondition_uses_foothold_anywhere():
    assert es._technique_preconditions("dcsync", ROOT) == [f"ds-replication-rights:{ROOT}", "live-foothold:*"]


def test_dcsync_still_requires_rights_without_da():
    # Regression: no da and no rights grant → NORTH DCSync still DEFERs (no free pass from the foothold change).
    s = es.EngagementState(objective="t", footholds=[_castelblack()])
    d, reason = es.gate_decision("dcsync", CHILD, s)
    assert d == GD.DEFER and "ds-replication-rights" in reason
