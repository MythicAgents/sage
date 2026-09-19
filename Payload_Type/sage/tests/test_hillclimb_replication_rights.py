"""Offline M1 ISC5 receipt/parser coverage."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.hillclimb import effect_registry  # noqa: E402
from ai.hillclimb.range_state import Milestone  # noqa: E402
from ai.hillclimb import replication_rights as rights  # noqa: E402
from ai.langgraph import proof_boundary  # noqa: E402


TARGET = "sevenkingdoms.local"
SUBJECT = "samwell.tarly@north.sevenkingdoms.local"
ARTIFACT_SHA = "a" * 64
CAPTURED = "2026-09-18T12:00:00+00:00"
OBSERVED = "2026-09-18T12:04:00+00:00"


def _input(subjects, target, *, job="bh-job-22", artifact_id="file-42", artifact_sha=ARTIFACT_SHA):
    result = rights.replication_rights_verifier_input(
        subjects, target, expected_ingest_job_id=job,
        expected_source_artifact_id=artifact_id, expected_source_artifact_sha256=artifact_sha,
    )
    assert result is not None
    return result


def _receipt(rows, *, subjects=(SUBJECT,), target=TARGET, query_status="success", engagement="op-m1",
             job="bh-job-22", artifact_id="file-42", artifact_sha=ARTIFACT_SHA):
    verifier_input = _input(subjects, target, job=job, artifact_id=artifact_id, artifact_sha=artifact_sha)
    verifier_result = rights.replication_rights_verifier_result(rows, query_status=query_status)
    assert verifier_result is not None
    return proof_boundary.make_runtime_bloodhound_envelope(
        engagement_id=engagement, callback_id="17", task_id="99", terminal_status="completed",
        command="execute_assembly", ingest_job_id=job, ingest_status="complete",
        source_artifact_id=artifact_id, source_artifact_sha256=artifact_sha,
        verifier_id=rights.VERIFIER_ID, verifier_version=rights.VERIFIER_VERSION,
        transaction_id="collect-22", verifier_input=verifier_input, verifier_result=verifier_result,
        captured_at=CAPTURED,
    ).to_dict()


def _observe(rows, **changes):
    values = {
        "expected_subjects": [SUBJECT], "target_domain": TARGET,
        "expected_engagement_id": "op-m1", "expected_ingest_job_id": "bh-job-22",
        "expected_source_artifact_id": "file-42", "expected_source_artifact_sha256": ARTIFACT_SHA,
        "observed_at": OBSERVED, "max_age_seconds": 300,
    }
    values.update(changes)
    if "collection_receipt" not in values:
        values["collection_receipt"] = _receipt(rows)
    return rights.parent_replication_rights_observation(rows, **values)


EDGE = {"principal": "SAMWELL.TARLY@NORTH.SEVENKINGDOMS.LOCAL.", "type": "DCSync", "target": "SEVENKINGDOMS.LOCAL."}


def test_exact_payload_edge_generates_an_admitted_passing_observation():
    observation = _observe([EDGE])

    assert observation is not None and observation.outcome is True
    assert observation.subject == SUBJECT and observation.target == TARGET
    assert effect_registry.admit_observation(
        Milestone.REPLICATION_RIGHTS_PARENT, observation,
        expected_target=TARGET, expected_subject=SUBJECT,
    ).disposition is effect_registry.ProofDisposition.PROVEN


def test_query_and_commitment_helpers_bind_exact_fields():
    query = rights.build_replication_rights_query([SUBJECT], TARGET)
    verifier_input = _input([SUBJECT], TARGET)

    assert query is not None
    assert "p.name AS principal" in query["query"] and "type(e) AS type" in query["query"]
    assert query["parameters"] == {"principals": [SUBJECT], "edge_type": "DCSync", "target": TARGET}
    assert verifier_input["query"] == query["query"]
    assert verifier_input["expected_ingest_job_id"] == "bh-job-22"


@pytest.mark.parametrize("row", [
    {"principal": "other@north.sevenkingdoms.local", "type": "DCSync", "target": TARGET},
    {"principal": SUBJECT, "type": "DCSync", "target": "evil.sevenkingdoms.local"},
    {"principal": f"x.{SUBJECT}", "type": "DCSync", "target": TARGET},
    {"principal": SUBJECT, "type": "GenericAll", "target": TARGET},
])
def test_valid_receipt_with_unrelated_row_is_unmeasured(row):
    assert _observe([row]) is None


def test_committed_successful_empty_result_is_a_measured_negative():
    observation = _observe([])

    assert observation is not None and observation.outcome is False
    assert effect_registry.admit_observation(
        Milestone.REPLICATION_RIGHTS_PARENT, observation, expected_target=TARGET,
    ).disposition is effect_registry.ProofDisposition.DISPROVEN


@pytest.mark.parametrize("changes", [
    {"expected_engagement_id": "foreign-op"},
    {"expected_ingest_job_id": "other-job"},
    {"expected_source_artifact_id": "other-file"},
    {"expected_source_artifact_sha256": "b" * 64},
    {"observed_at": "2026-09-18T12:06:00+00:00"},
])
def test_foreign_or_wrong_lineage_or_stale_receipt_is_unmeasured(changes):
    assert _observe([EDGE], **changes) is None


def test_wrong_query_subject_parameters_or_result_commitment_is_unmeasured():
    assert _observe([EDGE], collection_receipt=_receipt([EDGE], subjects=("other@north.sevenkingdoms.local",))) is None
    assert _observe([EDGE], collection_receipt=_receipt([EDGE], target="evil.sevenkingdoms.local")) is None
    assert _observe([EDGE], collection_receipt=_receipt([])) is None


def test_failed_query_and_malformed_row_are_unmeasured():
    assert _observe([EDGE], collection_receipt=_receipt([EDGE], query_status="failed")) is None
    assert _observe([{"principal": SUBJECT, "type": "DCSync"}], collection_receipt=_receipt([])) is None
