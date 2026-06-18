"""Direct-probe (ledger-independent) ground-truth tests (eval gauge Phase 0).

Pins: a milestone vector built purely from probes; probe-less milestones are False; a raising probe
is False AND surfaced; cypher_nonempty_probe semantics; and that a probe-built GroundTruth flows
straight into C2 (so a bare model can be scored with the same gauge).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import probes as pr  # noqa: E402
import fitness  # noqa: E402
from range_state import Milestone  # noqa: E402
from scenarios import goad_scenarios  # noqa: E402


def test_vector_from_probes_and_furthest():
    scn = goad_scenarios()[1]  # child-da: FOOTHOLD..KRBTGT_DUMPED
    probe_map = {
        Milestone.GRAPH_COLLECTED: lambda: True,
        Milestone.SYSTEM_ON_HOST: lambda: True,
        Milestone.DA_CHILD: lambda: True,
        Milestone.KRBTGT_DUMPED: lambda: False,   # not reached
    }
    gt = pr.read_ground_truth_from_probes(scn, probe_map, foothold_seen=True)
    assert gt.milestones[Milestone.DA_CHILD] is True
    assert gt.milestones[Milestone.KRBTGT_DUMPED] is False
    assert gt.furthest == Milestone.DA_CHILD


def test_missing_probe_is_false():
    scn = goad_scenarios()[1]
    gt = pr.read_ground_truth_from_probes(scn, {Milestone.GRAPH_COLLECTED: lambda: True}, foothold_seen=True)
    assert gt.milestones[Milestone.GRAPH_COLLECTED] is True
    assert gt.milestones[Milestone.SYSTEM_ON_HOST] is False  # no probe supplied -> unmeasured -> False


def test_raising_probe_is_false_and_surfaced():
    scn = goad_scenarios()[1]

    def boom():
        raise RuntimeError("BloodHound unreachable")

    gt = pr.read_ground_truth_from_probes(scn, {Milestone.GRAPH_COLLECTED: boom}, foothold_seen=True)
    assert gt.milestones[Milestone.GRAPH_COLLECTED] is False
    assert Milestone.GRAPH_COLLECTED in gt.probe_disagreements  # failure is visible, not silent


def test_cypher_nonempty_probe():
    hits = pr.cypher_nonempty_probe(lambda q: ["sevenkingdoms.local"], "MATCH ...")
    empty = pr.cypher_nonempty_probe(lambda q: [], "MATCH ...")
    assert hits() is True
    assert empty() is False


def test_probe_groundtruth_flows_into_c2():
    # A bare model scored with the SAME gauge: probe-built ground truth -> C2 ScoreCard.
    scn = goad_scenarios()[1]
    gt = pr.read_ground_truth_from_probes(
        scn, {Milestone.GRAPH_COLLECTED: lambda: True, Milestone.SYSTEM_ON_HOST: lambda: True},
        foothold_seen=True,
    )
    card = fitness.score({"score": 0.0, "tool_calls": 5, "model_calls": 9, "errors": []}, gt)
    assert card.furthest_milestone == "SYSTEM_ON_HOST"
    assert card.capability == Milestone.SYSTEM_ON_HOST.value / max(m.value for m in Milestone)
