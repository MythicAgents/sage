"""Typed independent-probe ground-truth tests."""
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("foothold_seen, proven", [(True, True), (False, False), (None, False)])
def test_caller_supplied_foothold_has_explicit_local_adapter_cost(foothold_seen, proven):
    scenario = SimpleNamespace(
        name="starting-callback",
        domains={"child": "north.sevenkingdoms.local"},
        milestone_subset=(Milestone.FOOTHOLD,),
    )
    truth = pr.read_ground_truth_from_probes(scenario, {}, foothold_seen=foothold_seen)
    assert truth.milestones[Milestone.FOOTHOLD] is proven
    if foothold_seen is None:
        assert Milestone.FOOTHOLD not in truth.verifier_costs
        return
    cost = truth.verifier_costs[Milestone.FOOTHOLD]
    assert cost.raw_probe_invocations == cost.control_plane_reads == 0
    assert cost.wall_seconds == 0.0
    assert cost.completeness == "complete"
    assert cost.monetary_cost_status == "not_applicable"
    assert "outside this local verifier" in cost.not_applicable_reason


def test_missing_probe_is_unmeasured_not_disproven():
    scn = goad_scenarios()[2]  # cross-forest-objective scores GRAPH_COLLECTED (child-da does not)
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


def test_fresh_positive_charges_baseline_and_terminal_call(monkeypatch):
    values = iter([False, True])
    probe = pr.registered_probe(
        Milestone.SYSTEM_ON_HOST,
        pr.fresh_positive(lambda: next(values), "system absent"),
        origin=er.ProofOrigin.MYTHIC_TASK_OUTPUT,
        proof_kind="system_identity",
    )
    pr.bind_verifier_cost(probe, control_plane_reads_per_invocation=1)

    assert probe().outcome is True
    cost = pr.verifier_cost_for(probe)
    assert cost.raw_probe_invocations == 2
    assert cost.control_plane_reads == 2
    assert cost.completeness == "complete"


def test_fallback_charges_every_attempted_candidate():
    calls = []

    def candidate(value):
        def raw():
            calls.append(value)
            return value
        return pr.registered_probe(
            Milestone.KRBTGT_DUMPED, raw,
            origin=er.ProofOrigin.MYTHIC_CREDENTIAL_STORE,
            proof_kind="krbtgt_material",
        )

    probe = pr.any_registered_probe(candidate(False), candidate(True))
    assert probe().outcome is True
    cost = pr.verifier_cost_for(probe)
    assert calls == [False, True]
    assert cost.raw_probe_invocations == 2
    assert cost.control_plane_reads is None
    assert cost.completeness == "incomplete"


def test_failure_timeout_and_limit_crossing_are_charged(monkeypatch):
    moments = iter([10.0, 12.0])
    monkeypatch.setattr(pr, "monotonic", lambda: next(moments))

    def timed_out():
        raise TimeoutError("adapter timed out")

    probe = pr.registered_probe(
        Milestone.GRAPH_COLLECTED, timed_out,
        origin=er.ProofOrigin.PAYLOAD_BLOODHOUND_FACT,
        proof_kind="graph_fact",
    )
    pr.bind_verifier_cost(
        probe,
        declared_wall_seconds=1.0,
        declared_control_plane_reads=0,
        control_plane_reads_per_invocation=1,
    )
    try:
        probe()
        assert False, "expected timeout"
    except TimeoutError:
        pass
    cost = pr.verifier_cost_for(probe)
    assert cost.raw_probe_invocations == 1
    assert cost.failed_invocations == 1
    assert cost.timeout_invocations == 1
    assert cost.wall_seconds == 2.0
    assert cost.limit_disposition == "exceeded"


def test_domain_admin_differential_charges_allow_and_deny():
    probe = pr.domain_admin_differential_probe(
        lambda: True, lambda: True, target="north", envelope_id="run",
        admin_subject="administrator@north", non_admin_subject="samwell@north",
    )
    assert probe().outcome is True
    assert pr.verifier_cost_for(probe).raw_probe_invocations == 2


def test_declared_read_bound_is_unavailable_when_adapter_reads_are_unknown():
    probe = pr.registered_probe(
        Milestone.GRAPH_COLLECTED, lambda: True,
        origin=er.ProofOrigin.PAYLOAD_BLOODHOUND_FACT, proof_kind="graph_fact",
    )
    pr.bind_verifier_cost(probe, declared_control_plane_reads=1)
    probe()
    cost = pr.verifier_cost_for(probe)
    assert cost.control_plane_reads is None
    assert cost.limit_disposition == "unavailable"


def test_crossed_wall_is_charged_then_blocks_the_next_raw_call(monkeypatch):
    moments = iter((0.0, 2.0))
    monkeypatch.setattr(pr, "monotonic", lambda: next(moments))
    calls = []
    probe = pr.registered_probe(
        Milestone.GRAPH_COLLECTED, lambda: calls.append("raw") or True,
        origin=er.ProofOrigin.PAYLOAD_BLOODHOUND_FACT, proof_kind="graph_fact",
    )
    pr.bind_verifier_cost(probe, declared_wall_seconds=1.0)
    probe()
    with pytest.raises(pr.ProbeUnavailable, match="budget exhausted"):
        probe()
    cost = pr.verifier_cost_for(probe)
    assert calls == ["raw"]
    assert cost.raw_probe_invocations == 1
    assert cost.wall_seconds == 2.0
    assert cost.limit_disposition == "exceeded"
