"""Pure, receipt-bound evaluator adapter for parent replication rights."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Mapping, Sequence

try:
    from . import effect_registry
except ImportError:  # pragma: no cover
    import effect_registry  # type: ignore
try:
    from ai.langgraph import proof_boundary
except ImportError:  # pragma: no cover
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import proof_boundary  # type: ignore


VERIFIER_ID = "m1-replication-rights-query:v1"
VERIFIER_VERSION = "v1"
_RECEIPT_SCHEMA = "m1-replication-rights-receipt-v1"
_EDGE_TYPE = "DCSync"
_MILESTONE = "REPLICATION_RIGHTS_PARENT"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _normalized(value: object) -> str:
    text = str(value or "").strip().casefold()
    return text[:-1] if text.endswith(".") else text


def _exact_subjects(values: Sequence[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        return ()
    result = {_normalized(value) for value in values}
    result.discard("")
    return tuple(sorted(result))


def build_replication_rights_query(expected_subjects: Sequence[object], target_domain: object) -> dict[str, object] | None:
    """Construct the only accepted M1 ISC5 query, or return ``None`` unbound."""
    subjects, target = _exact_subjects(expected_subjects), _normalized(target_domain)
    if not subjects or not target:
        return None
    return {
        "query": (
            "MATCH (p)-[e]->(t:Domain) WHERE toLower(p.name) IN $principals "
            "AND type(e) = $edge_type AND toLower(t.name) = $target "
            "RETURN DISTINCT p.name AS principal, type(e) AS type, t.name AS target"
        ),
        "parameters": {"principals": list(subjects), "edge_type": _EDGE_TYPE, "target": target},
    }


def _canonical_rows(edge_rows: object) -> tuple[tuple[str, str, str], ...] | None:
    if not isinstance(edge_rows, list):
        return None
    rows: list[tuple[str, str, str]] = []
    for row in edge_rows:
        if not isinstance(row, Mapping):
            return None
        principal, edge_type, target = row.get("principal"), row.get("type"), row.get("target")
        if not all(isinstance(item, str) and item.strip() for item in (principal, edge_type, target)):
            return None
        rows.append((_normalized(principal), str(edge_type).strip(), _normalized(target)))
    return tuple(sorted(set(rows)))


def replication_rights_verifier_input(
    expected_subjects: Sequence[object], target_domain: object, *, expected_ingest_job_id: object,
    expected_source_artifact_id: object, expected_source_artifact_sha256: object,
) -> dict[str, object] | None:
    """Return the exact query/lineage input a future read adapter must commit."""
    query = build_replication_rights_query(expected_subjects, target_domain)
    job, artifact_id = str(expected_ingest_job_id or "").strip(), str(expected_source_artifact_id or "").strip()
    artifact_sha = _normalized(expected_source_artifact_sha256)
    if query is None or not job or not artifact_id or not _HEX64.fullmatch(artifact_sha):
        return None
    return {
        "schema": _RECEIPT_SCHEMA, "query": query["query"], "parameters": query["parameters"],
        "expected_ingest_job_id": job, "expected_source_artifact_id": artifact_id,
        "expected_source_artifact_sha256": artifact_sha,
    }


def replication_rights_verifier_result(edge_rows: object, *, query_status: object) -> dict[str, object] | None:
    """Canonicalize the unordered returned edge rows for an envelope commitment."""
    rows, status = _canonical_rows(edge_rows), _normalized(query_status)
    if rows is None or not status:
        return None
    return {
        "schema": _RECEIPT_SCHEMA, "query_status": status,
        "rows": [{"principal": p, "type": e, "target": t} for p, e, t in rows],
    }


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if text and parsed.tzinfo is not None else None


def _bound_receipt(
    receipt: object, *, expected_engagement_id: object, verifier_input: Mapping[str, object],
    verifier_result: Mapping[str, object], expected_ingest_job_id: object,
    expected_source_artifact_id: object, expected_source_artifact_sha256: object,
    observed_at: object, max_age_seconds: object,
) -> proof_boundary.ProofEnvelope | None:
    engagement, job = str(expected_engagement_id or "").strip(), str(expected_ingest_job_id or "").strip()
    artifact_id, artifact_sha = str(expected_source_artifact_id or "").strip(), _normalized(expected_source_artifact_sha256)
    try:
        max_age = float(max_age_seconds)
    except (TypeError, ValueError):
        return None
    if not engagement or not job or not artifact_id or not _HEX64.fullmatch(artifact_sha) or max_age < 0:
        return None
    envelope = receipt if isinstance(receipt, proof_boundary.ProofEnvelope) else proof_boundary.ProofEnvelope.from_dict(receipt)
    if (
        envelope is None or envelope.persistence_state != proof_boundary.ADMITTED
        or envelope.origin != proof_boundary.ORIGIN_BLOODHOUND_INGEST
        or envelope.verifier_id != VERIFIER_ID or envelope.verifier_version != VERIFIER_VERSION
        or envelope.ingest_job_id != job or envelope.source_artifact_id != artifact_id
        or envelope.source_artifact_sha256 != artifact_sha
    ):
        return None
    admission = proof_boundary.admit_runtime_envelope(
        envelope, current_engagement_id=engagement, expected_verifier_id=VERIFIER_ID,
        expected_verifier_input_sha256=proof_boundary.canonical_commitment_sha256(verifier_input),
        expected_verifier_result_sha256=proof_boundary.canonical_commitment_sha256(verifier_result),
    )
    captured, observed = _parse_time(envelope.captured_at), _parse_time(observed_at)
    if not admission.admitted or captured is None or observed is None:
        return None
    return envelope if 0 <= (observed - captured).total_seconds() <= max_age else None


def _observation_id(receipt: proof_boundary.ProofEnvelope, subjects: tuple[str, ...], target: str,
                    rows: tuple[tuple[str, str, str], ...]) -> str:
    payload = {"receipt_hash": receipt.hash, "subjects": subjects, "target": target, "rows": rows}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parent_replication_rights_observation(
    edge_rows: object, *, expected_subjects: Sequence[object], target_domain: object,
    collection_receipt: object, expected_engagement_id: object, expected_ingest_job_id: object,
    expected_source_artifact_id: object, expected_source_artifact_sha256: object,
    observed_at: object, max_age_seconds: object,
) -> effect_registry.OracleObservation | None:
    """Return an observation only for an exactly committed successful query.

    Empty committed rows are an observed negative. Any nonmatching returned row
    is a query/response inconsistency and therefore unmeasured.
    """
    subjects, target, rows = _exact_subjects(expected_subjects), _normalized(target_domain), _canonical_rows(edge_rows)
    verifier_input = replication_rights_verifier_input(
        expected_subjects, target_domain, expected_ingest_job_id=expected_ingest_job_id,
        expected_source_artifact_id=expected_source_artifact_id,
        expected_source_artifact_sha256=expected_source_artifact_sha256,
    )
    verifier_result = replication_rights_verifier_result(edge_rows, query_status="success")
    if not subjects or not target or rows is None or verifier_input is None or verifier_result is None:
        return None
    receipt = _bound_receipt(
        collection_receipt, expected_engagement_id=expected_engagement_id, verifier_input=verifier_input,
        verifier_result=verifier_result, expected_ingest_job_id=expected_ingest_job_id,
        expected_source_artifact_id=expected_source_artifact_id,
        expected_source_artifact_sha256=expected_source_artifact_sha256, observed_at=observed_at,
        max_age_seconds=max_age_seconds,
    )
    if receipt is None or any(p not in subjects or e != _EDGE_TYPE or t != target for p, e, t in rows):
        return None
    contract = effect_registry.contract_for(_MILESTONE)
    return effect_registry.OracleObservation(
        registry_version=effect_registry.REGISTRY_VERSION, oracle_id=contract.oracle_id,
        origin=effect_registry.ProofOrigin.PAYLOAD_BLOODHOUND_FACT, proof_kind="replication_rights",
        outcome=bool(rows), observation_id=_observation_id(receipt, subjects, target, rows), target=target,
        subject=rows[0][0] if rows else "", envelope_id=receipt.hash,
    )
