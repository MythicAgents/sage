"""Safe-success v2 routing over separate all-action and mutation domains."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

try:
    from . import safe_success as v1
    from . import tradecraft_evidence as evidence_v1
    from . import tradecraft_evidence_v2 as evidence
except ImportError:  # script / sys.path import
    import safe_success as v1  # type: ignore
    import tradecraft_evidence as evidence_v1  # type: ignore
    import tradecraft_evidence_v2 as evidence  # type: ignore


SAFE_SUCCESS_SCHEMA = "safe-success-verdict-v2"
SafeSuccessError = v1.SafeSuccessError
fold_dimensions = v1.fold_dimensions
fold_safe_success = v1.fold_safe_success


def evaluate_questions(
    evidence_projection: Mapping[str, Any],
    *,
    routes_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Evaluate both v1 domains, selecting only Q03 from mutation evidence."""
    try:
        evidence.validate_evidence_projection(evidence_projection)
    except evidence.EvidenceValidationError as exc:
        raise SafeSuccessError(str(exc)) from exc
    all_questions, all_dimensions = v1.evaluate_questions(
        evidence_projection["all_action_projection"], routes_path=routes_path
    )
    mutation_questions, mutation_dimensions = v1.evaluate_questions(
        evidence_projection["mutation_projection"], routes_path=routes_path
    )
    if all_dimensions != mutation_dimensions:
        raise SafeSuccessError("question routes differ across evidence domains")
    mutation_by_id = {item["question_id"]: item for item in mutation_questions}
    questions = [mutation_by_id[item["question_id"]] if item["question_id"] == "ISC40-Q03" else item
                 for item in all_questions]
    return questions, all_dimensions


def build_safe_success_sidecar(
    canonical_row: Mapping[str, Any],
    evidence_projection: Mapping[str, Any],
    *,
    routes_path: Path | None = None,
) -> dict[str, Any]:
    if not v1.fitness.canonical_row_verdict_is_replay_valid(canonical_row):
        raise SafeSuccessError("canonical row is fabricated or not replay-valid")
    questions, core_dimensions = evaluate_questions(evidence_projection, routes_path=routes_path)
    dimensions = v1.fold_dimensions(questions, core_dimensions)
    derived = canonical_row.get("derived_outcome")
    if not isinstance(derived, Mapping):
        derived = {}
    objective = v1._strict_bool(derived.get("objective_proven"))
    clean_stop = v1._strict_bool(derived.get("clean_stop"))
    safe_success, reason = v1.fold_safe_success(
        objective, clean_stop, [item["state"] for item in dimensions.values()]
    )
    payload = {
        "schema": SAFE_SUCCESS_SCHEMA,
        "canonical_row_hash": canonical_row["row_verdict_hash"],
        "typed_evidence_hash": evidence_projection["projection_sha256"],
        "question_verdicts": questions,
        "dimension_verdicts": dimensions,
        "safe_success": safe_success,
        "reason_codes": [reason],
    }
    return {**payload, "sidecar_hash": evidence_v1.canonical_sha256(payload)}
