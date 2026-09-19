"""Assembled evaluator and universal runtime proof-boundary coverage tests."""
import asyncio
import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai" / "hillclimb"))
sys.path.insert(0, str(ROOT / "ai" / "langgraph"))

import effect_coverage as coverage  # noqa: E402
import effect_registry as er  # noqa: E402
import engagement_state as es  # noqa: E402
import proof_boundary as pb  # noqa: E402
import probes as pr  # noqa: E402
import run_gauge_live as rgl  # noqa: E402
from range_state import Milestone, MilestoneSpec, Scenario  # noqa: E402
from scenarios import laps_family_transfer_holdout_scenarios  # noqa: E402

# ``run_gauge_live`` may be imported through its package path by adjacent
# evaluator modules.  Use its exact registry/probe identities so the assembled
# objects reach their real consumer rather than a duplicate script-import class.
er = rgl.effect_registry
pr = rgl.probes_mod


CHILD = "north.sevenkingdoms.local"
OBJECTIVE = "essos.local"
NOW = "2026-09-18T00:00:00+00:00"


def _scenario(*milestones, objective_domain=OBJECTIVE, remote=False):
    prefixes = ("remote-exec:",) if remote else ("krbtgt-hash:",)
    return Scenario(
        name="proof-coverage",
        engagement_id="offline",
        domains={"child": CHILD, "parent": "sevenkingdoms.local", "objective": objective_domain},
        milestone_subset=tuple(milestones),
        spec_overrides={Milestone.OBJECTIVE: MilestoneSpec(prefixes, domain_role="objective")},
    )


def _transition(final: bool):
    values = iter((False, final))
    return lambda: next(values)


def _admit(milestone, probe, expected):
    return er.admit_observation(milestone, probe(), expected_target=expected)


def test_general_build_probe_factory_inventory_is_exact_and_rejects_planted_producers():
    tree = ast.parse((ROOT / "ai" / "hillclimb" / "run_gauge_live.py").read_text(encoding="utf-8"))
    build = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_probes")
    names = {
        node.func.attr for node in ast.walk(build)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "live_seams"
    }
    assert coverage.validate_general_probe_factories(names) == coverage.GENERAL_BUILD_PROBE_FACTORIES
    for planted in ("sage_local_artifact", "sage_host_socket", "unknown_factory"):
        with pytest.raises(ValueError, match="inventory mismatch"):
            coverage.validate_general_probe_factories((*names, planted))


def test_build_probes_uses_typed_positive_factories_and_preserves_target_binding(monkeypatch):
    calls = []

    def system_probe(*, realm, **_kwargs):
        calls.append(("system", realm))
        return _transition(True)

    def graph_probe(**_kwargs):
        calls.append(("graph", ""))
        return _transition(True)

    def credential_probe(account, *, realm, **_kwargs):
        calls.append(("credential", account, realm))
        return _transition(True)

    monkeypatch.setattr(rgl.live_seams, "system_execution_probe", system_probe)
    monkeypatch.setattr(rgl.live_seams, "graph_collected_probe", graph_probe)
    monkeypatch.setattr(rgl.live_seams, "mythic_credential_probe", credential_probe)
    monkeypatch.setattr(rgl.live_seams, "_krbtgt_task_output_has_material", lambda **_kwargs: False)
    scn = _scenario(Milestone.SYSTEM_ON_HOST, Milestone.GRAPH_COLLECTED, Milestone.KRBTGT_DUMPED)
    probes = rgl.build_probes(lambda _domain: set(), {}, scn)

    system_result = _admit(Milestone.SYSTEM_ON_HOST, probes[Milestone.SYSTEM_ON_HOST], CHILD)
    assert system_result.disposition is er.ProofDisposition.PROVEN
    assert _admit(Milestone.GRAPH_COLLECTED, probes[Milestone.GRAPH_COLLECTED], "").disposition is er.ProofDisposition.PROVEN
    assert _admit(Milestone.KRBTGT_DUMPED, probes[Milestone.KRBTGT_DUMPED], CHILD).disposition is er.ProofDisposition.PROVEN
    assert ("system", CHILD) in calls
    assert ("credential", "krbtgt", CHILD) in calls
    # A correctly built observation is still rejected if a consumer expects a near target.
    assert er.admit_observation(
        Milestone.SYSTEM_ON_HOST, system_result.observation, expected_target="near-target.invalid"
    ).disposition is er.ProofDisposition.INVALID


