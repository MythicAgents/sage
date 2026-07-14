"""Hermetic unseen-candidate scoring over packet-backed replay cases.

The packet corpus is authoritative for live-observed branch outcomes. This module
keeps those scores untouched and adds one bounded eval-only extension: for frontier
branches that were never selected live, it reconstructs the packet state and uses the
existing declared-effect reachability search to produce a synthetic downstream score.

Synthetic scores are always labeled as such. They are not live ground truth and cannot
replace the packet/source calibration gate.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

try:  # package import
    from . import frontier_census
    from . import policy_replay_calibration as calibration
    from . import policy_replay_corpus as corpus
    from ..langgraph import engagement_state
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import frontier_census  # type: ignore
    import policy_replay_calibration as calibration  # type: ignore
    import policy_replay_corpus as corpus  # type: ignore
    import engagement_state  # type: ignore


DEFAULT_CORPUS_PATH = corpus.DEFAULT_CORPUS_PATH
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_NODES = 120
SYNTHETIC_SCORE_SOURCE = "synthetic_modeled_reachability"
LIVE_SCORE_SOURCE = "live_observed"
SUPPORTED_SYNTHETIC_METRIC_KINDS = {"transactions_from_frontier"}


class UnseenCandidateEvaluatorError(ValueError):
    """Raised when packet-backed unseen-candidate evaluation cannot proceed."""


def _load_corpus(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UnseenCandidateEvaluatorError(f"missing packet corpus: {path}") from exc
    except json.JSONDecodeError as exc:
        raise UnseenCandidateEvaluatorError(f"invalid JSON in packet corpus {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise UnseenCandidateEvaluatorError(f"packet corpus {path} must be a JSON object")
    return value


def _packet_state(packet: dict[str, Any]) -> engagement_state.EngagementState:
    normalized = packet.get("normalized_state")
    normalized = normalized if isinstance(normalized, dict) else {}
    footholds = [
        engagement_state.Foothold(
            callback_id=str(item.get("callback_id") or ""),
            agent=str(item.get("agent") or ""),
            host=str(item.get("host") or ""),
            forest=str(item.get("forest") or ""),
            identity=str(item.get("identity") or ""),
            integrity=str(item.get("integrity") or ""),
            alive=item.get("alive") is True,
            source="policy_replay_unseen_candidate_evaluator",
            timestamp="packet",
        )
        for item in list(normalized.get("footholds") or [])
        if isinstance(item, dict)
    ]
    graph_facts = [
        engagement_state.GraphFact(
            predicate=str(predicate),
            source="policy_replay_unseen_candidate_evaluator",
            timestamp="packet",
            ttl_seconds=3600,
        )
        for predicate in list(normalized.get("graph_facts") or [])
        if str(predicate)
    ]
    hops = [
        engagement_state.Hop(
            id=f"packet-achieved-{index}",
            technique="packet-achieved-effect",
            target="",
            effect=str(effect),
            status="achieved",
            evidence={
                "source": "policy_replay_unseen_candidate_evaluator",
                "score_scope": "packet_state_reconstruction",
            },
            preconditions=[],
            satisfied_effects=[str(effect)],
            source="policy_replay_unseen_candidate_evaluator",
            timestamp="packet",
        )
        for index, effect in enumerate(list(normalized.get("achieved_effects") or []))
        if str(effect)
    ]
    for index, outcome in enumerate(list(normalized.get("recent_outcomes") or [])):
        if not isinstance(outcome, dict):
            continue
        status = str(outcome.get("status") or "").strip().casefold()
        if not status or status == "achieved":
            continue
        effect = str(outcome.get("effect") or "")
        if not effect:
            continue
        hops.append(engagement_state.Hop(
            id=f"packet-outcome-{index}",
            technique=str(outcome.get("capability") or "packet-outcome"),
            target=str(outcome.get("target") or ""),
            effect=effect,
            status=status,
            evidence={
                "source": "policy_replay_unseen_candidate_evaluator",
                "score_scope": "packet_state_reconstruction",
            },
            preconditions=[],
            satisfied_effects=[],
            source="policy_replay_unseen_candidate_evaluator",
            timestamp="packet",
        ))
    return engagement_state.EngagementState(
        objective=str(packet.get("objective") or ""),
        footholds=footholds,
        hops=hops,
        graph_facts=graph_facts,
    )


def _candidate(item: dict[str, Any]) -> Any:
    return SimpleNamespace(
        name=str(item.get("name") or ""),
        target=str(item.get("target") or ""),
        preconditions=list(item.get("preconditions") or []),
        effects=list(item.get("effects") or []),
        operational_cost=dict(item.get("operational_cost") or {}),
        reason=str(item.get("reason") or ""),
    )


def _modeled_branch_score(state: engagement_state.EngagementState, item: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(
        frontier_census.modeled_branch_reachability(
            state,
            _candidate(item),
            max_depth=DEFAULT_MAX_DEPTH,
            max_nodes=DEFAULT_MAX_NODES,
        )
    )


def _observed_metrics_by_index(stored_case: dict[str, Any], frontier: list[dict[str, Any]]) -> dict[int, float]:
    replays = stored_case.get("policy_replays")
    replays = replays if isinstance(replays, dict) else {}
    metrics: dict[int, float] = {}
    tolerance = float(stored_case.get("tie_tolerance", 0.0) or 0.0)
    for replay in replays.values():
        replay = replay if isinstance(replay, dict) else {}
        index = corpus._resolve_choice(frontier, replay)  # type: ignore[attr-defined]
        if index is None or replay.get("metric_mean") is None:
            continue
        value = float(replay["metric_mean"])
        prior = metrics.get(index)
        if prior is not None and abs(prior - value) > tolerance:
            raise UnseenCandidateEvaluatorError(
                f"{stored_case.get('id')}: live policies disagree on frontier index {index}"
            )
        metrics[index] = value
    return metrics


def _case_report(stored_case: dict[str, Any]) -> dict[str, Any]:
    packet = stored_case.get("decision_packet")
    packet = packet if isinstance(packet, dict) else {}
    frontier = corpus._packet_frontier(packet) if packet else []
    state = _packet_state(packet)
    observed_metrics = _observed_metrics_by_index(stored_case, frontier)
    metric = stored_case.get("metric")
    metric = metric if isinstance(metric, dict) else {}
    metric_kind = str(metric.get("kind") or "")
    branch_reports: list[dict[str, Any]] = []
    for index, item in enumerate(frontier):
        modeled = _modeled_branch_score(state, item)
        live_observed = index in observed_metrics
        synthetic_supported = metric_kind in SUPPORTED_SYNTHETIC_METRIC_KINDS
        if live_observed:
            effective_score = observed_metrics[index]
            score_source = LIVE_SCORE_SOURCE
        elif synthetic_supported and modeled.get("reachable") is True:
            effective_score = modeled.get("modeled_transactions")
            score_source = SYNTHETIC_SCORE_SOURCE
        else:
            effective_score = None
            score_source = "unscored"
        branch_reports.append({
            "frontier_index": index,
            "selected_capability": item.get("name"),
            "selected_target": item.get("target"),
            "live_observed": live_observed,
            "live_observed_metric": observed_metrics.get(index),
            "effective_score": effective_score,
            "score_source": score_source,
            "synthetic_score_is_ground_truth": False,
            "synthetic_metric_supported": synthetic_supported,
            "modeled_reachability": {
                "reachable": modeled.get("reachable"),
                "modeled_transactions": modeled.get("modeled_transactions"),
                "explored_nodes": modeled.get("explored_nodes"),
                "reason": modeled.get("reason"),
                "path": list(modeled.get("path") or []),
            },
        })
    unseen_reports = [branch for branch in branch_reports if not branch["live_observed"]]
    synthetic_unseen_reports = [
        branch for branch in unseen_reports
        if branch["score_source"] == SYNTHETIC_SCORE_SOURCE
    ]
    checks = {
        "packet_frontier_present": bool(frontier),
        "live_observed_scores_are_not_overwritten": all(
            branch["score_source"] == LIVE_SCORE_SOURCE
            for branch in branch_reports
            if branch["live_observed"]
        ),
        "synthetic_scores_apply_only_to_unseen_branches": all(
            branch["live_observed"] is False
            for branch in branch_reports
            if branch["score_source"] == SYNTHETIC_SCORE_SOURCE
        ),
        "synthetic_scores_are_labeled_not_ground_truth": all(
            branch["synthetic_score_is_ground_truth"] is False
            for branch in branch_reports
            if branch["score_source"] == SYNTHETIC_SCORE_SOURCE
        ),
    }
    return {
        "id": stored_case.get("id"),
        "scenario": stored_case.get("scenario"),
        "metric": metric,
        "decision_packet_hash": stored_case.get("decision_packet_hash"),
        "branch_reports": branch_reports,
        "unseen_frontier_indices": [branch["frontier_index"] for branch in unseen_reports],
        "synthetically_scored_unseen_frontier_indices": [
            branch["frontier_index"] for branch in synthetic_unseen_reports
        ],
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def run_unseen_candidate_evaluator(
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
        raise UnseenCandidateEvaluatorError(str(exc)) from exc
    if not validation["passes_gate"]:
        raise UnseenCandidateEvaluatorError("packet corpus validation gate must pass before unseen-candidate scoring")
    stored_corpus = _load_corpus(corpus_file)
    stored_cases = stored_corpus.get("cases")
    if not isinstance(stored_cases, list) or not stored_cases:
        raise UnseenCandidateEvaluatorError("packet corpus must declare at least one case")
    case_reports = [_case_report(case) for case in stored_cases if isinstance(case, dict)]
    if len(case_reports) != len(stored_cases):
        raise UnseenCandidateEvaluatorError("packet corpus cases must be objects")
    synthetic_unseen_branches = [
        {
            "case_id": case["id"],
            "frontier_index": branch["frontier_index"],
            "selected_capability": branch["selected_capability"],
            "selected_target": branch["selected_target"],
            "effective_score": branch["effective_score"],
        }
        for case in case_reports
        for branch in case["branch_reports"]
        if branch["score_source"] == SYNTHETIC_SCORE_SOURCE
    ]
    checks = {
        "corpus_validation_passes": validation["passes_gate"],
        "cases_pass": bool(case_reports) and all(case["passes_gate"] for case in case_reports),
        "scores_at_least_one_unseen_candidate": bool(synthetic_unseen_branches),
        "preserves_live_observed_score_sources": all(
            case["checks"]["live_observed_scores_are_not_overwritten"]
            for case in case_reports
        ),
        "synthetic_scores_are_not_ground_truth": all(
            case["checks"]["synthetic_scores_are_labeled_not_ground_truth"]
            for case in case_reports
        ),
        "hermetic_no_live_target_io": True,
    }
    return {
        "kind": "policy_replay_unseen_candidate_evaluator",
        "corpus_name": stored_corpus.get("name"),
        "corpus_hash": validation["corpus_hash"],
        "evaluator": {
            "kind": "declared_effect_reachability",
            "max_depth": DEFAULT_MAX_DEPTH,
            "max_nodes": DEFAULT_MAX_NODES,
            "synthetic_score_source": SYNTHETIC_SCORE_SOURCE,
            "supported_synthetic_metric_kinds": sorted(SUPPORTED_SYNTHETIC_METRIC_KINDS),
            "score_contract": (
                "Live-observed branch metrics remain authoritative; modeled reachability scores are attached "
                "only to previously unobserved frontier branches and are never ground truth."
            ),
        },
        "cases": case_reports,
        "aggregate": {
            "case_count": len(case_reports),
            "synthetically_scored_unseen_branch_count": len(synthetic_unseen_branches),
            "synthetically_scored_unseen_branches": synthetic_unseen_branches,
            "promotion_gate_required_before_live_claim": True,
            "claim_scope": "hermetic_declared_effect_scoring_only",
        },
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "policy-replay-unseen-candidate-evaluate",
        help="score packet-corpus unseen branches with hermetic declared-effect reachability",
    )
    parser.add_argument("--corpus", default=None, help="optional packet corpus JSON path")
    parser.add_argument("--source-manifest", default=None, help="optional packet source manifest JSON path")
    parser.add_argument("--results-root", default=None, help="optional root containing frozen JSONL artifacts")
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.add_argument("--skip-hash-check", action="store_true", help="allow source artifact drift")
    parser.set_defaults(func=_cmd_policy_replay_unseen_candidate_evaluate)


def _cmd_policy_replay_unseen_candidate_evaluate(args: Any) -> int:
    try:
        report = run_unseen_candidate_evaluator(
            corpus_path=args.corpus,
            source_manifest_path=args.source_manifest,
            results_root=args.results_root,
            verify_hashes=not args.skip_hash_check,
        )
    except UnseenCandidateEvaluatorError as exc:
        print(f"policy-replay-unseen-candidate-evaluate: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {'PASS' if report['passes_gate'] else 'FAIL'}  "
        f"(synthetically_scored_unseen_branch_count="
        f"{report['aggregate']['synthetically_scored_unseen_branch_count']})",
        flush=True,
    )
    return 0 if report["passes_gate"] else 1

