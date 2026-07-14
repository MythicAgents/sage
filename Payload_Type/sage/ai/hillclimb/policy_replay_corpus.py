"""Packet-backed frontier corpus export and validation for offline policy replay.

This layer stays intentionally narrow. It freezes decisive live decision packets, rebuilds
their admissible frontiers without touching the lab, and verifies that the packet-backed
surface still preserves the known live policy ordering. It does not claim to score an
unseen branch whose cost was never observed live.
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
import sys
from types import SimpleNamespace
from typing import Any, Callable

try:  # package import
    from . import policy_replay_calibration as calibration
    from ..langgraph import policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import policy_replay_calibration as calibration  # type: ignore
    from ai.langgraph import policy  # type: ignore


DEFAULT_SOURCE_MANIFEST_PATH = Path(__file__).with_name("policy_replay_corpus_sources.json")
DEFAULT_CORPUS_PATH = Path(__file__).with_name("policy_replay_frontier_corpus.json")
POLICY_MODES = calibration.POLICY_MODES


class CorpusError(ValueError):
    """Raised when packet-backed corpus inputs are malformed or drifted."""


def _canonical_json(value: Any) -> str:
    return calibration._canonical_json(value)


def _hash_json(value: Any) -> str:
    return f"sha256:{calibration._hash_json(value)}"


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CorpusError(f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CorpusError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CorpusError(f"{label} {path} must be a JSON object")
    return value


def _validate_source_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise CorpusError("corpus source manifest schema_version must be 1")
    if not str(manifest.get("name") or "").strip():
        raise CorpusError("corpus source manifest name is required")
    if not str(manifest.get("calibration_manifest") or "").strip():
        raise CorpusError("corpus source manifest must name a calibration manifest")
    sources = manifest.get("packet_sources")
    if not isinstance(sources, list) or not sources:
        raise CorpusError("corpus source manifest must declare packet_sources")
    case_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise CorpusError("packet source entries must be objects")
        case_id = str(source.get("case_id") or "").strip()
        relative_path = str(source.get("relative_path") or "").strip()
        sha256 = str(source.get("sha256") or "").strip().casefold()
        packet_hash = str(source.get("decision_packet_hash") or "").strip()
        if not case_id or not relative_path or len(sha256) != 64:
            raise CorpusError("packet source entries require case_id, relative_path, and 64-character sha256")
        if not packet_hash.startswith("sha256:"):
            raise CorpusError(f"{case_id}: decision_packet_hash must be a sha256:* value")
        if case_id in case_ids:
            raise CorpusError(f"duplicate packet source case_id: {case_id}")
        case_ids.add(case_id)


def load_source_manifest(path: str | Path | None = None) -> dict[str, Any]:
    manifest_path = Path(path) if path is not None else DEFAULT_SOURCE_MANIFEST_PATH
    manifest = _load_json(manifest_path, label="corpus source manifest")
    _validate_source_manifest(manifest)
    return manifest


def _calibration_manifest_path(source_manifest_path: Path, source_manifest: dict[str, Any]) -> Path:
    raw = Path(str(source_manifest["calibration_manifest"]))
    if raw.is_absolute() or ".." in raw.parts:
        raise CorpusError("calibration_manifest must stay beside the source manifest")
    return (source_manifest_path.parent / raw).resolve()


def _packet_hash(packet: dict[str, Any]) -> str:
    return _hash_json(packet)


def _packet_frontier(packet: dict[str, Any]) -> list[dict[str, Any]]:
    frontier = packet.get("admissible_frontier")
    if not isinstance(frontier, list) or not frontier:
        raise CorpusError("decision packet must contain a non-empty admissible_frontier")
    if not all(isinstance(item, dict) for item in frontier):
        raise CorpusError("decision packet admissible_frontier entries must be objects")
    return [dict(item) for item in frontier]


def _reconstructed_candidate_hash(packet: dict[str, Any]) -> str:
    candidates = [
        SimpleNamespace(
            name=str(item.get("name") or ""),
            target=str(item.get("target") or ""),
            preconditions=list(item.get("preconditions") or []),
            effects=list(item.get("effects") or []),
            operational_cost=dict(item.get("operational_cost") or {}),
            reason=str(item.get("reason") or ""),
        )
        for item in _packet_frontier(packet)
    ]
    return policy.candidate_hash(candidates)


def _case_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(case["id"]): case for case in manifest["cases"]}


def _source_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(source["case_id"]): source for source in manifest["packet_sources"]}


def _artifact_rows_by_id(
    manifest: dict[str, Any],
    *,
    results_root: Path,
) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for artifact in manifest["artifacts"]:
        path = calibration._safe_artifact_path(results_root, str(artifact["relative_path"]))
        rows[str(artifact["id"])] = calibration._load_jsonl(path)
    return rows


def _stable_policy_replays(
    case: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    replays: dict[str, dict[str, Any]] = {}
    for policy_mode in POLICY_MODES:
        matching_rows = [
            row
            for row in rows
            if str(row.get("scenario") or "") == str(case["scenario"])
            and str(row.get("configured_policy_mode") or "").strip().casefold() == policy_mode
        ]
        choices: list[dict[str, Any]] = []
        metric_values: list[float] = []
        for row in matching_rows:
            match = calibration._find_decisive_decision(row, str(case["decisive_frontier_hash"]))
            if match is None:
                raise CorpusError(f"{case['id']}: missing decisive frontier for {policy_mode}")
            decision_index, decision = match
            choices.append({
                "selected_capability": str(decision.get("selected_capability") or ""),
                "selected_target": str(decision.get("selected_target") or ""),
            })
            metric_values.append(calibration._metric_value(case, row, decision_index))
        unique_choices = {_canonical_json(choice) for choice in choices}
        if len(unique_choices) != 1 or not choices:
            raise CorpusError(f"{case['id']}: {policy_mode} does not have one stable decisive choice")
        replays[policy_mode] = {
            **choices[0],
            "metric_values": metric_values,
            "metric_mean": mean(metric_values),
        }
    return replays


def _resolve_choice(frontier: list[dict[str, Any]], choice: dict[str, Any]) -> int | None:
    matches = [
        index
        for index, item in enumerate(frontier)
        if str(item.get("name") or "") == str(choice.get("selected_capability") or "")
        and str(item.get("target") or "") == str(choice.get("selected_target") or "")
    ]
    return matches[0] if len(matches) == 1 else None


def _branch_outcome_scope(
    frontier: list[dict[str, Any]],
    policy_replays: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    coverage: list[dict[str, Any]] = []
    observed_indices: list[int] = []
    for index, item in enumerate(frontier):
        observed_by = sorted(
            policy_mode
            for policy_mode in POLICY_MODES
            if _resolve_choice(frontier, policy_replays.get(policy_mode) or {}) == index
        )
        if observed_by:
            observed_indices.append(index)
        coverage.append({
            "frontier_index": index,
            "selected_capability": item.get("name"),
            "selected_target": item.get("target"),
            "live_observed": bool(observed_by),
            "observed_by_policy_modes": observed_by,
        })
    unobserved_indices = [
        index
        for index in range(len(frontier))
        if index not in observed_indices
    ]
    return {
        "kind": "live_observed_frontier_choices_only",
        "can_score_unseen_candidates": False,
        "frontier_indices_with_live_observed_outcomes": observed_indices,
        "frontier_indices_without_live_observed_outcomes": unobserved_indices,
        "frontier_coverage": coverage,
    }


def _packet_source_row(
    source: dict[str, Any],
    case: dict[str, Any],
    *,
    results_root: Path,
    verify_hashes: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = calibration._safe_artifact_path(results_root, str(source["relative_path"]))
    actual_hash = calibration._hash_file(path)
    expected_hash = str(source["sha256"]).casefold()
    if verify_hashes and actual_hash != expected_hash:
        raise CorpusError(
            f"packet source hash mismatch for {source['case_id']}: "
            f"expected {expected_hash}, observed {actual_hash}"
        )
    rows = calibration._load_jsonl(path)
    matching_rows = [
        row
        for row in rows
        if str(row.get("scenario") or "") == str(case["scenario"])
        and str(row.get("configured_policy_mode") or "").strip().casefold()
        == str(source.get("policy_mode") or "").strip().casefold()
    ]
    if len(matching_rows) != 1:
        raise CorpusError(f"{case['id']}: packet source must contain exactly one matching row")
    row = matching_rows[0]
    failures = calibration._row_invariant_failures(row)
    if failures:
        raise CorpusError(f"{case['id']}: packet source row failed invariants: {failures}")
    match = calibration._find_decisive_decision(row, str(case["decisive_frontier_hash"]))
    if match is None:
        raise CorpusError(f"{case['id']}: packet source row omitted the decisive frontier")
    _decision_index, decision = match
    packet = decision.get("decision_packet")
    if not isinstance(packet, dict) or not packet:
        raise CorpusError(f"{case['id']}: decisive decision omitted decision_packet")
    packet_hash = str(decision.get("decision_packet_hash") or "")
    if packet_hash != str(source["decision_packet_hash"]):
        raise CorpusError(f"{case['id']}: decisive packet hash does not match the source manifest")
    if _packet_hash(packet) != packet_hash:
        raise CorpusError(f"{case['id']}: decisive packet hash does not recompute from persisted payload")
    if str(packet.get("candidate_hash") or "") != str(case["decisive_frontier_hash"]):
        raise CorpusError(f"{case['id']}: packet candidate_hash does not match the calibration case")
    if _reconstructed_candidate_hash(packet) != str(case["decisive_frontier_hash"]):
        raise CorpusError(f"{case['id']}: reconstructed frontier hash drifted from the calibration case")
    if len(_packet_frontier(packet)) != int(case["expected_candidate_count"]):
        raise CorpusError(f"{case['id']}: packet candidate count does not match the calibration case")
    metadata = {
        "relative_path": str(source["relative_path"]),
        "sha256": f"sha256:{actual_hash}",
        "policy_mode": str(source.get("policy_mode") or ""),
        "decision_packet_hash": packet_hash,
    }
    return metadata, packet


def export_corpus(
    *,
    source_manifest_path: str | Path | None = None,
    results_root: str | Path | None = None,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    source_path = Path(source_manifest_path) if source_manifest_path is not None else DEFAULT_SOURCE_MANIFEST_PATH
    source_manifest = load_source_manifest(source_path)
    calibration_path = _calibration_manifest_path(source_path, source_manifest)
    calibration_manifest = calibration.load_manifest(calibration_path)
    root = Path(results_root) if results_root is not None else calibration.DEFAULT_RESULTS_ROOT
    calibration_report = calibration.run_calibration(
        manifest_path=calibration_path,
        results_root=root,
        verify_hashes=verify_hashes,
        repeats=1,
    )
    if not calibration_report["passes_gate"]:
        raise CorpusError("calibration gate must pass before exporting the packet corpus")
    cases = _case_map(calibration_manifest)
    artifact_rows = _artifact_rows_by_id(calibration_manifest, results_root=root)
    exported_cases: list[dict[str, Any]] = []
    for source in source_manifest["packet_sources"]:
        case_id = str(source["case_id"])
        case = cases.get(case_id)
        if case is None:
            raise CorpusError(f"packet source references unknown calibration case: {case_id}")
        source_meta, packet = _packet_source_row(source, case, results_root=root, verify_hashes=verify_hashes)
        policy_replays = _stable_policy_replays(case, artifact_rows[str(case["artifact_id"])])
        frontier = _packet_frontier(packet)
        exported_cases.append({
            "id": case_id,
            "scenario": str(case["scenario"]),
            "decisive_frontier_hash": str(case["decisive_frontier_hash"]),
            "expected_candidate_count": int(case["expected_candidate_count"]),
            "metric": dict(case["metric"]),
            "expected_policy_order": calibration._normalize_order(case["expected_policy_order"]),
            "tie_tolerance": float(case.get("tie_tolerance", 0.0) or 0.0),
            "packet_source": source_meta,
            "decision_packet_hash": source_meta["decision_packet_hash"],
            "decision_packet": packet,
            "policy_replays": policy_replays,
            "branch_outcome_scope": _branch_outcome_scope(frontier, policy_replays),
        })
    expected_case_ids = set(cases)
    exported_case_ids = {str(case["id"]) for case in exported_cases}
    if exported_case_ids != expected_case_ids:
        raise CorpusError(
            f"packet sources must cover every calibration case exactly once: "
            f"expected {sorted(expected_case_ids)}, observed {sorted(exported_case_ids)}"
        )
    return {
        "schema_version": 1,
        "name": str(source_manifest["name"]),
        "purpose": str(source_manifest.get("purpose") or ""),
        "calibration_manifest": str(source_manifest["calibration_manifest"]),
        "calibration_manifest_hash": _hash_json(calibration_manifest),
        "source_manifest_hash": _hash_json(source_manifest),
        "outcome_scope": {
            "kind": "live_observed_frontier_choices_only",
            "can_score_unseen_candidates": False,
        },
        "cases": exported_cases,
    }


def _select_first_admissible(frontier: list[dict[str, Any]]) -> int:
    return 0


def _select_lowest_visible_wait(frontier: list[dict[str, Any]]) -> int:
    def wait_seconds(index: int) -> tuple[int, int]:
        raw = (frontier[index].get("operational_cost") or {}).get("expected_wait_seconds", 0)
        try:
            wait = int(raw or 0)
        except (TypeError, ValueError):
            wait = 0
        return wait, index

    return min(range(len(frontier)), key=wait_seconds)


SELECTORS: dict[str, Callable[[list[dict[str, Any]]], int]] = {
    "first_admissible": _select_first_admissible,
    "lowest_visible_wait": _select_lowest_visible_wait,
}


def _validate_case(
    stored_case: dict[str, Any],
    calibration_case: dict[str, Any],
    expected_policy_replays: dict[str, dict[str, Any]],
    expected_source_meta: dict[str, Any],
    expected_packet: dict[str, Any],
) -> dict[str, Any]:
    packet = stored_case.get("decision_packet")
    packet = dict(packet) if isinstance(packet, dict) else {}
    frontier = _packet_frontier(packet) if packet else []
    stored_replays = stored_case.get("policy_replays")
    stored_replays = dict(stored_replays) if isinstance(stored_replays, dict) else {}
    policy_metrics = {
        policy_mode: float((stored_replays.get(policy_mode) or {}).get("metric_mean"))
        for policy_mode in POLICY_MODES
        if isinstance(stored_replays.get(policy_mode), dict)
        and (stored_replays.get(policy_mode) or {}).get("metric_mean") is not None
    }
    expected_order = calibration._normalize_order(calibration_case["expected_policy_order"])
    tolerance = float(calibration_case.get("tie_tolerance", 0.0) or 0.0)
    direction = str(calibration_case["metric"]["direction"])
    observed_order = (
        calibration._observed_order(policy_metrics, direction=direction, tolerance=tolerance)
        if len(policy_metrics) == len(POLICY_MODES)
        else []
    )
    pairwise = (
        calibration._pairwise_report(expected_order, policy_metrics, direction=direction, tolerance=tolerance)
        if len(policy_metrics) == len(POLICY_MODES)
        else []
    )
    replay_indices = {
        policy_mode: _resolve_choice(frontier, stored_replays.get(policy_mode) or {})
        for policy_mode in POLICY_MODES
    }
    expected_branch_outcome_scope = _branch_outcome_scope(frontier, expected_policy_replays)
    observed_metrics_by_index: dict[int, float] = {}
    for policy_mode, index in replay_indices.items():
        if index is None or policy_mode not in policy_metrics:
            continue
        value = policy_metrics[policy_mode]
        prior = observed_metrics_by_index.get(index)
        if prior is not None and abs(prior - value) > tolerance:
            raise CorpusError(
                f"{stored_case.get('id')}: live policies disagree on the metric for frontier index {index}"
            )
        observed_metrics_by_index[index] = value
    selector_scores: list[dict[str, Any]] = []
    for selector_name, selector in SELECTORS.items():
        index = selector(frontier) if frontier else None
        selected = frontier[index] if isinstance(index, int) and 0 <= index < len(frontier) else {}
        selector_scores.append({
            "selector": selector_name,
            "selected_index": index,
            "selected_capability": selected.get("name"),
            "selected_target": selected.get("target"),
            "observed_metric": observed_metrics_by_index.get(index) if isinstance(index, int) else None,
            "scored_from_live_observation": isinstance(index, int) and index in observed_metrics_by_index,
        })
    checks = {
        "packet_hash_recomputes": bool(packet) and _packet_hash(packet) == str(stored_case.get("decision_packet_hash") or ""),
        "packet_source_matches_live_artifact": _canonical_json(stored_case.get("packet_source") or {})
        == _canonical_json(expected_source_meta),
        "decision_packet_matches_live_artifact": _canonical_json(packet) == _canonical_json(expected_packet),
        "packet_candidate_hash_matches_case": str(packet.get("candidate_hash") or "") == str(calibration_case["decisive_frontier_hash"]),
        "reconstructed_frontier_hash_matches_case": bool(packet)
        and _reconstructed_candidate_hash(packet) == str(calibration_case["decisive_frontier_hash"]),
        "candidate_count_matches_case": len(frontier) == int(calibration_case["expected_candidate_count"]),
        "policy_replays_match_frozen_matrix": _canonical_json(stored_replays) == _canonical_json(expected_policy_replays),
        "branch_outcome_scope_matches_live_observation": _canonical_json(stored_case.get("branch_outcome_scope") or {})
        == _canonical_json(expected_branch_outcome_scope),
        "policy_replays_resolve_on_frontier": all(index is not None for index in replay_indices.values()),
        "expected_policy_order_preserved": observed_order == expected_order,
        "pairwise_agreement": bool(pairwise) and all(item["passes"] for item in pairwise),
        "selector_baselines_scored": all(item["scored_from_live_observation"] for item in selector_scores),
    }
    return {
        "id": stored_case.get("id"),
        "scenario": stored_case.get("scenario"),
        "decisive_frontier_hash": stored_case.get("decisive_frontier_hash"),
        "decision_packet_hash": stored_case.get("decision_packet_hash"),
        "frontier": frontier,
        "policy_replays": stored_replays,
        "branch_outcome_scope": stored_case.get("branch_outcome_scope"),
        "replay_indices": replay_indices,
        "expected_policy_order": expected_order,
        "observed_policy_order": observed_order,
        "pairwise": pairwise,
        "selector_scores": selector_scores,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def validate_corpus(
    *,
    corpus_path: str | Path | None = None,
    source_manifest_path: str | Path | None = None,
    results_root: str | Path | None = None,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    source_path = Path(source_manifest_path) if source_manifest_path is not None else DEFAULT_SOURCE_MANIFEST_PATH
    source_manifest = load_source_manifest(source_path)
    calibration_path = _calibration_manifest_path(source_path, source_manifest)
    calibration_manifest = calibration.load_manifest(calibration_path)
    root = Path(results_root) if results_root is not None else calibration.DEFAULT_RESULTS_ROOT
    corpus_file = Path(corpus_path) if corpus_path is not None else DEFAULT_CORPUS_PATH
    corpus = _load_json(corpus_file, label="policy replay frontier corpus")
    if corpus.get("schema_version") != 1:
        raise CorpusError("policy replay frontier corpus schema_version must be 1")
    calibration_report = calibration.run_calibration(
        manifest_path=calibration_path,
        results_root=root,
        verify_hashes=verify_hashes,
        repeats=1,
    )
    if not calibration_report["passes_gate"]:
        raise CorpusError("calibration gate must pass before validating the packet corpus")
    artifact_rows = _artifact_rows_by_id(calibration_manifest, results_root=root)
    calibration_cases = _case_map(calibration_manifest)
    packet_sources = _source_map(source_manifest)
    stored_cases = corpus.get("cases")
    if not isinstance(stored_cases, list):
        raise CorpusError("policy replay frontier corpus must declare cases")
    case_reports: list[dict[str, Any]] = []
    for stored_case in stored_cases:
        if not isinstance(stored_case, dict):
            raise CorpusError("policy replay frontier corpus cases must be objects")
        case_id = str(stored_case.get("id") or "")
        calibration_case = calibration_cases.get(case_id)
        if calibration_case is None:
            raise CorpusError(f"policy replay frontier corpus references unknown case: {case_id}")
        packet_source = packet_sources.get(case_id)
        if packet_source is None:
            raise CorpusError(f"policy replay frontier corpus case has no packet source: {case_id}")
        expected_source_meta, expected_packet = _packet_source_row(
            packet_source,
            calibration_case,
            results_root=root,
            verify_hashes=verify_hashes,
        )
        expected_replays = _stable_policy_replays(
            calibration_case,
            artifact_rows[str(calibration_case["artifact_id"])],
        )
        case_reports.append(_validate_case(
            stored_case,
            calibration_case,
            expected_replays,
            expected_source_meta,
            expected_packet,
        ))
    checks = {
        "calibration_gate_passes": calibration_report["passes_gate"],
        "calibration_manifest_hash_matches": str(corpus.get("calibration_manifest_hash") or "")
        == _hash_json(calibration_manifest),
        "source_manifest_hash_matches": str(corpus.get("source_manifest_hash") or "") == _hash_json(source_manifest),
        "outcome_scope_is_live_observed_only": _canonical_json(corpus.get("outcome_scope") or {})
        == _canonical_json({
            "kind": "live_observed_frontier_choices_only",
            "can_score_unseen_candidates": False,
        }),
        "cases_cover_calibration_manifest": {str(case.get("id") or "") for case in stored_cases}
        == set(calibration_cases),
        "cases_pass": bool(case_reports) and all(case["passes_gate"] for case in case_reports),
    }
    return {
        "kind": "policy_replay_frontier_corpus",
        "corpus_name": corpus.get("name"),
        "corpus_hash": _hash_json(corpus),
        "calibration_manifest_hash": corpus.get("calibration_manifest_hash"),
        "source_manifest_hash": corpus.get("source_manifest_hash"),
        "cases": case_reports,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    export_parser = subparsers.add_parser(
        "policy-replay-corpus-export",
        help="export packet-backed decisive frontiers from clean live canary rows",
    )
    export_parser.add_argument("--source-manifest", default=None, help="optional packet source manifest JSON path")
    export_parser.add_argument("--results-root", default=None, help="optional root containing frozen JSONL artifacts")
    export_parser.add_argument("--output", default=str(DEFAULT_CORPUS_PATH), help="output corpus JSON path")
    export_parser.add_argument("--skip-hash-check", action="store_true", help="allow source artifact drift")
    export_parser.set_defaults(func=_cmd_policy_replay_corpus_export)

    validate_parser = subparsers.add_parser(
        "policy-replay-corpus-validate",
        help="validate the packet-backed frontier corpus against frozen live evidence",
    )
    validate_parser.add_argument("--corpus", default=None, help="optional packet corpus JSON path")
    validate_parser.add_argument("--source-manifest", default=None, help="optional packet source manifest JSON path")
    validate_parser.add_argument("--results-root", default=None, help="optional root containing frozen JSONL artifacts")
    validate_parser.add_argument("--output", default=None, help="optional JSON report path")
    validate_parser.add_argument("--skip-hash-check", action="store_true", help="allow source artifact drift")
    validate_parser.set_defaults(func=_cmd_policy_replay_corpus_validate)


def _cmd_policy_replay_corpus_export(args: Any) -> int:
    try:
        corpus = export_corpus(
            source_manifest_path=args.source_manifest,
            results_root=args.results_root,
            verify_hashes=not args.skip_hash_check,
        )
    except (calibration.CalibrationError, CorpusError) as exc:
        print(f"policy-replay-corpus-export: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=True)
    Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    print(
        f"\nEXPORTED: {args.output}  (cases={len(corpus['cases'])}, corpus_hash={_hash_json(corpus)})",
        flush=True,
    )
    return 0


def _cmd_policy_replay_corpus_validate(args: Any) -> int:
    try:
        report = validate_corpus(
            corpus_path=args.corpus,
            source_manifest_path=args.source_manifest,
            results_root=args.results_root,
            verify_hashes=not args.skip_hash_check,
        )
    except (calibration.CalibrationError, CorpusError) as exc:
        print(f"policy-replay-corpus-validate: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(corpus={report['corpus_name']}, cases={len(report['cases'])})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
