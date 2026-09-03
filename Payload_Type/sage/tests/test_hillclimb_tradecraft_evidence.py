from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from ai.hillclimb import safe_success
from ai.hillclimb import tradecraft_evidence as evidence


def _binding(tmp_path: Path):
    path = tmp_path / "source.json"
    path.write_text(
        json.dumps(
            {
                "facts": [True, "private-value"],
                "private_reasoning": "private-reasoning-value",
                "raw_task_output": "raw-task-output-value",
                "judge_output": "judge-output-value",
                "expected_label": "expected-label-value",
                "passes": True,
                "end": True,
            }
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    binding = evidence.SourceBinding.from_file(
        path, source_ref="fixture", source_version="v1", expected_sha256=digest
    )
    locator = evidence.EvidenceLocator("fixture", "/facts/0", "v1", digest)
    return binding, locator


def _atom(locator: evidence.EvidenceLocator, **updates):
    values = {
        "fact_kind": evidence.FactKind.TRAJECTORY_BOUNDARY,
        "truth": evidence.Truth.TRUE,
        "origin": evidence.EvidenceOrigin.EVALUATOR_OBSERVER,
        "locator": locator,
        "subject_handle": evidence.opaque_handle("subject", "raw-secret"),
    }
    values.update(updates)
    return evidence.EvidenceAtom(**values)


def test_schema_mutations_fail_closed_one_field_at_a_time(tmp_path):
    binding, locator = _binding(tmp_path)
    valid = _atom(locator).to_dict()
    mutations = []
    for field in ("fact_kind", "truth", "origin", "locator", "subject_handle"):
        mutant = deepcopy(valid)
        mutant.pop(field)
        mutations.append(mutant)
    for field, value in (
        ("fact_kind", "invented_fact"),
        ("truth", "probably"),
        ("origin", "caller_claim"),
        ("subject_handle", "raw-secret"),
        ("passes", True),
    ):
        mutant = deepcopy(valid)
        mutant[field] = value
        mutations.append(mutant)
    for mutant in mutations:
        with pytest.raises((evidence.EvidenceValidationError, ValueError)):
            evidence.EvidenceAtom.from_mapping(mutant)

    sequenced = deepcopy(valid)
    sequenced["fact_kind"] = evidence.FactKind.ACTION_ISSUED.value
    with pytest.raises(evidence.EvidenceValidationError, match="transaction and order"):
        evidence.EvidenceAtom.from_mapping(sequenced)

    projection = evidence.build_evidence_projection([_atom(locator)], {"fixture": binding})
    for field in ("schema", "atom_count", "atoms", "projection_sha256"):
        mutant = deepcopy(projection)
        mutant.pop(field)
        with pytest.raises(evidence.EvidenceValidationError):
            evidence.validate_evidence_projection(mutant)


def test_projection_positive_allowlist_excludes_forbidden_inputs_and_raw_values(tmp_path):
    binding, locator = _binding(tmp_path)
    projection = evidence.build_evidence_projection([_atom(locator)], {"fixture": binding})
    rendered = json.dumps(projection, sort_keys=True)
    for forbidden in (
        "private-value",
        "raw-secret",
        "private-reasoning-value",
        "raw-task-output-value",
        "judge-output-value",
        "expected-label-value",
        '"passes"',
    ):
        assert forbidden not in rendered
    assert set(projection) == {"schema", "atom_count", "atoms", "projection_sha256"}
    assert set(projection["atoms"][0]) == {
        "fact_kind", "truth", "origin", "locator", "subject_handle"
    }


def test_locator_resolution_hash_version_and_pointer_are_mandatory(tmp_path):
    binding, locator = _binding(tmp_path)
    atom = _atom(locator)
    with pytest.raises(evidence.EvidenceValidationError, match="unbound"):
        evidence.build_evidence_projection([atom], {})
    for replacement, message in (
        (evidence.EvidenceLocator("fixture", "/missing", "v1", locator.source_sha256), "unresolved"),
        (evidence.EvidenceLocator("fixture", "/facts/0", "v2", locator.source_sha256), "version"),
        (evidence.EvidenceLocator("fixture", "/facts/0", "v1", "0" * 64), "hash"),
    ):
        with pytest.raises(evidence.EvidenceValidationError, match=message):
            evidence.build_evidence_projection([_atom(replacement)], {"fixture": binding})


def test_projection_is_deterministic_and_rejects_duplicates(tmp_path):
    binding, locator = _binding(tmp_path)
    first = _atom(locator)
    second = _atom(
        locator,
        fact_kind=evidence.FactKind.CONTEXT_ABSENT,
        subject_handle=evidence.opaque_handle("subject", "other"),
    )
    assert evidence.build_evidence_projection([first, second], {"fixture": binding}) == evidence.build_evidence_projection(
        [second, first], {"fixture": binding}
    )
    with pytest.raises(evidence.EvidenceValidationError, match="duplicate"):
        evidence.build_evidence_projection([first, first], {"fixture": binding})


def test_q11_lifecycle_replay_preserves_three_agreements_and_two_conflicts(tmp_path):
    source_path = tmp_path / "events.json"
    source_path.write_text(json.dumps({"events": list(range(20)), "end": True}), encoding="utf-8")
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(json.dumps({"labels": ["PASS", "FAIL", "PASS", "PASS", "PASS"]}), encoding="utf-8")
    sources = {
        "events": {"path": source_path.name, "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest()},
        "labels": {"path": labels_path.name, "sha256": hashlib.sha256(labels_path.read_bytes()).hexdigest()},
    }

    def event(event_type, context, index, success=True):
        return {
            "event_type": event_type,
            "context_ref": context,
            "success": success,
            "locator": {"source": "events", "pointer": f"/events/{index}"},
        }

    records = [
        [event("created", "a", 0), event("used", "a", 1), event("retired", "a", 2)],
        [event("created", "b", 3), event("used", "b", 4)],
        [event("created", "c", 5), event("used", "c", 6)],
        [],
        [event("created", "e", 7), event("used", "e", 8)],
    ]
    protected = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source_path, labels_path)}
    comparisons = []
    for index, events in enumerate(records):
        record = {
            "record_key": f"record-{index}",
            "complete_trajectory": True,
            "termination_locator": {"source": "events", "pointer": "/end"},
            "events": events,
            "reference_locator": {"source": "labels", "pointer": f"/labels/{index}"},
        }
        if index == 3:
            record["absence_locator"] = {"source": "events", "pointer": "/end"}
        atoms, bindings = evidence.adapt_lifecycle_record(record, sources, tmp_path)
        projection = evidence.build_evidence_projection(atoms, bindings)
        verdicts, _ = safe_success.evaluate_questions(projection)
        observed = next(item["state"] for item in verdicts if item["question_id"] == "ISC40-Q11")
        reference = evidence.resolve_locator(
            evidence.EvidenceLocator(
                record["reference_locator"]["source"],
                record["reference_locator"]["pointer"],
                bindings[record["reference_locator"]["source"]].source_version,
                bindings[record["reference_locator"]["source"]].source_sha256,
            ),
            bindings,
        ).upper()
        comparisons.append((record["record_key"], observed, reference))
    conflicts = [item for item in comparisons if item[1] != item[2]]
    assert len(comparisons) == 5
    assert len(conflicts) == 2
    assert {item[0] for item in conflicts} == {"record-2", "record-4"}
    assert protected == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
