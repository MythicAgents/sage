import dataclasses
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import effect_registry as er  # noqa: E402
import engagement_ledger  # noqa: E402
import fitness  # noqa: E402
import live_runner  # noqa: E402
import probes  # noqa: E402
import range_state  # noqa: E402
from range_state import Milestone  # noqa: E402
from scenarios import all_scenarios  # noqa: E402


def _observation(milestone, **changes):
    contract = er.contract_for(milestone)
    values = {
        "registry_version": er.REGISTRY_VERSION,
        "oracle_id": contract.oracle_id,
        "origin": next(iter(contract.allowed_origins)),
        "proof_kind": next(iter(contract.proof_kinds)),
        "outcome": True,
        "observation_id": "observation-1",
        "target": "branch.local" if contract.target_role else "",
    }
    values.update(changes)
    return er.OracleObservation(**values)


def test_registry_covers_every_milestone_exactly_once():
    registered = er.validate_registry()
    assert set(registered) == {milestone.name for milestone in Milestone}
    duplicate = dataclasses.replace(er.REGISTRY[0], oracle_id="another.oracle")
    try:
        er.validate_registry((*er.REGISTRY, duplicate))
        assert False, "duplicate milestone must fail"
    except ValueError as exc:
        assert "duplicate milestone" in str(exc)
    try:
        er.contract_for("UNKNOWN_EFFECT")
        assert False, "unknown milestones must fail"
    except KeyError:
        pass


def test_all_current_scenarios_resolve_scored_milestones_without_self_report_bypass():
    for scenario in all_scenarios("Operation_Test"):
        scored = scenario.milestone_subset or tuple(Milestone)
        for milestone in scored:
            assert er.contract_for(milestone).milestone == milestone.name
        assert not scenario.self_report_exempt


def test_untyped_self_report_and_non_allowlisted_origin_are_invalid():
    assert er.admit_observation(Milestone.SYSTEM_ON_HOST, True).disposition is er.ProofDisposition.INVALID
    claimed = _observation(
        Milestone.SYSTEM_ON_HOST,
        origin="model_self_report",
        proof_kind="system_identity",
    )
    assert er.admit_observation(
        Milestone.SYSTEM_ON_HOST, claimed, expected_target="branch.local"
    ).disposition is er.ProofDisposition.INVALID


def test_false_is_disproven_but_missing_identity_and_wrong_version_are_invalid():
    false_result = er.admit_observation(
        Milestone.SYSTEM_ON_HOST,
        _observation(Milestone.SYSTEM_ON_HOST, outcome=False, proof_kind="system_identity"),
        expected_target="branch.local",
    )
    assert false_result.disposition is er.ProofDisposition.DISPROVEN
    assert er.admit_observation(
        Milestone.SYSTEM_ON_HOST,
        _observation(Milestone.SYSTEM_ON_HOST, observation_id="", proof_kind="system_identity"),
        expected_target="branch.local",
    ).disposition is er.ProofDisposition.INVALID
    assert er.admit_observation(
        Milestone.SYSTEM_ON_HOST,
        _observation(Milestone.SYSTEM_ON_HOST, registry_version="old", proof_kind="system_identity"),
        expected_target="branch.local",
    ).disposition is er.ProofDisposition.INVALID


def test_domain_admin_requires_matched_allow_deny_differential():
    base = _observation(
        Milestone.DA_CHILD,
        origin=er.ProofOrigin.MYTHIC_TASK_OUTPUT,
        proof_kind="domain_admin_differential",
        subject="administrator@branch.local",
        envelope_id="run-7",
        negative_control_denied=True,
        control_target="branch.local",
        control_subject="samwell@branch.local",
        control_envelope_id="run-7",
    )
    assert er.admit_observation(
        Milestone.DA_CHILD, base, expected_target="branch.local"
    ).disposition is er.ProofDisposition.PROVEN
    mutations = (
        {"negative_control_denied": None},
        {"negative_control_denied": False},
        {"control_target": "other.local"},
        {"control_envelope_id": "run-8"},
        {"control_subject": "administrator@branch.local"},
        {"control_subject": "samwell@zeta.branch.local"},
    )
    for changes in mutations:
        result = er.admit_observation(
            Milestone.DA_CHILD,
            dataclasses.replace(base, **changes),
            expected_target="branch.local",
        )
        assert result.disposition is er.ProofDisposition.INVALID


@pytest.mark.parametrize("control_subject", [
    "samwell@north.sevenkingdoms.local",
    "administrator@north.sevenkingdoms.local",
    "NORTH.SEVENKINGDOMS.LOCAL\\Administrator",
])
def test_objective_domain_admin_differential_allows_cross_domain_negative_control(control_subject):
    observation = _observation(
        Milestone.OBJECTIVE,
        origin=er.ProofOrigin.MYTHIC_TASK_OUTPUT,
        proof_kind="domain_admin_differential",
        target="essos.local",
        subject="administrator@essos.local",
        envelope_id="operation-8-domain-sacl",
        negative_control_denied=True,
        control_target="essos.local",
        control_subject=control_subject,
        control_envelope_id="operation-8-domain-sacl",
    )

    result = er.admit_observation(
        Milestone.OBJECTIVE,
        observation,
        expected_target="essos.local",
    )

    assert result.disposition is er.ProofDisposition.PROVEN


