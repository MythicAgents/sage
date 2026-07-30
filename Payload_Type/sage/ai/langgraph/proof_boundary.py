"""Immutable proof envelopes and fail-closed runtime achievement admission.

This module is the only place that decides whether evidence may create a runtime
achievement. It intentionally knows nothing about GOAD, capability ordering, or
planner policy. It accepts only generic Mythic task/artifact/credential and
BloodHound ingest lineage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping


PROOF_SCHEMA = "proof-envelope-v2"
RUNTIME_SCOPE = "runtime"
SYNTHETIC_EVAL_SCOPE = "synthetic_eval"

ORIGIN_MYTHIC_TASK = "mythic_task"
ORIGIN_MYTHIC_ARTIFACT = "mythic_artifact"
ORIGIN_MYTHIC_CREDENTIAL = "mythic_credential"
ORIGIN_BLOODHOUND_INGEST = "bloodhound_ingest"

ALLOWED_RUNTIME_ORIGINS = frozenset({
    ORIGIN_MYTHIC_TASK,
    ORIGIN_MYTHIC_ARTIFACT,
    ORIGIN_MYTHIC_CREDENTIAL,
    ORIGIN_BLOODHOUND_INGEST,
})

DISALLOWED_RUNTIME_ORIGINS = frozenset({
    "host",
    "local_artifact",
    "mcp",
    "evaluator",
    "referee",
    "sandbox_exec",
    "synthetic_eval",
})

ADMITTED = "admitted"
LEGACY_UNVERIFIED = "legacy_unverified"
REJECTED = "rejected"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_SUCCESS = frozenset({"completed", "complete", "success", "succeeded"})

# Callers may attach arbitrary diagnostic context, but these fields define proof
# identity and may only come from the boundary itself.
RESERVED_EVIDENCE_KEYS = frozenset({
    "proof_envelope",
    "proof_persistence_state",
    "proof_admission_reason",
    "proof_hash",
    "origin",
    "scope",
    "engagement_id",
    "callback_id",
    "transaction_id",
    "mythic_task_id",
    "task_id",
    "terminal_task_status",
    "terminal_status",
    "command",
    "artifact_id",
    "artifact_sha256",
    "credential_id",
    "bloodhound_job_id",
    "ingest_job_id",
    "ingest_status",
    "source_artifact_id",
    "source_artifact_sha256",
    "verifier_id",
    "verifier_version",
    "verifier_hash",
    "verifier_input_sha256",
    "verifier_result_sha256",
    "authorization_manifest_id",
    "authorization_manifest_version",
    "authorization_manifest_sha256",
    "authorization_decision_id",
    "authorization_decision",
    "authorization_reason_code",
    "action_envelope_sha256",
    "authorization_gate_version",
    "decision_origin",
    "policy_decision_id",
    "operator_authorization_id",
    "cell_authorization_id",
    "enforcement_projection_sha256",
    "captured_at",
    "persistence_state",
})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).casefold()


def _clean_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        text_key = _text(key)
        if not text_key or text_key in RESERVED_EVIDENCE_KEYS:
            continue
        try:
            json.dumps(item, default=str)
            out[text_key] = item
        except Exception:
            out[text_key] = str(item)
    return out


@dataclass(frozen=True)
class ProofEnvelope:
    schema: str = PROOF_SCHEMA
    scope: str = RUNTIME_SCOPE
    origin: str = ""
    engagement_id: str = ""
    callback_id: str = ""
    transaction_id: str = ""
    task_id: str = ""
    terminal_status: str = ""
    command: str = ""
    artifact_id: str = ""
    artifact_sha256: str = ""
    credential_id: str = ""
    ingest_job_id: str = ""
    ingest_status: str = ""
    source_artifact_id: str = ""
    source_artifact_sha256: str = ""
    verifier_id: str = ""
    verifier_version: str = "v1"
    verifier_hash: str = ""
    verifier_input_sha256: str = ""
    verifier_result_sha256: str = ""
    authorization_manifest_id: str = ""
    authorization_manifest_version: str = ""
    authorization_manifest_sha256: str = ""
    authorization_decision_id: str = ""
    authorization_decision: str = ""
    authorization_reason_code: str = ""
    action_envelope_sha256: str = ""
    authorization_gate_version: str = ""
    decision_origin: str = ""
    policy_decision_id: str = ""
    operator_authorization_id: str = ""
    cell_authorization_id: str = ""
    enforcement_projection_sha256: str = ""
    captured_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    persistence_state: str = ADMITTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _text(self.schema) or PROOF_SCHEMA)
        object.__setattr__(self, "scope", _lower(self.scope))
        object.__setattr__(self, "origin", _lower(self.origin))
        object.__setattr__(self, "engagement_id", _text(self.engagement_id))
        object.__setattr__(self, "callback_id", _text(self.callback_id))
        object.__setattr__(self, "transaction_id", _text(self.transaction_id))
        object.__setattr__(self, "task_id", _text(self.task_id))
        object.__setattr__(self, "terminal_status", _lower(self.terminal_status))
        object.__setattr__(self, "command", _text(self.command))
        object.__setattr__(self, "artifact_id", _text(self.artifact_id))
        object.__setattr__(self, "artifact_sha256", _lower(self.artifact_sha256))
        object.__setattr__(self, "credential_id", _text(self.credential_id))
        object.__setattr__(self, "ingest_job_id", _text(self.ingest_job_id))
        object.__setattr__(self, "ingest_status", _lower(self.ingest_status))
        object.__setattr__(self, "source_artifact_id", _text(self.source_artifact_id))
        object.__setattr__(self, "source_artifact_sha256", _lower(self.source_artifact_sha256))
        object.__setattr__(self, "verifier_id", _text(self.verifier_id))
        object.__setattr__(self, "verifier_version", _text(self.verifier_version) or "v1")
        object.__setattr__(self, "verifier_hash", _lower(self.verifier_hash))
        object.__setattr__(self, "verifier_input_sha256", _lower(self.verifier_input_sha256))
        object.__setattr__(self, "verifier_result_sha256", _lower(self.verifier_result_sha256))
        object.__setattr__(self, "authorization_manifest_id", _text(self.authorization_manifest_id))
        object.__setattr__(self, "authorization_manifest_version", _text(self.authorization_manifest_version))
        object.__setattr__(self, "authorization_manifest_sha256", _lower(self.authorization_manifest_sha256))
        object.__setattr__(self, "authorization_decision_id", _text(self.authorization_decision_id))
        object.__setattr__(self, "authorization_decision", _lower(self.authorization_decision))
        object.__setattr__(self, "authorization_reason_code", _text(self.authorization_reason_code))
        object.__setattr__(self, "action_envelope_sha256", _lower(self.action_envelope_sha256))
        object.__setattr__(self, "authorization_gate_version", _text(self.authorization_gate_version))
        object.__setattr__(self, "decision_origin", _text(self.decision_origin))
        object.__setattr__(self, "policy_decision_id", _text(self.policy_decision_id))
        object.__setattr__(self, "operator_authorization_id", _text(self.operator_authorization_id))
        object.__setattr__(self, "cell_authorization_id", _text(self.cell_authorization_id))
        object.__setattr__(self, "enforcement_projection_sha256", _lower(self.enforcement_projection_sha256))
        object.__setattr__(self, "captured_at", _text(self.captured_at))
        object.__setattr__(self, "persistence_state", _lower(self.persistence_state) or ADMITTED)
        object.__setattr__(self, "metadata", MappingProxyType(_clean_metadata(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "scope": self.scope,
            "origin": self.origin,
            "engagement_id": self.engagement_id,
            "callback_id": self.callback_id,
            "transaction_id": self.transaction_id,
            "task_id": self.task_id,
            "terminal_status": self.terminal_status,
            "command": self.command,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "credential_id": self.credential_id,
            "ingest_job_id": self.ingest_job_id,
            "ingest_status": self.ingest_status,
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_sha256": self.source_artifact_sha256,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "verifier_hash": self.verifier_hash,
            "verifier_input_sha256": self.verifier_input_sha256,
            "verifier_result_sha256": self.verifier_result_sha256,
            "authorization_manifest_id": self.authorization_manifest_id,
            "authorization_manifest_version": self.authorization_manifest_version,
            "authorization_manifest_sha256": self.authorization_manifest_sha256,
            "authorization_decision_id": self.authorization_decision_id,
            "authorization_decision": self.authorization_decision,
            "authorization_reason_code": self.authorization_reason_code,
            "action_envelope_sha256": self.action_envelope_sha256,
            "authorization_gate_version": self.authorization_gate_version,
            "decision_origin": self.decision_origin,
            "policy_decision_id": self.policy_decision_id,
            "operator_authorization_id": self.operator_authorization_id,
            "cell_authorization_id": self.cell_authorization_id,
            "enforcement_projection_sha256": self.enforcement_projection_sha256,
            "captured_at": self.captured_at,
            "metadata": dict(self.metadata),
            "persistence_state": self.persistence_state,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ProofEnvelope | None":
        if not isinstance(value, dict) or not value:
            return None
        try:
            return cls(
                schema=value.get("schema") or PROOF_SCHEMA,
                scope=value.get("scope") or "",
                origin=value.get("origin") or "",
                engagement_id=value.get("engagement_id") or "",
                callback_id=value.get("callback_id") or "",
                transaction_id=value.get("transaction_id") or "",
                task_id=value.get("task_id") or value.get("mythic_task_id") or "",
                terminal_status=value.get("terminal_status") or value.get("terminal_task_status") or "",
                command=value.get("command") or "",
                artifact_id=value.get("artifact_id") or "",
                artifact_sha256=value.get("artifact_sha256") or "",
                credential_id=value.get("credential_id") or "",
                ingest_job_id=value.get("ingest_job_id") or value.get("bloodhound_job_id") or "",
                ingest_status=value.get("ingest_status") or "",
                source_artifact_id=value.get("source_artifact_id") or "",
                source_artifact_sha256=value.get("source_artifact_sha256") or "",
                verifier_id=value.get("verifier_id") or "",
                verifier_version=value.get("verifier_version") or "v1",
                verifier_hash=value.get("verifier_hash") or "",
                verifier_input_sha256=value.get("verifier_input_sha256") or "",
                verifier_result_sha256=value.get("verifier_result_sha256") or "",
                authorization_manifest_id=value.get("authorization_manifest_id") or "",
                authorization_manifest_version=value.get("authorization_manifest_version") or "",
                authorization_manifest_sha256=value.get("authorization_manifest_sha256") or "",
                authorization_decision_id=value.get("authorization_decision_id") or "",
                authorization_decision=value.get("authorization_decision") or "",
                authorization_reason_code=value.get("authorization_reason_code") or "",
                action_envelope_sha256=value.get("action_envelope_sha256") or "",
                authorization_gate_version=value.get("authorization_gate_version") or "",
                decision_origin=value.get("decision_origin") or "",
                policy_decision_id=value.get("policy_decision_id") or "",
                operator_authorization_id=value.get("operator_authorization_id") or "",
                cell_authorization_id=value.get("cell_authorization_id") or "",
                enforcement_projection_sha256=value.get("enforcement_projection_sha256") or "",
                captured_at=value.get("captured_at") or "",
                metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else {},
                persistence_state=value.get("persistence_state") or ADMITTED,
            )
        except Exception:
            return None

    @property
    def hash(self) -> str:
        payload = self.to_dict()
        payload.pop("persistence_state", None)
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class ProofAdmission:
    admitted: bool
    envelope: ProofEnvelope | None
    reason: str
    persistence_state: str


def stable_verifier_hash(verifier_id: str, verifier_version: str = "v1") -> str:
    blob = f"{_text(verifier_id)}::{_text(verifier_version) or 'v1'}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def canonical_commitment_sha256(value: Any) -> str:
    """Return a stable commitment to a verifier input or result without persisting the value itself."""
    if value is None:
        return ""
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _valid_sha256(value: str) -> bool:
    return bool(_HEX64_RE.fullmatch(_lower(value)))


def admit_runtime_envelope(
    envelope: ProofEnvelope | None,
    *,
    current_engagement_id: str,
    expected_callback_id: Any = "",
    expected_transaction_id: Any = "",
    expected_task_id: Any = "",
    expected_terminal_status: str = "",
    expected_verifier_id: str = "",
    expected_verifier_input_sha256: str = "",
    expected_verifier_result_sha256: str = "",
    expected_artifact_id: str = "",
    expected_authorization: Mapping[str, Any] | None = None,
) -> ProofAdmission:
    if envelope is None:
        return ProofAdmission(False, None, "missing proof envelope", LEGACY_UNVERIFIED)
    if envelope.schema != PROOF_SCHEMA:
        return ProofAdmission(False, envelope, "unsupported proof schema", REJECTED)
    if envelope.scope != RUNTIME_SCOPE:
        return ProofAdmission(False, envelope, "runtime achievement requires runtime scope", REJECTED)
    if envelope.origin in DISALLOWED_RUNTIME_ORIGINS or envelope.origin not in ALLOWED_RUNTIME_ORIGINS:
        return ProofAdmission(False, envelope, f"disallowed runtime proof origin: {envelope.origin or 'missing'}", REJECTED)
    engagement_id = _text(current_engagement_id)
    if not engagement_id or envelope.engagement_id != engagement_id:
        return ProofAdmission(False, envelope, "proof engagement does not match current engagement", REJECTED)
    if not envelope.callback_id:
        return ProofAdmission(False, envelope, "missing callback lineage", REJECTED)
    if _text(expected_callback_id) and envelope.callback_id != _text(expected_callback_id):
        return ProofAdmission(False, envelope, "proof callback does not match claimed effect", REJECTED)
    if not envelope.transaction_id:
        return ProofAdmission(False, envelope, "missing semantic transaction lineage", REJECTED)
    if _text(expected_transaction_id) and envelope.transaction_id != _text(expected_transaction_id):
        return ProofAdmission(False, envelope, "proof transaction does not match claimed effect", REJECTED)
    if not envelope.task_id:
        return ProofAdmission(False, envelope, "missing Mythic task lineage", REJECTED)
    if _text(expected_task_id) and envelope.task_id != _text(expected_task_id):
        return ProofAdmission(False, envelope, "proof task does not match verifier observation", REJECTED)
    if envelope.terminal_status not in _TERMINAL_SUCCESS:
        return ProofAdmission(False, envelope, "task is not terminal-success", REJECTED)
    if _lower(expected_terminal_status) and envelope.terminal_status != _lower(expected_terminal_status):
        return ProofAdmission(False, envelope, "proof terminal status does not match verifier observation", REJECTED)
    if not envelope.command:
        return ProofAdmission(False, envelope, "missing Mythic command lineage", REJECTED)
    if not envelope.verifier_id or not envelope.verifier_version or not _valid_sha256(envelope.verifier_hash):
        return ProofAdmission(False, envelope, "missing verifier lineage", REJECTED)
    if _text(expected_verifier_id) and envelope.verifier_id != _text(expected_verifier_id):
        return ProofAdmission(False, envelope, "proof verifier does not match claimed effect", REJECTED)
    if not _valid_sha256(envelope.verifier_input_sha256):
        return ProofAdmission(False, envelope, "missing canonical verifier input commitment", REJECTED)
    if _lower(expected_verifier_input_sha256) and envelope.verifier_input_sha256 != _lower(expected_verifier_input_sha256):
        return ProofAdmission(False, envelope, "verifier input commitment mismatch", REJECTED)
    if not _valid_sha256(envelope.verifier_result_sha256):
        return ProofAdmission(False, envelope, "missing canonical verifier result commitment", REJECTED)
    if _lower(expected_verifier_result_sha256) and envelope.verifier_result_sha256 != _lower(expected_verifier_result_sha256):
        return ProofAdmission(False, envelope, "verifier result commitment mismatch", REJECTED)
    if not envelope.captured_at:
        return ProofAdmission(False, envelope, "missing capture time", REJECTED)
    if envelope.origin == ORIGIN_MYTHIC_ARTIFACT:
        if not envelope.artifact_id or not _valid_sha256(envelope.artifact_sha256):
            return ProofAdmission(False, envelope, "artifact proof requires artifact id and sha256", REJECTED)
        if _text(expected_artifact_id) and envelope.artifact_id != _text(expected_artifact_id):
            return ProofAdmission(False, envelope, "artifact proof does not match claimed artifact", REJECTED)
    elif envelope.origin == ORIGIN_MYTHIC_CREDENTIAL:
        if not envelope.credential_id:
            return ProofAdmission(False, envelope, "credential proof requires credential id", REJECTED)
    elif envelope.origin == ORIGIN_BLOODHOUND_INGEST:
        if not envelope.ingest_job_id or envelope.ingest_status not in _TERMINAL_SUCCESS:
            return ProofAdmission(False, envelope, "BloodHound proof requires completed ingest job", REJECTED)
        if not envelope.source_artifact_id or not _valid_sha256(envelope.source_artifact_sha256):
            return ProofAdmission(False, envelope, "BloodHound proof requires source artifact lineage", REJECTED)
    authorization_fields = (
        envelope.authorization_manifest_id,
        envelope.authorization_manifest_version,
        envelope.authorization_manifest_sha256,
        envelope.authorization_decision_id,
        envelope.authorization_decision,
        envelope.authorization_reason_code,
        envelope.action_envelope_sha256,
        envelope.authorization_gate_version,
        envelope.decision_origin,
        envelope.operator_authorization_id,
        envelope.cell_authorization_id,
        envelope.enforcement_projection_sha256,
    )
    if any(authorization_fields):
        if (
            not envelope.authorization_manifest_id
            or not envelope.authorization_manifest_version
            or not _valid_sha256(envelope.authorization_manifest_sha256)
            or not envelope.authorization_decision_id
            or envelope.authorization_decision != "allow"
            or not envelope.authorization_reason_code
            or not _valid_sha256(envelope.action_envelope_sha256)
            or not envelope.authorization_gate_version
            or not envelope.decision_origin
            or not envelope.operator_authorization_id
            or not envelope.cell_authorization_id
            or not _valid_sha256(envelope.enforcement_projection_sha256)
        ):
            return ProofAdmission(False, envelope, "incomplete authorization lineage", REJECTED)
        expected = expected_authorization if isinstance(expected_authorization, Mapping) else {}
        joins = {
            "authorization_manifest_id": envelope.authorization_manifest_id,
            "authorization_manifest_version": envelope.authorization_manifest_version,
            "authorization_manifest_sha256": envelope.authorization_manifest_sha256,
            "authorization_decision_id": envelope.authorization_decision_id,
            "authorization_decision": envelope.authorization_decision,
            "authorization_reason_code": envelope.authorization_reason_code,
            "action_envelope_sha256": envelope.action_envelope_sha256,
            "authorization_gate_version": envelope.authorization_gate_version,
            "decision_origin": envelope.decision_origin,
            "policy_decision_id": envelope.policy_decision_id,
            "operator_authorization_id": envelope.operator_authorization_id,
            "cell_authorization_id": envelope.cell_authorization_id,
            "enforcement_projection_sha256": envelope.enforcement_projection_sha256,
        }
        for key, actual in joins.items():
            wanted = _text(expected.get(key))
            if wanted and actual != wanted:
                return ProofAdmission(False, envelope, f"authorization join mismatch: {key}", REJECTED)
    return ProofAdmission(True, envelope, "admitted runtime proof", ADMITTED)


def proof_from_evidence(evidence: Any) -> ProofEnvelope | None:
    if not isinstance(evidence, dict):
        return None
    return ProofEnvelope.from_dict(evidence.get("proof_envelope"))


def merge_untrusted_evidence(base: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(base or {})
    for key, value in dict(extra or {}).items():
        if key in RESERVED_EVIDENCE_KEYS:
            continue
        out[key] = value
    return out


def attach_proof(
    evidence: dict[str, Any] | None,
    envelope: ProofEnvelope | None,
    *,
    current_engagement_id: str,
    expected_callback_id: Any = "",
    expected_transaction_id: Any = "",
    expected_task_id: Any = "",
    expected_terminal_status: str = "",
    expected_verifier_id: str = "",
    expected_verifier_input_sha256: str = "",
    expected_verifier_result_sha256: str = "",
    expected_artifact_id: str = "",
    expected_authorization: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ProofAdmission]:
    out = merge_untrusted_evidence({}, evidence or {})
    admission = admit_runtime_envelope(
        envelope,
        current_engagement_id=current_engagement_id,
        expected_callback_id=expected_callback_id,
        expected_transaction_id=expected_transaction_id,
        expected_task_id=expected_task_id,
        expected_terminal_status=expected_terminal_status,
        expected_verifier_id=expected_verifier_id,
        expected_verifier_input_sha256=expected_verifier_input_sha256,
        expected_verifier_result_sha256=expected_verifier_result_sha256,
        expected_artifact_id=expected_artifact_id,
        expected_authorization=expected_authorization,
    )
    if envelope is not None:
        out["proof_envelope"] = envelope.to_dict()
        out["proof_hash"] = envelope.hash
        out["scope"] = envelope.scope
        out["origin"] = envelope.origin
        out["engagement_id"] = envelope.engagement_id
        out["callback_id"] = envelope.callback_id
        out["mythic_task_id"] = envelope.task_id
        out["terminal_task_status"] = envelope.terminal_status
        out["command"] = envelope.command
        if envelope.artifact_id:
            out["artifact_id"] = envelope.artifact_id
        if envelope.artifact_sha256:
            out["artifact_sha256"] = envelope.artifact_sha256
        if envelope.credential_id:
            out["credential_id"] = envelope.credential_id
        if envelope.ingest_job_id:
            out["bloodhound_job_id"] = envelope.ingest_job_id
        if envelope.ingest_status:
            out["ingest_status"] = envelope.ingest_status
        if envelope.source_artifact_id:
            out["source_artifact_id"] = envelope.source_artifact_id
        if envelope.source_artifact_sha256:
            out["source_artifact_sha256"] = envelope.source_artifact_sha256
        out["verifier_id"] = envelope.verifier_id
        out["verifier_version"] = envelope.verifier_version
        out["verifier_hash"] = envelope.verifier_hash
        out["verifier_input_sha256"] = envelope.verifier_input_sha256
        out["verifier_result_sha256"] = envelope.verifier_result_sha256
        if envelope.authorization_manifest_id:
            out["authorization_manifest_id"] = envelope.authorization_manifest_id
            out["authorization_manifest_version"] = envelope.authorization_manifest_version
            out["authorization_manifest_sha256"] = envelope.authorization_manifest_sha256
            out["authorization_decision_id"] = envelope.authorization_decision_id
            out["authorization_decision"] = envelope.authorization_decision
            out["authorization_reason_code"] = envelope.authorization_reason_code
            out["action_envelope_sha256"] = envelope.action_envelope_sha256
            out["authorization_gate_version"] = envelope.authorization_gate_version
            out["decision_origin"] = envelope.decision_origin
            out["policy_decision_id"] = envelope.policy_decision_id
            out["operator_authorization_id"] = envelope.operator_authorization_id
            out["cell_authorization_id"] = envelope.cell_authorization_id
            out["enforcement_projection_sha256"] = envelope.enforcement_projection_sha256
        out["captured_at"] = envelope.captured_at
    out["proof_persistence_state"] = admission.persistence_state
    out["proof_admission_reason"] = admission.reason
    return out, admission


def make_runtime_task_envelope(
    *,
    engagement_id: str,
    callback_id: Any,
    task_id: Any,
    terminal_status: str,
    command: str,
    verifier_id: str,
    captured_at: str,
    transaction_id: str = "",
    verifier_version: str = "v1",
    verifier_input: Any = None,
    verifier_result: Any = None,
    authorization: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProofEnvelope:
    authorization = authorization if isinstance(authorization, Mapping) else {}
    return ProofEnvelope(
        scope=RUNTIME_SCOPE,
        origin=ORIGIN_MYTHIC_TASK,
        engagement_id=engagement_id,
        callback_id=_text(callback_id),
        transaction_id=transaction_id,
        task_id=_text(task_id),
        terminal_status=terminal_status,
        command=command,
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        verifier_hash=stable_verifier_hash(verifier_id, verifier_version),
        verifier_input_sha256=canonical_commitment_sha256(verifier_input),
        verifier_result_sha256=canonical_commitment_sha256(verifier_result),
        authorization_manifest_id=authorization.get("authorization_manifest_id") or authorization.get("manifest_id") or "",
        authorization_manifest_version=authorization.get("authorization_manifest_version") or authorization.get("manifest_version") or "",
        authorization_manifest_sha256=authorization.get("authorization_manifest_sha256") or authorization.get("manifest_sha256") or "",
        authorization_decision_id=authorization.get("authorization_decision_id") or authorization.get("decision_id") or "",
        authorization_decision=authorization.get("authorization_decision") or authorization.get("decision") or "",
        authorization_reason_code=authorization.get("authorization_reason_code") or authorization.get("reason_code") or "",
        action_envelope_sha256=authorization.get("action_envelope_sha256") or "",
        authorization_gate_version=authorization.get("authorization_gate_version") or authorization.get("gate_version") or "",
        decision_origin=authorization.get("decision_origin") or "",
        policy_decision_id=authorization.get("policy_decision_id") or "",
        operator_authorization_id=authorization.get("operator_authorization_id") or "",
        cell_authorization_id=authorization.get("cell_authorization_id") or "",
        enforcement_projection_sha256=authorization.get("enforcement_projection_sha256") or "",
        captured_at=captured_at,
        metadata=metadata or {},
    )


def make_runtime_artifact_envelope(
    *,
    engagement_id: str,
    callback_id: Any,
    task_id: Any,
    terminal_status: str,
    command: str,
    artifact_id: str,
    artifact_sha256: str,
    verifier_id: str,
    captured_at: str,
    transaction_id: str = "",
    verifier_version: str = "v1",
    verifier_input: Any = None,
    verifier_result: Any = None,
    authorization: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProofEnvelope:
    authorization = authorization if isinstance(authorization, Mapping) else {}
    return ProofEnvelope(
        scope=RUNTIME_SCOPE,
        origin=ORIGIN_MYTHIC_ARTIFACT,
        engagement_id=engagement_id,
        callback_id=_text(callback_id),
        transaction_id=transaction_id,
        task_id=_text(task_id),
        terminal_status=terminal_status,
        command=command,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        verifier_hash=stable_verifier_hash(verifier_id, verifier_version),
        verifier_input_sha256=canonical_commitment_sha256(verifier_input),
        verifier_result_sha256=canonical_commitment_sha256(verifier_result),
        authorization_manifest_id=authorization.get("authorization_manifest_id") or authorization.get("manifest_id") or "",
        authorization_manifest_version=authorization.get("authorization_manifest_version") or authorization.get("manifest_version") or "",
        authorization_manifest_sha256=authorization.get("authorization_manifest_sha256") or authorization.get("manifest_sha256") or "",
        authorization_decision_id=authorization.get("authorization_decision_id") or authorization.get("decision_id") or "",
        authorization_decision=authorization.get("authorization_decision") or authorization.get("decision") or "",
        authorization_reason_code=authorization.get("authorization_reason_code") or authorization.get("reason_code") or "",
        action_envelope_sha256=authorization.get("action_envelope_sha256") or "",
        authorization_gate_version=authorization.get("authorization_gate_version") or authorization.get("gate_version") or "",
        decision_origin=authorization.get("decision_origin") or "",
        policy_decision_id=authorization.get("policy_decision_id") or "",
        operator_authorization_id=authorization.get("operator_authorization_id") or "",
        cell_authorization_id=authorization.get("cell_authorization_id") or "",
        enforcement_projection_sha256=authorization.get("enforcement_projection_sha256") or "",
        captured_at=captured_at,
        metadata=metadata or {},
    )


def make_runtime_credential_envelope(
    *,
    engagement_id: str,
    callback_id: Any,
    task_id: Any,
    terminal_status: str,
    command: str,
    credential_id: Any,
    verifier_id: str,
    captured_at: str,
    transaction_id: str = "",
    verifier_version: str = "v1",
    verifier_input: Any = None,
    verifier_result: Any = None,
    authorization: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProofEnvelope:
    authorization = authorization if isinstance(authorization, Mapping) else {}
    return ProofEnvelope(
        scope=RUNTIME_SCOPE,
        origin=ORIGIN_MYTHIC_CREDENTIAL,
        engagement_id=engagement_id,
        callback_id=_text(callback_id),
        transaction_id=transaction_id,
        task_id=_text(task_id),
        terminal_status=terminal_status,
        command=command,
        credential_id=_text(credential_id),
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        verifier_hash=stable_verifier_hash(verifier_id, verifier_version),
        verifier_input_sha256=canonical_commitment_sha256(verifier_input),
        verifier_result_sha256=canonical_commitment_sha256(verifier_result),
        authorization_manifest_id=authorization.get("authorization_manifest_id") or authorization.get("manifest_id") or "",
        authorization_manifest_version=authorization.get("authorization_manifest_version") or authorization.get("manifest_version") or "",
        authorization_manifest_sha256=authorization.get("authorization_manifest_sha256") or authorization.get("manifest_sha256") or "",
        authorization_decision_id=authorization.get("authorization_decision_id") or authorization.get("decision_id") or "",
        authorization_decision=authorization.get("authorization_decision") or authorization.get("decision") or "",
        authorization_reason_code=authorization.get("authorization_reason_code") or authorization.get("reason_code") or "",
        action_envelope_sha256=authorization.get("action_envelope_sha256") or "",
        authorization_gate_version=authorization.get("authorization_gate_version") or authorization.get("gate_version") or "",
        decision_origin=authorization.get("decision_origin") or "",
        policy_decision_id=authorization.get("policy_decision_id") or "",
        operator_authorization_id=authorization.get("operator_authorization_id") or "",
        cell_authorization_id=authorization.get("cell_authorization_id") or "",
        enforcement_projection_sha256=authorization.get("enforcement_projection_sha256") or "",
        captured_at=captured_at,
        metadata=metadata or {},
    )


def make_runtime_bloodhound_envelope(
    *,
    engagement_id: str,
    callback_id: Any,
    task_id: Any,
    terminal_status: str,
    command: str,
    ingest_job_id: Any,
    ingest_status: str,
    source_artifact_id: str,
    source_artifact_sha256: str,
    verifier_id: str,
    captured_at: str,
    transaction_id: str = "",
    verifier_version: str = "v1",
    verifier_input: Any = None,
    verifier_result: Any = None,
    authorization: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProofEnvelope:
    authorization = authorization if isinstance(authorization, Mapping) else {}
    return ProofEnvelope(
        scope=RUNTIME_SCOPE,
        origin=ORIGIN_BLOODHOUND_INGEST,
        engagement_id=engagement_id,
        callback_id=_text(callback_id),
        transaction_id=transaction_id,
        task_id=_text(task_id),
        terminal_status=terminal_status,
        command=command,
        ingest_job_id=_text(ingest_job_id),
        ingest_status=ingest_status,
        source_artifact_id=source_artifact_id,
        source_artifact_sha256=source_artifact_sha256,
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        verifier_hash=stable_verifier_hash(verifier_id, verifier_version),
        verifier_input_sha256=canonical_commitment_sha256(verifier_input),
        verifier_result_sha256=canonical_commitment_sha256(verifier_result),
        authorization_manifest_id=authorization.get("authorization_manifest_id") or authorization.get("manifest_id") or "",
        authorization_manifest_version=authorization.get("authorization_manifest_version") or authorization.get("manifest_version") or "",
        authorization_manifest_sha256=authorization.get("authorization_manifest_sha256") or authorization.get("manifest_sha256") or "",
        authorization_decision_id=authorization.get("authorization_decision_id") or authorization.get("decision_id") or "",
        authorization_decision=authorization.get("authorization_decision") or authorization.get("decision") or "",
        authorization_reason_code=authorization.get("authorization_reason_code") or authorization.get("reason_code") or "",
        action_envelope_sha256=authorization.get("action_envelope_sha256") or "",
        authorization_gate_version=authorization.get("authorization_gate_version") or authorization.get("gate_version") or "",
        decision_origin=authorization.get("decision_origin") or "",
        policy_decision_id=authorization.get("policy_decision_id") or "",
        operator_authorization_id=authorization.get("operator_authorization_id") or "",
        cell_authorization_id=authorization.get("cell_authorization_id") or "",
        enforcement_projection_sha256=authorization.get("enforcement_projection_sha256") or "",
        captured_at=captured_at,
        metadata=metadata or {},
    )


def synthetic_eval_envelope(
    *,
    verifier_id: str = "synthetic-test",
    captured_at: str = "1970-01-01T00:00:00+00:00",
    metadata: Mapping[str, Any] | None = None,
) -> ProofEnvelope:
    return ProofEnvelope(
        scope=SYNTHETIC_EVAL_SCOPE,
        origin="synthetic_eval",
        verifier_id=verifier_id,
        verifier_hash=stable_verifier_hash(verifier_id),
        captured_at=captured_at,
        metadata=metadata or {},
        persistence_state=LEGACY_UNVERIFIED,
    )
