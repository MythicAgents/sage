"""Versioned all-action/mutation evidence projection for development evaluation."""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

try:
    from . import tradecraft_evidence as v1
except ImportError:  # script / sys.path import
    import tradecraft_evidence as v1  # type: ignore


EVIDENCE_PROJECTION_SCHEMA = "sage.tradecraft-evidence-projection.v2"
EvidenceAtom = v1.EvidenceAtom
EvidenceLocator = v1.EvidenceLocator
EvidenceOrigin = v1.EvidenceOrigin
EvidenceValidationError = v1.EvidenceValidationError
FactKind = v1.FactKind
SourceBinding = v1.SourceBinding
Truth = v1.Truth


def _action_identity(atom: EvidenceAtom) -> tuple[str, str | None, str | None, int | None]:
    return (atom.subject_handle, atom.object_handle, atom.semantic_transaction_id, atom.order_index)


def _canonical_atoms(atoms: Iterable[EvidenceAtom]) -> list[str]:
    return sorted(json.dumps(atom.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                  for atom in atoms)


def _validate_domains(all_atoms: list[EvidenceAtom], mutation_atoms: list[EvidenceAtom]) -> None:
    all_common = [atom for atom in all_atoms if atom.fact_kind is not FactKind.ACTION_ISSUED]
    mutation_common = [atom for atom in mutation_atoms if atom.fact_kind is not FactKind.ACTION_ISSUED]
    if _canonical_atoms(all_common) != _canonical_atoms(mutation_common):
        raise EvidenceValidationError("common evidence differs across action domains")
    all_actions = [atom for atom in all_atoms if atom.fact_kind is FactKind.ACTION_ISSUED]
    mutation_actions = [atom for atom in mutation_atoms if atom.fact_kind is FactKind.ACTION_ISSUED]
    all_by_identity = {_action_identity(atom): atom for atom in all_actions}
    mutation_by_identity = {_action_identity(atom): atom for atom in mutation_actions}
    if len(all_by_identity) != len(all_actions) or len(mutation_by_identity) != len(mutation_actions):
        raise EvidenceValidationError("duplicate action classification identity")
    if set(all_by_identity) != set(mutation_by_identity):
        raise EvidenceValidationError("action domain inventory mismatch")
    for identity, action in all_by_identity.items():
        mutation = mutation_by_identity[identity]
        if action.locator.to_dict() == mutation.locator.to_dict():
            raise EvidenceValidationError("action and mutation locators must differ")
        if action.truth is Truth.FALSE and mutation.truth is not Truth.FALSE:
            raise EvidenceValidationError("unissued action cannot have a mutation")
        if action.truth is Truth.UNKNOWN and mutation.truth is not Truth.UNKNOWN:
            raise EvidenceValidationError("unknown action cannot become a known mutation")


def build_evidence_projection(
    atoms: Iterable[EvidenceAtom],
    mutation_atoms: Iterable[EvidenceAtom],
    bindings: Mapping[str, SourceBinding],
) -> dict[str, Any]:
    """Build separately source-bound v1 domains and a canonical v2 envelope.

    ``mutation_atoms`` is the complete mutation-domain iterable: identical common
    atoms plus each separately source-bound mutation ``ACTION_ISSUED`` counterpart.
    """
    all_action_projection = v1.build_evidence_projection(atoms, bindings)
    mutation_projection = v1.build_evidence_projection(mutation_atoms, bindings)
    all_typed = v1.validate_evidence_projection(all_action_projection)
    mutation_typed = v1.validate_evidence_projection(mutation_projection)
    _validate_domains(all_typed, mutation_typed)
    payload = {
        "schema": EVIDENCE_PROJECTION_SCHEMA,
        "all_action_projection": all_action_projection,
        "mutation_projection": mutation_projection,
    }
    return {**payload, "projection_sha256": v1.canonical_sha256(payload)}


def validate_evidence_projection(value: Mapping[str, Any]) -> tuple[list[EvidenceAtom], list[EvidenceAtom]]:
    """Return ``(all_action_atoms, mutation_atoms)`` after full cross-domain validation."""
    required = {"schema", "all_action_projection", "mutation_projection", "projection_sha256"}
    if not isinstance(value, Mapping) or set(value) != required \
            or value.get("schema") != EVIDENCE_PROJECTION_SCHEMA:
        raise EvidenceValidationError("unexpected evidence projection schema")
    all_projection, mutation_projection = value["all_action_projection"], value["mutation_projection"]
    if not isinstance(all_projection, Mapping) or not isinstance(mutation_projection, Mapping):
        raise EvidenceValidationError("evidence domains must be projections")
    payload = {"schema": EVIDENCE_PROJECTION_SCHEMA,
               "all_action_projection": all_projection, "mutation_projection": mutation_projection}
    if value.get("projection_sha256") != v1.canonical_sha256(payload):
        raise EvidenceValidationError("evidence projection hash mismatch")
    all_atoms = v1.validate_evidence_projection(all_projection)
    mutation_atoms = v1.validate_evidence_projection(mutation_projection)
    _validate_domains(all_atoms, mutation_atoms)
    return all_atoms, mutation_atoms
