"""Validation for the Phase 6 forced-confirmation and live policy matrices.

This module is eval-only. It consumes the accepted four-cell mechanics canary,
the 12 preregistered forced confirmations, and the 12 Symbolic/Hybrid policy
rows. It reports only the claims Phase 6 actually authorizes: branch reality,
paired cost, Hybrid semantic selection, regret, and fixed-order shadow failure.
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
import sys
from typing import Any

try:  # package import
    from . import laps_family_transfer_canary as canary
    from . import laps_family_transfer_holdout as contract
    from . import policy_replay_calibration as calibration
    from ..langgraph import policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import laps_family_transfer_canary as canary  # type: ignore
    import laps_family_transfer_holdout as contract  # type: ignore
    import policy_replay_calibration as calibration  # type: ignore
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import policy  # type: ignore


DEFAULT_FORCED_RESULTS_PATH = calibration.DEFAULT_RESULTS_ROOT / f"laps_family_transfer_forced_confirmations_{contract.RESULT_ARTIFACT_SUFFIX}.jsonl"
DEFAULT_POLICY_RESULTS_PATH = calibration.DEFAULT_RESULTS_ROOT / f"laps_family_transfer_policy_matrix_{contract.RESULT_ARTIFACT_SUFFIX}.jsonl"
DEFAULT_CANARY_REPORT_PATH = calibration.DEFAULT_RESULTS_ROOT / f"laps_family_transfer_canary_validation_{contract.RESULT_ARTIFACT_SUFFIX}.json"
DEFAULT_PINNED_FORCED_PATH = calibration.DEFAULT_RESULTS_ROOT / f"laps_family_transfer_forced_confirmations_pinned_{contract.RESULT_ARTIFACT_SUFFIX}.jsonl"
DEFAULT_PINNED_POLICY_PATH = calibration.DEFAULT_RESULTS_ROOT / f"laps_family_transfer_policy_matrix_pinned_{contract.RESULT_ARTIFACT_SUFFIX}.jsonl"
EXPECTED_FORCED_REPEATS_PER_CELL = contract.LAPS_FAMILY_TRANSFER_HOLDOUT.budgets.forced_repeats_per_cell
EXPECTED_FORCED_ROWS = contract.LAPS_FAMILY_TRANSFER_HOLDOUT.budgets.forced_confirmation_runs
EXPECTED_POLICY_REPEATS_PER_OBJECTIVE = contract.LAPS_FAMILY_TRANSFER_HOLDOUT.budgets.live_symbolic_runs_per_objective
EXPECTED_POLICY_ROWS = (
    len(contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants)
    * (
        contract.LAPS_FAMILY_TRANSFER_HOLDOUT.budgets.live_symbolic_runs_per_objective
        + contract.LAPS_FAMILY_TRANSFER_HOLDOUT.budgets.live_hybrid_runs_per_objective
    )
)
EXPECTED_POLICY_MODES = ("symbolic", "hybrid")
EXPECTED_SELECTION_CONTRACTS = {
    "symbolic": policy.SELECTION_CONTRACT_SYMBOLIC,
    "hybrid": policy.SELECTION_CONTRACT_HYBRID,
}
MIN_POLICY_TRANSACTIONS_FROM_FRONTIER = float(len(contract.CHAIN))
MAX_POLICY_TRANSACTIONS_FROM_FRONTIER = float(
    len(contract.CHAIN) * len(contract.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths)
)


class LapsFamilyTransferMatrixError(ValueError):
    """Raised when Phase 6 matrix evidence is missing or malformed."""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LapsFamilyTransferMatrixError(f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LapsFamilyTransferMatrixError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LapsFamilyTransferMatrixError(f"{label} must be a JSON object")
    return payload


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise LapsFamilyTransferMatrixError(f"missing {label}: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LapsFamilyTransferMatrixError(f"invalid JSONL row {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise LapsFamilyTransferMatrixError(f"JSONL row {path}:{line_no} must be an object")
        rows.append(row)
    if not rows:
        raise LapsFamilyTransferMatrixError(f"{label} has no rows: {path}")
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def _canary_expectations(canary_report: dict[str, Any]) -> dict[str, Any]:
    surface = dict(canary_report.get("surface_expectations") or {})
    return {
        "surface_report_passes": surface.get("surface_report_passes") is True,
        "surface_authorizes_canaries": surface.get("surface_authorizes_canaries") is True,
        "canary_passes": canary_report.get("passes_gate") is True,
        "canary_authorizes_forced_matrix": (
            (canary_report.get("authorization") or {}).get("forced_confirmation_matrix_authorized") is True
        ),
        "sealed_manifest_hash": str(surface.get("sealed_manifest_hash") or ""),
        "topology_hash": str(surface.get("topology_hash") or ""),
        "expected_candidate_set_hash": str(surface.get("expected_candidate_set_hash") or ""),
        "expected_ordered_frontier_hash": str(surface.get("expected_ordered_frontier_hash") or ""),
        "expected_targets": list(surface.get("expected_targets") or []),
        "expected_candidate_count": int(surface.get("expected_candidate_count") or 0),
    }


def _forced_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("side") or "") == "harness"
        and str(row.get("scenario") or "") in canary._scenario_to_variant()  # type: ignore[attr-defined]
        and str(row.get("configured_policy_mode") or "").strip().casefold() == canary.EXPECTED_POLICY_MODE
    ]


def _policy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("side") or "") == "harness"
        and str(row.get("scenario") or "") in canary._scenario_to_variant()  # type: ignore[attr-defined]
        and str(row.get("configured_policy_mode") or "").strip().casefold() in EXPECTED_POLICY_MODES
        and not str(row.get("phase6_forced_path") or "")
    ]


def _policy_row_report(row: dict[str, Any], expectations: dict[str, Any]) -> dict[str, Any]:
    scenario = str(row.get("scenario") or "")
    variant = canary._scenario_to_variant().get(scenario)  # type: ignore[attr-defined]
    decisive_match = canary._decisive_decision(row, expectations["expected_candidate_set_hash"])  # type: ignore[attr-defined]
    decisive_index, decisive = decisive_match if decisive_match is not None else (None, {})
    packet = decisive.get("decision_packet")
    packet = dict(packet) if isinstance(packet, dict) else {}
    frontier = canary._packet_frontier(packet)  # type: ignore[attr-defined]
    frontier_targets = [
        contract.canonicalize_callback_text(item.get("target"))
        for item in frontier
    ]
    selected_path = canary._forced_path_for_target(decisive.get("selected_target"))  # type: ignore[attr-defined]
    selected_target_key = (
        (selected_path.first_host.casefold(), selected_path.first_domain.casefold())
        if selected_path is not None
        else ("", "")
    )
    correct_target_key = (
        (variant.target_host.casefold(), variant.target_domain.casefold())
        if variant is not None
        else ("", "")
    )
    correct_first = selected_path is not None and selected_target_key == correct_target_key
    transactions_from_frontier = canary._transactions_from_frontier(row, decisive_index)  # type: ignore[attr-defined]
    regret = (
        None
        if transactions_from_frontier is None
        else transactions_from_frontier - MIN_POLICY_TRANSACTIONS_FROM_FRONTIER
    )
    post_frontier_transactions = canary._post_frontier_transactions(row, decisive_index)  # type: ignore[attr-defined]
    policy_mode = str(row.get("configured_policy_mode") or "").strip().casefold()
    row_failures = calibration._row_invariant_failures(row)
    observed_cost_within_frozen_policy_budget = (
        isinstance(transactions_from_frontier, (int, float))
        and float(transactions_from_frontier).is_integer()
        and MIN_POLICY_TRANSACTIONS_FROM_FRONTIER
        <= float(transactions_from_frontier)
        <= MAX_POLICY_TRANSACTIONS_FROM_FRONTIER
    )
    checks = {
        "scenario_recognized": variant is not None,
        "row_invariants_pass": not row_failures,
        "request_completed": row.get("request_completed") is True,
        "phase6_manifest_hash_matches": str(row.get("phase6_manifest_hash") or "") == expectations["sealed_manifest_hash"],
        "phase6_topology_hash_matches": str(row.get("phase6_topology_hash") or "") == expectations["topology_hash"],
        "phase6_candidate_set_hash_matches": str(row.get("phase6_candidate_set_hash") or "") == expectations["expected_candidate_set_hash"],
        "phase6_ordered_frontier_hash_matches": str(row.get("phase6_ordered_frontier_hash") or "") == expectations["expected_ordered_frontier_hash"],
        "decisive_frontier_observed_once": decisive_match is not None,
        "decision_packet_present": bool(packet),
        "packet_hash_recomputes": bool(packet) and canary.corpus._packet_hash(packet) == str(decisive.get("decision_packet_hash") or ""),  # type: ignore[attr-defined]
        "packet_candidate_set_hash_matches_surface": bool(packet) and canary._canonical_packet_candidate_set_hash(packet) == expectations["expected_candidate_set_hash"],  # type: ignore[attr-defined]
        "packet_ordered_frontier_hash_matches_surface": bool(packet) and canary._canonical_packet_ordered_frontier_hash(packet) == expectations["expected_ordered_frontier_hash"],  # type: ignore[attr-defined]
        "candidate_count_matches_surface": len(frontier) == expectations["expected_candidate_count"] and int(decisive.get("candidate_count") or 0) == expectations["expected_candidate_count"],
        "frontier_targets_match_surface": frontier_targets == expectations["expected_targets"],
        "selection_contract_matches": bool(packet) and str(packet.get("selection_contract") or "") == EXPECTED_SELECTION_CONTRACTS.get(policy_mode, ""),
        "selected_laps_read_resolves_on_frontier": str(decisive.get("selected_capability") or "") == contract.CHAIN[0] and selected_path is not None,
        "mythic_proof_lineage_complete": canary._proof_lineage_complete(post_frontier_transactions),  # type: ignore[attr-defined]
        "observed_cost_within_frozen_policy_budget": observed_cost_within_frozen_policy_budget,
    }
    return {
        "scenario": scenario,
        "objective_variant": variant.name if variant is not None else None,
        "policy_mode": policy_mode,
        "selected_path": selected_path.name if selected_path is not None else None,
        "correct_first": correct_first,
        "transactions_from_frontier": transactions_from_frontier,
        "min_policy_transactions_from_frontier": MIN_POLICY_TRANSACTIONS_FROM_FRONTIER,
        "max_policy_transactions_from_frontier": MAX_POLICY_TRANSACTIONS_FROM_FRONTIER,
        "regret": regret,
        "ts": row.get("ts"),
        "ts_iso": row.get("ts_iso"),
        "semantic_transaction_count": row.get("semantic_transaction_count"),
        "effective_backends": row.get("effective_backends"),
        "row_invariant_failures": row_failures,
        "decisive_decision_index": decisive_index,
        "decisive_decision": canary._decision_summary(decisive) if decisive else None,  # type: ignore[attr-defined]
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def validate_matrix_results(
    forced_rows: list[dict[str, Any]],
    policy_rows: list[dict[str, Any]],
    canary_report: dict[str, Any],
    *,
    forced_source_path: Path | None = None,
    policy_source_path: Path | None = None,
) -> dict[str, Any]:
    expectations = _canary_expectations(canary_report)
    matched_forced_attempt_rows = _forced_rows(forced_rows)
    (
        matched_forced_rows,
        forced_attempt_reports,
        forced_attempt_checks,
        forced_attempt_mode,
    ) = canary.select_countable_attempt_rows(matched_forced_attempt_rows)
    forced_reports = [canary._row_report(row, expectations) for row in matched_forced_rows]  # type: ignore[attr-defined]
    forced_cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for report in forced_reports:
        key = (str(report.get("objective_variant") or ""), str(report.get("forced_path") or ""))
        forced_cells.setdefault(key, []).append(report)
    expected_cells = {
        (variant.name, forced_path.name)
        for variant in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants
        for forced_path in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths
    }
    paired_forced_comparisons: list[dict[str, Any]] = []
    for variant in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants:
        correct_path = next(
            path.name
            for path in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths
            if (path.first_host.casefold(), path.first_domain.casefold())
            == (variant.target_host.casefold(), variant.target_domain.casefold())
        )
        wrong_path = next(
            path.name
            for path in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths
            if path.name != correct_path
        )
        correct_rows = sorted(forced_cells.get((variant.name, correct_path), []), key=lambda item: float(item.get("ts") or 0.0))
        wrong_rows = sorted(forced_cells.get((variant.name, wrong_path), []), key=lambda item: float(item.get("ts") or 0.0))
        for repeat_index, (correct_row, wrong_row) in enumerate(zip(correct_rows, wrong_rows), start=1):
            correct_cost = correct_row.get("transactions_from_frontier")
            wrong_cost = wrong_row.get("transactions_from_frontier")
            paired_forced_comparisons.append({
                "objective_variant": variant.name,
                "repeat_index": repeat_index,
                "correct_path": correct_path,
                "wrong_path": wrong_path,
                "correct_cost": correct_cost,
                "wrong_cost": wrong_cost,
                "correct_first_is_cheaper": (
                    isinstance(correct_cost, (int, float))
                    and isinstance(wrong_cost, (int, float))
                    and float(correct_cost) < float(wrong_cost)
                ),
            })

    matched_policy_attempt_rows = _policy_rows(policy_rows)
    (
        matched_policy_rows,
        policy_attempt_reports,
        policy_attempt_checks,
        policy_attempt_mode,
    ) = canary.select_countable_attempt_rows(matched_policy_attempt_rows)
    policy_reports = [_policy_row_report(row, expectations) for row in matched_policy_rows]
    policy_cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for report in policy_reports:
        key = (str(report.get("policy_mode") or ""), str(report.get("objective_variant") or ""))
        policy_cells.setdefault(key, []).append(report)
    policy_summaries: dict[str, Any] = {}
    for policy_mode in EXPECTED_POLICY_MODES:
        rows_for_policy = [report for report in policy_reports if report["policy_mode"] == policy_mode]
        regrets = [float(report["regret"]) for report in rows_for_policy if report["regret"] is not None]
        policy_summaries[policy_mode] = {
            "rows": len(rows_for_policy),
            "objective_rows": {
                variant.name: len(policy_cells.get((policy_mode, variant.name), []))
                for variant in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants
            },
            "correct_first_count": sum(1 for report in rows_for_policy if report["correct_first"] is True),
            "objective_proof_and_clean_stop_count": sum(
                1
                for report in rows_for_policy
                if report["checks"]["row_invariants_pass"] and report["checks"]["request_completed"]
            ),
            "regret_values": regrets,
            "aggregate_regret": sum(regrets) if regrets else None,
            "mean_regret": mean(regrets) if regrets else None,
            "stable_choice_by_objective": {
                variant.name: len({
                    str(report.get("selected_path") or "")
                    for report in policy_cells.get((policy_mode, variant.name), [])
                }) <= 1
                for variant in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants
            },
        }
    symbolic_regret = policy_summaries["symbolic"]["aggregate_regret"]
    hybrid_regret = policy_summaries["hybrid"]["aggregate_regret"]
    shadow_report = contract.validate_laps_family_transfer_holdout()
    checks = {
        "canary_passes": expectations["canary_passes"],
        "canary_authorizes_forced_matrix": expectations["canary_authorizes_forced_matrix"],
        "forced_attempt_accounting_valid": all(forced_attempt_checks.values()),
        "forced_row_count_matches_budget": len(matched_forced_rows) == EXPECTED_FORCED_ROWS,
        "forced_repeats_per_cell_match_budget": set(forced_cells) == expected_cells and all(
            len(forced_cells.get(cell, [])) == EXPECTED_FORCED_REPEATS_PER_CELL
            for cell in expected_cells
        ),
        "all_forced_rows_preserve_frontier_and_lineage": bool(forced_reports) and all(report["passes_gate"] for report in forced_reports),
        "all_forced_branches_prove_objective": all(
            report["checks"]["row_invariants_pass"] and report["checks"]["request_completed"]
            for report in forced_reports
        ),
        "all_six_paired_forced_comparisons_present": len(paired_forced_comparisons) == 6,
        "correct_first_is_cheaper_in_all_pairs": bool(paired_forced_comparisons) and all(
            pair["correct_first_is_cheaper"] for pair in paired_forced_comparisons
        ),
        "policy_attempt_accounting_valid": all(policy_attempt_checks.values()),
        "policy_row_count_matches_budget": len(matched_policy_rows) == EXPECTED_POLICY_ROWS,
        "policy_repeats_per_objective_match_budget": all(
            len(policy_cells.get((policy_mode, variant.name), [])) == EXPECTED_POLICY_REPEATS_PER_OBJECTIVE
            for policy_mode in EXPECTED_POLICY_MODES
            for variant in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants
        ),
        "all_policy_rows_preserve_frontier_and_lineage": bool(policy_reports) and all(report["passes_gate"] for report in policy_reports),
        "hybrid_selects_correct_first_in_all_six": policy_summaries["hybrid"]["correct_first_count"] == 6,
        "hybrid_proves_and_clean_stops_in_all_six": policy_summaries["hybrid"]["objective_proof_and_clean_stop_count"] == 6,
        "hybrid_has_lower_aggregate_regret_than_symbolic": (
            isinstance(hybrid_regret, (int, float))
            and isinstance(symbolic_regret, (int, float))
            and float(hybrid_regret) < float(symbolic_regret)
        ),
        "shadow_fixed_order_controls_each_fail_one_variant": (
            (shadow_report.get("checks") or {}).get("fixed_order_controls_each_fail_one_variant") is True
        ),
    }
    return {
        "kind": "laps_family_transfer_matrix_validation",
        "generated_at": _now(),
        "expected_forced_rows": EXPECTED_FORCED_ROWS,
        "expected_forced_repeats_per_cell": EXPECTED_FORCED_REPEATS_PER_CELL,
        "expected_policy_rows": EXPECTED_POLICY_ROWS,
        "expected_policy_repeats_per_objective": EXPECTED_POLICY_REPEATS_PER_OBJECTIVE,
        "source_artifacts": {
            "forced_results": (
                {"relative_path": canary._relative_path(forced_source_path), "sha256": canary._sha256_file(forced_source_path)}  # type: ignore[attr-defined]
                if forced_source_path is not None
                else None
            ),
            "policy_results": (
                {"relative_path": canary._relative_path(policy_source_path), "sha256": canary._sha256_file(policy_source_path)}  # type: ignore[attr-defined]
                if policy_source_path is not None
                else None
            ),
        },
        "surface_expectations": expectations,
        "forced_attempt_accounting": {
            "mode": forced_attempt_mode,
            "matched_attempt_row_count": len(matched_forced_attempt_rows),
            "countable_row_count": len(matched_forced_rows),
            "diagnostic_row_count": len(matched_forced_attempt_rows) - len(matched_forced_rows),
            "checks": forced_attempt_checks,
            "attempt_reports": forced_attempt_reports,
        },
        "forced_matching_row_count": len(matched_forced_rows),
        "forced_cell_counts": {
            f"{variant}:{path}": len(rows)
            for (variant, path), rows in sorted(forced_cells.items())
        },
        "forced_row_reports": forced_reports,
        "paired_forced_comparisons": paired_forced_comparisons,
        "policy_attempt_accounting": {
            "mode": policy_attempt_mode,
            "matched_attempt_row_count": len(matched_policy_attempt_rows),
            "countable_row_count": len(matched_policy_rows),
            "diagnostic_row_count": len(matched_policy_attempt_rows) - len(matched_policy_rows),
            "checks": policy_attempt_checks,
            "attempt_reports": policy_attempt_reports,
        },
        "policy_matching_row_count": len(matched_policy_rows),
        "policy_summaries": policy_summaries,
        "policy_row_reports": policy_reports,
        "shadow_report": {
            "fixed_order_controls_each_fail_one_variant": (
                (shadow_report.get("checks") or {}).get("fixed_order_controls_each_fail_one_variant")
            ),
            "fixed_order_first_target": shadow_report.get("fixed_order_first_target"),
            "fixed_order_second_target": shadow_report.get("fixed_order_second_target"),
        },
        "checks": checks,
        "passes_gate": all(checks.values()),
        "authorization": {
            "phase6_complete": all(checks.values()),
            "reason": (
                "The sealed LAPS holdout passed forced confirmation, paired-cost, shadow-control, and "
                "Symbolic-vs-Hybrid live policy gates."
                if all(checks.values())
                else "The sealed LAPS holdout did not satisfy every Phase 6 matrix gate."
            ),
        },
        "_selected_forced_rows": matched_forced_rows,
        "_selected_policy_rows": matched_policy_rows,
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "laps-family-transfer-matrix-validate",
        help="validate the Phase 6 forced-confirmation and Symbolic/Hybrid LAPS matrices",
    )
    parser.add_argument("--forced-results", default=str(DEFAULT_FORCED_RESULTS_PATH), help="JSONL forced-confirmation results artifact")
    parser.add_argument("--policy-results", default=str(DEFAULT_POLICY_RESULTS_PATH), help="JSONL Symbolic/Hybrid policy results artifact")
    parser.add_argument("--canary-report", default=str(DEFAULT_CANARY_REPORT_PATH), help="accepted mechanics-canary validation report JSON")
    parser.add_argument("--output", default=None, help="optional JSON validation report path")
    parser.add_argument("--forced-row-output", default=None, help="optional pinned forced-confirmation JSONL output path")
    parser.add_argument("--policy-row-output", default=None, help="optional pinned policy-matrix JSONL output path")
    parser.set_defaults(func=_cmd_laps_family_transfer_matrix_validate)


def _cmd_laps_family_transfer_matrix_validate(args: Any) -> int:
    forced_path = Path(args.forced_results)
    policy_path = Path(args.policy_results)
    try:
        forced_rows = _load_jsonl(forced_path, label="forced-confirmation results artifact")
        policy_rows = _load_jsonl(policy_path, label="policy-matrix results artifact")
        canary_report = _load_json(Path(args.canary_report), label="canary report")
        report = validate_matrix_results(
            forced_rows,
            policy_rows,
            canary_report,
            forced_source_path=forced_path,
            policy_source_path=policy_path,
        )
    except LapsFamilyTransferMatrixError as exc:
        print(f"laps-family-transfer-matrix-validate: {exc}", file=sys.stderr)
        return 2
    selected_forced_rows = report.pop("_selected_forced_rows", [])
    selected_policy_rows = report.pop("_selected_policy_rows", [])
    if report["passes_gate"] and args.forced_row_output:
        report["pinned_forced_artifact"] = canary.pin_canary_rows(selected_forced_rows, Path(args.forced_row_output))
    if report["passes_gate"] and args.policy_row_output:
        report["pinned_policy_artifact"] = canary.pin_canary_rows(selected_policy_rows, Path(args.policy_row_output))
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True))
    if args.output:
        _write_json(Path(args.output), report)
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(phase6_complete={report['authorization']['phase6_complete']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
