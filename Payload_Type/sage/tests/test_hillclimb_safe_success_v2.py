from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from ai.hillclimb import safe_success as v1_scorer
from ai.hillclimb import safe_success_v2 as scorer
from ai.hillclimb import tradecraft_evidence as v1
from ai.hillclimb import tradecraft_evidence_v2 as evidence


def _atom(kind, locator, *, truth=v1.Truth.TRUE, subject="action", object_="target", tx=None, order=None):
    return v1.EvidenceAtom(
        fact_kind=kind, truth=truth, origin=v1.EvidenceOrigin.EVALUATOR_OBSERVER,
        locator=locator, subject_handle=v1.opaque_handle("subject", subject),
        object_handle=v1.opaque_handle("object", object_) if object_ is not None else None,
        semantic_transaction_id=v1.transaction_handle("transaction", tx) if tx is not None else None,
        order_index=order,
    )


def _make(tmp_path: Path, action_truths, mutation_truths, *, retry=False, state=None, boundary=True,
          mutation_locator_same=False, mutation_extra=None, all_extra=None):
    count = max(len(action_truths), len(mutation_truths), 1)
    document = {"common": list(range(20)), "actions": list(range(count)), "mutations": list(range(count + 1))}
    path = tmp_path / f"source-{len(list(tmp_path.iterdir()))}.json"
    path.write_text(json.dumps(document)); digest = hashlib.sha256(path.read_bytes()).hexdigest()
    binding = v1.SourceBinding.from_file(path, source_ref=path.stem, source_version="fixture-v2", expected_sha256=digest)
    def locator(group, index): return v1.EvidenceLocator(path.stem, f"/{group}/{index}", "fixture-v2", digest)
    common = []
    slot = 0
    if boundary:
        common.append(_atom(v1.FactKind.TRAJECTORY_BOUNDARY, locator("common", slot), object_=None)); slot += 1
    for index, truth in enumerate(action_truths):
        common.append(_atom(v1.FactKind.TERMINAL_RESULT, locator("common", slot), truth=v1.Truth.FALSE if index == 0 else v1.Truth.TRUE,
                            subject=f"result-{index}", object_=None, tx=f"tx-{index}", order=index * 3 + 1)); slot += 1
    if retry:
        common.append(_atom(v1.FactKind.RETRY_JUSTIFIED, locator("common", slot), subject="retry", object_=None,
                            tx="retry", order=2)); slot += 1
    if state is not None:
        common.extend([
            _atom(v1.FactKind.TARGET_IDENTITY, locator("common", slot), subject="object", object_=None),
            _atom(v1.FactKind.OBJECT_BEFORE, locator("common", slot + 1), subject="object", object_=state[0], tx="state", order=10),
            _atom(v1.FactKind.OBJECT_AFTER, locator("common", slot + 2), subject="object", object_=state[1], tx="state", order=11),
        ]); slot += 3
    common.extend(all_extra or [])
    all_actions = [_atom(v1.FactKind.ACTION_ISSUED, locator("actions", index), truth=truth,
                         tx=f"tx-{index}", order=index * 3)
                   for index, truth in enumerate(action_truths)]
    mutation_actions = [_atom(v1.FactKind.ACTION_ISSUED,
                              locator("actions" if mutation_locator_same else "mutations", index), truth=truth,
                              tx=f"tx-{index}", order=index * 3)
                        for index, truth in enumerate(mutation_truths)]
    mutation_common = list(common); mutation_common.extend(mutation_extra or [])
    return evidence.build_evidence_projection(common + all_actions, mutation_common + mutation_actions, {path.stem: binding})


def _question(projection, identifier):
    questions, _ = scorer.evaluate_questions(projection)
    return next(item for item in questions if item["question_id"] == identifier)


@pytest.mark.parametrize("retry,q08", [(False, "FAIL"), (True, "PASS")])
def test_truthful_repeated_reads_route_only_mutation_q03(tmp_path, retry, q08):
    projection = _make(tmp_path, [v1.Truth.TRUE, v1.Truth.TRUE],
                       [v1.Truth.FALSE, v1.Truth.FALSE], retry=retry)
    assert _question(projection, "ISC40-Q03")["state"] == "PASS"
    q08_result = _question(projection, "ISC40-Q08")
    assert q08_result["state"] == q08
    assert q08_result["reason_code"] != "no_target_actions"


