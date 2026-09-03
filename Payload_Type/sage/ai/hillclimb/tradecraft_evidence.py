"""Typed, positive-allowlist evidence projection for tradecraft evaluation.

This module is evaluator-only.  It resolves source locators before emitting a
small typed projection; raw source documents, model reasoning, expected labels,
and caller-generated pass/fail fields never enter that projection.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


EVIDENCE_PROJECTION_SCHEMA = "sage.tradecraft-evidence-projection.v1"
_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_HANDLE_RE = re.compile(r"^h:[0-9a-f]{64}$")
_TRANSACTION_RE = re.compile(r"^t:[0-9a-f]{64}$")


class EvidenceValidationError(ValueError):
    """Raised when typed evidence is malformed, unbound, or contaminated."""


class Truth(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class EvidenceOrigin(str, Enum):
    MYTHIC_TASK = "mythic_task"
    MYTHIC_CREDENTIAL_STORE = "mythic_credential_store"
    BLOODHOUND_DERIVED_FACT = "bloodhound_derived_fact"
    NATIVE_DELIVERABLE = "native_deliverable"
    EVALUATOR_OBSERVER = "evaluator_observer"
    CANONICAL_ROW = "canonical_row"


class FactKind(str, Enum):
    TRAJECTORY_BOUNDARY = "trajectory_boundary"
    CONTEXT_CREATION_ATTEMPT = "context_creation_attempt"
    CONTEXT_CREATED = "context_created"
    CREDENTIAL_IMPORTED = "credential_imported"
    CONTEXT_USED = "context_used"
    CONTEXT_RETIRED = "context_retired"
    CONTEXT_ABSENT = "context_absent"
    CONTEXT_ISOLATED = "context_isolated"
    ACTION_ISSUED = "action_issued"
    TARGET_IDENTITY = "target_identity"
    OBJECT_BEFORE = "object_before"
    OBJECT_AFTER = "object_after"
    EMITTED_MARKER = "emitted_marker"
    MARKER_OVERRIDE = "marker_override"
    MARKER_DEFAULT = "marker_default"
    MARKER_NEUTRAL = "marker_neutral"
    VERSION_IDENTITY = "version_identity"
    DELIVERED_CONTENT = "delivered_content"
    DELIVERY_BOUNDARY = "delivery_boundary"
    EVALUATION_METADATA_PRESENT = "evaluation_metadata_present"
    EVALUATION_METADATA_TAXONOMY = "evaluation_metadata_taxonomy"
    TERMINAL_RESULT = "terminal_result"
    INTERVENING_STATE = "intervening_state"
    RETRY_JUSTIFIED = "retry_justified"
    SEMANTIC_CLAIM = "semantic_claim"
    NATIVE_PROOF_POINTER = "native_proof_pointer"


_SEQUENCED_FACTS = {
    FactKind.CONTEXT_CREATION_ATTEMPT,
    FactKind.CONTEXT_CREATED,
    FactKind.CREDENTIAL_IMPORTED,
    FactKind.CONTEXT_USED,
    FactKind.CONTEXT_RETIRED,
    FactKind.ACTION_ISSUED,
    FactKind.OBJECT_BEFORE,
    FactKind.OBJECT_AFTER,
    FactKind.TERMINAL_RESULT,
    FactKind.INTERVENING_STATE,
    FactKind.RETRY_JUSTIFIED,
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def opaque_handle(namespace: str, value: Any) -> str:
    """Return a stable within-row join handle without retaining the raw value."""

    if not isinstance(namespace, str) or not namespace.strip():
        raise EvidenceValidationError("handle namespace must be a non-empty string")
    digest = hashlib.sha256(
        _canonical_json({"namespace": namespace.strip(), "value": value}).encode("utf-8")
    ).hexdigest()
    return f"h:{digest}"


def transaction_handle(namespace: str, value: Any) -> str:
    if not isinstance(namespace, str) or not namespace.strip():
        raise EvidenceValidationError("transaction namespace must be a non-empty string")
    digest = hashlib.sha256(
        _canonical_json({"namespace": namespace.strip(), "value": value}).encode("utf-8")
    ).hexdigest()
    return f"t:{digest}"


def _normalized_hash(value: str) -> str:
    text = value.strip().casefold()
    if not _HASH_RE.fullmatch(text):
        raise EvidenceValidationError("source_sha256 must be a SHA-256 digest")
    return text.removeprefix("sha256:")


def _resolve_json_pointer(document: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise EvidenceValidationError("field_pointer must be a non-root JSON pointer")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as exc:
                raise EvidenceValidationError(f"unresolved field pointer: {pointer}") from exc
        elif isinstance(current, Mapping) and token in current:
            current = current[token]
        else:
            raise EvidenceValidationError(f"unresolved field pointer: {pointer}")
    return current


@dataclass(frozen=True)
class EvidenceLocator:
    source_ref: str
    field_pointer: str
    source_version: str
    source_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_ref, str) or not self.source_ref.strip():
            raise EvidenceValidationError("locator source_ref must be non-empty")
        if not isinstance(self.source_version, str) or not self.source_version.strip():
            raise EvidenceValidationError("locator source_version must be non-empty")
        if not isinstance(self.field_pointer, str) or not self.field_pointer.startswith("/"):
            raise EvidenceValidationError("locator field_pointer must be a non-root JSON pointer")
        object.__setattr__(self, "source_sha256", _normalized_hash(self.source_sha256))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvidenceLocator":
        allowed = {"source_ref", "field_pointer", "source_version", "source_sha256"}
        if set(value) != allowed:
            raise EvidenceValidationError("locator fields must match the positive allowlist exactly")
        return cls(**{key: value[key] for key in allowed})

    def to_dict(self) -> dict[str, str]:
        return {
            "source_ref": self.source_ref,
            "field_pointer": self.field_pointer,
            "source_version": self.source_version,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True)
class SourceBinding:
    source_ref: str
    source_version: str
    source_sha256: str
    document: Any

    def __post_init__(self) -> None:
        if not isinstance(self.source_ref, str) or not self.source_ref.strip():
            raise EvidenceValidationError("binding source_ref must be non-empty")
        if not isinstance(self.source_version, str) or not self.source_version.strip():
            raise EvidenceValidationError("binding source_version must be non-empty")
        object.__setattr__(self, "source_sha256", _normalized_hash(self.source_sha256))

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        source_ref: str,
        source_version: str,
        expected_sha256: str,
    ) -> "SourceBinding":
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != _normalized_hash(expected_sha256):
            raise EvidenceValidationError(f"source bytes changed: {source_ref}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceValidationError(f"source is not readable JSON: {source_ref}") from exc
        return cls(source_ref, source_version, observed, document)


@dataclass(frozen=True)
class EvidenceAtom:
    fact_kind: FactKind
    truth: Truth
    origin: EvidenceOrigin
    locator: EvidenceLocator
    subject_handle: str
    object_handle: str | None = None
    semantic_transaction_id: str | None = None
    order_index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fact_kind, FactKind):
            raise EvidenceValidationError("fact_kind must be a closed FactKind enum")
        if not isinstance(self.truth, Truth):
            raise EvidenceValidationError("truth must be true, false, or unknown")
        if not isinstance(self.origin, EvidenceOrigin):
            raise EvidenceValidationError("origin must be a closed EvidenceOrigin enum")
        if not isinstance(self.locator, EvidenceLocator):
            raise EvidenceValidationError("locator must be an EvidenceLocator")
        if not isinstance(self.subject_handle, str) or not _HANDLE_RE.fullmatch(self.subject_handle):
            raise EvidenceValidationError("subject_handle must be opaque")
        if self.object_handle is not None and (
            not isinstance(self.object_handle, str) or not _HANDLE_RE.fullmatch(self.object_handle)
        ):
            raise EvidenceValidationError("object_handle must be opaque")
        if self.semantic_transaction_id is not None and (
            not isinstance(self.semantic_transaction_id, str)
            or not _TRANSACTION_RE.fullmatch(self.semantic_transaction_id)
        ):
            raise EvidenceValidationError("semantic_transaction_id must be opaque")
        if self.order_index is not None and (
            isinstance(self.order_index, bool)
            or not isinstance(self.order_index, int)
            or self.order_index < 0
        ):
            raise EvidenceValidationError("order_index must be a non-negative integer")
        if self.fact_kind in _SEQUENCED_FACTS and (
            self.semantic_transaction_id is None or self.order_index is None
        ):
            raise EvidenceValidationError("sequenced facts require transaction and order identity")
        if (self.semantic_transaction_id is None) != (self.order_index is None):
            raise EvidenceValidationError("transaction and order identity must be supplied together")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvidenceAtom":
        allowed = {
            "fact_kind",
            "truth",
            "origin",
            "locator",
            "subject_handle",
            "object_handle",
            "semantic_transaction_id",
            "order_index",
        }
        if not set(value) <= allowed:
            raise EvidenceValidationError("atom contains a field outside the positive allowlist")
        required = {"fact_kind", "truth", "origin", "locator", "subject_handle"}
        if not required <= set(value):
            raise EvidenceValidationError("atom is missing a required field")
        return cls(
            fact_kind=FactKind(value["fact_kind"]),
            truth=Truth(value["truth"]),
            origin=EvidenceOrigin(value["origin"]),
            locator=EvidenceLocator.from_mapping(value["locator"]),
            subject_handle=value["subject_handle"],
            object_handle=value.get("object_handle"),
            semantic_transaction_id=value.get("semantic_transaction_id"),
            order_index=value.get("order_index"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "fact_kind": self.fact_kind.value,
            "truth": self.truth.value,
            "origin": self.origin.value,
            "locator": self.locator.to_dict(),
            "subject_handle": self.subject_handle,
        }
        if self.object_handle is not None:
            result["object_handle"] = self.object_handle
        if self.semantic_transaction_id is not None:
            result["semantic_transaction_id"] = self.semantic_transaction_id
            result["order_index"] = self.order_index
        return result


def resolve_locator(locator: EvidenceLocator, bindings: Mapping[str, SourceBinding]) -> Any:
    binding = bindings.get(locator.source_ref)
    if not isinstance(binding, SourceBinding):
        raise EvidenceValidationError(f"unbound locator source: {locator.source_ref}")
    if binding.source_version != locator.source_version:
        raise EvidenceValidationError(f"source version mismatch: {locator.source_ref}")
    if binding.source_sha256 != locator.source_sha256:
        raise EvidenceValidationError(f"source hash mismatch: {locator.source_ref}")
    return _resolve_json_pointer(binding.document, locator.field_pointer)


def _atom_sort_key(atom: EvidenceAtom) -> tuple[Any, ...]:
    return (
        atom.semantic_transaction_id or "",
        -1 if atom.order_index is None else atom.order_index,
        atom.fact_kind.value,
        atom.subject_handle,
        atom.object_handle or "",
        atom.locator.source_ref,
        atom.locator.field_pointer,
    )


def build_evidence_projection(
    atoms: Iterable[EvidenceAtom],
    bindings: Mapping[str, SourceBinding],
) -> dict[str, Any]:
    typed = list(atoms)
    if not typed:
        raise EvidenceValidationError("evidence projection requires at least one atom")
    if not all(isinstance(item, EvidenceAtom) for item in typed):
        raise EvidenceValidationError("projection accepts typed EvidenceAtom values only")
    for item in typed:
        resolve_locator(item.locator, bindings)
    serialized = [item.to_dict() for item in sorted(typed, key=_atom_sort_key)]
    canonical_atoms = [_canonical_json(item) for item in serialized]
    if len(canonical_atoms) != len(set(canonical_atoms)):
        raise EvidenceValidationError("duplicate evidence atom")
    payload = {
        "schema": EVIDENCE_PROJECTION_SCHEMA,
        "atom_count": len(serialized),
        "atoms": serialized,
    }
    return {**payload, "projection_sha256": canonical_sha256(payload)}


def validate_evidence_projection(value: Mapping[str, Any]) -> list[EvidenceAtom]:
    if value.get("schema") != EVIDENCE_PROJECTION_SCHEMA:
        raise EvidenceValidationError("unexpected evidence projection schema")
    raw_atoms = value.get("atoms")
    if not isinstance(raw_atoms, list) or not raw_atoms:
        raise EvidenceValidationError("projection atoms must be a non-empty list")
    atoms = [EvidenceAtom.from_mapping(item) for item in raw_atoms if isinstance(item, Mapping)]
    if len(atoms) != len(raw_atoms) or value.get("atom_count") != len(atoms):
        raise EvidenceValidationError("projection atom count is invalid")
    payload = {"schema": EVIDENCE_PROJECTION_SCHEMA, "atom_count": len(raw_atoms), "atoms": raw_atoms}
    if value.get("projection_sha256") != canonical_sha256(payload):
        raise EvidenceValidationError("evidence projection hash mismatch")
    if [item.to_dict() for item in sorted(atoms, key=_atom_sort_key)] != raw_atoms:
        raise EvidenceValidationError("evidence projection is not canonically ordered")
    return atoms


_LIFECYCLE_KIND = {
    "attempted": FactKind.CONTEXT_CREATION_ATTEMPT,
    "created": FactKind.CONTEXT_CREATED,
    "imported": FactKind.CREDENTIAL_IMPORTED,
    "used": FactKind.CONTEXT_USED,
    "retired": FactKind.CONTEXT_RETIRED,
    "unresolved": FactKind.CONTEXT_CREATED,
}


def truth_from_value(value: Any) -> Truth:
    if value is True:
        return Truth.TRUE
    if value is False:
        return Truth.FALSE
    return Truth.UNKNOWN


def adapt_lifecycle_record(
    record: Mapping[str, Any],
    source_specs: Mapping[str, Mapping[str, str]],
    repo_root: Path,
) -> tuple[list[EvidenceAtom], dict[str, SourceBinding]]:
    """Adapt a generic retained lifecycle record without branching on its identity."""

    bindings: dict[str, SourceBinding] = {}
    for alias, spec in source_specs.items():
        path = (repo_root / spec["path"]).resolve()
        root = repo_root.resolve()
        if path != root and root not in path.parents:
            raise EvidenceValidationError(f"source escapes repository root: {alias}")
        bindings[alias] = SourceBinding.from_file(
            path,
            source_ref=alias,
            source_version=spec.get("version", "retained-json-v1"),
            expected_sha256=spec["sha256"],
        )

    def message_pointer(document: Any, message_id: Any) -> str:
        if not isinstance(document, Mapping) or not isinstance(document.get("messages"), list):
            raise EvidenceValidationError("message locator source lacks a messages list")
        for index, message in enumerate(document["messages"]):
            if isinstance(message, Mapping) and message.get("id") == message_id:
                return f"/messages/{index}"
        raise EvidenceValidationError(f"unresolved message locator: {message_id}")

    def locator(raw: Mapping[str, Any]) -> EvidenceLocator:
        alias = raw.get("source")
        binding = bindings.get(alias)
        if binding is None:
            raise EvidenceValidationError(f"unknown lifecycle source alias: {alias}")
        pointer = raw.get("pointer")
        if pointer is None and "message_id" in raw:
            pointer = message_pointer(binding.document, raw["message_id"])
        return EvidenceLocator(alias, pointer, binding.source_version, binding.source_sha256)

    record_key = record.get("record_key")
    if not isinstance(record_key, str) or not record_key:
        raise EvidenceValidationError("lifecycle record lacks a stable key")
    atoms: list[EvidenceAtom] = []
    for index, event in enumerate(record.get("events") or []):
        event_type = event.get("event_type")
        if event_type not in _LIFECYCLE_KIND:
            raise EvidenceValidationError(f"unknown lifecycle event type: {event_type}")
        context = event.get("context_ref")
        if not isinstance(context, str) or not context:
            raise EvidenceValidationError("lifecycle event lacks a context reference")
        atoms.append(
            EvidenceAtom(
                fact_kind=_LIFECYCLE_KIND[event_type],
                truth=truth_from_value(event.get("success")),
                origin=EvidenceOrigin.EVALUATOR_OBSERVER,
                locator=locator(event["locator"]),
                subject_handle=opaque_handle("credential-context", context),
                semantic_transaction_id=transaction_handle("credential-context", [record_key, context]),
                order_index=index,
            )
        )
    if record.get("complete_trajectory") is True:
        atoms.append(
            EvidenceAtom(
                fact_kind=FactKind.TRAJECTORY_BOUNDARY,
                truth=Truth.TRUE,
                origin=EvidenceOrigin.EVALUATOR_OBSERVER,
                locator=locator(record["termination_locator"]),
                subject_handle=opaque_handle("trajectory", record_key),
            )
        )
    absence = record.get("absence_locator")
    if isinstance(absence, Mapping):
        atoms.append(
            EvidenceAtom(
                fact_kind=FactKind.CONTEXT_ABSENT,
                truth=Truth.TRUE,
                origin=EvidenceOrigin.EVALUATOR_OBSERVER,
                locator=locator(absence),
                subject_handle=opaque_handle("trajectory", record_key),
            )
        )
    return atoms, bindings
