"""Schema and redaction helpers for trajectory transition records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1

EVIDENCE_ROLE_DIAGNOSTIC_ONLY = "diagnostic_only"
EVIDENCE_ROLE_EMPIRICAL_OUTCOME = "empirical_outcome"
EVIDENCE_ROLE_EMPIRICAL_NEGATIVE = "empirical_negative"

OUTCOME_INDEPENDENTLY_OBSERVED = "independently_observed"
OUTCOME_DIAGNOSTIC_ONLY = "diagnostic_only"

LABEL_SOURCE_CLASSIFIER = "classifier"
LABEL_SOURCE_HUMAN = "human_annotation"
LABEL_SOURCE_MYTHIC_PROOF = "mythic_proof"
LABEL_SOURCE_BLOODHOUND = "verified_bloodhound_lineage"
LABEL_SOURCE_DIAGNOSTIC_ONLY = "diagnostic_only"

TRANSITION_OUTCOMES = frozenset({"achieved", "blocked", "failed", "partial", "unknown"})

_NTLM_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_AES_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_PASSWORD_KV_RE = re.compile(r"(?i)\b(password|passwd|pwd|secret)\s*[:=]\s*([^\s,;]+)")
_SECRET_JSON_KEY_RE = re.compile(
    r"(?i)((?:\\?[\"'])(?:[A-Za-z0-9_.-]*"
    r"(?:password|passwd|pwd|secret|credential)"
    r"[A-Za-z0-9_.-]*)(?:\\?[\"'])\s*:\s*(?:\\?[\"']))([^\"'\\\r\n]+)"
)
_SECRET_CONTEXT_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|credential|plaintext)\b[^`'\"\r\n]{0,80}[`'\"])([^`'\"\r\n]{4,})([`'\"])"
)
_SECRET_COLON_QUOTED_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|credential|plaintext)\b[^`'\":\r\n]{0,80}:\s*[`'\"])([^`'\"\r\n]{4,})([`'\"])"
)
_SECRET_FOR_QUOTED_TARGET_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|credential|plaintext)\b[^`'\"\r\n]{0,80}[`'\"][^`'\"\r\n]{1,120}[`'\"]\s*:\s*[`'\"])([^`'\"\r\n]{4,})([`'\"])"
)
_SAGE_GENERATED_SECRET_RE = re.compile(r"\bSage(?:Pfx|Cert)-[A-Za-z0-9_.-]+")
_PFX_BASE64_RE = re.compile(r"(?i)(\bPFX_BASE64=)([A-Za-z0-9+/=]{40,})")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def secret_handle(value: str, kind: str = "secret") -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"<{kind}:sha256:{digest}>"


def redact_text(value: Any) -> str:
    """Return text with common credential material replaced by stable handles."""
    text = "" if value is None else str(value)

    def repl_aes(match: re.Match[str]) -> str:
        return secret_handle(match.group(0).lower(), "aes256")

    def repl_ntlm(match: re.Match[str]) -> str:
        return secret_handle(match.group(0).lower(), "ntlm")

    def repl_password(match: re.Match[str]) -> str:
        key = match.group(1)
        return f"{key}=<password:redacted>"

    def repl_secret_json(match: re.Match[str]) -> str:
        return f"{match.group(1)}<password:redacted>"

    def repl_secret_context(match: re.Match[str]) -> str:
        return f"{match.group(1)}<password:redacted>{match.group(3)}"

    def repl_secret_colon_quoted(match: re.Match[str]) -> str:
        return f"{match.group(1)}<password:redacted>{match.group(3)}"

    def repl_sage_secret(match: re.Match[str]) -> str:
        return secret_handle(match.group(0), "sage-secret")

    def repl_pfx_base64(match: re.Match[str]) -> str:
        return f"{match.group(1)}<base64_blob>"

    text = _AES_RE.sub(repl_aes, text)
    text = _NTLM_RE.sub(repl_ntlm, text)
    text = _PASSWORD_KV_RE.sub(repl_password, text)
    text = _SAGE_GENERATED_SECRET_RE.sub(repl_sage_secret, text)
    text = _PFX_BASE64_RE.sub(repl_pfx_base64, text)
    text = _SECRET_JSON_KEY_RE.sub(repl_secret_json, text)
    text = _SECRET_FOR_QUOTED_TARGET_RE.sub(repl_secret_colon_quoted, text)
    text = _SECRET_COLON_QUOTED_RE.sub(repl_secret_colon_quoted, text)
    text = _SECRET_CONTEXT_RE.sub(repl_secret_context, text)
    return text


@dataclass(frozen=True)
class SourceArtifact:
    path: str
    kind: str
    size: int
    mtime: str
    readable: bool
    sensitive: bool = True
    sha256: str | None = None
    note: str = ""


@dataclass(frozen=True)
class TransitionCommand:
    payload_command: str
    adapter: str = ""
    constructed_from_builder: bool = False
    argument_features: tuple[str, ...] = field(default_factory=tuple)
    parameters_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransitionObservation:
    kind: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    excerpt: str = ""
    source: str = ""


@dataclass(frozen=True)
class TransitionVerifier:
    status: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    evidence: dict[str, Any] = field(default_factory=dict)
    verifier_id: str = ""
    verifier_version: str = "v1"
    proof_ids: tuple[str, ...] = field(default_factory=tuple)
    admissible_proof: bool = False


@dataclass(frozen=True)
class TransitionRepair:
    kind: str
    retry_budget: int = 0
    notes: str = ""
    applied: bool = False
    independently_verified_outcome: bool | None = None
    label_source: str = LABEL_SOURCE_CLASSIFIER


@dataclass(frozen=True)
class TransitionRecord:
    run_id: str
    source_files: tuple[str, ...]
    objective: str
    capability: str
    observations: tuple[TransitionObservation, ...]
    verifier: TransitionVerifier
    failure_label: str
    repair: TransitionRepair | None = None
    env_fingerprint: dict[str, Any] = field(default_factory=dict)
    state_before: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    commands: tuple[TransitionCommand, ...] = field(default_factory=tuple)
    state_after: dict[str, Any] = field(default_factory=dict)
    episode_id: str = ""
    engagement_id: str = ""
    decision_id: str = ""
    transaction_id: str = ""
    parent_transaction_id: str = ""
    callback_id: str = ""
    task_ids: tuple[str, ...] = field(default_factory=tuple)
    proof_ids: tuple[str, ...] = field(default_factory=tuple)
    proof_envelope: dict[str, Any] = field(default_factory=dict)
    harness_candidate_version: str = ""
    policy_version: str = ""
    effective_backend: str = ""
    effective_request_id: str = ""
    raw_frontier_hash: str = ""
    admissible_frontier_hash: str = ""
    semantic_candidate_ids: tuple[str, ...] = field(default_factory=tuple)
    normalized_state_before: dict[str, Any] = field(default_factory=dict)
    normalized_state_after: dict[str, Any] = field(default_factory=dict)
    label_source: str = LABEL_SOURCE_CLASSIFIER
    evidence_role: str = EVIDENCE_ROLE_DIAGNOSTIC_ONLY
    outcome_source: str = OUTCOME_DIAGNOSTIC_ONLY
    transition_outcome: str = "unknown"
    cost: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    termination: dict[str, Any] = field(default_factory=dict)
    proof_envelope_ref: str = ""
    topology_family: str = ""
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now)

    @property
    def is_diagnostic_only(self) -> bool:
        return (
            int(self.schema_version or 0) < SCHEMA_VERSION
            or self.evidence_role == EVIDENCE_ROLE_DIAGNOSTIC_ONLY
            or self.outcome_source == OUTCOME_DIAGNOSTIC_ONLY
        )

    @property
    def has_admissible_proof(self) -> bool:
        proof = self.proof_envelope if isinstance(self.proof_envelope, dict) else {}
        return (
            self.verifier.admissible_proof is True
            and str(proof.get("scope") or "").casefold() == "runtime"
            and str(proof.get("persistence_state") or "admitted").casefold() == "admitted"
            and str(proof.get("origin") or "").casefold()
            in {"mythic_task", "mythic_artifact", "mythic_credential", "bloodhound_ingest"}
            and bool(str(proof.get("transaction_id") or self.transaction_id or "").strip())
            and bool(str(proof.get("callback_id") or self.callback_id or "").strip())
            and bool(str(proof.get("task_id") or "").strip())
            and str(proof.get("terminal_status") or "").casefold() in {"completed", "complete", "success", "succeeded"}
            and bool(str(proof.get("verifier_id") or "").strip())
        )

    @property
    def positive_repair_evidence(self) -> bool:
        return bool(
            not self.is_diagnostic_only
            and self.repair is not None
            and self.repair.applied is True
            and self.repair.independently_verified_outcome is True
            and self.has_admissible_proof
        )

    @property
    def content_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.to_json_line().encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TransitionRecord":
        observations = tuple(TransitionObservation(**item) for item in data.get("observations", ()))
        commands = tuple(TransitionCommand(**item) for item in data.get("commands", ()))
        verifier_data = data.get("verifier", {"status": "unknown"})
        verifier_data = verifier_data if isinstance(verifier_data, dict) else {"status": "unknown"}
        verifier = TransitionVerifier(
            status=str(verifier_data.get("status") or "unknown"),
            labels=tuple(verifier_data.get("labels") or ()),
            evidence=dict(verifier_data.get("evidence") or {}),
            verifier_id=str(verifier_data.get("verifier_id") or ""),
            verifier_version=str(verifier_data.get("verifier_version") or "v1"),
            proof_ids=tuple(verifier_data.get("proof_ids") or ()),
            admissible_proof=verifier_data.get("admissible_proof") is True,
        )
        repair_data = data.get("repair")
        repair = (
            TransitionRepair(
                kind=str(repair_data.get("kind") or ""),
                retry_budget=int(repair_data.get("retry_budget") or 0),
                notes=str(repair_data.get("notes") or ""),
                applied=repair_data.get("applied") is True,
                independently_verified_outcome=(
                    repair_data.get("independently_verified_outcome")
                    if isinstance(repair_data.get("independently_verified_outcome"), bool)
                    else None
                ),
                label_source=str(repair_data.get("label_source") or LABEL_SOURCE_CLASSIFIER),
            )
            if isinstance(repair_data, dict)
            else None
        )
        schema_version = int(data.get("schema_version", LEGACY_SCHEMA_VERSION) or LEGACY_SCHEMA_VERSION)
        legacy = schema_version < SCHEMA_VERSION
        evidence_role = str(data.get("evidence_role") or "")
        outcome_source = str(data.get("outcome_source") or "")
        label_source = str(data.get("label_source") or "")
        if legacy:
            evidence_role = EVIDENCE_ROLE_DIAGNOSTIC_ONLY
            outcome_source = OUTCOME_DIAGNOSTIC_ONLY
            label_source = LABEL_SOURCE_DIAGNOSTIC_ONLY
            if repair is not None:
                repair = TransitionRepair(
                    kind=repair.kind,
                    retry_budget=repair.retry_budget,
                    notes=repair.notes,
                    applied=False,
                    independently_verified_outcome=None,
                    label_source=LABEL_SOURCE_DIAGNOSTIC_ONLY,
                )
        else:
            evidence_role = evidence_role or EVIDENCE_ROLE_DIAGNOSTIC_ONLY
            outcome_source = outcome_source or OUTCOME_DIAGNOSTIC_ONLY
            label_source = label_source or LABEL_SOURCE_CLASSIFIER
        return cls(
            run_id=str(data.get("run_id", "")),
            source_files=tuple(data.get("source_files") or ()),
            objective=str(data.get("objective", "")),
            capability=str(data.get("capability", "")),
            observations=observations,
            verifier=verifier,
            failure_label=str(data.get("failure_label", "")),
            repair=repair,
            env_fingerprint=dict(data.get("env_fingerprint") or {}),
            state_before=dict(data.get("state_before") or {}),
            inputs=dict(data.get("inputs") or {}),
            commands=commands,
            state_after=dict(data.get("state_after") or {}),
            episode_id=str(data.get("episode_id") or ""),
            engagement_id=str(data.get("engagement_id") or ""),
            decision_id=str(data.get("decision_id") or ""),
            transaction_id=str(data.get("transaction_id") or ""),
            parent_transaction_id=str(data.get("parent_transaction_id") or ""),
            callback_id=str(data.get("callback_id") or ""),
            task_ids=tuple(str(item) for item in (data.get("task_ids") or ()) if str(item)),
            proof_ids=tuple(str(item) for item in (data.get("proof_ids") or ()) if str(item)),
            proof_envelope=dict(data.get("proof_envelope") or {}),
            harness_candidate_version=str(data.get("harness_candidate_version") or ""),
            policy_version=str(data.get("policy_version") or ""),
            effective_backend=str(data.get("effective_backend") or ""),
            effective_request_id=str(data.get("effective_request_id") or ""),
            raw_frontier_hash=str(data.get("raw_frontier_hash") or ""),
            admissible_frontier_hash=str(data.get("admissible_frontier_hash") or ""),
            semantic_candidate_ids=tuple(str(item) for item in (data.get("semantic_candidate_ids") or ()) if str(item)),
            normalized_state_before=dict(data.get("normalized_state_before") or {}),
            normalized_state_after=dict(data.get("normalized_state_after") or {}),
            label_source=label_source,
            evidence_role=evidence_role,
            outcome_source=outcome_source,
            transition_outcome=(
                str(data.get("transition_outcome") or "unknown").casefold()
                if str(data.get("transition_outcome") or "unknown").casefold() in TRANSITION_OUTCOMES
                else "unknown"
            ),
            cost=dict(data.get("cost") or {}),
            risk=dict(data.get("risk") or {}),
            termination=dict(data.get("termination") or {}),
            proof_envelope_ref=str(data.get("proof_envelope_ref") or ""),
            topology_family=str(data.get("topology_family") or ""),
            schema_version=schema_version,
            created_at=str(data.get("created_at") or utc_now()),
        )


def load_jsonl(path: str) -> list[TransitionRecord]:
    records: list[TransitionRecord] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(TransitionRecord.from_dict(json.loads(line)))
    return records


def write_jsonl(path: str, records: list[TransitionRecord], append: bool = False) -> None:
    mode = "a" if append else "w"
    with open(path, mode, encoding="utf-8") as handle:
        for record in records:
            handle.write(record.to_json_line())
            handle.write("\n")
