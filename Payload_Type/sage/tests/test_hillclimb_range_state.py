"""Independent recorded/live ground-truth reader tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import engagement_ledger  # noqa: E402
import effect_registry as er  # noqa: E402
import probes as pr  # noqa: E402
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


def test_sage_hops_and_verify_verdicts_cannot_award_ground_truth(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    gt = rs.read_ground_truth(_scenario("run1"))

    assert all(value is False for value in gt.milestones.values())
    assert all(result.disposition is er.ProofDisposition.UNMEASURED
               for result in gt.proof_results.values())
    assert gt.furthest == rs.Milestone.FOOTHOLD


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


def test_reader_accounts_caller_supplied_foothold_as_zero_local_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    scenario = rs.Scenario(
        name="foothold-only", engagement_id="none", domains={"child": CHILD},
        milestone_subset=(rs.Milestone.FOOTHOLD,),
    )
    proven = rs.read_ground_truth(scenario, foothold_seen=True)
    disproven = rs.read_ground_truth(scenario, foothold_seen=False)
    unknown = rs.read_ground_truth(scenario, foothold_seen=None)
    for truth, expected in ((proven, True), (disproven, False)):
        assert truth.milestones[rs.Milestone.FOOTHOLD] is expected
        cost = truth.verifier_costs[rs.Milestone.FOOTHOLD]
        assert (cost.raw_probe_invocations, cost.control_plane_reads, cost.wall_seconds) == (0, 0, 0.0)
        assert cost.completeness == "complete"
        assert cost.monetary_cost_status == "not_applicable"
        assert "outside this local verifier" in cost.not_applicable_reason
    assert rs.Milestone.FOOTHOLD not in unknown.verifier_costs


def test_reader_does_not_mutate_state(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    before = rs.ledger_state_hash("run1")
    rs.read_ground_truth(_scenario("run1"))
    after = rs.ledger_state_hash("run1")
    assert before == after  # ISC-4: a reader that mutates what it measures is a corrupted gauge


def test_typed_direct_negative_is_measured_disproven(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    scn = _scenario("run1")
    scn.direct_probes = {rs.Milestone.GRAPH_COLLECTED: pr.registered_probe(
        rs.Milestone.GRAPH_COLLECTED,
        lambda: False,
        origin=er.ProofOrigin.PAYLOAD_BLOODHOUND_FACT,
        proof_kind="graph_fact",
    )}
    gt = rs.read_ground_truth(scn, run_live_probes=True)
    assert gt.proof_results[rs.Milestone.GRAPH_COLLECTED].disposition is er.ProofDisposition.DISPROVEN
    assert rs.Milestone.GRAPH_COLLECTED not in gt.probe_disagreements


def test_plain_live_callable_has_outer_timing_but_unknown_reads(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    scn = _scenario("run1")
    scn.direct_probes = {rs.Milestone.GRAPH_COLLECTED: lambda: True}
    gt = rs.read_ground_truth(scn, run_live_probes=True)
    cost = gt.verifier_costs[rs.Milestone.GRAPH_COLLECTED]
    assert cost.raw_probe_invocations == 1
    assert cost.control_plane_reads is None
    assert cost.completeness == "incomplete"


def test_probeable_milestone_without_verifier_is_flagged_offline(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    scn = rs.Scenario(
        name="graph-without-verifier",
        engagement_id="run1",
        domains={"child": CHILD, "parent": PARENT, "objective": OBJ},
        milestone_subset=(rs.Milestone.FOOTHOLD, rs.Milestone.GRAPH_COLLECTED),
    )
    gt = rs.read_ground_truth(scn, run_live_probes=False)
    assert rs.Milestone.GRAPH_COLLECTED in gt.probe_disagreements


def test_wired_probe_is_not_executed_when_live_probes_disabled(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)

    def raising_probe():
        raise AssertionError("probe should not execute on offline path")

    scn = rs.Scenario(
        name="graph-offline",
        engagement_id="run1",
        domains={"child": CHILD, "parent": PARENT, "objective": OBJ},
        milestone_subset=(rs.Milestone.FOOTHOLD, rs.Milestone.GRAPH_COLLECTED),
        direct_probes={rs.Milestone.GRAPH_COLLECTED: raising_probe},
    )
    gt = rs.read_ground_truth(scn, run_live_probes=False)
    assert rs.Milestone.GRAPH_COLLECTED in gt.probe_disagreements
    assert gt.proof_results[rs.Milestone.GRAPH_COLLECTED].disposition is er.ProofDisposition.UNMEASURED


def test_recorded_probe_is_revalidated_and_ignores_conflicting_self_report(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    data = engagement_ledger.load("run1")
    contract = er.contract_for(rs.Milestone.KRBTGT_DUMPED)
    observation = er.OracleObservation(
        registry_version=er.REGISTRY_VERSION,
        oracle_id=contract.oracle_id,
        origin=er.ProofOrigin.MYTHIC_CREDENTIAL_STORE,
        proof_kind="krbtgt_material",
        outcome=False,
        observation_id="capture-1",
        target=CHILD,
    )
    data["ground_truth_probes"] = {
        "KRBTGT_DUMPED": er.admit_observation(
            rs.Milestone.KRBTGT_DUMPED, observation, expected_target=CHILD
        ).to_record(),
        "_captured_at": "2026-06-21T00:00:00+00:00",
        "_source": "independent_evaluator",
        "_registry_version": er.REGISTRY_VERSION,
    }
    engagement_ledger.save(data, engagement_id="run1")

    scn = rs.Scenario(
        name="recorded-krbtgt",
        engagement_id="run1",
        domains={"child": CHILD, "parent": PARENT, "objective": OBJ},
        milestone_subset=(rs.Milestone.FOOTHOLD, rs.Milestone.KRBTGT_DUMPED),
        recorded_probe_milestones=frozenset({rs.Milestone.KRBTGT_DUMPED}),
    )
    gt = rs.read_ground_truth(scn, run_live_probes=False)
    assert gt.milestones[rs.Milestone.KRBTGT_DUMPED] is False
    assert gt.proof_results[rs.Milestone.KRBTGT_DUMPED].disposition is er.ProofDisposition.DISPROVEN
    assert rs.Milestone.KRBTGT_DUMPED not in gt.probe_disagreements


def test_legacy_boolean_record_is_invalid_not_upgraded(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    data = engagement_ledger.load("run1")
    data["ground_truth_probes"] = {"KRBTGT_DUMPED": True}
    engagement_ledger.save(data, engagement_id="run1")
    scn = rs.Scenario(
        name="legacy",
        engagement_id="run1",
        domains={"child": CHILD},
        milestone_subset=(rs.Milestone.KRBTGT_DUMPED,),
    )
    gt = rs.read_ground_truth(scn)
    assert gt.milestones[rs.Milestone.KRBTGT_DUMPED] is False
    assert gt.proof_results[rs.Milestone.KRBTGT_DUMPED].disposition is er.ProofDisposition.INVALID


def test_recorded_probe_declaration_without_record_stays_unmeasured(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    scn = rs.Scenario(
        name="recorded-krbtgt-missing",
        engagement_id="run1",
        domains={"child": CHILD, "parent": PARENT, "objective": OBJ},
        milestone_subset=(rs.Milestone.FOOTHOLD, rs.Milestone.KRBTGT_DUMPED),
        recorded_probe_milestones=frozenset({rs.Milestone.KRBTGT_DUMPED}),
    )
    gt = rs.read_ground_truth(scn, run_live_probes=False)
    assert gt.milestones[rs.Milestone.KRBTGT_DUMPED] is False
    assert gt.proof_results[rs.Milestone.KRBTGT_DUMPED].disposition is er.ProofDisposition.UNMEASURED
    assert rs.Milestone.KRBTGT_DUMPED in gt.probe_disagreements


def test_legacy_recorded_probe_cost_is_unknown_not_zero(tmp_path, monkeypatch):
    _seed_ledger(tmp_path, monkeypatch)
    data = engagement_ledger.load("run1")
    contract = er.contract_for(rs.Milestone.KRBTGT_DUMPED)
    observation = er.OracleObservation(
        registry_version=er.REGISTRY_VERSION, oracle_id=contract.oracle_id,
        origin=er.ProofOrigin.MYTHIC_CREDENTIAL_STORE, proof_kind="krbtgt_material",
        outcome=False, observation_id="legacy-cost", target=CHILD,
    )
    data["ground_truth_probes"] = {
        "KRBTGT_DUMPED": er.admit_observation(
            rs.Milestone.KRBTGT_DUMPED, observation, expected_target=CHILD
        ).to_record(),
        "_captured_at": "2026-06-21T00:00:00+00:00",
        "_source": "independent_evaluator", "_registry_version": er.REGISTRY_VERSION,
    }
    engagement_ledger.save(data, engagement_id="run1")
    scn = rs.Scenario(
        name="legacy-cost", engagement_id="run1", domains={"child": CHILD},
        milestone_subset=(rs.Milestone.KRBTGT_DUMPED,),
        recorded_probe_milestones=frozenset({rs.Milestone.KRBTGT_DUMPED}),
    )
    cost = rs.read_ground_truth(scn).verifier_costs[rs.Milestone.KRBTGT_DUMPED]
    assert cost.monetary_cost_usd is None
    assert cost.monetary_cost_status == "legacy_unknown"
    assert cost.completeness == "legacy_unknown"
