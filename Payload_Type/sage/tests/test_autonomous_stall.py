"""Autonomous-solve stall detector: halt after N consecutive capability steps with no ledger progress, so a
dead/unsatisfiable hop (e.g. a repeatedly-failing dcsync) recovers instead of looping and burning tokens.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage -> ai.langgraph package
import ai.langgraph.model as model  # noqa: E402

LIMIT = model._AUTONOMOUS_STALL_LIMIT


def _bare():
    # Only the stall-counter attributes are touched; bypass the heavy Model.__init__.
    return object.__new__(model.Model)


def test_stall_halt_trips_after_no_progress():
    m = _bare()
    results = [m._autonomous_stall_halt(3) for _ in range(LIMIT)]
    assert results[:-1] == [False] * (LIMIT - 1)
    assert results[-1] is True


def test_stall_halt_resets_on_progress():
    m = _bare()
    for _ in range(LIMIT - 1):
        assert m._autonomous_stall_halt(3) is False
    assert m._autonomous_stall_halt(4) is False  # ledger grew -> reset
    results = [m._autonomous_stall_halt(4) for _ in range(LIMIT)]
    assert True not in results[:-1]
    assert results[-1] is True  # needs another full no-progress window to trip again


def test_steady_progress_never_trips():
    m = _bare()
    # capability count grows every step -> never a stall
    assert not any(m._autonomous_stall_halt(p) for p in range(1, LIMIT * 3))


def test_same_hop_no_progress_trips():
    m = _bare()
    # First call primes the hop signature (resets); then LIMIT consecutive no-progress re-selections trip.
    results = [m._autonomous_stall_halt(3, "dcsync:north") for _ in range(LIMIT + 1)]
    assert True not in results[:-1]
    assert results[-1] is True


def test_alternating_hops_never_trip():
    # Working through DISTINCT hops (no single dead re-selection) must not be flagged as a stall, even with
    # no achieved-effect growth — the B(a) false-positive guard.
    m = _bare()
    sigs = ["ensure-context", "adcs-enroll"] * (LIMIT * 2)
    assert not any(m._autonomous_stall_halt(3, s) for s in sigs)


def test_stall_report_is_self_contained():
    class _Snap:
        objective = "escalate to Domain Admin of north.sevenkingdoms.local and DCSync its krbtgt"

        def achieved_effects(self):
            return {"da:north.sevenkingdoms.local"}

    rpt = model._autonomous_stall_report(_Snap())
    assert "HALTED" in rpt
    assert "da:north.sevenkingdoms.local" in rpt
    assert "north.sevenkingdoms.local" in rpt
