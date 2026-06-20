"""Offline tests for the live driver's probe assembly: ground truth is scoped to the scenario's
milestones and read via the OUT-OF-BAND reader (never the agent callback). The live solve/baseline are
validated on the range; this pins the wiring that prevents the pollution + wrong-domain bugs.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import run_gauge_live as rgl  # noqa: E402
from range_state import Milestone  # noqa: E402


class _Scn:
    def __init__(self, subset):
        self.milestone_subset = subset


def test_build_probes_scopes_to_subset_child_da():
    """child-da scores DA_CHILD + KRBTGT but NOT OBJECTIVE -> no essos probe is built (so no essos
    query is issued and no essos referee creds are required)."""
    reader = lambda _d: set()
    scn = _Scn((Milestone.FOOTHOLD, Milestone.DA_CHILD, Milestone.KRBTGT_DUMPED))
    probes = rgl.build_probes(reader, {}, scn)
    assert Milestone.DA_CHILD in probes
    assert Milestone.KRBTGT_DUMPED in probes
    assert Milestone.OBJECTIVE not in probes


def test_build_probes_includes_objective_only_when_scored():
    reader = lambda _d: set()
    scn = _Scn((Milestone.OBJECTIVE, Milestone.KRBTGT_DUMPED))
    probes = rgl.build_probes(reader, {}, scn)
    assert Milestone.OBJECTIVE in probes
    assert Milestone.DA_CHILD not in probes


def test_build_probes_da_reads_via_reader_not_callback():
    """The DA probe must read membership via the injected out-of-band reader — proving ground truth is
    decoupled from the agent callback. An escalated reader set (vs baseline) -> probe True."""
    reader = lambda _d: {"administrator", "intruder"}
    scn = _Scn((Milestone.DA_CHILD,))
    probes = rgl.build_probes(reader, {rgl.CHILD: {"administrator"}}, scn)
    assert probes[Milestone.DA_CHILD]() is True
