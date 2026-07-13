"""Offline calibration for recorded policy decisions from live benchmark matrices.

This module is intentionally narrower than ``mock_mythic_candidate_eval``. It does not
pretend to score an unseen prompt or re-execute Mythic offline. Instead it replays the
recorded decisive policy frontier from frozen live matrices and asks a smaller question:

    does an offline reading of the recorded decision surface preserve the live ordering?

That is the prerequisite for any later hermetic hill-climb loop. If this layer cannot
preserve known live ties and known live separations, a broader simulator would only hide
the problem behind more machinery.
"""
from __future__ import annotations

from itertools import combinations
import hashlib
import json
from pathlib import Path
from statistics import mean
import sys
from typing import Any

try:  # package import
    from . import null_model_factorial
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import null_model_factorial  # type: ignore


DEFAULT_MANIFEST_PATH = Path(__file__).with_name("policy_replay_calibration_manifest.json")
DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[2] / ".hillclimb" / "results"
POLICY_MODES = ("symbolic", "llm", "hybrid")
FORBIDDEN_GOAD_LITERALS = (
    "north.sevenkingdoms",
    "sevenkingdoms.local",
    "essos.local",
    "samwell.tarly",
    "jorah.mormont",
)
REQUIRED_LIVE_ROW_CHECKS = (
    "objective_proven",
    "clean_stop",
    "policy_identity_valid",
    "backend_provenance_complete",
    "semantic_policy_coverage",
    "policy_switches",
)


