"""Bounded offline selector experiment over the packet-backed replay corpus.

The packet corpus is still a calibration surface, not a simulator. This module
therefore does one narrow thing: after the existing corpus validator proves the
packet/source/frontier integrity contract, it applies generic selector functions
to each stored decision packet and grades only choices with live-observed costs.

The experimental selector does not inspect capability names, targets, domains,
case identifiers, or benchmark labels. It ranks the admissible frontier by:

1. exact overlap between candidate effects and recent blocked effects;
2. visible expected wait cost;
3. original frontier order as the deterministic tie-break.

That is enough to test whether packet-local outcome memory adds anything beyond
the existing visible-cost baseline without pretending to predict unseen branches.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Callable

try:  # package import
    from . import policy_replay_calibration as calibration
    from . import policy_replay_corpus as corpus
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import policy_replay_calibration as calibration  # type: ignore
    import policy_replay_corpus as corpus  # type: ignore


DEFAULT_CORPUS_PATH = corpus.DEFAULT_CORPUS_PATH
SELECTOR_NAME = "blocked_effect_aware_visible_cost"
SUPPORTED_SELECTION_CONTRACT = "admissible_frontier"
POLICY_MODES = calibration.POLICY_MODES
LEARNED_POLICY_MODES = ("llm", "hybrid")
FEATURES_USED = (
    "selection_contract",
    "normalized_state.recent_outcomes[].status",
    "normalized_state.recent_outcomes[].effect",
    "admissible_frontier[].effects",
    "admissible_frontier[].operational_cost.expected_wait_seconds",
    "admissible_frontier order",
)


class SelectorExperimentError(ValueError):
    """Raised when the selector experiment inputs are invalid or unsupported."""


def _load_corpus(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SelectorExperimentError(f"missing packet corpus: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SelectorExperimentError(f"invalid JSON in packet corpus {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SelectorExperimentError(f"packet corpus {path} must be a JSON object")
    return value


def _selection_contract(packet: dict[str, Any]) -> str:
    return str(packet.get("selection_contract") or "").strip()


def _frontier(packet: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return corpus._packet_frontier(packet)
    except corpus.CorpusError as exc:
        raise SelectorExperimentError(str(exc)) from exc


def _wait_seconds(candidate: dict[str, Any]) -> int:
    raw = (candidate.get("operational_cost") or {}).get("expected_wait_seconds", 0)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _candidate_effects(candidate: dict[str, Any]) -> set[str]:
    effects = candidate.get("effects")
    if not isinstance(effects, list):
        return set()
    return {str(effect) for effect in effects if str(effect)}


def _recent_blocked_effects(packet: dict[str, Any]) -> set[str]:
    state = packet.get("normalized_state")
    state = state if isinstance(state, dict) else {}
    outcomes = state.get("recent_outcomes")
    outcomes = outcomes if isinstance(outcomes, list) else []
    blocked: set[str] = set()
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        if str(outcome.get("status") or "").strip().casefold() != "blocked":
            continue
        effect = str(outcome.get("effect") or "")
        if effect:
            blocked.add(effect)
    return blocked


def _require_supported_contract(packet: dict[str, Any]) -> None:
    contract = _selection_contract(packet)
    if contract != SUPPORTED_SELECTION_CONTRACT:
        raise SelectorExperimentError(
            f"unsupported selection contract for selector experiment: {contract!r}"
        )


def _select_first_admissible(packet: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    _require_supported_contract(packet)
    frontier = _frontier(packet)
    return 0, [{"frontier_index": index} for index in range(len(frontier))]


def _select_lowest_visible_wait(packet: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    _require_supported_contract(packet)
    frontier = _frontier(packet)
    ranking = [
        {
            "frontier_index": index,
            "expected_wait_seconds": _wait_seconds(candidate),
        }
        for index, candidate in enumerate(frontier)
    ]
    index = min(range(len(frontier)), key=lambda item: (_wait_seconds(frontier[item]), item))
    return index, ranking


def select_blocked_effect_aware_visible_cost(
    packet: dict[str, Any],
) -> tuple[int, list[dict[str, Any]]]:
    """Select one admissible candidate using only generic packet-local evidence."""

    _require_supported_contract(packet)
    frontier = _frontier(packet)
    blocked_effects = _recent_blocked_effects(packet)
    ranking: list[dict[str, Any]] = []
    for index, candidate in enumerate(frontier):
        overlap = sorted(_candidate_effects(candidate) & blocked_effects)
        ranking.append({
            "frontier_index": index,
            "blocked_effect_overlap_count": len(overlap),
            "blocked_effect_overlap": overlap,
            "expected_wait_seconds": _wait_seconds(candidate),
        })
    index = min(
        range(len(frontier)),
        key=lambda item: (
            ranking[item]["blocked_effect_overlap_count"],
            ranking[item]["expected_wait_seconds"],
            item,
        ),
    )
    return index, ranking


SELECTORS: dict[str, Callable[[dict[str, Any]], tuple[int, list[dict[str, Any]]]]] = {
    "first_admissible": _select_first_admissible,
    "lowest_visible_wait": _select_lowest_visible_wait,
    SELECTOR_NAME: select_blocked_effect_aware_visible_cost,
}


def _resolve_choice(frontier: list[dict[str, Any]], choice: dict[str, Any]) -> int | None:
    matches = [
        index
        for index, candidate in enumerate(frontier)
        if str(candidate.get("name") or "") == str(choice.get("selected_capability") or "")
        and str(candidate.get("target") or "") == str(choice.get("selected_target") or "")
    ]
    return matches[0] if len(matches) == 1 else None


def _observed_metrics_by_index(
    stored_case: dict[str, Any],
    frontier: list[dict[str, Any]],
) -> tuple[dict[int, float], dict[str, int | None]]:
    replays = stored_case.get("policy_replays")
    replays = replays if isinstance(replays, dict) else {}
    tolerance = float(stored_case.get("tie_tolerance", 0.0) or 0.0)
    metrics: dict[int, float] = {}
    replay_indices: dict[str, int | None] = {}
    for policy_mode in POLICY_MODES:
        replay = replays.get(policy_mode)
        replay = replay if isinstance(replay, dict) else {}
        index = _resolve_choice(frontier, replay)
        replay_indices[policy_mode] = index
        if index is None or replay.get("metric_mean") is None:
            continue
        value = float(replay["metric_mean"])
        prior = metrics.get(index)
        if prior is not None and abs(prior - value) > tolerance:
            raise SelectorExperimentError(
                f"{stored_case.get('id')}: live policies disagree on frontier index {index}"
            )
        metrics[index] = value
    return metrics, replay_indices


def _selector_score(
    *,
    selector_name: str,
    selector: Callable[[dict[str, Any]], tuple[int, list[dict[str, Any]]]],
    packet: dict[str, Any],
    frontier: list[dict[str, Any]],
    observed_metrics_by_index: dict[int, float],
    replay_indices: dict[str, int | None],
) -> dict[str, Any]:
    index, ranking = selector(packet)
    selected = frontier[index] if 0 <= index < len(frontier) else {}
    observed_metric = observed_metrics_by_index.get(index)
    return {
        "selector": selector_name,
        "selected_index": index,
        "selected_capability": selected.get("name"),
        "selected_target": selected.get("target"),
        "observed_metric": observed_metric,
        "scored_from_live_observation": observed_metric is not None,
        "matches_live_symbolic_choice": index == replay_indices.get("symbolic"),
        "matches_live_learned_choices": all(index == replay_indices.get(mode) for mode in LEARNED_POLICY_MODES),
        "ranking": ranking,
    }


def _metric_relation(left: float | None, right: float | None, *, direction: str) -> str:
    if left is None or right is None:
        return "unscored"
    if left == right:
        return "tie"
    left_better = left < right if direction == "lower_is_better" else left > right
    return "better" if left_better else "worse"


def _case_report(stored_case: dict[str, Any]) -> dict[str, Any]:
    packet = stored_case.get("decision_packet")
    packet = packet if isinstance(packet, dict) else {}
    frontier = _frontier(packet)
    observed_metrics_by_index, replay_indices = _observed_metrics_by_index(stored_case, frontier)
    selector_scores = [
        _selector_score(
            selector_name=selector_name,
            selector=selector,
            packet=packet,
            frontier=frontier,
            observed_metrics_by_index=observed_metrics_by_index,
            replay_indices=replay_indices,
        )
        for selector_name, selector in SELECTORS.items()
    ]
    scores_by_name = {str(item["selector"]): item for item in selector_scores}
    experimental = scores_by_name[SELECTOR_NAME]
    symbolic_baseline = scores_by_name["first_admissible"]
    policy_metrics = {
        "symbolic": symbolic_baseline["observed_metric"],
        "llm": experimental["observed_metric"],
        "hybrid": experimental["observed_metric"],
    }
    complete_policy_metrics = {
        policy_mode: float(value)
        for policy_mode, value in policy_metrics.items()
        if value is not None
    }
    expected_order = calibration._normalize_order(stored_case.get("expected_policy_order") or [])
    tolerance = float(stored_case.get("tie_tolerance", 0.0) or 0.0)
    metric = stored_case.get("metric")
    metric = metric if isinstance(metric, dict) else {}
    direction = str(metric.get("direction") or "")
    observed_order = (
        calibration._observed_order(
            complete_policy_metrics,
            direction=direction,
            tolerance=tolerance,
        )
        if len(complete_policy_metrics) == len(POLICY_MODES)
        else []
    )
    pairwise = (
        calibration._pairwise_report(
            expected_order,
            complete_policy_metrics,
            direction=direction,
            tolerance=tolerance,
        )
        if len(complete_policy_metrics) == len(POLICY_MODES)
        else []
    )
    evaluation_checks = {
        "selection_contract_supported": _selection_contract(packet) == SUPPORTED_SELECTION_CONTRACT,
        "selectors_scored_from_live_observation": all(
            item["scored_from_live_observation"] for item in selector_scores
        ),
        "symbolic_baseline_matches_live_symbolic_choice": symbolic_baseline["matches_live_symbolic_choice"],
    }
    selector_findings = {
        "experimental_selector_matches_live_learned_choices": experimental["matches_live_learned_choices"],
        "experimental_selector_preserves_expected_order": observed_order == expected_order,
        "experimental_selector_preserves_learned_policy_tie": bool(pairwise)
        and next(
            (
                item["expected"] == "tie" and item["observed"] == "tie" and item["passes"]
                for item in pairwise
                if {item["left"], item["right"]} == {"llm", "hybrid"}
            ),
            False,
        ),
    }
    checks = {**evaluation_checks, **selector_findings}
    comparison_to_lowest_wait = [
        {
            "selector": selector_name,
            "relation": _metric_relation(
                score["observed_metric"],
                scores_by_name["lowest_visible_wait"]["observed_metric"],
                direction=direction,
            ),
        }
        for selector_name, score in scores_by_name.items()
        if selector_name != "lowest_visible_wait"
    ]
    return {
        "id": stored_case.get("id"),
        "scenario": stored_case.get("scenario"),
        "decision_packet_hash": stored_case.get("decision_packet_hash"),
        "selection_contract": _selection_contract(packet),
        "frontier_size": len(frontier),
        "replay_indices": replay_indices,
        "expected_policy_order": expected_order,
        "experimental_policy_order": observed_order,
        "pairwise": pairwise,
        "selector_scores": selector_scores,
        "selector_comparison_to_lowest_visible_wait": comparison_to_lowest_wait,
        "experimental_matches_lowest_visible_wait": (
            experimental["selected_index"] == scores_by_name["lowest_visible_wait"]["selected_index"]
        ),
        "evaluation_checks": evaluation_checks,
        "selector_findings": selector_findings,
        "checks": checks,
        "passes_gate": all(evaluation_checks.values()),
    }


def run_selector_experiment(
    *,
    corpus_path: str | Path | None = None,
    source_manifest_path: str | Path | None = None,
    results_root: str | Path | None = None,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    corpus_file = Path(corpus_path) if corpus_path is not None else DEFAULT_CORPUS_PATH
    try:
        validation = corpus.validate_corpus(
            corpus_path=corpus_file,
            source_manifest_path=source_manifest_path,
            results_root=results_root,
            verify_hashes=verify_hashes,
        )
    except (calibration.CalibrationError, corpus.CorpusError) as exc:
        raise SelectorExperimentError(str(exc)) from exc
    if not validation["passes_gate"]:
        raise SelectorExperimentError("packet corpus validation gate must pass before selector experiment")
    stored_corpus = _load_corpus(corpus_file)
    stored_cases = stored_corpus.get("cases")
    if not isinstance(stored_cases, list) or not stored_cases:
        raise SelectorExperimentError("packet corpus must declare at least one case")
    case_reports = [
        _case_report(stored_case)
        for stored_case in stored_cases
        if isinstance(stored_case, dict)
    ]
    if len(case_reports) != len(stored_cases):
        raise SelectorExperimentError("packet corpus cases must be objects")
    all_match_lowest_wait = all(case["experimental_matches_lowest_visible_wait"] for case in case_reports)
    selector_names_with_improvement = sorted({
        str(item["selector"])
        for case in case_reports
        for item in case["selector_comparison_to_lowest_visible_wait"]
        if item["relation"] == "better"
    })
    evaluation_checks = {
        "corpus_validation_passes": validation["passes_gate"],
        "cases_pass": bool(case_reports) and all(case["passes_gate"] for case in case_reports),
    }
    selector_findings = {
        "experimental_selector_matches_live_learned_choices": all(
            case["selector_findings"]["experimental_selector_matches_live_learned_choices"]
            for case in case_reports
        ),
        "experimental_selector_preserves_expected_order": all(
            case["selector_findings"]["experimental_selector_preserves_expected_order"]
            for case in case_reports
        ),
        "experimental_selector_preserves_learned_policy_tie": all(
            case["selector_findings"]["experimental_selector_preserves_learned_policy_tie"]
            for case in case_reports
        ),
    }
    checks = {**evaluation_checks, **selector_findings}
    return {
        "kind": "policy_replay_selector_experiment",
        "corpus_name": stored_corpus.get("name"),
        "corpus_hash": validation["corpus_hash"],
        "selector": {
            "name": SELECTOR_NAME,
            "features_used": list(FEATURES_USED),
            "ranking": [
                "blocked_effect_overlap_count",
                "expected_wait_seconds",
                "frontier_index",
            ],
        },
        "cases": case_reports,
        "aggregate": {
            "case_count": len(case_reports),
            "experimental_matches_lowest_visible_wait_on_all_cases": all_match_lowest_wait,
            "adds_discrimination_over_lowest_visible_wait": not all_match_lowest_wait,
            "selectors_with_any_improvement_over_lowest_visible_wait": selector_names_with_improvement,
            "any_selector_improves_over_lowest_visible_wait": bool(selector_names_with_improvement),
            "case_ids_where_experimental_selector_diverges_from_live_learned_choices": [
                case["id"]
                for case in case_reports
                if not case["selector_findings"]["experimental_selector_matches_live_learned_choices"]
            ],
            "case_ids_where_experimental_selector_fails_expected_order": [
                case["id"]
                for case in case_reports
                if not case["selector_findings"]["experimental_selector_preserves_expected_order"]
            ],
            "general_selector_claim_supported": False,
            "claim_scope": "bounded_packet_replay_only",
            "claim_limit": (
                "The packet corpus contains only live-observed branch outcomes and cannot score unseen "
                "candidates; this case count is too small for a general selector claim."
            ),
        },
        "evaluation_checks": evaluation_checks,
        "selector_findings": selector_findings,
        "checks": checks,
        "passes_gate": all(evaluation_checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "policy-replay-selector-experiment",
        help="run the bounded offline selector experiment over the packet-backed frontier corpus",
    )
    parser.add_argument("--corpus", default=None, help="optional packet corpus JSON path")
    parser.add_argument("--source-manifest", default=None, help="optional packet source manifest JSON path")
    parser.add_argument("--results-root", default=None, help="optional root containing frozen JSONL artifacts")
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.add_argument("--skip-hash-check", action="store_true", help="allow source artifact drift")
    parser.set_defaults(func=_cmd_policy_replay_selector_experiment)


def _cmd_policy_replay_selector_experiment(args: Any) -> int:
    try:
        report = run_selector_experiment(
            corpus_path=args.corpus,
            source_manifest_path=args.source_manifest,
            results_root=args.results_root,
            verify_hashes=not args.skip_hash_check,
        )
    except SelectorExperimentError as exc:
        print(f"policy-replay-selector-experiment: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(selector={report['selector']['name']}, cases={len(report['cases'])})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
