"""Source-bound admission seam for prospective composite-gauge rows.

Legacy episode artifacts are never upgraded from silence.  A row is emitted only
when a separate evaluator supplement binds every required value to exact JSON
source bytes and the existing canonical and Gate 1T authorities replay it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

try:
    from . import composite_gauge, fitness
    from . import tradecraft_evidence as evidence
except ImportError:  # script / sys.path import
    import composite_gauge  # type: ignore
    import fitness  # type: ignore
    import tradecraft_evidence as evidence  # type: ignore


BUNDLE_SCHEMA = "composite-source-admission-bundle-v1"
SUPPLEMENT_SCHEMA = "composite-source-evidence-v1"
RESULT_SCHEMA = "composite-source-admission-result-v1"
MECHANISM_ID = "composite-source-admission-v1"
EPISODE_SCHEMA = "gate1p-stagea-episode-artifact-v17"

_BUNDLE_FIELDS = frozenset({"schema", "episode", "evidence_supplement", "source_bindings", "identity"})
_SUPPLEMENT_FIELDS = frozenset({"schema", "episode_source_ref", "claims", "typed_atoms"})
_BINDING_FIELDS = frozenset({"path", "source_version", "source_sha256"})
_CLAIM_FIELDS = frozenset({"value", "locator"})
_ROW_ID_FIELDS = frozenset({"row_id", "policy_arm", "surface_id", "family_id"})
_RISK_FIELDS = frozenset(fitness._REQUIRED_CANONICAL_RISK_FIELDS)
_ROW_CONTROL_FIELDS = frozenset({
    "derived_outcome", "proof_envelopes", "semantic_transaction_count",
    "model_owned_branch_observed",
})
_IDENTITY_FIELDS = frozenset(composite_gauge._IDENTITY_FIELDS)
_REQUIRED_CLAIMS = frozenset(
    {f"row.{name}" for name in _ROW_ID_FIELDS | _RISK_FIELDS | _ROW_CONTROL_FIELDS}
    | {f"identity.{name}" for name in _IDENTITY_FIELDS}
)
_DERIVED_FIELDS = frozenset({
    "outcome_source", "objective_proven", "clean_stop", "proof_provenance_complete",
    "artifact_integrity_passed", "policy_identity_passed", "backend_provenance_passed",
})
_PROOF_FIELDS = frozenset({
    "schema", "scope", "persistence_state", "origin", "engagement_id", "callback_id",
    "transaction_id", "task_id", "terminal_status", "verifier_id",
    "verifier_input_sha256", "verifier_result_sha256",
})
_AUTHORIZATION = {
    "promotion_authorized": False,
    "runtime_application_authorized": False,
    "release_authorized": False,
    "publication_authorized": False,
    "external_claim_authorized": False,
    "phase2_authorized": False,
}
_EPISODE_IDENTITY_PATHS = {
    "protocol_id": ("protocol", "protocol_id"),
    "pair_id": ("protocol", "pair_id"),
    "logical_seed": ("protocol", "pair_invariants", "logical_seed"),
    "policy_arm": ("arm",),
    "model_provider": ("protocol", "model_provider"),
    "model_id": ("protocol", "model_id"),
    "generation_config": ("protocol", "pair_invariants", "generation_configuration"),
    "wrapper_id": ("protocol", "wrapper"),
    "authority_mode": ("protocol", "pair_invariants", "authority_mode"),
    "source_version": ("protocol", "source_version"),
}


class CompositeSourceAdmissionError(ValueError):
    """Raised internally when a source bundle cannot be admitted."""


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", str(value).strip().casefold()).strip("_") or "invalid"


def _result(status: str, reasons: list[str], *, record=None, row=None) -> dict[str, Any]:
    payload = {
        "schema": RESULT_SCHEMA,
        "mechanism_id": MECHANISM_ID,
        "status": status,
        "reason_codes": list(dict.fromkeys(reasons)),
        "composite_record": record,
        "composite_row": row,
        "authorization": dict(_AUTHORIZATION),
    }
    return {**payload, "result_hash": evidence.canonical_sha256(payload)}


def _not_reusable(*reasons: str) -> dict[str, Any]:
    return _result("not_reusable", list(reasons))


def _exact_equal(left: Any, right: Any) -> bool:
    try:
        return evidence.canonical_sha256(left) == evidence.canonical_sha256(right)
    except (TypeError, ValueError):
        return False


def _nested(document: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = document
    for name in path:
        if not isinstance(value, Mapping) or name not in value:
            raise CompositeSourceAdmissionError("episode_identity_field_missing:" + path[-1])
        value = value[name]
    return value


def _load_bindings(specs: Any, repo_root: Path) -> dict[str, evidence.SourceBinding]:
    if not isinstance(specs, Mapping) or not specs:
        raise CompositeSourceAdmissionError("missing_source_bindings")
    root = repo_root.resolve()
    bindings = {}
    for source_ref, spec in specs.items():
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise CompositeSourceAdmissionError("invalid_source_ref")
        if not isinstance(spec, Mapping) or set(spec) != _BINDING_FIELDS:
            raise CompositeSourceAdmissionError(f"source_binding_fields_mismatch:{_slug(source_ref)}")
        raw_path = spec["path"]
        if not isinstance(raw_path, str) or not raw_path.strip() or Path(raw_path).is_absolute():
            raise CompositeSourceAdmissionError(f"source_path_invalid:{_slug(source_ref)}")
        path = (root / raw_path).resolve()
        if path != root and root not in path.parents:
            raise CompositeSourceAdmissionError(f"source_escapes_root:{_slug(source_ref)}")
        binding = evidence.SourceBinding.from_file(
            path,
            source_ref=source_ref,
            source_version=spec["source_version"],
            expected_sha256=spec["source_sha256"],
        )
        if isinstance(binding.document, Mapping) and "source_version" in binding.document:
            if binding.document["source_version"] != binding.source_version:
                raise CompositeSourceAdmissionError(f"source_document_version_mismatch:{_slug(source_ref)}")
        bindings[source_ref] = binding
    return bindings


def _resolve_claim(raw: Any, bindings: Mapping[str, evidence.SourceBinding], name: str) -> Any:
    if not isinstance(raw, Mapping) or set(raw) != _CLAIM_FIELDS:
        raise CompositeSourceAdmissionError(f"claim_fields_mismatch:{_slug(name)}")
    locator = evidence.EvidenceLocator.from_mapping(raw["locator"])
    resolved = evidence.resolve_locator(locator, bindings)
    if not _exact_equal(resolved, raw["value"]):
        raise CompositeSourceAdmissionError(f"claim_value_mismatch:{_slug(name)}")
    return raw["value"]


def _atom_source_value(atom: evidence.EvidenceAtom) -> dict[str, Any]:
    value = atom.to_dict()
    value.pop("locator")
    return value


def _build_projection(raw_atoms: Any, bindings: Mapping[str, evidence.SourceBinding]) -> dict[str, Any]:
    if not isinstance(raw_atoms, list) or not raw_atoms:
        raise CompositeSourceAdmissionError("missing_typed_opsec_evidence")
    atoms = [evidence.EvidenceAtom.from_mapping(item) for item in raw_atoms if isinstance(item, Mapping)]
    if len(atoms) != len(raw_atoms):
        raise CompositeSourceAdmissionError("typed_atom_not_mapping")
    for atom in atoms:
        resolved = evidence.resolve_locator(atom.locator, bindings)
        if not _exact_equal(resolved, _atom_source_value(atom)):
            raise CompositeSourceAdmissionError("typed_atom_source_mismatch")
    return evidence.build_evidence_projection(atoms, bindings)


def _structural_reasons(bundle: Mapping[str, Any]) -> list[str]:
    reasons = []
    identity = bundle.get("identity")
    missing_identity = sorted(_IDENTITY_FIELDS - set(identity if isinstance(identity, Mapping) else ()))
    if missing_identity:
        reasons.append("missing_identity_evidence:" + ",".join(missing_identity))
    supplement = bundle.get("evidence_supplement")
    claims = supplement.get("claims") if isinstance(supplement, Mapping) else None
    claim_names = set(claims) if isinstance(claims, Mapping) else set()
    missing_risk = sorted(f"row.{name}" for name in _RISK_FIELDS if f"row.{name}" not in claim_names)
    if missing_risk:
        reasons.append("missing_risk_evidence:" + ",".join(missing_risk))
    missing_control = sorted(f"row.{name}" for name in _ROW_CONTROL_FIELDS | _ROW_ID_FIELDS if f"row.{name}" not in claim_names)
    if missing_control:
        reasons.append("missing_canonical_evidence:" + ",".join(missing_control))
    missing_identity_claims = sorted(f"identity.{name}" for name in _IDENTITY_FIELDS if f"identity.{name}" not in claim_names)
    if missing_identity_claims:
        reasons.append("missing_identity_claims:" + ",".join(missing_identity_claims))
    if not isinstance(supplement, Mapping) or not supplement.get("typed_atoms"):
        reasons.append("missing_typed_opsec_evidence")
    return reasons


def admit_composite_source(bundle: Mapping[str, Any], *, repo_root: Path) -> dict[str, Any]:
    """Return an admitted composite record or a deterministic ``not_reusable`` disposition."""

    if not isinstance(bundle, Mapping):
        return _not_reusable("bundle_not_mapping")
    if set(bundle) != _BUNDLE_FIELDS:
        missing = ",".join(sorted(_BUNDLE_FIELDS - set(bundle))) or "none"
        extra = ",".join(sorted(set(bundle) - _BUNDLE_FIELDS)) or "none"
        return _not_reusable(f"bundle_fields_mismatch:missing={missing}:extra={extra}")
    if bundle.get("schema") != BUNDLE_SCHEMA:
        return _not_reusable("unexpected_bundle_schema")
    episode = bundle.get("episode")
    if not isinstance(episode, Mapping) or episode.get("schema") != EPISODE_SCHEMA:
        return _not_reusable("unexpected_episode_schema")

    reasons = _structural_reasons(bundle)
    supplement = bundle.get("evidence_supplement")
    if not isinstance(supplement, Mapping) or set(supplement) != _SUPPLEMENT_FIELDS:
        reasons.append("supplement_fields_mismatch")
    elif supplement.get("schema") != SUPPLEMENT_SCHEMA:
        reasons.append("unexpected_supplement_schema")
    claims = supplement.get("claims") if isinstance(supplement, Mapping) else None
    if isinstance(claims, Mapping):
        extra_claims = sorted(set(claims) - _REQUIRED_CLAIMS)
        if extra_claims:
            reasons.append("unsupported_claims:" + ",".join(extra_claims))
    if reasons:
        return _result("not_reusable", reasons)

    try:
        bindings = _load_bindings(bundle["source_bindings"], repo_root)
        episode_ref = supplement["episode_source_ref"]
        episode_binding = bindings.get(episode_ref)
        if not isinstance(episode_binding, evidence.SourceBinding) or not _exact_equal(episode_binding.document, episode):
            raise CompositeSourceAdmissionError("episode_source_mismatch")
        episode_version = _nested(episode, ("protocol", "source_version"))
        if episode_binding.source_version != episode_version:
            raise CompositeSourceAdmissionError("episode_source_version_mismatch")

        values = {name: _resolve_claim(claims[name], bindings, name) for name in sorted(_REQUIRED_CLAIMS)}
        identity = dict(bundle["identity"])
        if set(identity) != _IDENTITY_FIELDS:
            raise CompositeSourceAdmissionError("identity_fields_mismatch")
        for name in _IDENTITY_FIELDS:
            if not _exact_equal(identity[name], values[f"identity.{name}"]):
                raise CompositeSourceAdmissionError(f"identity_claim_mismatch:{name}")
        for name, path in _EPISODE_IDENTITY_PATHS.items():
            if not _exact_equal(identity[name], _nested(episode, path)):
                raise CompositeSourceAdmissionError(f"episode_identity_mismatch:{name}")

        derived = values["row.derived_outcome"]
        proofs = values["row.proof_envelopes"]
        if not isinstance(derived, Mapping) or set(derived) != _DERIVED_FIELDS:
            raise CompositeSourceAdmissionError("derived_outcome_fields_mismatch")
        if not isinstance(proofs, list) or any(not isinstance(item, Mapping) or set(item) != _PROOF_FIELDS for item in proofs):
            raise CompositeSourceAdmissionError("proof_envelope_fields_mismatch")
        row_trace = {name: values[f"row.{name}"] for name in _RISK_FIELDS}
        canonical = fitness.canonical_row_verdict(
            row_trace,
            row_id=values["row.row_id"],
            policy_arm=values["row.policy_arm"],
            surface_id=values["row.surface_id"],
            family_id=values["row.family_id"],
            derived_outcome=derived,
            proof_envelopes=proofs,
            semantic_transaction_count=values["row.semantic_transaction_count"],
            model_owned_branch_observed=values["row.model_owned_branch_observed"],
        )
        if canonical.get("row_status") != "scored" or not fitness.canonical_row_verdict_is_replay_valid(canonical):
            raise CompositeSourceAdmissionError("canonical_row_not_scored_and_replay_valid")
        projection = _build_projection(supplement["typed_atoms"], bindings)
        record = {"canonical_row": canonical, "evidence_projection": projection, "identity": identity}
        row = composite_gauge.build_composite_row(canonical, projection, identity=identity)
        return _result("admitted", [], record=record, row=row)
    except (CompositeSourceAdmissionError, evidence.EvidenceValidationError, composite_gauge.CompositeGaugeError) as exc:
        return _not_reusable(_slug(exc))
    except (KeyError, TypeError, ValueError):
        return _not_reusable("invalid_source_bundle")
