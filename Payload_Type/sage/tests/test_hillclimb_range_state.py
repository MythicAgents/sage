"""C1 ground-truth milestone reader tests (eval gauge Phase 0).

Pins: (a) milestone vector from a synthetic ledger, (b) achieved-but-UNVERIFIED hops
are rejected (the wall between ground truth and the substring eval), (c) the reader
never mutates on-disk state.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import engagement_ledger  # noqa: E402
import range_state as rs  # noqa: E402

CHILD = "north.sevenkingdoms.local"
PARENT = "sevenkingdoms.local"
OBJ = "essos.local"


def _hop(technique, effect, status="achieved", evidence=None):
    return {
        "id": f"{technique}:{effect}",
        "technique": technique,
        "target": effect.split(":", 1)[-1],
        "effect": effect,
        "status": status,
        "evidence": evidence or {},
        "satisfied_effects": [effect],
        "preconditions": [],
        "source": "test",
        "timestamp": "2026-06-18T00:00:00Z",
    }


def _scenario(eid):
    return rs.Scenario(
        name="north-da",
        engagement_id=eid,
        domains={"child": CHILD, "parent": PARENT, "objective": OBJ},
    )


def _seed_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    engagement_ledger.save({"hops": [
        _hop("collect-graph", f"graph-built:{CHILD}|x", evidence={"graph_verified": True}),
        _hop("gpo-abuse", "system:winterfell", evidence={"system_command_succeeded": True}),
        _hop("golden-ticket", f"da:{CHILD}", evidence={"domain_admin": True}),
        # credential family stores the verdict directly:
        _hop("dcsync", f"krbtgt-hash:{CHILD}", evidence={"verified_on_record": True, "verify_verdict": "achieved"}),
        # achieved but NO proof -> must be rejected (the false-achieved guard):
        _hop("dcsync", f"krbtgt-hash:{PARENT}", evidence={}),
    ]}, engagement_id="run1")


def test_milestone_vector_from_synthetic_ledger(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    gt = rs.read_ground_truth(_scenario("run1"))

    assert gt.milestones[rs.Milestone.FOOTHOLD] is True
    assert gt.milestones[rs.Milestone.GRAPH_COLLECTED] is True
    assert gt.milestones[rs.Milestone.SYSTEM_ON_HOST] is True
    assert gt.milestones[rs.Milestone.DA_CHILD] is True
    assert gt.milestones[rs.Milestone.KRBTGT_DUMPED] is True
    # parent krbtgt is achieved-but-unverified -> rejected
    assert gt.milestones[rs.Milestone.DCSYNC_PARENT] is False
    assert gt.milestones[rs.Milestone.REPLICATION_RIGHTS_PARENT] is False
    assert gt.milestones[rs.Milestone.CROSS_DOMAIN_CONTEXT] is False
    assert gt.furthest == rs.Milestone.KRBTGT_DUMPED


def test_unverified_only_scores_foothold_floor(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    # All hops achieved-but-unproven -> no milestone beyond the foothold floor counts.
    engagement_ledger.save({"hops": [
        _hop("collect-graph", f"graph-built:{CHILD}|x", evidence={}),
        _hop("dcsync", f"krbtgt-hash:{CHILD}", evidence={"verify_verdict": "partial"}),
    ]}, engagement_id="run2")
    gt = rs.read_ground_truth(_scenario("run2"), foothold_seen=True)
    assert gt.milestones[rs.Milestone.GRAPH_COLLECTED] is False
    assert gt.milestones[rs.Milestone.KRBTGT_DUMPED] is False
    assert gt.furthest == rs.Milestone.FOOTHOLD


def test_missing_ledger_is_empty_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    gt = rs.read_ground_truth(_scenario("nope"), foothold_seen=False)
    assert gt.furthest == rs.Milestone.FOOTHOLD
    assert all(v is False for v in gt.milestones.values())


def test_reader_does_not_mutate_state(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    before = rs.ledger_state_hash("run1")
    rs.read_ground_truth(_scenario("run1"))
    after = rs.ledger_state_hash("run1")
    assert before == after  # ISC-4: a reader that mutates what it measures is a corrupted gauge


def test_direct_probe_disagreement_is_flagged(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    # Ledger says GRAPH_COLLECTED True; an independent probe says False -> validity alarm.
    scn = _scenario("run1")
    scn.direct_probes = {rs.Milestone.GRAPH_COLLECTED: lambda: False}
    gt = rs.read_ground_truth(scn)
    assert rs.Milestone.GRAPH_COLLECTED in gt.probe_disagreements