def test_objective_domain_admin_differential_rejects_different_control_target():
    observation = _observation(
        Milestone.OBJECTIVE,
        origin=er.ProofOrigin.MYTHIC_TASK_OUTPUT,
        proof_kind="domain_admin_differential",
        target="essos.local",
        subject="administrator@essos.local",
        envelope_id="operation-8-domain-sacl",
        negative_control_denied=True,
        control_target="north.sevenkingdoms.local",
        control_subject="samwell@north.sevenkingdoms.local",
        control_envelope_id="operation-8-domain-sacl",
    )

    result = er.admit_observation(
        Milestone.OBJECTIVE,
        observation,
        expected_target="essos.local",
    )

    assert result.disposition is er.ProofDisposition.INVALID
    assert result.reason == "domain-admin control target mismatch"


@pytest.mark.parametrize("control_subject", [
    "ADMINISTRATOR@ESSOS.LOCAL.", r"essos.local\Administrator", "administrator", "",
])
def test_objective_differential_rejects_same_or_unqualified_principal(control_subject):
    observation = _observation(
        Milestone.OBJECTIVE, origin=er.ProofOrigin.MYTHIC_TASK_OUTPUT,
        proof_kind="domain_admin_differential", target="essos.local",
        subject="administrator@essos.local", envelope_id="task-54",
        negative_control_denied=True, control_target="essos.local",
        control_subject=control_subject, control_envelope_id="task-54",
    )
    assert er.admit_observation(
        Milestone.OBJECTIVE, observation, expected_target="essos.local",
    ).disposition is er.ProofDisposition.INVALID


def test_exact_binding_accepts_case_only_and_rejects_suffix_collision_class():
    case_only = _observation(
        Milestone.SYSTEM_ON_HOST,
        proof_kind="system_identity",
        target="BRANCH.LOCAL.",
        subject="NORTH\\Samwell",
    )
    assert er.admit_observation(
        Milestone.SYSTEM_ON_HOST,
        case_only,
        expected_target="branch.local",
        expected_subject="north\\samwell",
    ).disposition is er.ProofDisposition.PROVEN
    for prefix in ("zeta", "north", "a", "branch"):
        collision = dataclasses.replace(case_only, target=f"{prefix}.branch.local")
        assert er.admit_observation(
            Milestone.SYSTEM_ON_HOST, collision, expected_target="branch.local"
        ).disposition is er.ProofDisposition.INVALID
        identity_collision = dataclasses.replace(case_only, subject=f"samwell@{prefix}.branch.local")
        assert er.admit_observation(
            Milestone.SYSTEM_ON_HOST,
            identity_collision,
            expected_target="branch.local",
            expected_subject="samwell@branch.local",
        ).disposition is er.ProofDisposition.INVALID


def test_replay_revalidates_observation_instead_of_trusting_claimed_disposition():
    observation = _observation(
        Milestone.SYSTEM_ON_HOST,
        proof_kind="system_identity",
        outcome=False,
    )
    record = er.admit_observation(
        Milestone.SYSTEM_ON_HOST, observation, expected_target="branch.local"
    ).to_record()
    record["disposition"] = "proven"
    replay = er.admit_record(Milestone.SYSTEM_ON_HOST, record, expected_target="branch.local")
    assert replay.disposition is er.ProofDisposition.DISPROVEN
    assert er.admit_record(
        Milestone.SYSTEM_ON_HOST, True, expected_target="branch.local"
    ).disposition is er.ProofDisposition.INVALID


def test_verifier_hash_tracks_semantics_but_not_registry_order(monkeypatch):
    baseline = fitness.verifier_hash()
    monkeypatch.setattr(er, "REGISTRY", tuple(reversed(er.REGISTRY)))
    assert fitness.verifier_hash() == baseline
    changed = list(er.REGISTRY)
    changed[0] = dataclasses.replace(changed[0], proof_kinds=frozenset({"changed_semantics"}))
    monkeypatch.setattr(er, "REGISTRY", tuple(changed))
    assert fitness.verifier_hash() != baseline


def test_live_capture_replay_and_direct_scoring_share_one_typed_result_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    scenario = range_state.Scenario(
        name="assembled",
        engagement_id="run-assembled",
        domains={"child": "branch.local"},
        milestone_subset=(Milestone.SYSTEM_ON_HOST,),
        recorded_probe_milestones=frozenset({Milestone.SYSTEM_ON_HOST}),
    )
    probe = probes.registered_probe(
        Milestone.SYSTEM_ON_HOST,
        lambda: True,
        origin=er.ProofOrigin.MYTHIC_TASK_OUTPUT,
        proof_kind="system_identity",
        target="branch.local",
    )
    direct = probes.read_ground_truth_from_probes(scenario, {Milestone.SYSTEM_ON_HOST: probe})
    live_runner._persist_ground_truth_probe_results(
        scenario.engagement_id,
        direct.proof_results,
        recorded_milestones={Milestone.SYSTEM_ON_HOST},
        verifier_hash=fitness.verifier_hash(scenario),
    )
    replay = range_state.read_ground_truth(scenario)

    assert direct.milestones == replay.milestones
    assert direct.proof_results[Milestone.SYSTEM_ON_HOST].disposition is er.ProofDisposition.PROVEN
    assert replay.proof_results[Milestone.SYSTEM_ON_HOST].disposition is er.ProofDisposition.PROVEN
    assert (
        fitness.score({}, direct, scenario=scenario).proof_dispositions
        == fitness.score({}, replay, scenario=scenario).proof_dispositions
    )
    assert (
        engagement_ledger.load(scenario.engagement_id)["ground_truth_probes"]["_verifier_hash"]
        == fitness.verifier_hash(scenario)
    )
