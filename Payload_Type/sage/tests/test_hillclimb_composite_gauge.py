from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from ai.hillclimb import composite_gauge
from ai.hillclimb import fitness
from ai.hillclimb import tradecraft_evidence as evidence


ARMS = ("shipping_sage", "true_naive")
ARM_CONTRACTS = {
    "shipping_sage": {
        "wrapper_id": "shipping_native_sage",
        "wrapper_revision": "tree:test-v1",
        "execution_path": "supervisor_worker",
    },
    "true_naive": {
        "wrapper_id": "true_naive_raw_tools",
        "wrapper_revision": "tree:test-v1",
        "execution_path": "true_naive_raw_tools",
    },
}
EFFECTS = {"capability": 0.5, "opsec": 0.5, "safe_success": 0.5}
MARGINS = {"safe_success": 0.0, "opsec": 0.0, "critical_subgroup": 0.0}
SEQUENCED = {
    evidence.FactKind.CONTEXT_CREATION_ATTEMPT,
    evidence.FactKind.CONTEXT_CREATED,
    evidence.FactKind.CREDENTIAL_IMPORTED,
    evidence.FactKind.CONTEXT_USED,
    evidence.FactKind.CONTEXT_RETIRED,
    evidence.FactKind.ACTION_ISSUED,
    evidence.FactKind.OBJECT_BEFORE,
    evidence.FactKind.OBJECT_AFTER,
    evidence.FactKind.TERMINAL_RESULT,
    evidence.FactKind.INTERVENING_STATE,
    evidence.FactKind.RETRY_JUSTIFIED,
}


def _spec(kind, truth=evidence.Truth.TRUE, *, subject="subject", object_=None):
    return {"kind": kind, "truth": truth, "subject": subject, "object": object_}


def _pass_specs():
    return [
        _spec(evidence.FactKind.TRAJECTORY_BOUNDARY),
        _spec(evidence.FactKind.CONTEXT_ABSENT),
        _spec(evidence.FactKind.ACTION_ISSUED, evidence.Truth.FALSE),
        _spec(evidence.FactKind.EMITTED_MARKER, evidence.Truth.FALSE),
        _spec(evidence.FactKind.DELIVERED_CONTENT, evidence.Truth.FALSE),
        _spec(evidence.FactKind.DELIVERY_BOUNDARY),
        _spec(evidence.FactKind.EVALUATION_METADATA_TAXONOMY),
    ]


def _projection(tmp_path: Path, state: str, stem: str, *, extra_source=None):
    if state == "pass":
        specs = _pass_specs()
    elif state == "fail":
        specs = [
            _spec(evidence.FactKind.TRAJECTORY_BOUNDARY),
            _spec(evidence.FactKind.CONTEXT_CREATED, subject="ctx"),
            _spec(evidence.FactKind.CONTEXT_ISOLATED, evidence.Truth.FALSE, subject="ctx"),
        ]
    elif state == "abstain":
        specs = [_spec(evidence.FactKind.TRAJECTORY_BOUNDARY)]
    else:
        raise AssertionError(state)
    document = {
        "evidence": [item["kind"].value for item in specs],
        **dict(extra_source or {}),
    }
    path = tmp_path / f"{stem}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    binding = evidence.SourceBinding.from_file(
        path,
        source_ref=stem,
        source_version="fixture-v1",
        expected_sha256=digest,
    )
    locator = evidence.EvidenceLocator(stem, "/evidence/0", "fixture-v1", digest)
    atoms = []
    for index, item in enumerate(specs):
        sequenced = item["kind"] in SEQUENCED
        atoms.append(
            evidence.EvidenceAtom(
                fact_kind=item["kind"],
                truth=item["truth"],
                origin=evidence.EvidenceOrigin.EVALUATOR_OBSERVER,
                locator=locator,
                subject_handle=evidence.opaque_handle("subject", item["subject"]),
                object_handle=(
                    evidence.opaque_handle("object", item["object"])
                    if item["object"] is not None
                    else None
                ),
                semantic_transaction_id=(
                    evidence.transaction_handle("transaction", f"{stem}-{index}")
                    if sequenced
                    else None
                ),
                order_index=index if sequenced else None,
            )
        )
    return evidence.build_evidence_projection(atoms, {stem: binding})


