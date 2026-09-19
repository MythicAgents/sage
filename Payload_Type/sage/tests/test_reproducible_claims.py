"""Actual offline reducer and source-binding counterexamples, no live fixtures."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys

import pytest

from ai.hillclimb import reproducible_claims as claims
from ai.hillclimb.fitness import canonical_row_verdict


def _native_fixture_module():
    spec = importlib.util.spec_from_file_location(
        "_claims_native_fixture", Path(__file__).with_name("test_trust_walker_pilot_cli.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity(arm="shipping-sage"):
    return dict(protocol_id="synthetic-protocol", pair_id="pair-1", logical_seed=0, policy_arm=arm,
                model_provider="synthetic-provider", model_id="synthetic-model", model_version="revision-1",
                backend_route="synthetic-route", generation_config={"temperature": 0}, wrapper_id=arm,
                wrapper_revision="revision-2", authority_mode="auto", execution_path="supervisor_worker",
                source_version="synthetic-source")


def fixture_manifest(tmp_path):
    inputs = dict(row=None, row_id="row-1", policy_arm="shipping-sage", surface_id="synthetic-surface",
                  family_id="synthetic-family", derived_outcome=None)
    result = canonical_row_verdict(**inputs)
    inputs["measurement_identity"] = identity()
    document = {"inputs": inputs, "identities": [identity()], "result": result}
    source = tmp_path / "source.json"
    source.write_text(json.dumps(document))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    def ref(pointer):
        return {"source": {"source_ref": "sample", "source_version": "sample-v1",
                           "source_sha256": digest, "field_pointer": pointer}}

    manifest = {"schema": claims.SCHEMA, "evaluator_bindings": claims.evaluator_bindings(),
                "sources": {"sample": {"path": "source.json", "sha256": digest,
                                       "version": "sample-v1", "format": "json"}},
                "claims": [{"id": "c1", "reducer": "canonical_row", "inputs": ref("/inputs"),
                            "identities": ref("/identities"), "expected": ref("/result"),
                            "supersedes": None, "amendment_reason": None}]}
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(manifest))
    return path, manifest, document


def test_one_command_reproduces_real_reducer_without_network(tmp_path, monkeypatch, capsys):
    path, _, _ = fixture_manifest(tmp_path)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network"))
    script = claims.ROOT / "skills/sage-eval-gauge/scripts/verify_claims.py"
    spec = importlib.util.spec_from_file_location("claim_cli", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main([str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["verified"] is True
    assert output["effective_claims"] == ["c1"]
    assert output["claims"][0]["measurement"]["performance_assertion_allowed"] is True


@pytest.mark.parametrize("mutation", ["missing", "changed", "evaluator", "expected", "label", "path", "orphan", "forward", "duplicate", "literal"])
def test_actual_consumer_rejects_changed_inputs_and_orphan_claims(tmp_path, mutation):
    path, manifest, document = fixture_manifest(tmp_path)
    if mutation == "missing":
        (tmp_path / "source.json").unlink()
    elif mutation == "changed":
        (tmp_path / "source.json").write_text("{}")
    elif mutation == "evaluator":
        manifest["evaluator_bindings"][next(iter(manifest["evaluator_bindings"]))] = "0" * 64
    elif mutation in {"expected", "label", "path"}:
        if mutation == "expected":
            document["result"]["paired_effect_mean"] = 99
        elif mutation == "label":
            del document["identities"][0]["model_version"]
        else:
            document["identities"][0]["execution_path"] = "unknown"
        raw = json.dumps(document)
        (tmp_path / "source.json").write_text(raw)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        manifest["sources"]["sample"]["sha256"] = digest
        for field in ("inputs", "identities", "expected"):
            manifest["claims"][0][field]["source"]["source_sha256"] = digest
    elif mutation == "orphan":
        manifest["sources"]["unused"] = deepcopy(manifest["sources"]["sample"])
    elif mutation == "forward":
        manifest["claims"][0]["inputs"] = {"claim": "later", "pointer": "/result"}
    elif mutation == "duplicate":
        manifest["claims"].append(deepcopy(manifest["claims"][0]))
    else:
        manifest["claims"][0]["inputs"] = {"candidate_scores": [1, 1]}
    path.write_text(json.dumps(manifest))
    with pytest.raises((claims.ClaimError, ValueError, OSError)):
        claims.verify_claims(path)


@pytest.mark.parametrize("field", sorted(claims.IDENTITY_FIELDS))
def test_every_required_identity_field_is_enforced(field):
    value = identity()
    del value[field]
    assert claims.measurement_labels([value])["performance_assertion_allowed"] is False


def test_append_only_amendments_rederive_old_and_current_and_reject_forks(tmp_path):
    path, manifest, _ = fixture_manifest(tmp_path)
    revision = {**deepcopy(manifest["claims"][0]), "id": "c2", "supersedes": "c1",
                "amendment_reason": "Explicit repeated calculation with corrected annotation"}
    manifest["claims"].append(revision)
    path.write_text(json.dumps(manifest))
    report = claims.verify_claims(path)
    assert report["effective_claims"] == ["c2"]
    assert len(report["claims"]) == 2
    manifest["claims"].append({**deepcopy(revision), "id": "c3"})
    path.write_text(json.dumps(manifest))
    with pytest.raises(claims.ClaimError, match="forked"):
        claims.verify_claims(path)


def test_aggregate_refuses_stale_parser_even_if_input_says_reproduced():
    pair = {"projection_sha256": "0" * 64, "outcome_parser_sha256": "0" * 64,
            "reproduced": True, "dimensions": {"protocol_valid": True}}
    with pytest.raises(claims.ClaimError, match="binding is stale"):
        claims.reduce_trust_walker_batch([pair])


def test_empty_or_unknown_claim_set_fails_closed(tmp_path):
    path, manifest, _ = fixture_manifest(tmp_path)
    manifest["claims"] = []
    path.write_text(json.dumps(manifest))
    with pytest.raises(claims.ClaimError, match="empty"):
        claims.verify_claims(path)


def test_scorecard_replay_uses_bound_proof_not_ambient_ledger_or_generated_passes(monkeypatch):
    from ai.hillclimb import effect_registry
    from ai.hillclimb.range_state import Milestone, engagement_ledger
    from ai.hillclimb import process_state
    monkeypatch.setattr(engagement_ledger, "load", lambda *_: pytest.fail("ambient ledger read"))
    monkeypatch.setattr(process_state, "read_process_signals", lambda *_: pytest.fail("ambient trajectory read"))
    oracle = effect_registry.contract_for(Milestone.SYSTEM_ON_HOST)
    proof = effect_registry.admit_observation(
        Milestone.SYSTEM_ON_HOST,
        effect_registry.OracleObservation(
            registry_version=effect_registry.REGISTRY_VERSION, oracle_id=oracle.oracle_id,
            origin=effect_registry.ProofOrigin.MYTHIC_TASK_OUTPUT, proof_kind="system_identity",
            outcome=True, observation_id="synthetic-system-task", target="branch.example",
        ), expected_target="branch.example",
    ).to_record()
    inputs = {"run_record": {"status": "stopped", "tool_calls": 1, "model_calls": 1,
                             "measurement_identity": identity()},
              "scenario": {"name": "synthetic-system", "engagement_id": "unused",
                           "domains": {"child": "branch.example"}, "milestone_subset": ["SYSTEM_ON_HOST"]},
              "proof_results": {"SYSTEM_ON_HOST": proof}}
    result = claims.replay_scorecard(**inputs)
    assert result["milestones"]["SYSTEM_ON_HOST"] is True
    assert result["measurement"]["performance_assertion_allowed"] is True
    inputs["proof_results"] = {"SYSTEM_ON_HOST": {"passes": True}}
    rejected = claims.replay_scorecard(**inputs)
    assert rejected["milestones"]["SYSTEM_ON_HOST"] is False
    assert rejected["objective_proven"] is False
    assert rejected["proof_dispositions"]["SYSTEM_ON_HOST"] == "invalid"


def test_legacy_score_and_statistics_carry_explicit_missing_identity():
    from ai.hillclimb import fitness, reliability, bare_runner
    from ai.hillclimb.range_state import GroundTruth, Milestone
    card = fitness.score({}, GroundTruth("fixture", {Milestone.FOOTHOLD: False}, Milestone.FOOTHOLD))
    assert card.measurement["performance_assertion_allowed"] is False
    assert reliability.noise_floor([card]).measurement["performance_assertion_allowed"] is False
    report = bare_runner.compare_bare_vs_harness("fixture", [card], [card], allow_missing_rates=True)
    assert report.measurement["performance_assertion_allowed"] is False


@pytest.mark.parametrize("aggregate", ["trust_walker_batch", "trust_walker_improvement"])
def test_native_pair_and_batch_claims_replay_actual_producer_and_refuse_stale_version(tmp_path, monkeypatch, aggregate):
    # This native-pilot fixture deliberately rejects the development-row kernel.
    # Earlier test modules may have imported it, so isolate only this fixture's
    # process-global import state; do not weaken the pilot's production guard.
    for name in tuple(sys.modules):
        if name == "development_row_kernel" or name.endswith(".development_row_kernel"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    fixture = _native_fixture_module()
    cli, retained_native_pair = fixture.cli, fixture.retained_native_pair
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network"))
    sources, records, label_list, expected_pairs = {}, [], [], []
    data = {"identities": [], "expected": []}
    improvement = cli.ImprovementContract("a" * 64, "b" * 64)
    for index in range(2):
        root = tmp_path / f"pair-{index}"
        root.mkdir()
        def metadata(arm):
            return cli._assembled_measurement_metadata(
                {"logical_seed": index, "model_version": "synthetic-original-version",
                 "generation_config": {"provider": cli.PROVIDER}},
                arm=arm, config={"provider": cli.PROVIDER, "model": cli.MODEL, "mode": "auto", "max_steps": 0},
            )
        selected = (cli.ImprovementContract("a" * 64, "b" * 64,
                    arm_order=cli.IMPROVEMENT_ARM_ORDER if index == 0 else tuple(reversed(cli.IMPROVEMENT_ARM_ORDER)))
                    if aggregate == "trust_walker_improvement" else None)
        produced = retained_native_pair(root, monkeypatch, contract=selected, measurement_metadata=metadata,
                                       improvement_metric=aggregate == "trust_walker_improvement")
        report = cli.replay_pair(root)
        assert report["performance_assertion_allowed"] is True
        assert report["terminal_verifier_cost_summary"]["monetary_cost_usd"] is None
        labels = [row["measurement_identity"] for row in produced if row.get("record_type") == "arm_result"]
        assert labels == [row["measurement_identity"] for row in report["arms"]]
        label_list.extend(labels)
        data["identities"].append(labels)
        data["expected"].append(report)
        expected_pairs.append(report)
        ledger = root / "pilot.jsonl"
        sources[f"ledger-{index}"] = {"path": str(ledger.relative_to(tmp_path)),
                                      "sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
                                      "version": "synthetic-ledger-v1", "format": "jsonl"}
    data["aggregate_labels"] = label_list
    aggregate_inputs = {"pairs": expected_pairs}
    if aggregate == "trust_walker_improvement":
        data["contract"] = improvement.to_record()
        aggregate_inputs["contract"] = data["contract"]
    data["aggregate"] = claims._reduce(aggregate, aggregate_inputs)
    if aggregate == "trust_walker_improvement":
        assert data["aggregate"]["disposition"] == "accept"
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data))
    sources["data"] = {"path": "data.json", "sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
                       "version": "synthetic-data-v1", "format": "json"}
    def ref(name, pointer):
        return {"source": {"source_ref": name, "source_version": sources[name]["version"],
                           "source_sha256": sources[name]["sha256"], "field_pointer": pointer}}
    for index in range(2):
        records.append({"id": f"pair-{index}", "reducer": "trust_walker_pair",
                        "inputs": {"ledger": ref(f"ledger-{index}", "/0")},
                        "identities": ref("data", f"/identities/{index}"),
                        "expected": ref("data", f"/expected/{index}"), "supersedes": None, "amendment_reason": None})
    records.append({"id": "batch", "reducer": aggregate,
                    "inputs": {"pairs": [{"claim": f"pair-{index}", "pointer": "/result"} for index in range(2)],
                               **({"contract": ref("data", "/contract")} if aggregate == "trust_walker_improvement" else {})},
                    "identities": ref("data", "/aggregate_labels"), "expected": ref("data", "/aggregate"),
                    "supersedes": None, "amendment_reason": None})
    manifest = {"schema": claims.SCHEMA, "evaluator_bindings": claims.evaluator_bindings(),
                "sources": sources, "claims": records}
    manifest_path = tmp_path / "claims.json"
    manifest_path.write_text(json.dumps(manifest))
    assert claims.verify_claims(manifest_path)["verified"] is True
    data["identities"][0][0]["model_version"] = "stale-but-valid-version"
    data_path.write_text(json.dumps(data))
    old_hash = sources["data"]["sha256"]
    new_hash = hashlib.sha256(data_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest).replace(old_hash, new_hash))
    with pytest.raises(claims.ClaimError, match="differ from its retained producer"):
        claims.verify_claims(manifest_path)


def test_metadata_loader_and_assembled_configuration_reject_secrets_and_stale_values(tmp_path):
    cli = _native_fixture_module().cli
    path = tmp_path / "metadata.json"
    declared = {"logical_seed": 1, "model_version": "revision-1", "generation_config": {"max_steps": 0}}
    path.write_text(json.dumps(declared))
    assert cli._measurement_metadata(str(path)) == declared
    actual = cli._assembled_measurement_metadata(declared, arm="shipping-sage", config={
        "provider": cli.PROVIDER, "model": cli.MODEL, "mode": "auto", "max_steps": 0,
        "API_KEY": "synthetic-must-not-retain",
    })
    assert "synthetic-must-not-retain" not in json.dumps(actual)
    assert actual["generation_config"]["max_steps"] == 0
    declared["generation_config"]["max_steps"] = 1
    with pytest.raises(cli.PilotError, match="differs from the assembled producer"):
        cli._assembled_measurement_metadata(declared, arm="shipping-sage", config={"max_steps": 0})
    declared["generation_config"] = {"nested": {"api_key": "synthetic-secret"}}
    path.write_text(json.dumps(declared))
    with pytest.raises(cli.PilotError, match="forbidden"):
        cli._measurement_metadata(str(path))


def test_historical_terminal_verifier_wall_is_unknown_not_zero():
    from ai.hillclimb import trust_walker_pilot
    result = trust_walker_pilot._terminal_verifier_cost(None, observed_wall_seconds=None, wall_limit_seconds=7200)
    assert result["wall_seconds"] is None
    assert result["limit_disposition"] == "unavailable"
    assert result["complete"] is False
