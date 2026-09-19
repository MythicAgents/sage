"""Finite source-bound coverage for every current M1 reporting consumer."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import importlib.util
import json
import socket
from pathlib import Path

import pytest

from ai.hillclimb import composite_gauge, fitness, reproducible_claims as claims
from ai.hillclimb.reliability import noise_floor


def _fixture_module(name):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_consumer_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_composite_fixtures = _fixture_module("test_hillclimb_composite_gauge")
_claims_fixtures = _fixture_module("test_reproducible_claims")
ARMS = _composite_fixtures.ARMS
ARM_CONTRACTS = _composite_fixtures.ARM_CONTRACTS
EFFECTS = _composite_fixtures.EFFECTS
MARGINS = _composite_fixtures.MARGINS
_record = _composite_fixtures._record
identity = _claims_fixtures.identity


def _score_inputs(label, capability):
    """A retained ScoreCard producer packet with no ambient ledger dependency."""
    from ai.hillclimb import effect_registry
    from ai.hillclimb.range_state import Milestone

    oracle = effect_registry.contract_for(Milestone.SYSTEM_ON_HOST)
    proof = effect_registry.admit_observation(
        Milestone.SYSTEM_ON_HOST,
        effect_registry.OracleObservation(
            registry_version=effect_registry.REGISTRY_VERSION, oracle_id=oracle.oracle_id,
            origin=effect_registry.ProofOrigin.MYTHIC_TASK_OUTPUT, proof_kind="system_identity",
            outcome=True, observation_id=f"consumer-{label['policy_arm']}-{capability}",
            target="branch.example",
        ), expected_target="branch.example",
    ).to_record()
    return {
        "run_record": {"status": "stopped", "score": capability, "tool_calls": 1, "model_calls": 1,
                       "measurement_identity": label},
        "scenario": {"name": "consumer-system", "engagement_id": "retained-only",
                     "domains": {"child": "branch.example"}, "milestone_subset": ["SYSTEM_ON_HOST"]},
        "proof_results": {"SYSTEM_ON_HOST": proof},
    }


def _canonical_inputs(label):
    return {
        "row": None, "row_id": f"row-{label['policy_arm']}", "policy_arm": label["policy_arm"],
        "surface_id": "consumer-surface", "family_id": "consumer-family", "derived_outcome": None,
        "measurement_identity": label,
    }


def _source_ref(digest, pointer):
    return {"source": {"source_ref": "fixture", "source_version": "fixture-v1",
                       "source_sha256": digest, "field_pointer": pointer}}


def _claim(name, reducer, inputs, identities, expected):
    return {"id": name, "reducer": reducer, "inputs": inputs, "identities": identities,
            "expected": expected, "supersedes": None, "amendment_reason": None}


def _write_manifest(tmp_path):
    labels = [identity("shipping-sage"), identity("shipping-sage"), identity("shipping-sage")]
    for index, label in enumerate(labels):
        label["logical_seed"] = index
    raw_label = identity("raw-mythic-bloodhound")
    raw_label["execution_path"] = "true_naive_raw_tools"
    raw_label["logical_seed"] = 10

    score_inputs = [_score_inputs(label, value) for label, value in zip(labels, (0.2, 0.5, 0.9), strict=True)]
    score_results = [claims.replay_scorecard(**packet) for packet in score_inputs]
    canonical_inputs = [_canonical_inputs(labels[0]), _canonical_inputs(raw_label)]
    canonical_results = [fitness.canonical_row_verdict(**{k: v for k, v in packet.items() if k != "measurement_identity"})
                         for packet in canonical_inputs]

    composite_records = [
        _record(tmp_path, 0, ARMS[0], objective=True, opsec="pass"),
        _record(tmp_path, 0, ARMS[1], objective=True, opsec="pass"),
    ]
    composite_row_results = [
        composite_gauge.build_composite_row(row["canonical_row"], row["evidence_projection"], identity=row["identity"])
        for row in composite_records
    ]
    composite_report_inputs = {
        "records": composite_records, "evaluated_arm": ARMS[0], "baseline_arm": ARMS[1],
        "arm_contracts": ARM_CONTRACTS, "smallest_relevant_effects": EFFECTS,
        "noninferiority_margins": MARGINS, "critical_subgroups": [["surface-a", "family-a"]],
    }
    comparison_records = [
        {"record_type": "result", "scenario": "consumer-system", "side": "bare", "card": score_results[0],
         "measurement_identity": labels[0]},
        {"record_type": "result", "scenario": "consumer-system", "side": "harness", "card": score_results[1],
         "measurement_identity": labels[1]},
    ]
    document = {
        "score_inputs": score_inputs, "score_labels": [[label] for label in labels],
        "score_results": score_results,
        "noise_labels": labels[:2], "noise_result": asdict(noise_floor([fitness.ScoreCard(**item) for item in score_results[:2]])),
        "gate_labels": labels, "gate_result": claims._reduce("gate_experiment", {
            "cards_by_config": {"low": [score_results[0]], "mid": [score_results[1]], "high": [score_results[2]]},
            "seeds": 1,
        }),
        "comparison_records": comparison_records, "comparison_labels": labels[:2],
        "comparison_result": claims._reduce("comparison", {"records": comparison_records, "scenario": "consumer-system"}),
        "canonical_inputs": canonical_inputs, "canonical_labels": [[labels[0]], [raw_label]],
        "canonical_results": canonical_results, "aggregate_labels": [labels[0], raw_label],
        "aggregate_result": fitness.canonical_aggregate_verdict(
            canonical_results, evaluated_policy="shipping-sage", baseline_policy="raw-mythic-bloodhound"),
        "composite_records": composite_records,
        "composite_row_labels": [[row["identity"]] for row in composite_records],
        "composite_labels": [row["identity"] for row in composite_records],
        "composite_row_results": composite_row_results,
        "composite_report_inputs": composite_report_inputs,
        "composite_report_result": composite_gauge.build_paired_report(**composite_report_inputs),
    }
    source = tmp_path / "fixture.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    ref = lambda pointer: _source_ref(digest, pointer)
    output = lambda claim_id: {"claim": claim_id, "pointer": "/result"}
    manifest = {
        "schema": claims.SCHEMA, "evaluator_bindings": claims.evaluator_bindings(),
        "sources": {"fixture": {"path": "fixture.json", "sha256": digest, "version": "fixture-v1", "format": "json"}},
        "claims": [
            *[_claim(f"score-{index}", "scorecard", ref(f"/score_inputs/{index}"), ref(f"/score_labels/{index}"), ref(f"/score_results/{index}"))
              for index in range(3)],
            _claim("noise", "noise_floor", {"scorecards": [output("score-0"), output("score-1")]}, ref("/noise_labels"), ref("/noise_result")),
            _claim("gate", "gate_experiment", {"cards_by_config": {"low": [output("score-0")], "mid": [output("score-1")], "high": [output("score-2")]}, "seeds": ref("/gate_seeds")}, ref("/gate_labels"), ref("/gate_result")),
            _claim("comparison", "comparison", ref("/comparison_inputs"), ref("/comparison_labels"), ref("/comparison_result")),
            _claim("row-shipping", "canonical_row", ref("/canonical_inputs/0"), ref("/canonical_labels/0"), ref("/canonical_results/0")),
            _claim("row-raw", "canonical_row", ref("/canonical_inputs/1"), ref("/canonical_labels/1"), ref("/canonical_results/1")),
            _claim("canonical-aggregate", "canonical_aggregate", {"row_verdicts": [output("row-shipping"), output("row-raw")], "evaluated_policy": ref("/evaluated_policy"), "baseline_policy": ref("/baseline_policy")}, ref("/aggregate_labels"), ref("/aggregate_result")),
            *[_claim(f"composite-row-{index}", "composite_row", {"canonical_row": ref(f"/composite_records/{index}/canonical_row"), "evidence_projection": ref(f"/composite_records/{index}/evidence_projection"), "identity": ref(f"/composite_records/{index}/identity")}, ref(f"/composite_row_labels/{index}"), ref(f"/composite_row_results/{index}")) for index in range(2)],
            _claim("composite-report", "composite_report", ref("/composite_report_inputs"), ref("/composite_labels"), ref("/composite_report_result")),
        ],
    }
    document.update({"gate_seeds": 1, "comparison_inputs": {"records": comparison_records, "scenario": "consumer-system"},
                     "evaluated_policy": "shipping-sage", "baseline_policy": "raw-mythic-bloodhound"})
    source.write_text(json.dumps(document), encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest["sources"]["fixture"]["sha256"] = digest
    for claim in manifest["claims"]:
        for value in (claim["inputs"], claim["identities"], claim["expected"]):
            def refresh(item):
                if isinstance(item, dict):
                    if set(item) == {"source"}:
                        item["source"]["source_sha256"] = digest
                    else:
                        for nested in item.values(): refresh(nested)
                elif isinstance(item, list):
                    for nested in item: refresh(nested)
            refresh(value)
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest, document, source


def _rewrite(path, manifest, document, source):
    source.write_text(json.dumps(document), encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest["sources"]["fixture"]["sha256"] = digest
    def update(value):
        if isinstance(value, dict):
            if set(value) == {"source"}: value["source"]["source_sha256"] = digest
            else:
                for item in value.values(): update(item)
        elif isinstance(value, list):
            for item in value: update(item)
    for claim in manifest["claims"]:
        for value in (claim["inputs"], claim["identities"], claim["expected"]): update(value)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_current_reporting_consumers_reproduce_from_one_bound_manifest_without_network(tmp_path, monkeypatch):
    path, _, _, _ = _write_manifest(tmp_path)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    report = claims.verify_claims(path)
    assert report["verified"] is True
    assert {item["reducer"] for item in report["claims"]} >= {
        "scorecard", "noise_floor", "gate_experiment", "comparison", "canonical_row",
        "canonical_aggregate", "composite_row", "composite_report",
    }


@pytest.mark.parametrize("label_key", [
    "score_labels", "noise_labels", "gate_labels", "comparison_labels", "canonical_labels",
    "aggregate_labels", "composite_row_labels", "composite_labels",
])
def test_reporting_consumers_reject_well_formed_stale_producer_labels(tmp_path, label_key):
    path, manifest, document, source = _write_manifest(tmp_path)
    label = document[label_key]
    while isinstance(label, list):
        label = label[0]
    label["model_version"] = "stale-but-well-formed"
    _rewrite(path, manifest, document, source)
    with pytest.raises(claims.ClaimError):
        claims.verify_claims(path)


def test_reporting_consumers_reject_wrong_expected_output_and_evaluator_binding(tmp_path):
    path, manifest, document, source = _write_manifest(tmp_path)
    document["gate_result"]["verdict"] = "PASS" if document["gate_result"]["verdict"] != "PASS" else "FAIL"
    _rewrite(path, manifest, document, source)
    with pytest.raises(claims.ClaimError, match="does not reproduce"):
        claims.verify_claims(path)
    manifest["evaluator_bindings"][next(iter(manifest["evaluator_bindings"]))] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(claims.ClaimError, match="evaluator binding"):
        claims.verify_claims(path)


def test_comparison_claim_rejects_a_well_formed_card_root_identity_split(tmp_path):
    path, manifest, _document, source = _write_manifest(tmp_path)
    document = json.loads(source.read_text(encoding="utf-8"))
    document["comparison_records"][0]["card"]["measurement_identity"]["model_version"] = "different-but-well-formed"
    document["comparison_inputs"]["records"] = document["comparison_records"]
    document["comparison_result"] = claims._reduce("comparison", document["comparison_inputs"])
    _rewrite(path, manifest, document, source)
    with pytest.raises(claims.ClaimError, match="card labels differ"):
        claims.verify_claims(path)


def test_descriptive_comparison_marks_card_root_identity_split_nonassertive(tmp_path):
    _path, _manifest, _document, source = _write_manifest(tmp_path)
    document = json.loads(source.read_text(encoding="utf-8"))
    records = document["comparison_records"]
    records[0]["card"]["measurement_identity"]["source_version"] = "different-but-well-formed"
    summary = claims._load_script("summarize_comparison").summarize(records, "consumer-system")
    assert summary["scope"] == "descriptive_summary_only"
    assert summary["measurement"]["performance_assertion_allowed"] is False
    assert summary["identity_mismatch_lines"] == [1]
