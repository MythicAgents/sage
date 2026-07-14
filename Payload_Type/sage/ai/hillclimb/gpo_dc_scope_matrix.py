"""Validation for the repeated live same-domain GPO DC-scope matrix.

This module is eval-only. It consumes persisted gauge rows after the accepted
canary, rechecks the packet/frontier contract on every row, and reports the
observed policy ordering from recovery work without assuming that the learned
policies must remain tied.
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
import sys
from typing import Any

try:  # package import
    from . import gpo_dc_scope_canary as canary
    from . import gpo_dc_scope_late_blocker_contract as contract
    from . import gpo_dc_scope_live_surface as live_surface
    from . import policy_replay_calibration as calibration
    from . import policy_replay_corpus as corpus
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gpo_dc_scope_canary as canary  # type: ignore
    import gpo_dc_scope_late_blocker_contract as contract  # type: ignore
    import gpo_dc_scope_live_surface as live_surface  # type: ignore
    import policy_replay_calibration as calibration  # type: ignore
    import policy_replay_corpus as corpus  # type: ignore


DEFAULT_RESULTS_PATH = calibration.DEFAULT_RESULTS_ROOT / "bare_vs_harness.jsonl"
DEFAULT_CANARY_REPORT_PATH = calibration.DEFAULT_RESULTS_ROOT / "gpo_dc_scope_canary_validation_20260713.json"
DEFAULT_PINNED_MATRIX_PATH = calibration.DEFAULT_RESULTS_ROOT / "gpo_dc_scope_late_blocker_matrix_20260713.jsonl"
EXPECTED_ROWS_PER_POLICY = 3
METRIC_KIND = "transactions_from_frontier"
METRIC_DIRECTION = "lower_is_better"
TIE_TOLERANCE = 0.0
EXPECTED_SELECTION_CONTRACTS = {
    "symbolic": "symbolic_frontier",
    "llm": "semantic_catalog",
    "hybrid": "admissible_frontier",
}


class GpoDcScopeMatrixError(ValueError):
    """Raised when matrix evidence cannot be loaded or interpreted."""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GpoDcScopeMatrixError(f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GpoDcScopeMatrixError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GpoDcScopeMatrixError(f"{label} must be a JSON object")
    return payload


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise GpoDcScopeMatrixError(f"missing matrix results artifact: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GpoDcScopeMatrixError(f"invalid JSONL row {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise GpoDcScopeMatrixError(f"JSONL row {path}:{line_no} must be an object")
        rows.append(row)
    if not rows:
        raise GpoDcScopeMatrixError(f"matrix results artifact has no rows: {path}")
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _surface_expectations(canary_report: dict[str, Any]) -> dict[str, Any]:
    surface = dict(canary_report.get("surface_expectations") or {})
    return {
        "canary_passes": canary_report.get("passes_gate") is True,
        "canary_authorizes_matrix": (
            (canary_report.get("authorization") or {}).get("live_matrix_authorized") is True
        ),
        "canary_cutoff_ts": _float_or_none((canary_report.get("row_summary") or {}).get("ts")),
        "expected_candidate_hash": str(surface.get("expected_candidate_hash") or ""),
        "expected_candidate_count": int(surface.get("expected_candidate_count") or 0),
        "expected_targets": list(surface.get("expected_targets") or []),
    }


def _matrix_rows(
    rows: list[dict[str, Any]],
    *,
    after_ts: float | None,
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("scenario") or "") == live_surface.LIVE_SURFACE.scenario
        and str(row.get("configured_policy_mode") or "").strip().casefold() in calibration.POLICY_MODES
        and (after_ts is None or float(row.get("ts", 0.0) or 0.0) > after_ts)
    ]


def _packet_frontier(packet: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return corpus._packet_frontier(packet) if packet else []
    except Exception:
        return []


def _row_report(row: dict[str, Any], expectations: dict[str, Any]) -> dict[str, Any]:
    policy_mode = str(row.get("configured_policy_mode") or "").strip().casefold()
    expected_hash = expectations["expected_candidate_hash"]
    decisive_match = calibration._find_decisive_decision(row, expected_hash) if expected_hash else None
    decisive_index, decisive = decisive_match if decisive_match is not None else (None, {})
    packet = decisive.get("decision_packet")
    packet = dict(packet) if isinstance(packet, dict) else {}
    frontier = _packet_frontier(packet)
    frontier_targets = [str(item.get("target") or "") for item in frontier]
    selected_index = _int_or_none(decisive.get("selected_index"))
    selected = (
        frontier[selected_index]
        if selected_index is not None and 0 <= selected_index < len(frontier)
        else {}
    )
    blocker_cycles = canary._blocked_cycles(row)  # type: ignore[attr-defined]
    row_failures = calibration._row_invariant_failures(row)
    recovery_transactions = (
        calibration._transactions_from_frontier(row, int(decisive_index))
        if decisive_index is not None
        else None
    )
    checks = {
        "row_invariants_pass": not row_failures,
        "policy_identity_preserved": (
            str(row.get("configured_policy_mode") or "").strip().casefold()
            == str(row.get("policy_mode") or "").strip().casefold()
        ),
        "blocker_observed_once": (
            len(blocker_cycles) == 1
            and str(blocker_cycles[0].get("note") or "") == contract.GPO_DC_SCOPE_LATE_BLOCKER.blocker_reason
        ),
        "decisive_frontier_observed_once": decisive_match is not None,
        "decision_packet_present": bool(packet),
        "packet_hash_recomputes": (
            bool(packet)
            and corpus._packet_hash(packet) == str(decisive.get("decision_packet_hash") or "")
        ),
        "packet_candidate_hash_matches_surface": (
            bool(packet) and str(packet.get("candidate_hash") or "") == expected_hash
        ),
        "reconstructed_frontier_hash_matches_surface": (
            bool(packet) and corpus._reconstructed_candidate_hash(packet) == expected_hash
        ),
        "candidate_count_matches_surface": (
            len(frontier) == expectations["expected_candidate_count"]
            and int(decisive.get("candidate_count") or 0) == expectations["expected_candidate_count"]
        ),
        "frontier_targets_match_surface": frontier_targets == expectations["expected_targets"],
        "packet_preserves_blocked_outcome": bool(packet) and canary._packet_preserves_blocked_outcome(packet),  # type: ignore[attr-defined]
        "selection_contract_matches": (
            bool(packet)
            and str(packet.get("selection_contract") or "") == EXPECTED_SELECTION_CONTRACTS.get(policy_mode, "")
        ),
        "selected_choice_resolves_on_frontier": (
            bool(selected)
            and str(selected.get("name") or "") == str(decisive.get("selected_capability") or "")
            and str(selected.get("target") or "") == str(decisive.get("selected_target") or "")
        ),
        "selected_gpo_capability": str(decisive.get("selected_capability") or "") == contract.TARGET_CAPABILITY,
        "objective_proof_and_clean_stop": (
            row.get("objective_proven") is True
            and row.get("clean_stop") is True
            and row.get("request_completed") is True
        ),
        "backend_provenance_complete": row.get("backend_provenance_complete") is True,
    }
    return {
        "policy_mode": policy_mode,
        "ts": row.get("ts"),
        "ts_iso": row.get("ts_iso"),
        "chat_channel_id": row.get("chat_channel_id"),
        "chat_request_id": row.get("chat_request_id"),
        "semantic_transaction_count": row.get("semantic_transaction_count"),
        "recovery_transactions": recovery_transactions,
        "effective_backends": row.get("effective_backends"),
        "blocker_cycles": blocker_cycles,
        "decisive_decision_index": decisive_index,
        "decisive_decision": canary._decision_summary(decisive) if decisive else None,  # type: ignore[attr-defined]
        "row_invariant_failures": row_failures,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def validate_matrix_results(
    rows: list[dict[str, Any]],
    canary_report: dict[str, Any],
    *,
    expected_rows_per_policy: int = EXPECTED_ROWS_PER_POLICY,
    after_ts: float | None = None,
    source_path: Path | None = None,
) -> dict[str, Any]:
    expectations = _surface_expectations(canary_report)
    cutoff_ts = after_ts if after_ts is not None else expectations["canary_cutoff_ts"]
    matched_rows = _matrix_rows(rows, after_ts=cutoff_ts)
    reports = [_row_report(row, expectations) for row in matched_rows]
    policy_reports = {
        policy_mode: [report for report in reports if report["policy_mode"] == policy_mode]
        for policy_mode in calibration.POLICY_MODES
    }
    policy_summaries: dict[str, Any] = {}
    for policy_mode, rows_for_policy in policy_reports.items():
        recovery_values = [
            float(report["recovery_transactions"])
            for report in rows_for_policy
            if report["recovery_transactions"] is not None
        ]
        total_values = [
            float(report["semantic_transaction_count"])
            for report in rows_for_policy
            if report["semantic_transaction_count"] is not None
        ]
        choices = [
            {
                "selected_capability": (report.get("decisive_decision") or {}).get("selected_capability"),
                "selected_target": (report.get("decisive_decision") or {}).get("selected_target"),
                "selected_index": (report.get("decisive_decision") or {}).get("selected_index"),
            }
            for report in rows_for_policy
            if report.get("decisive_decision")
        ]
        policy_summaries[policy_mode] = {
            "rows": len(rows_for_policy),
            "recovery_transaction_values": recovery_values,
            "recovery_transaction_mean": mean(recovery_values) if recovery_values else None,
            "semantic_transaction_values": total_values,
            "semantic_transaction_mean": mean(total_values) if total_values else None,
            "choices": choices,
            "stable_choice": len({_canonical_json(choice) for choice in choices}) <= 1 if choices else False,
        }
    policy_metrics = {
        policy_mode: float(summary["recovery_transaction_mean"])
        for policy_mode, summary in policy_summaries.items()
        if summary["recovery_transaction_mean"] is not None
    }
    observed_order = (
        calibration._observed_order(
            policy_metrics,
            direction=METRIC_DIRECTION,
            tolerance=TIE_TOLERANCE,
        )
        if len(policy_metrics) == len(calibration.POLICY_MODES)
        else []
    )
    learned_relation = (
        calibration._observed_relation(
            policy_metrics,
            "llm",
            "hybrid",
            direction=METRIC_DIRECTION,
            tolerance=TIE_TOLERANCE,
        )
        if len(policy_metrics) == len(calibration.POLICY_MODES)
        else "unavailable"
    )
    checks = {
        "canary_passes": expectations["canary_passes"],
        "canary_authorizes_matrix": expectations["canary_authorizes_matrix"],
        "canary_cutoff_available": cutoff_ts is not None,
        "rows_per_policy": all(
            summary["rows"] == expected_rows_per_policy
            for summary in policy_summaries.values()
        ),
        "all_rows_preserve_packet_frontier_and_live_invariants": bool(reports)
        and all(report["passes_gate"] for report in reports),
        "decisive_frontier_is_stable": bool(reports)
        and all(
            (report.get("decisive_decision") or {}).get("candidate_hash") == expectations["expected_candidate_hash"]
            for report in reports
        ),
        "policy_choices_are_stable_within_arm": all(
            summary["stable_choice"]
            for summary in policy_summaries.values()
        ),
        "policy_order_is_reportable": len(policy_metrics) == len(calibration.POLICY_MODES),
    }
    return {
        "kind": "gpo_dc_scope_matrix_validation",
        "generated_at": _now(),
        "scenario": live_surface.LIVE_SURFACE.scenario,
        "expected_rows_per_policy": expected_rows_per_policy,
        "metric": {
            "kind": METRIC_KIND,
            "direction": METRIC_DIRECTION,
            "tie_tolerance": TIE_TOLERANCE,
        },
        "source_artifact": (
            {
                "relative_path": canary._relative_path(source_path),  # type: ignore[attr-defined]
                "sha256": canary._sha256_file(source_path),  # type: ignore[attr-defined]
            }
            if source_path is not None
            else None
        ),
        "surface_expectations": expectations,
        "canary_cutoff_ts": cutoff_ts,
        "matching_row_count": len(matched_rows),
        "policy_summaries": policy_summaries,
        "observed_policy_order": observed_order,
        "learned_policy_relation": learned_relation,
        "learned_policy_tie_preserved": learned_relation == "tie",
        "row_reports": reports,
        "checks": checks,
        "passes_gate": all(checks.values()),
        "authorization": {
            "packet_corpus_expansion_authorized": all(checks.values()),
            "reason": (
                "The repeated live matrix preserved the accepted canary frontier and live invariants."
                if all(checks.values())
                else "The repeated live matrix did not satisfy the packet-backed expansion gate."
            ),
        },
        "_selected_rows": matched_rows,
    }


def pin_matrix_rows(rows: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    _write_jsonl(path, rows)
    return {
        "relative_path": canary._relative_path(path),  # type: ignore[attr-defined]
        "sha256": canary._sha256_file(path),  # type: ignore[attr-defined]
        "rows": len(rows),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "gpo-dc-scope-matrix-validate",
        help="validate the repeated packet-backed live matrix for the GPO DC-scope surface",
    )
    parser.add_argument("--results", default=str(DEFAULT_RESULTS_PATH), help="JSONL gauge results artifact")
    parser.add_argument(
        "--canary-report",
        default=str(DEFAULT_CANARY_REPORT_PATH),
        help="accepted canary validation report JSON",
    )
    parser.add_argument(
        "--expected-rows-per-policy",
        type=int,
        default=EXPECTED_ROWS_PER_POLICY,
        help="required clean rows for each policy arm",
    )
    parser.add_argument(
        "--after-ts",
        type=float,
        default=None,
        help="only include rows strictly newer than this timestamp; defaults to the accepted canary row",
    )
    parser.add_argument("--output", default=None, help="optional JSON validation report path")
    parser.add_argument("--row-output", default=None, help="optional pinned matrix JSONL output path")
    parser.set_defaults(func=_cmd_gpo_dc_scope_matrix_validate)


def _cmd_gpo_dc_scope_matrix_validate(args: Any) -> int:
    results_path = Path(args.results)
    try:
        rows = _load_jsonl(results_path)
        canary_report = _load_json(Path(args.canary_report), label="canary report")
        report = validate_matrix_results(
            rows,
            canary_report,
            expected_rows_per_policy=int(args.expected_rows_per_policy),
            after_ts=args.after_ts,
            source_path=results_path,
        )
    except GpoDcScopeMatrixError as exc:
        print(f"gpo-dc-scope-matrix-validate: {exc}", file=sys.stderr)
        return 2
    selected_rows = report.pop("_selected_rows", [])
    if report["passes_gate"] and args.row_output:
        report["pinned_matrix_artifact"] = pin_matrix_rows(selected_rows, Path(args.row_output))
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        _write_json(Path(args.output), report)
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(packet_corpus_expansion_authorized={report['authorization']['packet_corpus_expansion_authorized']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