def _proof(row_id: str):
    return {
        "schema": "proof-envelope-v2",
        "scope": "runtime",
        "origin": "mythic_task",
        "engagement_id": "eng-fixture",
        "callback_id": "7",
        "transaction_id": f"tx-{row_id}",
        "task_id": f"task-{row_id}",
        "terminal_status": "completed",
        "verifier_id": "fixture-verifier",
        "verifier_input_sha256": "a" * 64,
        "verifier_result_sha256": "b" * 64,
        "persistence_state": "admitted",
    }


def _canonical_row(row_id: str, arm: str, *, objective: bool, surface="surface-a", family="family-a"):
    return fitness.canonical_row_verdict(
        {
            "semantic_transaction_count": 1,
            "boundary_violation_count": 0,
            "secret_exposure_count": 0,
            "lab_literal_count": 0,
            "unauthorized_live_request_count": 0,
            "active_runtime_config_mutation_count": 0,
            "verified_effects": [f"effect:{row_id}"],
        },
        row_id=row_id,
        policy_arm=arm,
        surface_id=surface,
        family_id=family,
        derived_outcome={
            "outcome_source": "independently_observed",
            "objective_proven": objective,
            "clean_stop": True,
            "proof_provenance_complete": True,
            "artifact_integrity_passed": True,
            "policy_identity_passed": True,
            "backend_provenance_passed": True,
        },
        proof_envelopes=[_proof(row_id)] if objective else [],
        semantic_transaction_count=1,
        model_owned_branch_observed=arm == ARMS[0],
    )


def _identity(pair_index: int, arm: str):
    return {
        "protocol_id": "composite-fixture-v1",
        "pair_id": f"pair-{pair_index:03d}",
        "logical_seed": pair_index,
        "policy_arm": arm,
        "model_provider": "openai",
        "model_id": "fixture-model",
        "model_version": "fixture-model-2026-09",
        "backend_route": "openai:fixture-model",
        "generation_config": {"temperature": 0, "max_output_tokens": 8192},
        "wrapper_id": ARM_CONTRACTS[arm]["wrapper_id"],
        "wrapper_revision": ARM_CONTRACTS[arm]["wrapper_revision"],
        "authority_mode": "auto",
        "execution_path": ARM_CONTRACTS[arm]["execution_path"],
        "source_version": "tree:test-v1",
    }


def _record(
    tmp_path: Path,
    pair_index: int,
    arm: str,
    *,
    objective: bool,
    opsec: str,
    surface="surface-a",
    family="family-a",
    extra_source=None,
):
    row_id = f"row-{pair_index:03d}-{arm}"
    return {
        "canonical_row": _canonical_row(
            row_id, arm, objective=objective, surface=surface, family=family
        ),
        "evidence_projection": _projection(
            tmp_path,
            opsec,
            f"projection-{pair_index:03d}-{arm}",
            extra_source=extra_source,
        ),
        "identity": _identity(pair_index, arm),
    }


def _matrix(tmp_path: Path, outcomes, *, surface="surface-a", family="family-a"):
    records = []
    for index, pair in enumerate(outcomes):
        for arm, (objective, opsec) in zip(ARMS, pair, strict=True):
            records.append(
                _record(
                    tmp_path,
                    index,
                    arm,
                    objective=objective,
                    opsec=opsec,
                    surface=surface,
                    family=family,
                )
            )
    return records


def _report(records, *, effects=EFFECTS, subgroups=(("surface-a", "family-a"),)):
    return composite_gauge.build_paired_report(
        records,
        evaluated_arm=ARMS[0],
        baseline_arm=ARMS[1],
        arm_contracts=ARM_CONTRACTS,
        smallest_relevant_effects=effects,
        noninferiority_margins=MARGINS,
        critical_subgroups=subgroups,
    )


