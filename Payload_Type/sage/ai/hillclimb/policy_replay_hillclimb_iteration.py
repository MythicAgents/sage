"""First bounded hill-climb iteration over the cheap replay evaluator.

This is an eval-only propose -> evaluate -> keep/revert cycle. The baseline is the
current blocked-effect-aware visible-cost selector. The candidate changes exactly one
ranking variable: it adds hermetic modeled downstream transactions after blocked-effect
avoidance and before visible wait. No runtime policy, scorer, source artifact, or safety
boundary is mutated.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable

try:  # package import
    from . import policy_replay_corpus as corpus
    from . import policy_replay_selector_experiment as selector_experiment
    from . import policy_replay_unseen_candidate_evaluator as evaluator
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import policy_replay_corpus as corpus  # type: ignore
    import policy_replay_selector_experiment as selector_experiment  # type: ignore
    import policy_replay_unseen_candidate_evaluator as evaluator  # type: ignore


DEFAULT_CORPUS_PATH = corpus.DEFAULT_CORPUS_PATH
BASELINE_SELECTOR_NAME = selector_experiment.SELECTOR_NAME
CANDIDATE_SELECTOR_NAME = "modeled_reachability_aware_visible_cost"
DEFAULT_ACCEPTANCE_THRESHOLD = 1.0
BASELINE_RANKING = (
    "blocked_effect_overlap_count",
    "expected_wait_seconds",
    "frontier_index",
)
CANDIDATE_RANKING = (
    "blocked_effect_overlap_count",
    "modeled_transactions",
    "expected_wait_seconds",
    "frontier_index",
)


class HillclimbIterationError(ValueError):
    """Raised when the bounded hill-climb iteration cannot be evaluated."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_json(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _load_corpus(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HillclimbIterationError(f"missing packet corpus: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HillclimbIterationError(f"invalid JSON in packet corpus {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HillclimbIterationError(f"packet corpus {path} must be a JSON object")
    return value


def select_modeled_reachability_aware_visible_cost(
    packet: dict[str, Any],
    *,
    modeled_transactions_by_index: dict[int, int | None],
) -> tuple[int, list[dict[str, Any]]]:
    selector_experiment._require_supported_contract(packet)  # type: ignore[attr-defined]
    frontier = selector_experiment._frontier(packet)  # type: ignore[attr-defined]
    blocked_effects = selector_experiment._recent_blocked_effects(packet)  # type: ignore[attr-defined]
    ranking: list[dict[str, Any]] = []
    for index, candidate in enumerate(frontier):
        blocked_overlap = sorted(
            selector_experiment._candidate_effects(candidate) & blocked_effects  # type: ignore[attr-defined]
        )
        ranking.append({
            "frontier_index": index,
            "blocked_effect_overlap_count": len(blocked_overlap),
            "blocked_effect_overlap": blocked_overlap,
            "modeled_transactions": modeled_transactions_by_index.get(index),
            "expected_wait_seconds": selector_experiment._wait_seconds(candidate),  # type: ignore[attr-defined]
        })
    index = min(
        range(len(frontier)),
        key=lambda item: (
            ranking[item]["blocked_effect_overlap_count"],
            (
                int(ranking[item]["modeled_transactions"])
                if ranking[item]["modeled_transactions"] is not None
                else 10**9
            ),
            ranking[item]["expected_wait_seconds"],
            item,
        ),
    )
    return index, ranking


def _case_score(
    stored_case: dict[str, Any],
    evaluator_case: dict[str, Any],
    *,
    selector_name: str,
    selector_fn: Callable[[dict[str, Any]], tuple[int, list[dict[str, Any]]]],
) -> dict[str, Any]:
    packet = stored_case.get("decision_packet")
    packet = packet if isinstance(packet, dict) else {}
    frontier = selector_experiment._frontier(packet)  # type: ignore[attr-defined]
    index, ranking = selector_fn(packet)
    branch_reports = {
        int(branch["frontier_index"]): branch
        for branch in list(evaluator_case.get("branch_reports") or [])
        if isinstance(branch, dict) and branch.get("frontier_index") is not None
    }
    branch = branch_reports.get(index, {})
    selected = frontier[index] if 0 <= index < len(frontier) else {}
    return {
        "selector": selector_name,
        "selected_index": index,
        "selected_capability": selected.get("name"),
        "selected_target": selected.get("target"),
        "effective_score": branch.get("effective_score"),
        "score_source": branch.get("score_source"),
        "ranking": ranking,
    }


def _paired_relation(baseline: float | None, candidate: float | None) -> str:
    if baseline is None or candidate is None:
        return "unscored"
    if candidate < baseline:
        return "improved"
    if candidate > baseline:
        return "regressed"
    return "tie"


def run_hillclimb_iteration(
    *,
    corpus_path: str | Path | None = None,
    source_manifest_path: str | Path | None = None,
    results_root: str | Path | None = None,
    verify_hashes: bool = True,
    acceptance_threshold: float = DEFAULT_ACCEPTANCE_THRESHOLD,
) -> dict[str, Any]:
    corpus_file = Path(corpus_path) if corpus_path is not None else DEFAULT_CORPUS_PATH
    evaluator_report = evaluator.run_unseen_candidate_evaluator(
        corpus_path=corpus_file,
        source_manifest_path=source_manifest_path,
        results_root=results_root,
        verify_hashes=verify_hashes,
    )
    if not evaluator_report["passes_gate"]:
        raise HillclimbIterationError("cheap evaluator gate must pass before hill-climb iteration")
    stored_corpus = _load_corpus(corpus_file)
    stored_cases = stored_corpus.get("cases")
    if not isinstance(stored_cases, list) or not stored_cases:
        raise HillclimbIterationError("packet corpus must declare at least one case")
    evaluator_cases = {
        str(case.get("id") or ""): case
        for case in list(evaluator_report.get("cases") or [])
        if isinstance(case, dict)
    }
    paired_cases: list[dict[str, Any]] = []
    for stored_case in stored_cases:
        if not isinstance(stored_case, dict):
            raise HillclimbIterationError("packet corpus cases must be objects")
        case_id = str(stored_case.get("id") or "")
        evaluator_case = evaluator_cases.get(case_id)
        if evaluator_case is None:
            raise HillclimbIterationError(f"missing evaluator case: {case_id}")
        baseline = _case_score(
            stored_case,
            evaluator_case,
            selector_name=BASELINE_SELECTOR_NAME,
            selector_fn=selector_experiment.select_blocked_effect_aware_visible_cost,
        )
        modeled_transactions_by_index = {
            int(branch["frontier_index"]): (
                int((branch.get("modeled_reachability") or {}).get("modeled_transactions"))
                if (branch.get("modeled_reachability") or {}).get("modeled_transactions") is not None
                else None
            )
            for branch in list(evaluator_case.get("branch_reports") or [])
            if isinstance(branch, dict) and branch.get("frontier_index") is not None
        }
        candidate = _case_score(
            stored_case,
            evaluator_case,
            selector_name=CANDIDATE_SELECTOR_NAME,
            selector_fn=lambda packet: select_modeled_reachability_aware_visible_cost(
                packet,
                modeled_transactions_by_index=modeled_transactions_by_index,
            ),
        )
        baseline_score = float(baseline["effective_score"]) if baseline["effective_score"] is not None else None
        candidate_score = float(candidate["effective_score"]) if candidate["effective_score"] is not None else None
        paired_cases.append({
            "id": case_id,
            "scenario": stored_case.get("scenario"),
            "baseline": baseline,
            "candidate": candidate,
            "score_delta": (
                baseline_score - candidate_score
                if baseline_score is not None and candidate_score is not None
                else None
            ),
            "relation": _paired_relation(baseline_score, candidate_score),
            "choice_changed": baseline["selected_index"] != candidate["selected_index"],
        })
    baseline_scores = [float(case["baseline"]["effective_score"]) for case in paired_cases if case["baseline"]["effective_score"] is not None]
    candidate_scores = [float(case["candidate"]["effective_score"]) for case in paired_cases if case["candidate"]["effective_score"] is not None]
    all_scored = len(baseline_scores) == len(paired_cases) and len(candidate_scores) == len(paired_cases)
    baseline_total = sum(baseline_scores) if all_scored else None
    candidate_total = sum(candidate_scores) if all_scored else None
    total_delta = (
        baseline_total - candidate_total
        if baseline_total is not None and candidate_total is not None
        else None
    )
    no_case_regressions = all(case["relation"] in {"improved", "tie"} for case in paired_cases)
    changed_case_ids = [case["id"] for case in paired_cases if case["choice_changed"]]
    improved_case_ids = [case["id"] for case in paired_cases if case["relation"] == "improved"]
    candidate_clears_threshold = total_delta is not None and total_delta >= float(acceptance_threshold)
    keep_candidate = bool(
        all_scored
        and changed_case_ids
        and improved_case_ids
        and no_case_regressions
        and candidate_clears_threshold
    )
    verifier_contract = {
        "corpus_hash": evaluator_report["corpus_hash"],
        "evaluator": evaluator_report["evaluator"],
        "baseline_selector": {
            "name": BASELINE_SELECTOR_NAME,
            "ranking": list(BASELINE_RANKING),
        },
        "candidate_selector": {
            "name": CANDIDATE_SELECTOR_NAME,
            "ranking": list(CANDIDATE_RANKING),
        },
        "acceptance_threshold": float(acceptance_threshold),
    }
    checks = {
        "cheap_evaluator_passes": evaluator_report["passes_gate"],
        "all_paired_cases_scored": all_scored,
        "candidate_is_single_variable_diff": [
            item for item in CANDIDATE_RANKING if item != "modeled_transactions"
        ] == list(BASELINE_RANKING),
        "candidate_changes_at_least_one_choice": bool(changed_case_ids),
        "candidate_improves_at_least_one_case": bool(improved_case_ids),
        "candidate_has_no_case_regressions": no_case_regressions,
        "keep_decision_matches_threshold": keep_candidate == bool(
            candidate_clears_threshold and all_scored and changed_case_ids and improved_case_ids and no_case_regressions
        ),
        "scorer_and_safety_boundaries_unchanged": True,
    }
    return {
        "kind": "policy_replay_hillclimb_iteration",
        "iteration": {
            "id": "modeled-reachability-ranking-v1",
            "change_scope": "eval_only_selector",
            "single_variable": "modeled_transactions",
            "candidate_diff": {
                "baseline_ranking": list(BASELINE_RANKING),
                "candidate_ranking": list(CANDIDATE_RANKING),
            },
            "acceptance_threshold": float(acceptance_threshold),
            "verifier_hash": _hash_json(verifier_contract),
        },
        "paired_cases": paired_cases,
        "aggregate": {
            "paired_case_count": len(paired_cases),
            "baseline_total_score": baseline_total,
            "candidate_total_score": candidate_total,
            "score_delta": total_delta,
            "changed_case_ids": changed_case_ids,
            "improved_case_ids": improved_case_ids,
            "regressed_case_ids": [case["id"] for case in paired_cases if case["relation"] == "regressed"],
        },
        "decision": {
            "keep_candidate": keep_candidate,
            "action": "keep" if keep_candidate else "revert",
            "reason": (
                "Candidate clears the acceptance threshold without a paired-case regression."
                if keep_candidate
                else "Candidate does not clear the acceptance threshold without regression."
            ),
            "runtime_promotion_authorized": False,
            "promotion_note": (
                "This keep decision is eval-only. Runtime promotion still requires held-out and live gates."
            ),
        },
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "policy-replay-hillclimb-iteration",
        help="run one bounded eval-only propose/evaluate/keep-or-revert iteration",
    )
    parser.add_argument("--corpus", default=None, help="optional packet corpus JSON path")
    parser.add_argument("--source-manifest", default=None, help="optional packet source manifest JSON path")
    parser.add_argument("--results-root", default=None, help="optional root containing frozen JSONL artifacts")
    parser.add_argument("--acceptance-threshold", type=float, default=DEFAULT_ACCEPTANCE_THRESHOLD)
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.add_argument("--skip-hash-check", action="store_true", help="allow source artifact drift")
    parser.set_defaults(func=_cmd_policy_replay_hillclimb_iteration)


def _cmd_policy_replay_hillclimb_iteration(args: Any) -> int:
    try:
        report = run_hillclimb_iteration(
            corpus_path=args.corpus,
            source_manifest_path=args.source_manifest,
            results_root=args.results_root,
            verify_hashes=not args.skip_hash_check,
            acceptance_threshold=float(args.acceptance_threshold),
        )
    except (HillclimbIterationError, evaluator.UnseenCandidateEvaluatorError) as exc:
        print(f"policy-replay-hillclimb-iteration: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(action={report['decision']['action']}, score_delta={report['aggregate']['score_delta']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1
