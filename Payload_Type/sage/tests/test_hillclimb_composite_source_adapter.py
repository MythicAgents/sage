from __future__ import annotations

import hashlib
import inspect
import json
from copy import deepcopy
from pathlib import Path

import pytest

from ai.hillclimb import composite_source_adapter as adapter
from ai.hillclimb import fitness
from ai.hillclimb import tradecraft_evidence as evidence


def _write_json(path: Path, value) -> str:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _proof(row_id: str):
    return {
        "schema": "proof-envelope-v2",
        "scope": "runtime",
        "persistence_state": "admitted",
        "origin": "mythic_task",
        "engagement_id": "eng-fixture",
        "callback_id": "7",
        "transaction_id": f"tx-{row_id}",
        "task_id": f"task-{row_id}",
        "terminal_status": "completed",
        "verifier_id": "fixture-verifier",
        "verifier_input_sha256": "a" * 64,
        "verifier_result_sha256": "b" * 64,
    }


def _fixture(
    tmp_path: Path,
    *,
    arm="shipping_sage",
    wrapper="shipping_native_sage",
    execution_path="supervisor_worker",
    objective=True,
    risk=0,
):
    source_version = "tree:fixture-v1"
    observer_version = "observer-v1"
    row_id = f"row-{arm}"
    identity = {
        "protocol_id": "composite-source-fixture-v1",
        "pair_id": "pair-001",
        "logical_seed": 1,
        "policy_arm": arm,
        "model_provider": "openai",
        "model_id": "fixture-model",
        "model_version": "fixture-model-2026-09",
        "backend_route": "openai:fixture-model",
        "generation_config": {"temperature": 0, "max_output_tokens": 8192},
        "wrapper_id": wrapper,
        "wrapper_revision": source_version,
        "authority_mode": "auto",
        "execution_path": execution_path,
        "source_version": source_version,
    }
    episode = {
        "schema": adapter.EPISODE_SCHEMA,
        "arm": arm,
        "protocol": {
            "protocol_id": identity["protocol_id"],
            "pair_id": identity["pair_id"],
            "model_provider": identity["model_provider"],
            "model_id": identity["model_id"],
            "wrapper": wrapper,
            "source_version": source_version,
            "pair_invariants": {
                "logical_seed": identity["logical_seed"],
                "generation_configuration": identity["generation_config"],
                "authority_mode": identity["authority_mode"],
            },
        },
    }
    derived = {
        "outcome_source": "independently_observed",
        "objective_proven": objective,
        "clean_stop": True,
        "proof_provenance_complete": True,
        "artifact_integrity_passed": True,
        "policy_identity_passed": True,
        "backend_provenance_passed": True,
    }
    fact = {
        "fact_kind": evidence.FactKind.TRAJECTORY_BOUNDARY.value,
        "truth": evidence.Truth.TRUE.value,
        "origin": evidence.EvidenceOrigin.EVALUATOR_OBSERVER.value,
        "subject_handle": evidence.opaque_handle("row", row_id),
    }
    row = {
        "row_id": row_id,
        "policy_arm": arm,
        "surface_id": "surface-a",
        "family_id": "family-a",
        "boundary_violation_count": risk,
        "secret_exposure_count": 0,
        "lab_literal_count": 0,
        "unauthorized_live_request_count": 0,
        "active_runtime_config_mutation_count": 0,
        "derived_outcome": derived,
        "proof_envelopes": [_proof(row_id)] if objective else [],
        "semantic_transaction_count": 1,
        "model_owned_branch_observed": arm == "shipping_sage",
    }
    observer = {
        "source_version": observer_version,
        "row": row,
        "identity": identity,
        "facts": [fact],
    }
    episode_digest = _write_json(tmp_path / "episode.json", episode)
    observer_digest = _write_json(tmp_path / "observer.json", observer)

    def locator(pointer):
        return {
            "source_ref": "observer",
            "field_pointer": pointer,
            "source_version": observer_version,
            "source_sha256": observer_digest,
        }

    claims = {
        **{
            f"row.{name}": {"value": value, "locator": locator(f"/row/{name}")}
            for name, value in row.items()
        },
        **{
            f"identity.{name}": {"value": value, "locator": locator(f"/identity/{name}")}
            for name, value in identity.items()
        },
    }
    typed_atom = {**fact, "locator": locator("/facts/0")}
    bundle = {
        "schema": adapter.BUNDLE_SCHEMA,
        "episode": episode,
        "evidence_supplement": {
            "schema": adapter.SUPPLEMENT_SCHEMA,
            "episode_source_ref": "episode",
            "claims": claims,
            "typed_atoms": [typed_atom],
        },
        "source_bindings": {
            "episode": {
                "path": "episode.json",
                "source_version": source_version,
                "source_sha256": episode_digest,
            },
            "observer": {
                "path": "observer.json",
                "source_version": observer_version,
                "source_sha256": observer_digest,
            },
        },
        "identity": identity,
    }
    return bundle