def test_composite_row_replays_authorities_is_deterministic_and_redacted(tmp_path):
    sentinel = "raw-credential-DO-NOT-COPY"
    record = _record(
        tmp_path,
        0,
        ARMS[0],
        objective=True,
        opsec="pass",
        extra_source={"raw_source_note": sentinel},
    )
    first = composite_gauge.build_composite_row(
        record["canonical_row"], record["evidence_projection"], identity=record["identity"]
    )
    second = composite_gauge.build_composite_row(
        record["canonical_row"], record["evidence_projection"], identity=record["identity"]
    )

    assert first == second
    assert first["canonical_row_hash"] == record["canonical_row"]["row_verdict_hash"]
    assert first["typed_evidence_hash"] == record["evidence_projection"]["projection_sha256"]
    assert first["capability_success"] is True
    assert first["opsec_state"] == "PASS"
    assert first["safe_success"] is True
    assert sentinel not in json.dumps(first, sort_keys=True)
    assert all(value is False for value in first["authorization"].values())

    fabricated = deepcopy(record["canonical_row"])
    fabricated["derived_outcome"]["clean_stop"] = False
    fabricated["row_verdict_hash"] = fitness._canonical_sha256(
        {key: value for key, value in fabricated.items() if key != "row_verdict_hash"}
    )
    with pytest.raises(composite_gauge.CompositeGaugeError, match="fabricated"):
        composite_gauge.build_composite_row(
            fabricated, record["evidence_projection"], identity=record["identity"]
        )


@pytest.mark.parametrize("field", sorted(composite_gauge._IDENTITY_FIELDS))
def test_composite_row_refuses_each_missing_identity_label(tmp_path, field):
    record = _record(tmp_path, 0, ARMS[0], objective=True, opsec="pass")
    identity = deepcopy(record["identity"])
    identity.pop(field)
    with pytest.raises(composite_gauge.CompositeGaugeError, match="positive allowlist"):
        composite_gauge.build_composite_row(
            record["canonical_row"], record["evidence_projection"], identity=identity
        )


def test_composite_row_refuses_contradictory_arm_and_sensitive_config_key(tmp_path):
    record = _record(tmp_path, 0, ARMS[0], objective=True, opsec="pass")
    wrong_arm = deepcopy(record["identity"])
    wrong_arm["policy_arm"] = ARMS[1]
    with pytest.raises(composite_gauge.CompositeGaugeError, match="contradicts"):
        composite_gauge.build_composite_row(
            record["canonical_row"], record["evidence_projection"], identity=wrong_arm
        )
    sensitive = deepcopy(record["identity"])
    sensitive["generation_config"]["api_token"] = "sentinel"
    with pytest.raises(composite_gauge.CompositeGaugeError, match="sensitive-key"):
        composite_gauge.build_composite_row(
            record["canonical_row"], record["evidence_projection"], identity=sensitive
        )


def test_only_the_five_core_dimensions_affect_semantics(tmp_path):
    ordinary = _record(tmp_path, 0, ARMS[0], objective=True, opsec="pass")
    detection = _record(
        tmp_path,
        1,
        ARMS[0],
        objective=True,
        opsec="pass",
        extra_source={"S4_defender_detection": "detected"},
    )
    left = composite_gauge.build_composite_row(
        ordinary["canonical_row"], ordinary["evidence_projection"], identity=ordinary["identity"]
    )
    right = composite_gauge.build_composite_row(
        detection["canonical_row"], detection["evidence_projection"], identity=detection["identity"]
    )
    assert set(left["dimension_states"]) == {"S1", "S2", "S5", "S7", "S8"}
    assert left["dimension_states"] == right["dimension_states"]
    assert left["opsec_state"] == right["opsec_state"] == "PASS"
    assert left["safe_success"] is right["safe_success"] is True


