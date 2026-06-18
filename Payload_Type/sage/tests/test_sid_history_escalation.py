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


# --- da:{domain} live-Kerberos-context implication of replication rights -----------------------------

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


def test_dcsync_precondition_uses_foothold_anywhere():
    assert es._technique_preconditions("dcsync", ROOT) == [f"ds-replication-rights:{ROOT}", "live-foothold:*"]
