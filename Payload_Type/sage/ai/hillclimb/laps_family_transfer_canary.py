"""Validation for the Phase 6 LAPS mechanics canaries.

This module is eval-only. It consumes the four preregistered forced-path rows
after the repeated live-surface gate passes and verifies that the declared LAPS
chain is real on both objective variants without crediting forced labels as
policy wins.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

try:  # package import
    from . import laps_family_transfer_holdout as contract
    from . import laps_family_transfer_live_surface as live_surface
    from . import policy_replay_calibration as calibration
    from . import policy_replay_corpus as corpus
    from ..langgraph import policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import laps_family_transfer_holdout as contract  # type: ignore
    import laps_family_transfer_live_surface as live_surface  # type: ignore
    import policy_replay_calibration as calibration  # type: ignore
    import policy_replay_corpus as corpus  # type: ignore
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import policy  # type: ignore


DEFAULT_RESULTS_PATH = calibration.DEFAULT_RESULTS_ROOT / f"laps_family_transfer_mechanics_canaries_{contract.RESULT_ARTIFACT_SUFFIX}.jsonl"
DEFAULT_SURFACE_REPORT_PATH = (
    calibration.DEFAULT_RESULTS_ROOT / f"laps_family_transfer_live_surface_validation_{contract.RESULT_ARTIFACT_SUFFIX}.json"
)
DEFAULT_PINNED_ROWS_PATH = (
    calibration.DEFAULT_RESULTS_ROOT / f"laps_family_transfer_mechanics_canaries_pinned_{contract.RESULT_ARTIFACT_SUFFIX}.jsonl"
)
EXPECTED_POLICY_MODE = "symbolic"
EXPECTED_SELECTION_CONTRACT = policy.SELECTION_CONTRACT_SYMBOLIC
EXPECTED_CANARY_ROWS = contract.LAPS_FAMILY_TRANSFER_HOLDOUT.budgets.mechanics_canaries
DEFAULT_MAX_PRE_FRONTIER_DIAGNOSTIC_RETRIES = 1
COLLECTION_CAPABILITY = "collect-graph"


class LapsFamilyTransferCanaryError(ValueError):
    """Raised when Phase 6 canary evidence is missing or malformed."""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LapsFamilyTransferCanaryError(f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LapsFamilyTransferCanaryError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LapsFamilyTransferCanaryError(f"{label} must be a JSON object")
    return payload


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise LapsFamilyTransferCanaryError(f"missing canary results artifact: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LapsFamilyTransferCanaryError(f"invalid JSONL row {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise LapsFamilyTransferCanaryError(f"JSONL row {path}:{line_no} must be an object")
        rows.append(row)
    if not rows:
        raise LapsFamilyTransferCanaryError(f"canary results artifact has no rows: {path}")
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
    candidate_hashes = [
        str(value or "")
        for value in list(surface_report.get("candidate_set_hashes") or [])
        if str(value or "")
    ]
    ordered_hashes = [
        str(value or "")
        for value in list(surface_report.get("ordered_frontier_hashes") or [])
        if str(value or "")
    ]
    unique_candidate_hashes = sorted(set(candidate_hashes))
    unique_ordered_hashes = sorted(set(ordered_hashes))
    return {
        "surface_report_passes": surface_report.get("passes_gate") is True,
        "surface_authorizes_canaries": (
            (surface_report.get("authorization") or {}).get("mechanics_canaries_authorized") is True
        ),
        "sealed_manifest_hash": str(surface_report.get("sealed_manifest_hash") or ""),
        "topology_hash": str(surface_report.get("topology_hash") or ""),
        "expected_candidate_set_hash": unique_candidate_hashes[0] if len(unique_candidate_hashes) == 1 else "",
        "expected_ordered_frontier_hash": unique_ordered_hashes[0] if len(unique_ordered_hashes) == 1 else "",
        "expected_targets": list((surface_report.get("spec") or {}).get("expected_targets") or []),
        "expected_candidate_count": len((surface_report.get("spec") or {}).get("expected_targets") or []),
    }


def _scenario_to_variant() -> dict[str, contract.ObjectiveVariantSpec]:
    return {
        f"laps-family-transfer-{variant.name}": variant
        for variant in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants
    }


def _matching_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scenarios = set(_scenario_to_variant())
    return [
        row
        for row in rows
        if str(row.get("side") or "") == "harness"
        and str(row.get("scenario") or "") in scenarios
        and str(row.get("configured_policy_mode") or "").strip().casefold() == EXPECTED_POLICY_MODE
    ]


def _decision_frontier_capabilities(decision: dict[str, Any]) -> list[str]:
    packet = decision.get("decision_packet")
    packet = dict(packet) if isinstance(packet, dict) else {}
    return [
        str(item.get("name") or "")
        for item in _packet_frontier(packet)
        if isinstance(item, dict)
    ]


def _branch_surface_observed(row: dict[str, Any]) -> bool:
    for decision in list(row.get("decisions") or []):
        if not isinstance(decision, dict):
            continue
        selected = str(decision.get("selected_capability") or "")
        if selected and selected != COLLECTION_CAPABILITY:
            return True
        if any(
            capability and capability != COLLECTION_CAPABILITY
            for capability in _decision_frontier_capabilities(decision)
        ):
            return True
    return False


def _attempt_report(row: dict[str, Any]) -> dict[str, Any]:
    branch_surface_observed = _branch_surface_observed(row)
    planned_row_id = str(row.get("phase6_planned_row_id") or "")
    raw_attempt_index = row.get("phase6_attempt_index")
    raw_retry_budget = row.get("phase6_max_pre_frontier_diagnostic_retries")
    try:
        attempt_index = int(raw_attempt_index) if raw_attempt_index is not None else None
    except (TypeError, ValueError):
        attempt_index = None
    try:
        retry_budget = (
            int(raw_retry_budget)
            if raw_retry_budget is not None
            else DEFAULT_MAX_PRE_FRONTIER_DIAGNOSTIC_RETRIES
        )
    except (TypeError, ValueError):
        retry_budget = -1
    selected_capabilities = [
        str(decision.get("selected_capability") or "")
        for decision in list(row.get("decisions") or [])
        if isinstance(decision, dict)
    ]
    return {
        "planned_row_id": planned_row_id or None,
        "attempt_index": attempt_index,
        "declared_max_pre_frontier_diagnostic_retries": retry_budget,
        "branch_surface_observed": branch_surface_observed,
        "countable": branch_surface_observed,
        "pre_frontier_diagnostic": not branch_surface_observed,
        "controller_status": row.get("controller_status"),
        "controller_terminal_reason": row.get("controller_terminal_reason"),
        "selected_capabilities": selected_capabilities,
    }


def select_countable_attempt_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, bool], str]:
    """Retain all rows while counting only attempts that reached a branch surface.

    A row that only selected collection work never exposed the frozen branch and is diagnostic-only.
    Once any non-collection candidate or action appears, the row is countable even if it later fails
    frontier, proof, lineage, or objective checks. This prevents infrastructure retries from becoming
    hidden success filters while still keeping pre-frontier hangs append-only and auditable.
    """
    reports = [_attempt_report(row) for row in rows]
    metadata_flags = [
        bool(report["planned_row_id"]) or report["attempt_index"] is not None
        for report in reports
    ]
    if not any(metadata_flags):
        return list(rows), reports, {
            "attempt_metadata_complete": True,
            "declared_retry_budget_is_stable": True,
            "attempt_indices_are_contiguous": True,
            "pre_frontier_diagnostic_retry_budget_respected": True,
            "countable_attempt_is_unique_per_planned_row": True,
            "no_attempt_after_countable_attempt": True,
        }, "legacy-untracked"

    groups: dict[str, list[dict[str, Any]]] = {}
    metadata_complete = True
    for row, report in zip(rows, reports):
        del row
        planned_row_id = str(report.get("planned_row_id") or "")
        attempt_index = report.get("attempt_index")
        if not planned_row_id or not isinstance(attempt_index, int) or attempt_index < 1:
            metadata_complete = False
            continue
        groups.setdefault(planned_row_id, []).append(report)
    declared_budgets = {
        int(report["declared_max_pre_frontier_diagnostic_retries"])
        for report in reports
        if isinstance(report.get("declared_max_pre_frontier_diagnostic_retries"), int)
    }
    declared_retry_budget_is_stable = (
        len(declared_budgets) == 1
        and next(iter(declared_budgets), -1) >= 0
    )
    retry_budget = next(iter(declared_budgets), -1)
    attempt_indices_are_contiguous = metadata_complete and all(
        sorted(int(item["attempt_index"]) for item in group) == list(range(1, len(group) + 1))
        for group in groups.values()
    )
    pre_frontier_diagnostic_retry_budget_respected = (
        declared_retry_budget_is_stable
        and all(
            sum(1 for item in group if item["pre_frontier_diagnostic"]) <= retry_budget
            for group in groups.values()
        )
    )
    countable_attempt_is_unique_per_planned_row = all(
        sum(1 for item in group if item["countable"]) <= 1
        for group in groups.values()
    )
    no_attempt_after_countable_attempt = True
    for group in groups.values():
        ordered = sorted(group, key=lambda item: int(item["attempt_index"]))
        countable_indices = [
            int(item["attempt_index"])
            for item in ordered
            if item["countable"]
        ]
        if countable_indices and countable_indices[0] != int(ordered[-1]["attempt_index"]):
            no_attempt_after_countable_attempt = False
            break
    checks = {
        "attempt_metadata_complete": metadata_complete and all(metadata_flags),
        "declared_retry_budget_is_stable": declared_retry_budget_is_stable,
        "attempt_indices_are_contiguous": attempt_indices_are_contiguous,
        "pre_frontier_diagnostic_retry_budget_respected": pre_frontier_diagnostic_retry_budget_respected,
        "countable_attempt_is_unique_per_planned_row": countable_attempt_is_unique_per_planned_row,
        "no_attempt_after_countable_attempt": no_attempt_after_countable_attempt,
    }
    countable_rows = [
        row
        for row, report in zip(rows, reports)
        if report["countable"]
    ]
    return countable_rows, reports, checks, "append-only"


def _target_fields(target: Any) -> tuple[str, str]:
    fields = {
        key.strip().casefold(): value.strip().casefold()
        for part in str(target or "").split(";")
        if "=" in part
        for key, value in [part.split("=", 1)]
    }
    return fields.get("target", ""), fields.get("target_domain", "")


def _forced_path_for_target(target: Any) -> contract.ForcedPathSpec | None:
    host, domain = _target_fields(target)
    return next(
        (
            path
            for path in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths
            if (path.first_host.casefold(), path.first_domain.casefold()) == (host, domain)
        ),
        None,
    )


def _decisive_decision(row: dict[str, Any], expected_hash: str) -> tuple[int, dict[str, Any]] | None:
    decisions = list(row.get("decisions") or [])
    matches = [
        (index, decision)
        for index, decision in enumerate(decisions)
        if isinstance(decision, dict)
        and _canonical_packet_candidate_set_hash(decision.get("decision_packet")) == expected_hash
    ]
    return matches[0] if len(matches) == 1 else None


def _packet_frontier(packet: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return corpus._packet_frontier(packet) if packet else []
    except Exception:
        return []


def _canonical_packet_candidate_set_hash(packet: Any) -> str:
    frontier = _packet_frontier(dict(packet)) if isinstance(packet, dict) else []
    return contract.canonical_candidate_set_hash(frontier) if frontier else ""


def _canonical_packet_ordered_frontier_hash(packet: Any) -> str:
    frontier = _packet_frontier(dict(packet)) if isinstance(packet, dict) else []
    return contract.canonical_ordered_frontier_hash(frontier) if frontier else ""


def _transactions_from_frontier(row: dict[str, Any], decision_index: int | None) -> float | None:
    if decision_index is None:
        return None
    return calibration._transactions_from_frontier(row, int(decision_index))


def _post_frontier_transactions(row: dict[str, Any], decision_index: int | None) -> list[dict[str, Any]]:
    if decision_index is None:
        return []
    decisions = list(row.get("decisions") or [])
    decision_ids = {
        str(decision.get("decision_id") or "")
        for decision in decisions[int(decision_index):]
        if isinstance(decision, dict) and str(decision.get("decision_id") or "")
    }
    return [
        transaction
        for transaction in list(row.get("transactions") or [])
        if isinstance(transaction, dict)
        and str(transaction.get("decision_id") or "") in decision_ids
    ]


def _proof_lineage_complete(transactions: list[dict[str, Any]]) -> bool:
    if not transactions:
        return False
    for transaction in transactions:
        if str(transaction.get("capability") or "") not in contract.CHAIN:
            continue
        child_tasks = list(transaction.get("child_tasks") or [])
        verifier_ids = list(transaction.get("verifier_ids") or [])
        proof_ids = list(transaction.get("proof_envelope_ids") or [])
        proof_lineage = list(transaction.get("proof_lineage") or [])
        if not child_tasks or not verifier_ids or not proof_ids or not proof_lineage:
            return False
        if not all(
            isinstance(item, dict)
            and item.get("admissible_for_runtime_achievement") is True
            and str(item.get("task_id") or "")
            and str(item.get("verifier_id") or "")
            and str(item.get("proof_envelope_id") or "")
            for item in proof_lineage
        ):
            return False
    return True


def _decision_summary(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": decision.get("decision_id"),
        "candidate_set_hash": decision.get("candidate_set_hash"),
        "ordered_frontier_hash": decision.get("ordered_frontier_hash"),
        "candidate_count": decision.get("candidate_count"),
        "selected_capability": decision.get("selected_capability"),
        "selected_target": decision.get("selected_target"),
        "selected_index": decision.get("selected_index"),
        "selected_candidate_id": decision.get("selected_candidate_id"),
        "decision_packet_hash": decision.get("decision_packet_hash"),
        "forced_intervention": decision.get("forced_intervention"),
        "intervention_id": decision.get("intervention_id"),
        "forced_policy_win_credit": decision.get("forced_policy_win_credit"),
    }


def _row_report(row: dict[str, Any], expectations: dict[str, Any]) -> dict[str, Any]:
    scenario = str(row.get("scenario") or "")
    variant = _scenario_to_variant().get(scenario)
    expected_hash = expectations["expected_candidate_set_hash"]
    decisive_match = _decisive_decision(row, expected_hash) if expected_hash else None
    decisive_index, decisive = decisive_match if decisive_match is not None else (None, {})
    packet = decisive.get("decision_packet")
    packet = dict(packet) if isinstance(packet, dict) else {}
    frontier = _packet_frontier(packet)
    frontier_targets = [
        contract.canonicalize_callback_text(item.get("target"))
        for item in frontier
    ]
    forced_path = _forced_path_for_target(decisive.get("selected_target"))
    forced_decisions = [
        decision
        for decision in list(row.get("decisions") or [])
        if isinstance(decision, dict) and decision.get("forced_intervention") is True
    ]
    forced_names = [str(decision.get("selected_capability") or "") for decision in forced_decisions]
    forced_targets = [_target_fields(decision.get("selected_target")) for decision in forced_decisions]
    path_target = (
        (forced_path.first_host.casefold(), forced_path.first_domain.casefold())
        if forced_path is not None
        else ("", "")
    )
    correct_first = (
        variant is not None
        and forced_path is not None
        and path_target == (variant.target_host.casefold(), variant.target_domain.casefold())
    )
    expected_transactions = 3.0 if correct_first else 6.0
    transactions_from_frontier = _transactions_from_frontier(row, decisive_index)
    post_frontier_transactions = _post_frontier_transactions(row, decisive_index)
    row_failures = calibration._row_invariant_failures(row)
    checks = {
        "surface_report_passes": expectations["surface_report_passes"],
        "surface_authorizes_canaries": expectations["surface_authorizes_canaries"],
        "scenario_recognized": variant is not None,
        "row_invariants_pass": not row_failures,
        "request_completed": row.get("request_completed") is True,
        "phase6_manifest_hash_matches": (
            str(row.get("phase6_manifest_hash") or "") == expectations["sealed_manifest_hash"]
        ),
        "phase6_topology_hash_matches": (
            str(row.get("phase6_topology_hash") or "") == expectations["topology_hash"]
        ),
        "phase6_candidate_set_hash_matches": (
            str(row.get("phase6_candidate_set_hash") or "") == expectations["expected_candidate_set_hash"]
        ),
        "phase6_ordered_frontier_hash_matches": (
            str(row.get("phase6_ordered_frontier_hash") or "") == expectations["expected_ordered_frontier_hash"]
        ),
        "decisive_frontier_observed_once": decisive_match is not None,
        "decision_packet_present": bool(packet),
        "packet_hash_recomputes": (
            bool(packet)
            and corpus._packet_hash(packet) == str(decisive.get("decision_packet_hash") or "")
        ),
        "packet_candidate_set_hash_matches_surface": (
            bool(packet) and _canonical_packet_candidate_set_hash(packet) == expectations["expected_candidate_set_hash"]
        ),
        "packet_ordered_frontier_hash_matches_surface": (
            bool(packet) and _canonical_packet_ordered_frontier_hash(packet) == expectations["expected_ordered_frontier_hash"]
        ),
        "candidate_count_matches_surface": (
            len(frontier) == expectations["expected_candidate_count"]
            and int(decisive.get("candidate_count") or 0) == expectations["expected_candidate_count"]
        ),
        "frontier_targets_match_surface": frontier_targets == expectations["expected_targets"],
        "selection_contract_matches": (
            bool(packet) and str(packet.get("selection_contract") or "") == EXPECTED_SELECTION_CONTRACT
        ),
        "first_decision_is_laps_read": (
            str(decisive.get("selected_capability") or "") == contract.CHAIN[0]
            and forced_path is not None
        ),
        "forced_path_metadata_matches": (
            forced_path is not None and str(row.get("phase6_forced_path") or "") == forced_path.name
        ),
        "forced_chain_is_exact_target_label_only": (
            len(forced_decisions) == len(contract.CHAIN)
            and forced_names == list(contract.CHAIN)
            and all(target == path_target for target in forced_targets)
            and all(
                str(decision.get("decision_owner") or "") == "forced_intervention"
                and decision.get("forced_policy_win_credit") is False
                and str(decision.get("intervention_id") or "")
                for decision in forced_decisions
            )
        ),
        "expected_branch_cost_observed": transactions_from_frontier == expected_transactions,
        "mythic_proof_lineage_complete": _proof_lineage_complete(post_frontier_transactions),
    }
    return {
        "scenario": scenario,
        "objective_variant": variant.name if variant is not None else None,
        "forced_path": forced_path.name if forced_path is not None else None,
        "correct_first": correct_first,
        "expected_transactions_from_frontier": expected_transactions,
        "transactions_from_frontier": transactions_from_frontier,
        "ts": row.get("ts"),
        "ts_iso": row.get("ts_iso"),
        "chat_channel_id": row.get("chat_channel_id"),
        "chat_request_id": row.get("chat_request_id"),
        "semantic_transaction_count": row.get("semantic_transaction_count"),
        "effective_backends": row.get("effective_backends"),
        "row_invariant_failures": row_failures,
        "decisive_decision_index": decisive_index,
        "decisive_decision": _decision_summary(decisive) if decisive else None,
        "decisive_frontier": frontier,
        "forced_decisions": [_decision_summary(decision) for decision in forced_decisions],
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def validate_canary_results(
    rows: list[dict[str, Any]],
    surface_report: dict[str, Any],
    *,
    source_path: Path | None = None,
) -> dict[str, Any]:
    expectations = _surface_expectations(surface_report)
    matched_attempt_rows = _matching_rows(rows)
    matched_rows, attempt_reports, attempt_checks, attempt_mode = select_countable_attempt_rows(
        matched_attempt_rows
    )
    row_reports = [_row_report(row, expectations) for row in matched_rows]
    cells = [
        (str(report.get("objective_variant") or ""), str(report.get("forced_path") or ""))
        for report in row_reports
    ]
    expected_cells = {
        (variant.name, forced_path.name)
        for variant in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants
        for forced_path in contract.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths
    }
    checks = {
        "surface_report_passes": expectations["surface_report_passes"],
        "surface_authorizes_canaries": expectations["surface_authorizes_canaries"],
        "attempt_accounting_valid": all(attempt_checks.values()),
        "exact_preregistered_row_count": len(matched_rows) == EXPECTED_CANARY_ROWS,
        "all_preregistered_cells_present_once": set(cells) == expected_cells and len(cells) == len(set(cells)),
        "all_rows_preserve_frontier_and_lineage": bool(row_reports) and all(report["passes_gate"] for report in row_reports),
        "both_branches_prove_both_objectives": all(
            report["checks"]["row_invariants_pass"]
            and report["checks"]["request_completed"]
            for report in row_reports
        ),
        "correct_first_cost_is_three": all(
            report["transactions_from_frontier"] == 3.0
            for report in row_reports
            if report["correct_first"] is True
        ),
        "wrong_first_cost_is_six": all(
            report["transactions_from_frontier"] == 6.0
            for report in row_reports
            if report["correct_first"] is False
        ),
    }
    return {
        "kind": "laps_family_transfer_canary_validation",
        "generated_at": _now(),
        "expected_policy_mode": EXPECTED_POLICY_MODE,
        "expected_rows": EXPECTED_CANARY_ROWS,
        "source_artifact": (
            {
                "relative_path": _relative_path(source_path),
                "sha256": _sha256_file(source_path),
            }
            if source_path is not None
            else None
        ),
        "attempt_accounting": {
            "mode": attempt_mode,
            "matched_attempt_row_count": len(matched_attempt_rows),
            "countable_row_count": len(matched_rows),
            "diagnostic_row_count": len(matched_attempt_rows) - len(matched_rows),
            "checks": attempt_checks,
            "attempt_reports": attempt_reports,
        },
        "surface_expectations": expectations,
        "matching_row_count": len(matched_rows),
        "observed_cells": cells,
        "row_reports": row_reports,
        "checks": checks,
        "passes_gate": all(checks.values()),
        "authorization": {
            "forced_confirmation_matrix_authorized": all(checks.values()),
            "reason": (
                "All four forced LAPS mechanics cells preserved the frozen frontier, exact label-only branch "
                "interventions, Mythic-derived proof lineage, and expected 3-vs-6 transaction cost."
                if all(checks.values())
                else "The LAPS mechanics canaries did not satisfy the forced-confirmation release gate."
            ),
        },
        "_selected_rows": matched_rows,
    }


def pin_canary_rows(rows: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    _write_jsonl(path, rows)
    return {
        "relative_path": _relative_path(path),
        "sha256": _sha256_file(path),
        "rows": len(rows),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "laps-family-transfer-canary-validate",
        help="validate the four forced mechanics canaries for the Phase 6 LAPS holdout",
    )
    parser.add_argument("--results", default=str(DEFAULT_RESULTS_PATH), help="JSONL gauge results artifact")
    parser.add_argument(
        "--surface-report",
        default=str(DEFAULT_SURFACE_REPORT_PATH),
        help="accepted live-surface validation report JSON",
    )
    parser.add_argument("--output", default=None, help="optional JSON validation report path")
    parser.add_argument("--row-output", default=None, help="optional pinned four-row JSONL output path")
    parser.set_defaults(func=_cmd_laps_family_transfer_canary_validate)


def _cmd_laps_family_transfer_canary_validate(args: Any) -> int:
    results_path = Path(args.results)
    try:
        rows = _load_jsonl(results_path)
        surface_report = _load_json(Path(args.surface_report), label="live-surface report")
        report = validate_canary_results(rows, surface_report, source_path=results_path)
    except LapsFamilyTransferCanaryError as exc:
        print(f"laps-family-transfer-canary-validate: {exc}", file=sys.stderr)
        return 2
    selected_rows = report.pop("_selected_rows", [])
    if report["passes_gate"] and args.row_output:
        report["pinned_row_artifact"] = pin_canary_rows(selected_rows, Path(args.row_output))
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True))
    if args.output:
        _write_json(Path(args.output), report)
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(forced_confirmation_matrix_authorized={report['authorization']['forced_confirmation_matrix_authorized']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