class CalibrationError(ValueError):
    """Raised when the frozen evidence package or manifest is malformed."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CalibrationError(f"missing calibration manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CalibrationError(f"invalid JSON in calibration manifest {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CalibrationError(f"calibration manifest {path} must be a JSON object")
    return raw


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CalibrationError(f"missing frozen evidence artifact: {path}") from exc
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CalibrationError(f"invalid JSONL row {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise CalibrationError(f"JSONL row {path}:{line_no} must be an object")
        rows.append(row)
    if not rows:
        raise CalibrationError(f"frozen evidence artifact has no rows: {path}")
    return rows


def _safe_artifact_path(results_root: Path, relative_path: str) -> Path:
    rel = Path(relative_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise CalibrationError(f"artifact path must stay under the results root: {relative_path!r}")
    root = results_root.resolve()
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise CalibrationError(f"artifact path escaped the results root: {relative_path!r}")
    return path


def _normalize_group(group: list[str] | tuple[str, ...]) -> list[str]:
    return sorted(str(item).strip().casefold() for item in group)


def _normalize_order(groups: list[list[str]] | tuple[tuple[str, ...], ...]) -> list[list[str]]:
    return [_normalize_group(group) for group in groups]


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise CalibrationError("calibration manifest schema_version must be 1")
    if not str(manifest.get("name") or "").strip():
        raise CalibrationError("calibration manifest name is required")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise CalibrationError("calibration manifest must declare artifacts")
    artifact_ids: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise CalibrationError("artifact entries must be objects")
        artifact_id = str(artifact.get("id") or "").strip()
        relative_path = str(artifact.get("relative_path") or "").strip()
        sha256 = str(artifact.get("sha256") or "").strip().casefold()
        if not artifact_id or not relative_path or len(sha256) != 64:
            raise CalibrationError("artifact entries require id, relative_path, and 64-character sha256")
        if artifact_id in artifact_ids:
            raise CalibrationError(f"duplicate artifact id: {artifact_id}")
        artifact_ids.add(artifact_id)
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CalibrationError("calibration manifest must declare at least one case")
    case_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise CalibrationError("case entries must be objects")
        case_id = str(case.get("id") or "").strip()
        if not case_id or case_id in case_ids:
            raise CalibrationError(f"invalid or duplicate calibration case id: {case_id!r}")
        case_ids.add(case_id)
        if str(case.get("artifact_id") or "").strip() not in artifact_ids:
            raise CalibrationError(f"{case_id}: artifact_id does not name a declared artifact")
        if not str(case.get("scenario") or "").strip():
            raise CalibrationError(f"{case_id}: scenario is required")
        if not str(case.get("decisive_frontier_hash") or "").startswith("sha256:"):
            raise CalibrationError(f"{case_id}: decisive_frontier_hash must be a sha256:* value")
        if int(case.get("expected_candidate_count") or 0) <= 0:
            raise CalibrationError(f"{case_id}: expected_candidate_count must be positive")
        if int(case.get("expected_rows_per_policy") or 0) <= 0:
            raise CalibrationError(f"{case_id}: expected_rows_per_policy must be positive")
        metric = case.get("metric")
        if not isinstance(metric, dict) or metric.get("kind") not in {
            "semantic_transaction_count",
            "transactions_from_frontier",
        }:
            raise CalibrationError(f"{case_id}: unsupported metric kind")
        if metric.get("direction") not in {"lower_is_better", "higher_is_better"}:
            raise CalibrationError(f"{case_id}: metric direction must be lower_is_better or higher_is_better")
        expected_order = case.get("expected_policy_order")
        if not isinstance(expected_order, list) or not expected_order:
            raise CalibrationError(f"{case_id}: expected_policy_order is required")
        flattened = [item for group in expected_order for item in group]
        if sorted(str(item).casefold() for item in flattened) != sorted(POLICY_MODES):
            raise CalibrationError(f"{case_id}: expected_policy_order must cover symbolic, llm, and hybrid once")


def load_manifest(path: str | Path | None = None) -> dict[str, Any]:
    manifest_path = Path(path) if path is not None else DEFAULT_MANIFEST_PATH
    manifest = _load_json(manifest_path)
    _validate_manifest(manifest)
    return manifest


def _manifest_has_no_goad_literals(manifest: dict[str, Any]) -> bool:
    text = _canonical_json(manifest).casefold()
    return not any(literal in text for literal in FORBIDDEN_GOAD_LITERALS)


def _row_invariant_failures(row: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if row.get("objective_proven") is not True:
        failures.append("objective_proven")
    if row.get("clean_stop") is not True:
        failures.append("clean_stop")
    if row.get("policy_identity_valid") is not True:
        failures.append("policy_identity_valid")
    if row.get("backend_provenance_complete") is not True:
        failures.append("backend_provenance_complete")
    if float(row.get("semantic_policy_coverage", 0.0) or 0.0) != 1.0:
        failures.append("semantic_policy_coverage")
    if row.get("policy_switches") != []:
        failures.append("policy_switches")
    configured = str(row.get("configured_policy_mode") or "").strip().casefold()
    observed = str(row.get("policy_mode") or "").strip().casefold()
    if configured != observed:
        failures.append("policy_mode_mismatch")
    return failures


def _find_decisive_decision(row: dict[str, Any], frontier_hash: str) -> tuple[int, dict[str, Any]] | None:
    decisions = row.get("decisions")
    if not isinstance(decisions, list):
        return None
    matches = [
        (index, decision)
        for index, decision in enumerate(decisions)
        if isinstance(decision, dict) and decision.get("candidate_hash") == frontier_hash
    ]
    return matches[0] if len(matches) == 1 else None


def _transactions_from_frontier(row: dict[str, Any], decision_index: int) -> float:
    decisions = row.get("decisions") or []
    decision_ids = {
        str(decision.get("decision_id") or "")
        for decision in decisions[decision_index:]
        if isinstance(decision, dict) and str(decision.get("decision_id") or "")
    }
    transactions = [
        transaction
        for transaction in (row.get("transactions") or [])
        if isinstance(transaction, dict) and str(transaction.get("decision_id") or "") in decision_ids
    ]
    if transactions:
        return float(len(transactions))
    return float(len(decisions[decision_index:]))


def _metric_value(case: dict[str, Any], row: dict[str, Any], decision_index: int) -> float:
    metric_kind = case["metric"]["kind"]
    if metric_kind == "semantic_transaction_count":
        return float(row.get("semantic_transaction_count", 0.0) or 0.0)
    if metric_kind == "transactions_from_frontier":
        return _transactions_from_frontier(row, decision_index)
    raise CalibrationError(f"{case['id']}: unsupported metric kind {metric_kind!r}")


def _observed_order(
    policy_metrics: dict[str, float],
    *,
    direction: str,
    tolerance: float,
) -> list[list[str]]:
    reverse = direction == "higher_is_better"
    ordered = sorted(policy_metrics.items(), key=lambda item: (item[1], item[0]), reverse=reverse)
    groups: list[list[str]] = []
    anchor: float | None = None
    for policy_mode, value in ordered:
        if anchor is None or abs(value - anchor) > tolerance:
            groups.append([policy_mode])
            anchor = value
        else:
            groups[-1].append(policy_mode)
    return _normalize_order(groups)


def _expected_relation(expected_order: list[list[str]], left: str, right: str) -> str:
    positions = {
        policy_mode: index
        for index, group in enumerate(expected_order)
        for policy_mode in group
    }
    if positions[left] == positions[right]:
        return "tie"
    return "left_better" if positions[left] < positions[right] else "right_better"


def _observed_relation(
    policy_metrics: dict[str, float],
    left: str,
    right: str,
    *,
    direction: str,
    tolerance: float,
) -> str:
    left_value = policy_metrics[left]
    right_value = policy_metrics[right]
    if abs(left_value - right_value) <= tolerance:
        return "tie"
    left_better = left_value < right_value if direction == "lower_is_better" else left_value > right_value
    return "left_better" if left_better else "right_better"


def _pairwise_report(
    expected_order: list[list[str]],
    policy_metrics: dict[str, float],
    *,
    direction: str,
    tolerance: float,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for left, right in combinations(POLICY_MODES, 2):
        expected = _expected_relation(expected_order, left, right)
        observed = _observed_relation(
            policy_metrics,
            left,
            right,
            direction=direction,
            tolerance=tolerance,
        )
        pairs.append({
            "left": left,
            "right": right,
            "expected": expected,
            "observed": observed,
            "passes": expected == observed,
        })
    return pairs


def _summarize_null_model_factorial() -> dict[str, Any]:
    report = null_model_factorial.run_null_model_factorial()
    rows = []
    for row in report.get("rows", []):
        telemetry = row.get("telemetry") or {}
        scorecard = row.get("scorecard") or {}
        rows.append({
            "policy_mode": row.get("policy_mode"),
            "controller_status": row.get("controller_status"),
            "executed_capabilities": list(row.get("executed_capabilities") or []),
            "semantic_transaction_count": telemetry.get("semantic_transaction_count"),
            "objective_proven": scorecard.get("objective_proven"),
            "clean_stop": scorecard.get("clean_stop"),
        })
    return {
        "id": "null-model-factorial",
        "kind": "offline_invariant",
        "verdict": report.get("verdict"),
        "failures": report.get("failures") or {},
        "rows": rows,
    }


def _build_case_report(
    case: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    scenario = str(case["scenario"])
    frontier_hash = str(case["decisive_frontier_hash"])
    expected_candidate_count = int(case["expected_candidate_count"])
    expected_rows_per_policy = int(case["expected_rows_per_policy"])
    choice_fields = [str(item) for item in case.get("choice_fields") or ["selected_capability"]]
    policy_rows = {
        policy_mode: [
            row for row in rows
            if str(row.get("scenario") or "") == scenario
            and str(row.get("configured_policy_mode") or "").strip().casefold() == policy_mode
        ]
        for policy_mode in POLICY_MODES
    }
    policy_summaries: dict[str, Any] = {}
    decisive_frontier_found = True
    candidate_counts: set[int] = set()
    row_failures: list[dict[str, Any]] = []
    for policy_mode, matching_rows in policy_rows.items():
        metric_values: list[float] = []
        choices: list[dict[str, Any]] = []
        for row_index, row in enumerate(matching_rows):
            failures = _row_invariant_failures(row)
            if failures:
                row_failures.append({
                    "policy_mode": policy_mode,
                    "row_index": row_index,
                    "failures": failures,
                })
            match = _find_decisive_decision(row, frontier_hash)
            if match is None:
                decisive_frontier_found = False
                continue
            decision_index, decision = match
            candidate_counts.add(int(decision.get("candidate_count") or 0))
            metric_values.append(_metric_value(case, row, decision_index))
            choices.append({field: decision.get(field) for field in choice_fields})
        policy_summaries[policy_mode] = {
            "rows": len(matching_rows),
            "metric_values": metric_values,
            "metric_mean": mean(metric_values) if metric_values else None,
            "choices": choices,
            "stable_choice": len({_canonical_json(choice) for choice in choices}) <= 1 if choices else False,
        }
    policy_metrics = {
        policy_mode: float(summary["metric_mean"])
        for policy_mode, summary in policy_summaries.items()
        if summary["metric_mean"] is not None
    }
    expected_order = _normalize_order(case["expected_policy_order"])
    tolerance = float(case.get("tie_tolerance", 0.0) or 0.0)
    direction = str(case["metric"]["direction"])
    observed_order = (
        _observed_order(policy_metrics, direction=direction, tolerance=tolerance)
        if len(policy_metrics) == len(POLICY_MODES)
        else []
    )
    pairwise = (
        _pairwise_report(expected_order, policy_metrics, direction=direction, tolerance=tolerance)
        if len(policy_metrics) == len(POLICY_MODES)
        else []
    )
    checks = {
        "rows_per_policy": all(
            summary["rows"] == expected_rows_per_policy
            for summary in policy_summaries.values()
        ),
        "rows_preserve_live_invariants": not row_failures,
        "decisive_frontier_replayed": decisive_frontier_found,
        "decisive_frontier_candidate_count": candidate_counts == {expected_candidate_count},
        "policy_choices_stable": all(summary["stable_choice"] for summary in policy_summaries.values()),
        "expected_policy_order_preserved": observed_order == expected_order,
        "pairwise_agreement": bool(pairwise) and all(item["passes"] for item in pairwise),
    }
    return {
        "id": case["id"],
        "artifact_id": case["artifact_id"],
        "scenario": scenario,
        "decisive_frontier_hash": frontier_hash,
        "metric": dict(case["metric"]),
        "expected_policy_order": expected_order,
        "observed_policy_order": observed_order,
        "policy_summaries": policy_summaries,
        "pairwise": pairwise,
        "row_failures": row_failures,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def _build_core_report(
    manifest: dict[str, Any],
    *,
    results_root: Path,
    verify_hashes: bool,
) -> dict[str, Any]:
    artifact_rows: dict[str, list[dict[str, Any]]] = {}
    artifact_reports: list[dict[str, Any]] = []
    for artifact in manifest["artifacts"]:
        path = _safe_artifact_path(results_root, str(artifact["relative_path"]))
        actual_hash = _hash_file(path)
        expected_hash = str(artifact["sha256"]).casefold()
        hash_matches = actual_hash == expected_hash
        if verify_hashes and not hash_matches:
            raise CalibrationError(
                f"frozen evidence hash mismatch for {artifact['id']}: "
                f"expected {expected_hash}, observed {actual_hash}"
            )
        rows = _load_jsonl(path)
        artifact_rows[str(artifact["id"])] = rows
        artifact_reports.append({
            "id": artifact["id"],
            "relative_path": artifact["relative_path"],
            "sha256": f"sha256:{actual_hash}",
            "expected_sha256": f"sha256:{expected_hash}",
            "hash_matches": hash_matches,
            "rows": len(rows),
            "scenarios": sorted({str(row.get("scenario") or "") for row in rows}),
        })
    cases = [
        _build_case_report(case, artifact_rows[str(case["artifact_id"])])
        for case in manifest["cases"]
    ]
    invariants = [_summarize_null_model_factorial()]
    checks = {
        "artifact_hashes_match": all(item["hash_matches"] for item in artifact_reports),
        "manifest_has_no_goad_literals": _manifest_has_no_goad_literals(manifest),
        "cases_pass": all(case["passes_gate"] for case in cases),
        "offline_invariants_pass": all(item["verdict"] == "PASS" for item in invariants),
    }
    return {
        "kind": "policy_replay_calibration",
        "manifest_name": manifest["name"],
        "manifest_hash": f"sha256:{_hash_json(manifest)}",
        "artifacts": artifact_reports,
        "cases": cases,
        "invariants": invariants,
        "excluded_surfaces": list(manifest.get("excluded_surfaces") or []),
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def run_calibration(
    *,
    manifest_path: str | Path | None = None,
    results_root: str | Path | None = None,
    verify_hashes: bool = True,
    repeats: int = 2,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    root = Path(results_root) if results_root is not None else DEFAULT_RESULTS_ROOT
    repeat_count = max(1, int(repeats))
    reports = [
        _build_core_report(manifest, results_root=root, verify_hashes=verify_hashes)
        for _ in range(repeat_count)
    ]
    report_hashes = [f"sha256:{_hash_json(report)}" for report in reports]
    report = reports[0]
    report["determinism"] = {
        "repeats": repeat_count,
        "report_hashes": report_hashes,
        "deterministic": len(set(report_hashes)) == 1,
    }
    report["checks"]["deterministic_replay"] = report["determinism"]["deterministic"]
    report["passes_gate"] = all(report["checks"].values())
    return report


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "policy-replay-calibrate",
        help="calibrate offline recorded-decision replay against frozen live policy matrices",
    )
    parser.add_argument("--manifest", default=None, help="optional calibration manifest JSON path")
    parser.add_argument("--results-root", default=None, help="optional root containing frozen JSONL artifacts")
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.add_argument("--repeats", type=int, default=2, help="repeat offline replay to prove determinism")
    parser.add_argument(
        "--skip-hash-check",
        action="store_true",
        help="allow evidence file drift while still reporting the observed hashes",
    )
    parser.set_defaults(func=_cmd_policy_replay_calibrate)


def _cmd_policy_replay_calibrate(args: Any) -> int:
    try:
        report = run_calibration(
            manifest_path=args.manifest,
            results_root=args.results_root,
            verify_hashes=not args.skip_hash_check,
            repeats=args.repeats,
        )
    except CalibrationError as exc:
        print(f"policy-replay-calibrate: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(manifest={report['manifest_name']}, deterministic={report['determinism']['deterministic']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