@pytest.mark.parametrize("field", sorted(adapter._BUNDLE_FIELDS))
def test_bundle_top_level_positive_allowlist_refuses_each_missing_field(tmp_path, field):
    bundle = _fixture(tmp_path)
    bundle.pop(field)
    result = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert result["status"] == "not_reusable"
    assert result["composite_row"] is None
    assert result["reason_codes"][0].startswith("bundle_fields_mismatch:")


def test_bundle_top_level_positive_allowlist_refuses_extra_field(tmp_path):
    bundle = _fixture(tmp_path)
    bundle["expected_label"] = "PASS"
    result = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert result["status"] == "not_reusable"
    assert "extra=expected_label" in result["reason_codes"][0]


@pytest.mark.parametrize("claim", sorted(adapter._REQUIRED_CLAIMS))
def test_every_required_source_claim_is_fail_closed(tmp_path, claim):
    bundle = _fixture(tmp_path)
    bundle["evidence_supplement"]["claims"].pop(claim)
    result = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert result["status"] == "not_reusable"
    assert result["composite_record"] is result["composite_row"] is None
    assert any(claim in reason for reason in result["reason_codes"])


def test_legacy_episode_is_deterministically_not_reusable_without_defaults(tmp_path):
    complete = _fixture(tmp_path)
    legacy = {
        "schema": adapter.BUNDLE_SCHEMA,
        "episode": complete["episode"],
        "evidence_supplement": None,
        "source_bindings": {},
        "identity": {},
    }
    first = adapter.admit_composite_source(legacy, repo_root=tmp_path)
    second = adapter.admit_composite_source(legacy, repo_root=tmp_path)
    assert first == second
    assert first["status"] == "not_reusable"
    assert any(item.startswith("missing_risk_evidence:") for item in first["reason_codes"])
    assert "missing_typed_opsec_evidence" in first["reason_codes"]
    assert any(item.startswith("missing_identity_evidence:") for item in first["reason_codes"])
    assert first["composite_record"] is first["composite_row"] is None


def test_complete_source_bound_fixture_admits_deterministically_and_replays(tmp_path):
    bundle = _fixture(tmp_path)
    first = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    second = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert first == second
    assert first["status"] == "admitted"
    assert first["reason_codes"] == []
    record = first["composite_record"]
    assert fitness.canonical_row_verdict_is_replay_valid(record["canonical_row"])
    assert first["composite_row"]["canonical_row_hash"] == record["canonical_row"]["row_verdict_hash"]
    assert first["composite_row"]["dimension_states"] == {name: "ABSTAIN" for name in ("S1", "S2", "S5", "S7", "S8")}
    assert first["composite_row"]["safe_success"] == "ABSTAIN"
    assert all(value is False for value in first["authorization"].values())
    assert all(value is False for value in first["composite_row"]["authorization"].values())


@pytest.mark.parametrize("mutation,reason", [
    ("hash", "source_hash_mismatch"),
    ("version", "source_version_mismatch"),
    ("pointer", "unresolved_field_pointer"),
    ("count", "claim_value_mismatch"),
    ("truth", "typed_atom_source_mismatch"),
])
def test_source_hash_version_pointer_truth_and_count_mutations_are_refused(tmp_path, mutation, reason):
    bundle = _fixture(tmp_path)
    risk = bundle["evidence_supplement"]["claims"]["row.boundary_violation_count"]
    if mutation == "hash":
        risk["locator"]["source_sha256"] = "f" * 64
    elif mutation == "version":
        risk["locator"]["source_version"] = "wrong-version"
    elif mutation == "pointer":
        risk["locator"]["field_pointer"] = "/row/absent"
    elif mutation == "count":
        risk["value"] = 1
    else:
        bundle["evidence_supplement"]["typed_atoms"][0]["truth"] = "false"
    result = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert result["status"] == "not_reusable"
    assert result["composite_row"] is None
    assert any(reason in item for item in result["reason_codes"])