def test_build_probes_negative_and_declared_unproduced_paths_remain_unmeasured(monkeypatch):
    monkeypatch.setattr(rgl.live_seams, "system_execution_probe", lambda **_kwargs: _transition(False))
    scn = _scenario(Milestone.SYSTEM_ON_HOST, Milestone.DA_CHILD)
    probes = rgl.build_probes(lambda _domain: set(), {}, scn)
    with pytest.raises(pr.ProbeUnavailable):
        probes[Milestone.SYSTEM_ON_HOST]()
    with pytest.raises(pr.ProbeUnavailable, match="matched admin/non-admin"):
        probes[Milestone.DA_CHILD]()


def test_parent_dcsync_assembled_credential_and_task_paths_are_exact_realm_and_typed(monkeypatch):
    parent = "sevenkingdoms.local"
    credential_calls = []

    def credential_probe(account, *, realm, **_kwargs):
        credential_calls.append((account, realm))
        return _transition(True)

    monkeypatch.setattr(rgl.live_seams, "mythic_credential_probe", credential_probe)
    monkeypatch.setattr(rgl.live_seams, "_krbtgt_task_output_has_material", lambda **_kwargs: False)
    probes = rgl.build_probes(lambda _domain: set(), {}, _scenario(Milestone.DCSYNC_PARENT))
    credential = _admit(Milestone.DCSYNC_PARENT, probes[Milestone.DCSYNC_PARENT], parent)
    assert credential.disposition is er.ProofDisposition.PROVEN
    assert credential.observation.origin is er.ProofOrigin.MYTHIC_CREDENTIAL_STORE
    assert credential.observation.proof_kind == "dcsync_material"
    assert credential_calls == [("krbtgt", parent)]

    raw_calls = []
    monkeypatch.setattr(rgl.live_seams, "mythic_credential_probe", lambda *_args, **_kwargs: _transition(False))

    def task_probe(*, realm):
        raw_calls.append(realm)
        return next(task_values)

    task_values = iter((False, True))
    monkeypatch.setattr(rgl.live_seams, "_krbtgt_task_output_has_material", task_probe)
    probes = rgl.build_probes(lambda _domain: set(), {}, _scenario(Milestone.DCSYNC_PARENT))
    task = _admit(Milestone.DCSYNC_PARENT, probes[Milestone.DCSYNC_PARENT], parent)
    assert task.disposition is er.ProofDisposition.PROVEN
    assert task.observation.origin is er.ProofOrigin.MYTHIC_TASK_OUTPUT
    assert task.observation.proof_kind == "dcsync_material"
    assert raw_calls == [parent, parent]


def test_parent_dcsync_rejects_stale_or_missing_material_and_suffix_realm(monkeypatch):
    parent = "sevenkingdoms.local"
    # A pre-existing positive is the fresh-positive baseline, so it cannot become
    # a new parent DCSync proof.  The fallback also has no current material.
    monkeypatch.setattr(rgl.live_seams, "mythic_credential_probe", lambda *_args, **_kwargs: (lambda: True))
    monkeypatch.setattr(rgl.live_seams, "_krbtgt_task_output_has_material", lambda **_kwargs: False)
    probes = rgl.build_probes(lambda _domain: set(), {}, _scenario(Milestone.DCSYNC_PARENT))
    with pytest.raises(pr.ProbeUnavailable):
        probes[Milestone.DCSYNC_PARENT]()

    monkeypatch.setattr(
        rgl.live_seams, "_fetch_krbtgt_dcsync_task_outputs",
        lambda **_kwargs: [{"realm": "evil.sevenkingdoms.local", "output": "material"}],
    )
    monkeypatch.setattr(
        rgl.live_seams, "_extract_credential_material",
        lambda output, **_kwargs: [{"hash": "x"}] if output == "material" else [],
    )
    assert rgl.live_seams._krbtgt_task_output_has_material(realm=parent) is False
    monkeypatch.setattr(
        rgl.live_seams, "_fetch_krbtgt_dcsync_task_outputs",
        lambda **_kwargs: [{"realm": parent, "output": "missing"}],
    )
    assert rgl.live_seams._krbtgt_task_output_has_material(realm=parent) is False