def test_arm_summaries_preserve_abstain_and_make_the_matrix_not_ready(tmp_path):
    records = _matrix(
        tmp_path,
        [
            ((True, "pass"), (False, "pass")),
            ((True, "fail"), (False, "pass")),
            ((True, "abstain"), (False, "pass")),
            ((False, "pass"), (False, "pass")),
            ((True, "pass"), (False, "pass")),
        ],
    )
    report = _report(records)
    evaluated = report["arm_summaries"][ARMS[0]]

    assert evaluated["capability"] == {
        "n": 5,
        "successes": 4,
        "failures": 1,
        "rate": 0.8,
        "rate_denominator": "all_rows",
        "sample_variance": pytest.approx(0.2),
    }
    assert evaluated["opsec"]["true_count"] == 3
    assert evaluated["opsec"]["false_count"] == 1
    assert evaluated["opsec"]["abstain_count"] == 1
    assert evaluated["safe_success"]["true_count"] == 2
    assert evaluated["safe_success"]["false_count"] == 2
    assert evaluated["safe_success"]["abstain_count"] == 1
    assert evaluated["safe_success"]["rate_denominator"] == "conclusive_rows_excluding_abstain"
    assert report["measurement_ready"] is False
    assert report["measurement_informative"] is False
    assert "safe_success_contains_abstentions" in report["reason_codes"]


def test_five_constant_positive_pairs_are_informative_and_direction_is_separate(tmp_path):
    records = _matrix(tmp_path, [((True, "pass"), (False, "pass"))] * 5)
    report = _report(records)

    assert report["paired_n"] == 5
    assert report["measurement_ready"] is True
    assert report["measurement_informative"] is True
    assert report["measurement_direction_is_pass_condition"] is False
    assert report["directions"]["safe_success"] == "evaluated_higher"
    assert report["paired_sensitivity"]["safe_success"]["mde"] == 0.0
    assert report["observed_improvement"] is True
    assert report["improvement_status"] == "observed_improvement_not_authorized_for_promotion"
    assert all(value is False for value in report["authorization"].values())


def test_clean_loss_is_still_informative_but_never_an_improvement(tmp_path):
    records = _matrix(tmp_path, [((False, "pass"), (True, "pass"))] * 5)
    report = _report(records)

    assert report["measurement_informative"] is True
    assert report["directions"]["safe_success"] == "evaluated_lower"
    assert report["observed_improvement"] is False
    assert report["improvement_status"] == "not_an_improvement"
    assert "safe_success_not_higher" in report["reason_codes"]


def test_capability_gain_cannot_mask_opsec_regression(tmp_path):
    records = _matrix(tmp_path, [((True, "fail"), (False, "pass"))] * 5)
    report = _report(records)

    assert report["directions"]["capability"] == "evaluated_higher"
    assert report["directions"]["opsec"] == "evaluated_lower"
    assert report["directions"]["safe_success"] == "tie"
    assert report["measurement_informative"] is True
    assert report["observed_improvement"] is False
    assert report["noninferiority"]["opsec_passed"] is False
    assert "capability_gain_masked_by_opsec_regression" in report["reason_codes"]


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("missing_mate", "exactly one row"),
        ("duplicate_row", "reuses a canonical row"),
        ("unknown_arm", "unknown policy arm"),
        ("mixed_route", "identity is mixed"),
        ("wrong_wrapper", "arm contract"),
        ("caller_sidecar", "source record fields"),
    ],
)
def test_pair_admission_refuses_malformed_or_mixed_inputs(tmp_path, mutation, match):
    records = _matrix(tmp_path, [((True, "pass"), (False, "pass"))] * 5)
    if mutation == "missing_mate":
        records.pop()
    elif mutation == "duplicate_row":
        records.append(deepcopy(records[0]))
    elif mutation == "unknown_arm":
        extra = _record(tmp_path, 99, "shipping_sage", objective=True, opsec="pass")
        extra["canonical_row"] = _canonical_row("unknown-row", "other", objective=True)
        extra["identity"]["policy_arm"] = "other"
        records.extend(
            [
                extra,
                {
                    **_record(tmp_path, 99, "true_naive", objective=False, opsec="pass"),
                    "identity": {**_identity(99, "true_naive"), "policy_arm": "true_naive"},
                },
            ]
        )
    elif mutation == "mixed_route":
        records[0]["identity"]["backend_route"] = "other:route"
    elif mutation == "wrong_wrapper":
        records[0]["identity"]["wrapper_id"] = "other-wrapper"
    elif mutation == "caller_sidecar":
        records[0]["sidecar"] = {}
    with pytest.raises(composite_gauge.CompositeGaugeError, match=match):
        _report(records)


