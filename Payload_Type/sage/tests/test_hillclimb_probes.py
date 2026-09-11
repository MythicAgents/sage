"""Typed independent-probe ground-truth tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import probes as pr  # noqa: E402
import effect_registry as er  # noqa: E402
import fitness  # noqa: E402
from range_state import Milestone  # noqa: E402
from scenarios import goad_scenarios  # noqa: E402


def test_vector_from_probes_and_furthest():
    scn = goad_scenarios()[1]  # child-da: FOOTHOLD..KRBTGT_DUMPED
    probe_map = {
        Milestone.SYSTEM_ON_HOST: pr.registered_probe(
            Milestone.SYSTEM_ON_HOST, lambda: True,
            origin=er.ProofOrigin.MYTHIC_TASK_OUTPUT, proof_kind="system_identity", target=scn.domains["child"],
        ),
        Milestone.DA_CHILD: pr.domain_admin_differential_probe(
            lambda: True, lambda: True, target=scn.domains["child"], envelope_id="run-1",
            admin_subject=f"administrator@{scn.domains['child']}",
            non_admin_subject=f"samwell@{scn.domains['child']}",
        ),
        Milestone.KRBTGT_DUMPED: pr.registered_probe(
            Milestone.KRBTGT_DUMPED, lambda: False,
            origin=er.ProofOrigin.MYTHIC_CREDENTIAL_STORE, proof_kind="krbtgt_material",
            target=scn.domains["child"],
        ),
    }
    gt = pr.read_ground_truth_from_probes(scn, probe_map, foothold_seen=True)
    assert gt.milestones[Milestone.DA_CHILD] is True
    assert gt.milestones[Milestone.KRBTGT_DUMPED] is False
    assert gt.furthest == Milestone.DA_CHILD


def test_missing_probe_is_unmeasured_not_disproven():
    scn = goad_scenarios()[2]  # cross-forest-objective still scores GRAPH_COLLECTED (child-da no longer does)
    graph = pr.registered_probe(
        Milestone.GRAPH_COLLECTED, lambda: True,
        origin=er.ProofOrigin.PAYLOAD_BLOODHOUND_FACT, proof_kind="graph_fact",
    )
    gt = pr.read_ground_truth_from_probes(scn, {Milestone.GRAPH_COLLECTED: graph}, foothold_seen=True)
    assert gt.milestones[Milestone.GRAPH_COLLECTED] is True
    assert gt.milestones[Milestone.SYSTEM_ON_HOST] is False
    assert gt.proof_results[Milestone.SYSTEM_ON_HOST].disposition is er.ProofDisposition.UNMEASURED


def test_raising_probe_is_false_and_surfaced():
    scn = goad_scenarios()[2]  # cross-forest-objective still scores GRAPH_COLLECTED

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
        scn, {Milestone.SYSTEM_ON_HOST: pr.registered_probe(
            Milestone.SYSTEM_ON_HOST, lambda: True,
            origin=er.ProofOrigin.MYTHIC_TASK_OUTPUT, proof_kind="system_identity", target=scn.domains["child"],
        )},
        foothold_seen=True,
    )
    card = fitness.score({"score": 0.0, "tool_calls": 5, "model_calls": 9, "errors": []}, gt)
    assert card.furthest_milestone == "SYSTEM_ON_HOST"
    assert card.capability == Milestone.SYSTEM_ON_HOST.value / max(m.value for m in Milestone)
    assert card.proof_dispositions["SYSTEM_ON_HOST"] == "proven"
    assert card.proof_dispositions["KRBTGT_DUMPED"] == "unmeasured"
