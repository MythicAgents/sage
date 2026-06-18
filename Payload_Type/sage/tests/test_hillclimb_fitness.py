"""C2 vector-fitness tests (eval gauge Phase 0).

Pins: capability comes from ground truth and orders runs correctly (ISC-11); the
Goodhart gap fires when substring eval disagrees with ground truth (ISC-8); verifier_hash
versions the gauge (ISC-9); the scalar's productivity gate rejects do-nothing runs (ISC-10).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import fitness  # noqa: E402
from range_state import Milestone, GroundTruth  # noqa: E402
from process_state import ProcessSignals  # noqa: E402


def _gt(furthest, reached_through):
    ms = {m: (m.value <= reached_through.value) for m in Milestone if m.value <= furthest.value}
    # ensure foothold floor present
    ms[Milestone.FOOTHOLD] = True
    return GroundTruth("north-da", ms, furthest)


_RUN = {"score": 0.9, "status": "pass", "tool_calls": 10, "model_calls": 20,
        "recursion_deaths": 0, "total_tokens": 1000, "wall_seconds": 5.0, "errors": []}


def test_capability_from_ground_truth_orders_runs():
    hi = fitness.score(_RUN, _gt(Milestone.KRBTGT_DUMPED, Milestone.KRBTGT_DUMPED))
    lo = fitness.score(_RUN, _gt(Milestone.GRAPH_COLLECTED, Milestone.GRAPH_COLLECTED))
    assert hi.capability > lo.capability                      # ISC-11: ground truth, not substring
    assert fitness.to_scalar(hi) > fitness.to_scalar(lo)


def test_goodhart_gap_fires_when_substring_disagrees():
    # substring eval says 0.9, ground truth barely past foothold -> large gap = Goodhart alarm
    lo = fitness.score(_RUN, _gt(Milestone.GRAPH_COLLECTED, Milestone.GRAPH_COLLECTED))
    assert lo.substring_score == 0.9
    assert lo.metric_capability_gap > 0.5


def test_productive_action_ratio_and_tradecraft_fold_in():
    proc = ProcessSignals(run_id="r1", store_path="x", total_transitions=4,
                          failure_class_counts={"ambiguous_account_name": 3, "unclassified": 1},
                          unclassified_count=1, unclassified_rate=0.25)
    card = fitness.score(_RUN, _gt(Milestone.KRBTGT_DUMPED, Milestone.KRBTGT_DUMPED), proc)
    assert card.productive_action_ratio == card.milestones_reached / 10  # reached / tool_calls
    assert card.unclassified_rate == 0.25
    assert card.failure_class_counts["ambiguous_account_name"] == 3


def test_productivity_gate_rejects_do_nothing():
    nothing = fitness.score({"score": 0.0, "tool_calls": 0, "model_calls": 1, "errors": []},
                            _gt(Milestone.FOOTHOLD, Milestone.FOOTHOLD))
    assert fitness.to_scalar(nothing) == float("-inf")   # the gate: cannot win by doing nothing


def test_verifier_hash_versions_the_gauge():
    h1 = fitness.verifier_hash(None, gauge_version="phase0-1")
    h2 = fitness.verifier_hash(None, gauge_version="phase0-2")
    assert h1 != h2 and h1.startswith("sha256:")
    # stable for the same version
    assert fitness.verifier_hash(None, "phase0-1") == h1