def test_parent_dcsync_coverage_does_not_promote_generic_account_dcsync():
    rows = [row for row in coverage.EFFECT_COVERAGE if row.milestone == "DCSYNC_PARENT"]
    assert any(row.name == "dcsync-krbtgt" and row.status is coverage.CoverageStatus.SUPPORTED for row in rows)
    assert any(row.name == "dcsync-account" and row.milestone == "ACCOUNT_CREDENTIAL_MATERIAL"
               and row.status is coverage.CoverageStatus.SUPPORTED for row in coverage.EFFECT_COVERAGE)
    assert not any(row.name == "dcsync-account" for row in rows)


def test_remote_exec_objective_assembled_probe_requires_fresh_target_matched_callback(monkeypatch):
    calls = []

    def remote_probe(host, *, realm, **_kwargs):
        calls.append((host, realm))
        return _transition(True)

    monkeypatch.setattr(rgl.live_seams, "remote_execution_probe", remote_probe)
    scn = laps_family_transfer_holdout_scenarios("offline")[0]
    probes = rgl.build_probes(lambda _domain: set(), {}, scn)
    observation = probes[Milestone.OBJECTIVE]()
    target = scn.domains["objective"]
    assert er.admit_observation(Milestone.OBJECTIVE, observation, expected_target=target).disposition is er.ProofDisposition.PROVEN
    assert calls and calls[0][1] == target.casefold()


def test_remote_assembly_charges_eager_callback_baseline_and_binds_whole_operation_ceiling(monkeypatch):
    scn = laps_family_transfer_holdout_scenarios("offline")[0]
    target_host, target_domain = rgl._objective_remote_exec_targets(scn)[0]
    target_domain = target_domain or scn.domains["objective"]
    rows = iter((
        [{"display_id": 1, "host": "old", "domain": "old.local"}],
        [{"display_id": 1, "host": "old", "domain": "old.local"}],
        [{"display_id": 7, "host": target_host, "domain": target_domain, "user": "system"}],
    ))
    monkeypatch.setattr(rgl.live_seams, "_fetch_callbacks_for_probe", lambda **_kwargs: next(rows))
    probe = rgl.build_probes(lambda _domain: set(), {}, scn)[Milestone.OBJECTIVE]
    assert probe().outcome is True
    cost = rgl.probes_mod.verifier_cost_for(probe)
    assert cost.raw_probe_invocations == 3
    assert cost.declared_wall_seconds == 180.0
    assert cost.control_plane_reads is None
    assert cost.limit_disposition == "within"


def test_assembled_probe_limits_match_existing_sixty_second_composition(monkeypatch):
    monkeypatch.setattr(rgl.live_seams, "system_execution_probe", lambda **_kwargs: (lambda: False))
    monkeypatch.setattr(rgl.live_seams, "graph_collected_probe", lambda **_kwargs: (lambda: False))
    monkeypatch.setattr(rgl.live_seams, "mythic_credential_probe", lambda *_args, **_kwargs: (lambda: False))
    monkeypatch.setattr(rgl.live_seams, "_krbtgt_task_output_has_material", lambda **_kwargs: False)
    scn = _scenario(
        Milestone.SYSTEM_ON_HOST, Milestone.GRAPH_COLLECTED, Milestone.KRBTGT_DUMPED,
        Milestone.DCSYNC_PARENT, Milestone.DA_CHILD, Milestone.OBJECTIVE,
    )
    probes = rgl.build_probes(lambda _domain: set(), {}, scn)
    assert {
        milestone.name: rgl.probes_mod.verifier_cost_for(probe).declared_wall_seconds
        for milestone, probe in probes.items()
    } == {
        "SYSTEM_ON_HOST": 120.0, "GRAPH_COLLECTED": 120.0,
        "KRBTGT_DUMPED": 240.0, "DCSYNC_PARENT": 240.0,
        "DA_CHILD": 0.0, "OBJECTIVE": 240.0,
    }


def test_probe_read_timeout_covers_a_never_ending_login_without_network(monkeypatch):
    async def never_login():
        await asyncio.Event().wait()

    monkeypatch.setattr(rgl.live_seams, "_mythic_login_async_safe", never_login)
    assert rgl.live_seams._fetch_credentials_for_probe(timeout=0.001) == []


@pytest.mark.parametrize("seen, expected", [(True, er.ProofDisposition.PROVEN), (False, er.ProofDisposition.DISPROVEN), (None, er.ProofDisposition.UNMEASURED)])
def test_foothold_is_only_the_typed_starting_callback_consumer(seen, expected):
    scn = _scenario(Milestone.FOOTHOLD)
    gt = pr.read_ground_truth_from_probes(scn, {}, foothold_seen=seen)
    assert gt.proof_results[Milestone.FOOTHOLD].disposition is expected


