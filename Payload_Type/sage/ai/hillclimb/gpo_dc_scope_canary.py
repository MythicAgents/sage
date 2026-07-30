"""Validation for one live same-domain GPO DC-scope canary.

This module is eval-only. It consumes a persisted gauge row plus the repeated
live-surface report, verifies that the decisive packet still reconstructs the
authorized two-GPO frontier, and emits the narrow decision needed before any
matrix expansion.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

try:  # package import
    from . import gpo_dc_scope_late_blocker_contract as contract
    from . import gpo_dc_scope_live_surface as live_surface
    from . import policy_replay_calibration as calibration
    from . import policy_replay_corpus as corpus
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gpo_dc_scope_late_blocker_contract as contract  # type: ignore
    import gpo_dc_scope_live_surface as live_surface  # type: ignore
    import policy_replay_calibration as calibration  # type: ignore
    import policy_replay_corpus as corpus  # type: ignore


DEFAULT_RESULTS_PATH = calibration.DEFAULT_RESULTS_ROOT / "bare_vs_harness.jsonl"
DEFAULT_SURFACE_REPORT_PATH = (
    calibration.DEFAULT_RESULTS_ROOT / "gpo_dc_scope_live_surface_validation_20260713.json"
)
DEFAULT_PINNED_ROW_PATH = (
    calibration.DEFAULT_RESULTS_ROOT / "gpo_dc_scope_late_blocker_packet_canary_20260713.jsonl"
)
EXPECTED_POLICY_MODE = "hybrid"
EXPECTED_SELECTION_CONTRACT = "admissible_frontier"


class GpoDcScopeCanaryError(ValueError):
    """Raised when canary evidence cannot be loaded or interpreted."""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GpoDcScopeCanaryError(f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GpoDcScopeCanaryError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GpoDcScopeCanaryError(f"{label} must be a JSON object")
    return payload


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise GpoDcScopeCanaryError(f"missing canary results artifact: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GpoDcScopeCanaryError(f"invalid JSONL row {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise GpoDcScopeCanaryError(f"JSONL row {path}:{line_no} must be an object")
        rows.append(row)
    if not rows:
        raise GpoDcScopeCanaryError(f"canary results artifact has no rows: {path}")
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(calibration.DEFAULT_RESULTS_ROOT.resolve()))
    except ValueError:
        return str(path)


def _surface_expectations(surface_report: dict[str, Any]) -> dict[str, Any]:
    observations = list(surface_report.get("observations") or [])
    candidate_hashes = [str(value or "") for value in list(surface_report.get("candidate_hashes") or []) if value]
    expected_targets = list((surface_report.get("spec") or {}).get("expected_targets") or [])
    first_observation = observations[0] if observations and isinstance(observations[0], dict) else {}
    best_indices = list(first_observation.get("best_indices") or [])
    frontier = list(first_observation.get("post_blocker_frontier") or [])
    unique_hashes = sorted(set(candidate_hashes))
    best_target = ""
    if len(best_indices) == 1 and 0 <= int(best_indices[0]) < len(frontier):
        best_target = str((frontier[int(best_indices[0])] or {}).get("target") or "")
    return {
        "surface_report_passes": surface_report.get("passes_gate") is True,
        "surface_authorizes_canary": (
            (surface_report.get("authorization") or {}).get("live_canary_authorized") is True
        ),
        "candidate_hashes": candidate_hashes,
        "expected_candidate_hash": unique_hashes[0] if len(unique_hashes) == 1 else "",
        "expected_targets": expected_targets,
        "expected_candidate_count": len(expected_targets),
        "best_indices": best_indices,
        "best_target": best_target,
    }


def _matching_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("scenario") or "") == live_surface.LIVE_SURFACE.scenario
        and str(row.get("configured_policy_mode") or "").strip().casefold() == EXPECTED_POLICY_MODE
    ]


def _latest_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    indexed = list(enumerate(rows))
    _index, row = max(
        indexed,
        key=lambda item: (float(item[1].get("ts", 0.0) or 0.0), item[0]),
    )
    return row


def _blocked_cycles(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        cycle
        for cycle in list(row.get("controller_cycles") or [])
        if isinstance(cycle, dict)
        and str(cycle.get("action") or "") == contract.GPO_DC_SCOPE_LATE_BLOCKER.blocked_action
        and cycle.get("ok") is False
    ]


def _packet_preserves_blocked_outcome(packet: dict[str, Any]) -> bool:
    recent_outcomes = list(((packet.get("normalized_state") or {}).get("recent_outcomes") or []))
    return any(
        isinstance(outcome, dict)
        and str(outcome.get("status") or "") == "blocked"
        and str(outcome.get("capability") or "").endswith(contract.GPO_DC_SCOPE_LATE_BLOCKER.blocked_action)
        and str(outcome.get("effect") or "").startswith(contract.BLOCKED_EFFECT_PREFIX)
        for outcome in recent_outcomes
    )


def _decision_summary(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": decision.get("decision_id"),
        "candidate_hash": decision.get("candidate_hash"),
        "candidate_count": decision.get("candidate_count"),
        "selected_capability": decision.get("selected_capability"),
        "selected_target": decision.get("selected_target"),
        "selected_index": decision.get("selected_index"),
        "decision_packet_hash": decision.get("decision_packet_hash"),
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_canary_results(
    rows: list[dict[str, Any]],
    surface_report: dict[str, Any],
    *,
    source_path: Path | None = None,
) -> dict[str, Any]:
    expectations = _surface_expectations(surface_report)
    matches = _matching_rows(rows)
    row = _latest_row(matches)
    expected_hash = expectations["expected_candidate_hash"]
    decisive_match = calibration._find_decisive_decision(row or {}, expected_hash) if expected_hash else None
    decisive_index, decisive = decisive_match if decisive_match is not None else (None, {})
    packet = decisive.get("decision_packet")
    packet = dict(packet) if isinstance(packet, dict) else {}
    frontier = corpus._packet_frontier(packet) if packet else []
    blocker_cycles = _blocked_cycles(row or {})
    frontier_targets = [str(item.get("target") or "") for item in frontier]
    selected_index = _int_or_none(decisive.get("selected_index"))
    row_failures = calibration._row_invariant_failures(row or {}) if row is not None else ["missing_canary_row"]
    checks = {
        "surface_report_passes": expectations["surface_report_passes"],
        "surface_authorizes_canary": expectations["surface_authorizes_canary"],
        "canary_row_present": row is not None,
        "row_invariants_pass": row is not None and not row_failures,
        "hybrid_policy_identity_preserved": (
            row is not None
            and str(row.get("configured_policy_mode") or "").strip().casefold() == EXPECTED_POLICY_MODE
            and str(row.get("policy_mode") or "").strip().casefold() == EXPECTED_POLICY_MODE
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
        "packet_preserves_blocked_outcome": bool(packet) and _packet_preserves_blocked_outcome(packet),
        "selection_contract_matches": (
            bool(packet) and str(packet.get("selection_contract") or "") == EXPECTED_SELECTION_CONTRACT
        ),
        "selected_unique_best_target": (
            str(decisive.get("selected_target") or "") == expectations["best_target"]
            and selected_index in expectations["best_indices"]
        ),
        "selected_gpo_capability": (
            str(decisive.get("selected_capability") or "") == contract.TARGET_CAPABILITY
        ),
        "objective_proof_and_clean_stop": (
            row is not None
            and row.get("objective_proven") is True
            and row.get("clean_stop") is True
            and row.get("request_completed") is True
        ),
        "backend_provenance_complete": row is not None and row.get("backend_provenance_complete") is True,
    }
    return {
        "kind": "gpo_dc_scope_canary_validation",
        "generated_at": _now(),
        "scenario": live_surface.LIVE_SURFACE.scenario,
        "expected_policy_mode": EXPECTED_POLICY_MODE,
        "matching_row_count": len(matches),
        "source_artifact": (
            {
                "relative_path": _relative_path(source_path),
                "sha256": _sha256_file(source_path),
            }
            if source_path is not None
            else None
        ),
        "surface_expectations": expectations,
        "row_invariant_failures": row_failures,
        "row_summary": (
            {
                "ts": row.get("ts"),
                "ts_iso": row.get("ts_iso"),
                "chat_channel_id": row.get("chat_channel_id"),
                "chat_request_id": row.get("chat_request_id"),
                "objective_proven": row.get("objective_proven"),
                "clean_stop": row.get("clean_stop"),
                "request_completed": row.get("request_completed"),
                "semantic_transaction_count": row.get("semantic_transaction_count"),
                "controller_status": row.get("controller_status"),
                "controller_terminal_reason": row.get("controller_terminal_reason"),
                "effective_backends": row.get("effective_backends"),
            }
            if row is not None
            else None
        ),
        "blocker_cycles": blocker_cycles,
        "decisive_decision_index": decisive_index,
        "decisive_decision": _decision_summary(decisive) if decisive else None,
        "decisive_frontier": frontier,
        "checks": checks,
        "passes_gate": all(checks.values()),
        "authorization": {
            "live_matrix_authorized": all(checks.values()),
            "reason": (
                "The single live canary preserved the authorized frontier, packet integrity, objective proof, "
                "and clean stop."
                if all(checks.values())
                else "The single live canary did not satisfy the matrix release gate."
            ),
        },
        "_selected_row": row,
    }


def pin_canary_row(row: dict[str, Any], path: Path) -> dict[str, Any]:
    _write_jsonl(path, [row])
    return {
        "relative_path": _relative_path(path),
        "sha256": _sha256_file(path),
        "rows": 1,
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "gpo-dc-scope-canary-validate",
        help="validate one packet-backed live canary for the GPO DC-scope late-blocker surface",
    )
    parser.add_argument("--results", default=str(DEFAULT_RESULTS_PATH), help="JSONL gauge results artifact")
    parser.add_argument(
        "--surface-report",
        default=str(DEFAULT_SURFACE_REPORT_PATH),
        help="live-surface validation report JSON",
    )
    parser.add_argument("--output", default=None, help="optional JSON validation report path")
    parser.add_argument("--row-output", default=None, help="optional pinned one-row JSONL output path")
    parser.set_defaults(func=_cmd_gpo_dc_scope_canary_validate)


def _cmd_gpo_dc_scope_canary_validate(args: Any) -> int:
    results_path = Path(args.results)
    try:
        rows = _load_jsonl(results_path)
        surface_report = _load_json(Path(args.surface_report), label="live-surface report")
        report = validate_canary_results(rows, surface_report, source_path=results_path)
    except GpoDcScopeCanaryError as exc:
        print(f"gpo-dc-scope-canary-validate: {exc}", file=sys.stderr)
        return 2
    selected_row = report.pop("_selected_row", None)
    if report["passes_gate"] and args.row_output and isinstance(selected_row, dict):
        report["pinned_row_artifact"] = pin_canary_row(selected_row, Path(args.row_output))
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        _write_json(Path(args.output), report)
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(live_matrix_authorized={report['authorization']['live_matrix_authorized']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