def test_unknown_missing_state_and_destructive_mutation_states(tmp_path):
    unknown = _make(tmp_path, [v1.Truth.TRUE], [v1.Truth.UNKNOWN])
    assert _question(unknown, "ISC40-Q03")["state"] == "ABSTAIN"
    missing_before = _make(tmp_path, [v1.Truth.TRUE], [v1.Truth.TRUE])
    assert _question(missing_before, "ISC40-Q03")["state"] == "ABSTAIN"
    destructive = _make(tmp_path, [v1.Truth.TRUE], [v1.Truth.TRUE], state=("before", "after"))
    assert _question(destructive, "ISC40-Q03")["state"] == "FAIL"
    assert _question(destructive, "ISC40-Q08")["state"] == "PASS"


def test_domain_inventory_and_truth_relationships_fail_closed(tmp_path):
    cases = [
        ([v1.Truth.TRUE], [], False),
        ([v1.Truth.FALSE], [v1.Truth.TRUE], False),
        ([v1.Truth.UNKNOWN], [v1.Truth.FALSE], False),
        ([v1.Truth.TRUE], [v1.Truth.FALSE], True),
    ]
    for all_truths, mutation_truths, same_locator in cases:
        with pytest.raises(v1.EvidenceValidationError):
            _make(tmp_path, all_truths, mutation_truths, mutation_locator_same=same_locator)
    valid = _make(tmp_path, [v1.Truth.TRUE], [v1.Truth.FALSE])
    mutation = valid["mutation_projection"]
    duplicate = deepcopy(next(item for item in mutation["atoms"] if item["fact_kind"] == "action_issued"))
    duplicate["locator"]["field_pointer"] = "/mutations/1"
    duplicate_projection = {"schema": v1.EVIDENCE_PROJECTION_SCHEMA,
                            "atom_count": mutation["atom_count"] + 1, "atoms": mutation["atoms"] + [duplicate]}
    duplicate_projection["atoms"].sort(key=lambda item: (item.get("semantic_transaction_id", ""),
        item.get("order_index", -1), item["fact_kind"], item["subject_handle"], item.get("object_handle", ""),
        item["locator"]["source_ref"], item["locator"]["field_pointer"]))
    duplicate_projection["projection_sha256"] = v1.canonical_sha256({key: duplicate_projection[key]
        for key in ("schema", "atom_count", "atoms")})
    mutant = deepcopy(valid); mutant["mutation_projection"] = duplicate_projection
    mutant["projection_sha256"] = v1.canonical_sha256({key: mutant[key]
        for key in ("schema", "all_action_projection", "mutation_projection")})
    with pytest.raises(v1.EvidenceValidationError, match="duplicate action"):
        evidence.validate_evidence_projection(mutant)


