"""Phase 13 canonical promotion replay and superseding status map.

This module is offline-only. It replays the retained Phase 6 and Phase 8 artifacts
through the row-first canonical authority in ``fitness.py`` and emits a content-
addressed status map. It never mutates historical rows, runs the lab, or infers a
promotion from legacy aggregate booleans.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

try:  # package import
    from . import fitness
    from . import phase8_goad_regression as phase8
    from .experiment_contracts import (
        NOT_ESTIMABLE,
        POLICY_APPLICATION_SCOPE_EXPLICIT_AUTHORIZED_HARNESS,
        POLICY_DEFAULT_RECOMMENDATION_INVALIDATED,
        POLICY_EVIDENCE_SCOPE_AUTHORIZED_LAB_HARNESS,
        SCOPE_GOVERNANCE_NOT_EVALUATED,
    )
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fitness  # type: ignore
    import phase8_goad_regression as phase8  # type: ignore
    from experiment_contracts import (  # type: ignore
        NOT_ESTIMABLE,
        POLICY_APPLICATION_SCOPE_EXPLICIT_AUTHORIZED_HARNESS,
        POLICY_DEFAULT_RECOMMENDATION_INVALIDATED,
        POLICY_EVIDENCE_SCOPE_AUTHORIZED_LAB_HARNESS,
        SCOPE_GOVERNANCE_NOT_EVALUATED,
    )


SCHEMA_VERSION = 1
KIND = "phase13_canonical_promotion_status_map"
PHASE6_SURFACE_ID = "phase6-laps-family-transfer"
PHASE6_FAMILY_ID = "laps-family-transfer-r5"
DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[2] / ".hillclimb" / "results"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PHASE6_POLICY_ROWS_PATH = DEFAULT_RESULTS_ROOT / "laps_family_transfer_policy_matrix_pinned_r5_20260715.jsonl"
DEFAULT_PHASE7_REPORT_PATH = DEFAULT_RESULTS_ROOT / "trust_context_corroboration_live_validation_v2_20260715.json"
DEFAULT_PHASE8_ROWS_PATH = DEFAULT_RESULTS_ROOT / "phase8_goad_regression_rows_v2_20260715.jsonl"
DEFAULT_PHASE8_LEGACY_REPORT_PATH = DEFAULT_RESULTS_ROOT / "phase8_goad_regression_validation_v2_20260716.json"
# The architecture-policy campaign was archived after it closed; both its Phase 12 input and this
# module's Phase 13 output moved under Plans/Archived/architecture-policy-eval/ while these constants
# kept pointing at the Plans/ root. That left the default replay path reading a file that no longer
# existed there, which is why the no-argument replay test had been failing.
DEFAULT_ARCHIVED_CAMPAIGN_DIR = DEFAULT_REPO_ROOT / "Plans" / "Archived" / "architecture-policy-eval"
DEFAULT_PHASE12_AUDIT_PATH = DEFAULT_ARCHIVED_CAMPAIGN_DIR / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE12_PROOF_BINDING_AUDIT_2026-07-16.json"
DEFAULT_OUTPUT_PATH = DEFAULT_ARCHIVED_CAMPAIGN_DIR / "SAGE_ARCHITECTURE_POLICY_EVAL_PHASE13_CANONICAL_PROMOTION_STATUS_2026-07-16.json"


class Phase13CanonicalPromotionError(ValueError):
    """Raised when the retained replay inputs are missing or malformed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _portable_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(DEFAULT_REPO_ROOT.resolve()))
    except ValueError:
        return path.name


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Phase13CanonicalPromotionError(f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Phase13CanonicalPromotionError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise Phase13CanonicalPromotionError(f"{label} must be a JSON object")
    return payload


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise Phase13CanonicalPromotionError(f"missing {label}: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Phase13CanonicalPromotionError(f"invalid JSONL row {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise Phase13CanonicalPromotionError(f"JSONL row {path}:{line_no} must be an object")
        rows.append(row)
    if not rows:
        raise Phase13CanonicalPromotionError(f"{label} has no rows: {path}")
    return rows


def _write_json(path: Path, payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    digest = _sha256_file(path)
    sidecar = path.with_suffix(".sha256")
    sidecar.write_text(f"{digest.removeprefix('sha256:')}  {path.name}\n", encoding="utf-8")
    return digest


def _model_owned_branch_observed(row: Mapping[str, Any]) -> bool:
    return any(
        str(item.get("decision_owner") or "").strip() == "model_branch"
        for item in list(row.get("decisions") or [])
        if isinstance(item, Mapping)
    )


def _phase6_row_verdicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        fitness.canonical_row_verdict(
            row,
            row_id=str(row.get("phase6_planned_row_id") or ""),
            policy_arm=str(row.get("policy_mode") or row.get("configured_policy_mode") or ""),
            surface_id=PHASE6_SURFACE_ID,
            family_id=PHASE6_FAMILY_ID,
            derived_outcome=(row.get("derived_outcome") if isinstance(row.get("derived_outcome"), dict) else None),
            proof_envelopes=(row.get("proof_envelopes") or ()),
            semantic_transaction_count=(
                row.get("semantic_transaction_count")
                if isinstance(row.get("semantic_transaction_count"), int)
                else None
            ),
            model_owned_branch_observed=_model_owned_branch_observed(row),
        )
        for row in rows
    ]


def _phase8_row_verdicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        fitness.canonical_row_verdict(
            row,
            row_id=str(row.get("phase8_planned_row_id") or ""),
            policy_arm=str(row.get("phase8_policy_arm") or ""),
            surface_id=phase8.PHASE8_SURFACE_ID,
            family_id=phase8.PHASE8_FAMILY_ID,
            derived_outcome=(row.get("derived_outcome") if isinstance(row.get("derived_outcome"), dict) else None),
            proof_envelopes=(row.get("proof_envelopes") or ()),
            semantic_transaction_count=(
                row.get("semantic_transaction_count")
                if isinstance(row.get("semantic_transaction_count"), int)
                else None
            ),
            model_owned_branch_observed=_model_owned_branch_observed(row),
        )
        for row in rows
    ]


def _legacy_claimed_hard_gates(legacy_phase8_report: Mapping[str, Any]) -> dict[str, Any]:
    verdict = dict(legacy_phase8_report.get("typed_verdict") or {})
    return {
        "artifact_integrity": verdict.get("artifact_integrity_passed"),
        "boundary": verdict.get("boundary_passed"),
        "policy_identity": verdict.get("policy_identity_passed"),
        "backend_provenance": verdict.get("backend_provenance_passed"),
    }


def _row_status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("row_status") or "missing")
        counts[status] = counts.get(status, 0) + 1
    return counts


def build_phase13_status_map(
    *,
    phase6_policy_rows_path: Path = DEFAULT_PHASE6_POLICY_ROWS_PATH,
    phase7_report_path: Path = DEFAULT_PHASE7_REPORT_PATH,
    phase8_rows_path: Path = DEFAULT_PHASE8_ROWS_PATH,
    phase8_legacy_report_path: Path = DEFAULT_PHASE8_LEGACY_REPORT_PATH,
    phase12_audit_path: Path = DEFAULT_PHASE12_AUDIT_PATH,
) -> dict[str, Any]:
    phase6_rows = _load_jsonl(phase6_policy_rows_path, label="Phase 6 pinned policy rows")
    phase7_report = _load_json(phase7_report_path, label="Phase 7 retained report")
    phase8_rows = _load_jsonl(phase8_rows_path, label="Phase 8 retained rows")
    legacy_phase8_report = _load_json(phase8_legacy_report_path, label="Phase 8 legacy validation report")
    phase12_audit = _load_json(phase12_audit_path, label="Phase 12 proof audit")
    phase6_verdicts = _phase6_row_verdicts(phase6_rows)
    phase8_verdicts = _phase8_row_verdicts(phase8_rows)
    phase6_hybrid_verdicts = [
        row for row in phase6_verdicts if row.get("policy_arm") == "hybrid"
    ]
    transfer_evidence = phase8._phase7_symbolic_surrogate_transfer_evidence(phase7_report)
    aggregate = fitness.canonical_aggregate_verdict(
        phase8_verdicts,
        evaluated_policy="hybrid",
        baseline_policy="symbolic",
        causal_row_verdicts=phase6_hybrid_verdicts,
        transfer_row_verdicts=(),
        transfer_evidence=transfer_evidence,
        claimed_hard_gates=_legacy_claimed_hard_gates(legacy_phase8_report),
        implementation_status="committed_source_default_hybrid_unchanged",
    )
    typed = dict(aggregate["typed_verdict"])
    metrics = dict(aggregate["metrics"])
    phase12_totals = dict(phase12_audit.get("totals") or {})
    checks = {
        "phase6_policy_rows_replayed": len(phase6_verdicts) == 12,
        "phase8_rows_replayed": len(phase8_verdicts) == 10,
        "retained_phase6_rows_remain_unscorable_without_retrofit": all(
            row.get("row_status") == "unscorable" for row in phase6_verdicts
        ),
        "retained_phase8_rows_remain_unscorable_without_retrofit": all(
            row.get("row_status") == "unscorable" for row in phase8_verdicts
        ),
        "phase12_audit_totals_reconcile": (
            phase12_totals.get("terminal_proof_count") == 151
            and (phase12_totals.get("status_counts") or {}).get("invalid") == 39
            and (phase12_totals.get("status_counts") or {}).get("unverifiable") == 112
        ),
        "row_aggregate_disagreement_fails_closed": aggregate["row_aggregate_disagreement"] is True,
        "phase7_symbolic_surrogate_does_not_satisfy_hybrid_transfer": typed.get("transfer_passed") is False,
        "missing_metrics_are_not_estimable": metrics == {
            "rank_correlation": NOT_ESTIMABLE,
            "achieved_power": NOT_ESTIMABLE,
            "mde": NOT_ESTIMABLE,
        },
        "former_phase8_recommendation_invalidated": (
            typed.get("product_policy_status") == POLICY_DEFAULT_RECOMMENDATION_INVALIDATED
            and aggregate["promotion_evidence_passed"] is False
        ),
        "typed_statuses_are_separate": bool(
            typed.get("descriptive_status")
            and typed.get("within_family_causal_status")
            and typed.get("transfer_status")
            and typed.get("research_claim_status")
            and typed.get("product_policy_status")
            and typed.get("approval_status")
            and typed.get("implementation_status")
        ),
        "scope_dispositions_are_separate_and_bounded": (
            typed.get("policy_evidence_scope") == POLICY_EVIDENCE_SCOPE_AUTHORIZED_LAB_HARNESS
            and typed.get("policy_application_scope") == POLICY_APPLICATION_SCOPE_EXPLICIT_AUTHORIZED_HARNESS
            and typed.get("scope_governance_status") == SCOPE_GOVERNANCE_NOT_EVALUATED
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "generated_at": _now(),
        "authority": {
            "version": fitness.CANONICAL_PROMOTION_AUTHORITY_VERSION,
            "row_function": "ai.hillclimb.fitness.canonical_row_verdict",
            "aggregate_function": "ai.hillclimb.fitness.canonical_aggregate_verdict",
            "dense_reward_function": "ai.hillclimb.fitness.dense_reward_from_trace",
            "typed_verdict_type": "ai.hillclimb.experiment_contracts.TypedVerdict",
            "legacy_aggregate_bools_are_not_authoritative": True,
        },
        "source_artifacts": {
            "phase6_policy_rows": {
                "path": _portable_path(phase6_policy_rows_path),
                "sha256": _sha256_file(phase6_policy_rows_path),
            },
            "phase7_report": {
                "path": _portable_path(phase7_report_path),
                "sha256": _sha256_file(phase7_report_path),
            },
            "phase8_rows": {
                "path": _portable_path(phase8_rows_path),
                "sha256": _sha256_file(phase8_rows_path),
            },
            "phase8_legacy_report": {
                "path": _portable_path(phase8_legacy_report_path),
                "sha256": _sha256_file(phase8_legacy_report_path),
            },
            "phase12_proof_audit": {
                "path": _portable_path(phase12_audit_path),
                "sha256": _sha256_file(phase12_audit_path),
            },
        },
        "phase12_audit_totals": phase12_totals,
        "phase6_replay": {
            "surface_id": PHASE6_SURFACE_ID,
            "family_id": PHASE6_FAMILY_ID,
            "row_count": len(phase6_verdicts),
            "status_counts": _row_status_counts(phase6_verdicts),
            "row_verdict_hashes": [row["row_verdict_hash"] for row in phase6_verdicts],
            "hybrid_causal_row_count": len(phase6_hybrid_verdicts),
        },
        "phase8_replay": {
            "surface_id": phase8.PHASE8_SURFACE_ID,
            "family_id": phase8.PHASE8_FAMILY_ID,
            "row_count": len(phase8_verdicts),
            "status_counts": _row_status_counts(phase8_verdicts),
            "row_verdict_hashes": [row["row_verdict_hash"] for row in phase8_verdicts],
            "legacy_claimed_hard_gates": _legacy_claimed_hard_gates(legacy_phase8_report),
            "canonical_aggregate": aggregate,
        },
        "superseding_status_map": {
            "former_phase8_recommendation": POLICY_DEFAULT_RECOMMENDATION_INVALIDATED,
            "descriptive_status": typed["descriptive_status"],
            "within_family_causal_status": typed["within_family_causal_status"],
            "transfer_status": typed["transfer_status"],
            "research_claim_status": typed["research_claim_status"],
            "product_policy_status": typed["product_policy_status"],
            "approval_status": typed["approval_status"],
            "implementation_status": typed["implementation_status"],
            "policy_evidence_scope": typed["policy_evidence_scope"],
            "policy_application_scope": typed["policy_application_scope"],
            "scope_governance_status": typed["scope_governance_status"],
            "reason_codes": typed["reason_codes"],
        },
        "validation": {
            "checks": checks,
            "passes_gate": all(checks.values()),
        },
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "phase13-canonical-promotion-replay",
        help="replay retained Phase 6/8 artifacts through the canonical promotion authority",
    )
    parser.add_argument("--phase6-policy-rows", default=str(DEFAULT_PHASE6_POLICY_ROWS_PATH))
    parser.add_argument("--phase7-report", default=str(DEFAULT_PHASE7_REPORT_PATH))
    parser.add_argument("--phase8-rows", default=str(DEFAULT_PHASE8_ROWS_PATH))
    parser.add_argument("--phase8-legacy-report", default=str(DEFAULT_PHASE8_LEGACY_REPORT_PATH))
    parser.add_argument("--phase12-audit", default=str(DEFAULT_PHASE12_AUDIT_PATH))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.set_defaults(func=_cmd_phase13_canonical_promotion_replay)


def _cmd_phase13_canonical_promotion_replay(args: Any) -> int:
    try:
        report = build_phase13_status_map(
            phase6_policy_rows_path=Path(args.phase6_policy_rows),
            phase7_report_path=Path(args.phase7_report),
            phase8_rows_path=Path(args.phase8_rows),
            phase8_legacy_report_path=Path(args.phase8_legacy_report),
            phase12_audit_path=Path(args.phase12_audit),
        )
    except Phase13CanonicalPromotionError as exc:
        print(f"phase13-canonical-promotion-replay: {exc}", file=sys.stderr)
        return 2
    output = Path(args.output) if args.output else None
    output_sha256 = _write_json(output, report) if output is not None else ""
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True))
    if output_sha256:
        print(f"\nOUTPUT: {output} ({output_sha256})", flush=True)
    print(
        f"\nVERDICT: {'PASS' if report['validation']['passes_gate'] else 'FAIL'}  "
        f"(product_policy_status={report['superseding_status_map']['product_policy_status']})",
        flush=True,
    )
    return 0 if report["validation"]["passes_gate"] else 1