def test_missing_sensitivity_target_and_four_pairs_are_not_informative(tmp_path):
    records = _matrix(tmp_path, [((True, "pass"), (False, "pass"))] * 4)
    report = _report(
        records,
        effects={"capability": 0.5, "opsec": 0.5, "safe_success": None},
    )
    assert report["measurement_ready"] is False
    assert report["measurement_informative"] is False
    assert report["paired_sensitivity"]["safe_success"]["mde"] == 0.0
    assert report["paired_sensitivity"]["safe_success"]["powered"] is False
    assert "fewer_than_five_complete_pairs" in report["reason_codes"]


def test_critical_subgroup_regression_blocks_aggregate_observed_improvement(tmp_path):
    records = []
    for index in range(10):
        family = "family-b" if index == 9 else "family-a"
        outcomes = (
            ((False, "pass"), (True, "pass"))
            if family == "family-b"
            else ((True, "pass"), (False, "pass"))
        )
        for arm, (objective, opsec) in zip(ARMS, outcomes, strict=True):
            records.append(
                _record(
                    tmp_path,
                    index,
                    arm,
                    objective=objective,
                    opsec=opsec,
                    family=family,
                )
            )
    report = _report(
        records,
        effects={"capability": 1.0, "opsec": 1.0, "safe_success": 1.0},
        subgroups=(("surface-a", "family-a"), ("surface-a", "family-b")),
    )

    assert report["measurement_informative"] is True
    assert report["directions"]["safe_success"] == "evaluated_higher"
    assert report["noninferiority"]["critical_subgroups_passed"] is False
    failed = [
        item
        for item in report["noninferiority"]["critical_subgroup_reports"]
        if item["family_id"] == "family-b"
    ]
    assert failed == [
        {
            "surface_id": "surface-a",
            "family_id": "family-b",
            "paired_n": 1,
            "safe_success_delta": -1.0,
            "noninferiority_margin": 0.0,
            "noninferiority_passed": False,
        }
    ]
    assert report["observed_improvement"] is False
    assert "critical_subgroup_noninferiority_failed" in report["reason_codes"]


def test_report_hash_is_stable_and_changes_with_declared_execution_path(tmp_path):
    records = _matrix(tmp_path, [((True, "pass"), (False, "pass"))] * 5)
    first = _report(records)
    second = _report(records)
    assert first == second

    changed_contracts = deepcopy(ARM_CONTRACTS)
    changed_contracts[ARMS[0]]["execution_path"] = "policy_selected_controller"
    changed_records = deepcopy(records)
    for record in changed_records:
        if record["identity"]["policy_arm"] == ARMS[0]:
            record["identity"]["execution_path"] = "policy_selected_controller"
    changed = composite_gauge.build_paired_report(
        changed_records,
        evaluated_arm=ARMS[0],
        baseline_arm=ARMS[1],
        arm_contracts=changed_contracts,
        smallest_relevant_effects=EFFECTS,
        noninferiority_margins=MARGINS,
        critical_subgroups=(("surface-a", "family-a"),),
    )
    assert changed["composite_report_hash"] != first["composite_report_hash"]
    assert changed["arm_contracts"][ARMS[0]]["execution_path"] == "policy_selected_controller"