def test_changed_source_bytes_and_fabricated_canonical_output_are_refused(tmp_path, monkeypatch):
    bundle = _fixture(tmp_path)
    (tmp_path / "observer.json").write_text("{}", encoding="utf-8")
    changed = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert changed["status"] == "not_reusable"
    assert "source_bytes_changed" in changed["reason_codes"][0]

    bundle = _fixture(tmp_path)
    monkeypatch.setattr(adapter.fitness, "canonical_row_verdict", lambda *args, **kwargs: {"row_status": "scored"})
    fabricated = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert fabricated["status"] == "not_reusable"
    assert fabricated["reason_codes"] == ["canonical_row_not_scored_and_replay_valid"]


def test_missing_claims_and_caller_asserted_execution_path_are_refused(tmp_path):
    bundle = _fixture(tmp_path)
    bundle["evidence_supplement"]["claims"].pop("row.secret_exposure_count")
    result = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert any("row.secret_exposure_count" in item for item in result["reason_codes"])

    bundle = _fixture(tmp_path)
    bundle["identity"]["execution_path"] = "policy-arm-name-is-not-proof"
    result = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert result["reason_codes"] == ["identity_claim_mismatch_execution_path"]


@pytest.mark.parametrize("field", sorted(adapter._IDENTITY_FIELDS))
def test_every_composite_identity_label_must_match_its_source_claim(tmp_path, field):
    bundle = _fixture(tmp_path)
    if field == "logical_seed":
        bundle["identity"][field] += 1
    elif field == "generation_config":
        bundle["identity"][field] = {**bundle["identity"][field], "temperature": 0.5}
    else:
        bundle["identity"][field] += "-contradiction"
    result = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert result["status"] == "not_reusable"
    assert result["reason_codes"] == [f"identity_claim_mismatch_{field}"]


def test_source_bound_identity_cannot_contradict_the_episode(tmp_path):
    bundle = _fixture(tmp_path)
    bundle["episode"]["protocol"]["model_id"] = "contradictory-model"
    digest = _write_json(tmp_path / "episode.json", bundle["episode"])
    bundle["source_bindings"]["episode"]["source_sha256"] = digest
    result = adapter.admit_composite_source(bundle, repo_root=tmp_path)
    assert result["status"] == "not_reusable"
    assert result["reason_codes"] == ["episode_identity_mismatch_model_id"]


def test_explicit_nonzero_risk_and_distinct_arm_wrapper_are_scored_without_promotion(tmp_path):
    risky = adapter.admit_composite_source(_fixture(tmp_path, risk=1), repo_root=tmp_path)
    assert risky["status"] == "admitted"
    assert risky["composite_record"]["canonical_row"]["row_status"] == "scored"
    assert risky["composite_record"]["canonical_row"]["promotion_row_passed"] is False

    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    baseline = adapter.admit_composite_source(
        _fixture(
            baseline_root,
            arm="true_naive",
            wrapper="true_naive_raw_tools",
            execution_path="true_naive_raw_tools",
            objective=False,
        ),
        repo_root=baseline_root,
    )
    assert baseline["status"] == "admitted"
    assert baseline["composite_row"]["identity"]["execution_path"] == "true_naive_raw_tools"
    assert baseline["composite_row"]["capability_success"] is False


def test_adapter_has_no_runtime_or_external_import_surface():
    source = inspect.getsource(adapter)
    forbidden = ("langgraph", "sage_chat", "mythic", "requests", "httpx", "socket", "subprocess")
    assert not any(f"import {name}" in source or f"from {name}" in source for name in forbidden)
    assert "promotion_authorized\": True" not in source
