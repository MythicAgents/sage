"""The objective_clean_stop scorecard signal: the Phase-1-fix metric for a capability-saturated scenario.

It must be True ONLY when the run reached the scenario's terminal milestone by ground truth AND halted on its
own (clean status). It must be False for a churn-to-timeout at the objective (the over-reach bug) and for any
under-reach (stopping before the objective) — so optimizing it can't be gamed by quitting early.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
import fitness  # noqa: E402
from range_state import GroundTruth, Milestone  # noqa: E402


def _gt(furthest, milestones):
    return GroundTruth(scenario="child-da", milestones=milestones, furthest=furthest)


def _child_ms(*, da, krbtgt):
    return {
        Milestone.FOOTHOLD: True,
        Milestone.GRAPH_COLLECTED: False,
        Milestone.SYSTEM_ON_HOST: False,
        Milestone.DA_CHILD: da,
        Milestone.KRBTGT_DUMPED: krbtgt,
    }


def test_objective_reached_and_recognized_is_clean_stop():
    # query.py sets status="objective-recognized" ONLY when Sage streamed the completion report. With the
    # ground-truth terminal milestone also met, this is the one True case.
    card = fitness.score({"status": "objective-recognized"}, _gt(Milestone.KRBTGT_DUMPED, _child_ms(da=True, krbtgt=True)))
    assert card.objective_clean_stop is True


def test_objective_reached_but_timeout_is_not_clean_stop():
    # The over-reach / klist-loop signature: reached the objective but churned to the wall-clock.
    card = fitness.score({"status": "timeout"}, _gt(Milestone.KRBTGT_DUMPED, _child_ms(da=True, krbtgt=True)))
    assert card.objective_clean_stop is False


def test_objective_reached_but_not_recognized_is_not_clean_stop():
    # Forge hole: a normal finish lands on Mythic's default "success"/"stopped" whether Sage RECOGNIZED the
    # objective or just terminated (step-limit, wandered). Reached-but-not-recognized must NOT count.
    for status in ("success", "stopped", "done"):
        card = fitness.score({"status": status}, _gt(Milestone.KRBTGT_DUMPED, _child_ms(da=True, krbtgt=True)))
        assert card.objective_clean_stop is False, f"{status!r} must not count as a clean stop"


def test_native_chat_completed_plus_ground_truth_is_clean_stop():
    card = fitness.score(
        {"status": "completed"},
        _gt(Milestone.KRBTGT_DUMPED, _child_ms(da=True, krbtgt=True)),
    )
    assert card.objective_clean_stop is True


def test_under_reach_foothold_success_is_not_clean_stop():
    ms = _child_ms(da=False, krbtgt=False)
    card = fitness.score({"status": "success"}, _gt(Milestone.FOOTHOLD, ms))
    assert card.objective_clean_stop is False


def test_da_but_no_krbtgt_timeout_is_not_clean_stop():
    card = fitness.score({"status": "timeout"}, _gt(Milestone.DA_CHILD, _child_ms(da=True, krbtgt=False)))
    assert card.objective_clean_stop is False


def test_false_recognition_without_ground_truth_earns_nothing():
    # The Goodhart guard: even if Sage emits "objective-recognized", the OUT-OF-BAND ground truth must agree.
    # Here the terminal milestone (krbtgt) is NOT met by probes -> no credit despite the recognition status.
    card = fitness.score({"status": "objective-recognized"}, _gt(Milestone.DA_CHILD, _child_ms(da=True, krbtgt=False)))
    assert card.objective_clean_stop is False