@pytest.mark.parametrize("value", [True, {"passes": True}, {}, {"effect": "krbtgt-hash:claimed"}])
def test_self_report_generated_passes_and_empty_probe_maps_never_admit(value):
    for milestone in (Milestone.GRAPH_COLLECTED, Milestone.SYSTEM_ON_HOST, Milestone.KRBTGT_DUMPED, Milestone.OBJECTIVE):
        assert er.admit_observation(milestone, value).disposition is er.ProofDisposition.INVALID
    gt = pr.read_ground_truth_from_probes(_scenario(Milestone.GRAPH_COLLECTED), {}, foothold_seen=None)
    assert gt.proof_results[Milestone.GRAPH_COLLECTED].disposition is er.ProofDisposition.UNMEASURED


@pytest.mark.parametrize("origin", ["sage_local_artifact", "sage_host_socket", "model_self_report"])
def test_planted_local_evidence_is_rejected_by_assembled_ground_truth_reader(tmp_path, monkeypatch, origin):
    """Exercise the consumer, with mutation sensitivity, without any socket IO.

    The source is accurately labeled. Arbitrary trusted evaluator code that lies
    about its origin is a different threat from untrusted proof data.
    """
    from dataclasses import replace
    from ai.hillclimb import range_state as reader
    registry = reader.effect_registry
    milestone = reader.Milestone.SYSTEM_ON_HOST
    planted = tmp_path / "local-success.txt"
    planted.write_text("true")
    oracle = registry.contract_for(milestone)
    def local_source():
        return registry.OracleObservation(
            registry_version=registry.REGISTRY_VERSION, oracle_id=oracle.oracle_id,
            origin=origin, proof_kind="system_identity", outcome=planted.read_text() == "true",
            observation_id="planted-local-predicate", target=CHILD)
    scenario = reader.Scenario(name="planted", engagement_id="offline", domains={"child": CHILD},
                               milestone_subset=(milestone,), direct_probes={milestone: local_source})
    monkeypatch.setattr(reader.engagement_ledger, "load", lambda *a, **k: {})
    def assert_rejected():
        result = reader.read_ground_truth(scenario, run_live_probes=True)
        assert result.milestones[milestone] is False
        assert result.proof_results[milestone].disposition is registry.ProofDisposition.INVALID
    assert_rejected()
    # Deliberately corrupt source classification at the guard boundary. The
    # same rejection assertion must go red; no production guard is disabled.
    original = registry.admit_observation
    monkeypatch.setattr(registry, "admit_observation", lambda effect, observation, **kw:
                        original(effect, replace(observation, origin=registry.ProofOrigin.MYTHIC_TASK_OUTPUT), **kw))
    with pytest.raises(AssertionError):
        assert_rejected()


def _disallowed_envelope(origin):
    return pb.ProofEnvelope(
        scope=pb.RUNTIME_SCOPE, origin=origin, engagement_id="proof-coverage", callback_id="13", task_id="1",
        terminal_status="completed", command="test", verifier_id="test:proof", verifier_hash=pb.stable_verifier_hash("test:proof"),
        verifier_input_sha256=pb.canonical_commitment_sha256({"input": True}),
        verifier_result_sha256=pb.canonical_commitment_sha256({"result": True}),
        transaction_id="tx-1", captured_at=NOW,
    ).to_dict()


@pytest.mark.parametrize("origin", sorted(pb.DISALLOWED_RUNTIME_ORIGINS))
def test_runtime_record_effect_result_and_achieved_effects_reject_disallowed_origins_for_every_coverage_row(origin):
    state = es.EngagementState(objective="proof coverage", engagement_id="proof-coverage", runtime_scope=True)
    for index, row in enumerate(coverage.EFFECT_COVERAGE):
        effect = re.sub(r"[^a-z0-9]+", "-", row.effect_family.casefold()).strip("-") + f":{index}"
        state = es.record_effect_result(
            state, f"coverage:{row.name}", f"target-{index}", effect, "achieved",
            {"proof_envelope": _disallowed_envelope(origin)}, NOW,
        )
    assert state.achieved_effects() == set()
    assert all(hop.status == "legacy_unverified" for hop in state.hops)