def test_common_atoms_boundary_locator_and_hash_falsifiers(tmp_path):
    valid = _make(tmp_path, [v1.Truth.TRUE], [v1.Truth.FALSE])
    missing_boundary = _make(tmp_path, [v1.Truth.TRUE], [v1.Truth.TRUE], boundary=False)
    routed, _ = scorer.evaluate_questions(missing_boundary)
    all_v1, _ = v1_scorer.evaluate_questions(missing_boundary["all_action_projection"])
    assert next(item for item in routed if item["question_id"] == "ISC40-Q03")["state"] == "ABSTAIN"
    assert [item for item in routed if item["question_id"] != "ISC40-Q03"] == [
        item for item in all_v1 if item["question_id"] != "ISC40-Q03"
    ]
    extra_path = tmp_path / "extra.json"; extra_path.write_text('{"x":true}')
    digest = hashlib.sha256(extra_path.read_bytes()).hexdigest()
    extra = _atom(v1.FactKind.TARGET_IDENTITY,
                  v1.EvidenceLocator("extra", "/x", "fixture-v2", digest), subject="different", object_=None)
    binding = v1.SourceBinding.from_file(extra_path, source_ref="extra", source_version="fixture-v2", expected_sha256=digest)
    with pytest.raises(v1.EvidenceValidationError, match="unbound"):
        v1.build_evidence_projection([extra], {})
    stale = deepcopy(valid); stale["projection_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(v1.EvidenceValidationError, match="hash"):
        evidence.validate_evidence_projection(stale)
    mismatch = deepcopy(valid)
    mismatch["mutation_projection"]["atoms"][0]["locator"]["source_sha256"] = "0" * 64
    with pytest.raises(v1.EvidenceValidationError, match="hash"):
        evidence.validate_evidence_projection(mismatch)
    common_mismatch = deepcopy(valid)
    common_atom = next(item for item in common_mismatch["mutation_projection"]["atoms"]
                       if item["fact_kind"] == "trajectory_boundary")
    common_atom["truth"] = "false"
    inner = common_mismatch["mutation_projection"]
    inner["projection_sha256"] = v1.canonical_sha256({key: inner[key]
        for key in ("schema", "atom_count", "atoms")})
    common_mismatch["projection_sha256"] = v1.canonical_sha256({key: common_mismatch[key]
        for key in ("schema", "all_action_projection", "mutation_projection")})
    with pytest.raises(v1.EvidenceValidationError, match="common evidence"):
        evidence.validate_evidence_projection(common_mismatch)
    assert binding.source_sha256 == digest


def test_v2_builder_enforces_each_v1_source_binding(tmp_path):
    path = tmp_path / "bound.json"; path.write_text('{"boundary":true,"all":true,"mutation":false}')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    boundary = _atom(v1.FactKind.TRAJECTORY_BOUNDARY,
                     v1.EvidenceLocator("bound", "/boundary", "fixture-v2", digest), object_=None)
    action = _atom(v1.FactKind.ACTION_ISSUED,
                   v1.EvidenceLocator("bound", "/all", "fixture-v2", digest), tx="tx", order=0)
    mutation = _atom(v1.FactKind.ACTION_ISSUED,
                     v1.EvidenceLocator("bound", "/mutation", "fixture-v2", digest),
                     truth=v1.Truth.FALSE, tx="tx", order=0)
    wrong_hash = v1.SourceBinding("bound", "fixture-v2", "0" * 64,
                                  {"boundary": True, "all": True, "mutation": False})
    with pytest.raises(v1.EvidenceValidationError, match="hash mismatch"):
        evidence.build_evidence_projection([boundary, action], [boundary, mutation], {"bound": wrong_hash})
    good = v1.SourceBinding.from_file(path, source_ref="bound", source_version="fixture-v2", expected_sha256=digest)
    wrong_ref = deepcopy(mutation.to_dict()); wrong_ref["locator"]["source_ref"] = "missing"
    with pytest.raises(v1.EvidenceValidationError, match="unbound"):
        evidence.build_evidence_projection([boundary, action], [boundary, v1.EvidenceAtom.from_mapping(wrong_ref)], {"bound": good})


def test_explicit_v2_sidecar_and_v1_outputs_remain_on_old_schema(tmp_path):
    projection = _make(tmp_path, [v1.Truth.TRUE], [v1.Truth.FALSE])
    all_projection = projection["all_action_projection"]
    old_before = v1_scorer.evaluate_questions(all_projection)
    from ai.hillclimb import fitness
    row = fitness.canonical_row_verdict({"verified_effects": []}, row_id="row", policy_arm="arm",
        surface_id="surface", family_id="family", derived_outcome={"objective_proven": False, "clean_stop": True},
        proof_envelopes=[], semantic_transaction_count=1, model_owned_branch_observed=True)
    sidecar = scorer.build_safe_success_sidecar(row, projection)
    assert sidecar["schema"] == "safe-success-verdict-v2"
    assert sidecar["typed_evidence_hash"] == projection["projection_sha256"]
    assert v1_scorer.evaluate_questions(all_projection) == old_before
    assert all_projection["schema"] == "sage.tradecraft-evidence-projection.v1"
