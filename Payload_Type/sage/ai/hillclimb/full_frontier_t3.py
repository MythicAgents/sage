"""Phase 5 full-frontier falsifiers and model-strength T3 evaluation.

This module stays on the frozen packet corpus boundary. It does not replay Mythic,
invent branch outcomes, mutate runtime policy, or promote a candidate. It asks the
Phase 5 question directly:

* do required deterministic controls reproduce the observed best branch once every
  frontier order is permuted; and
* if a real model matrix is run, do response-derived weak/strong backends differ in
  a way that survives schema, provenance, order, and annotation-ablation checks?

Only independently observed branch outcomes are scored. A legacy packet that lacks
raw-frontier or rejection-reason evidence is reported as such instead of being
upgraded into stronger evidence.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, dataclass
import hashlib
from itertools import permutations
import json
import os
import random
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

try:  # package import
    from . import policy_replay_calibration as calibration
    from . import policy_replay_corpus as corpus
    from . import policy_replay_hillclimb_iteration as hillclimb_iteration
    from . import policy_replay_selector_experiment as selector_experiment
    from . import policy_replay_unseen_candidate_evaluator as unseen_evaluator
    from .experiment_contracts import TypedVerdict
    from ..langgraph import autonomous_controller as controller
    from ..langgraph import policy
except Exception:  # script / flat import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
    import policy_replay_calibration as calibration  # type: ignore
    import policy_replay_corpus as corpus  # type: ignore
    import policy_replay_hillclimb_iteration as hillclimb_iteration  # type: ignore
    import policy_replay_selector_experiment as selector_experiment  # type: ignore
    import policy_replay_unseen_candidate_evaluator as unseen_evaluator  # type: ignore
    from experiment_contracts import TypedVerdict  # type: ignore
    import autonomous_controller as controller  # type: ignore
    import policy  # type: ignore


DEFAULT_CORPUS_PATH = corpus.DEFAULT_CORPUS_PATH
DEFAULT_ROUTE_ENV_PATH = Path(__file__).resolve().parents[4] / "skills" / "sage-eval-gauge" / ".env.local"
DEFAULT_SAGE_CERTS_DIR = Path(__file__).resolve().parents[2] / "certs"
DEFAULT_SAGE_COMBINED_CA_BUNDLE_PATH = DEFAULT_SAGE_CERTS_DIR / "combined-bundle.pem"
DEFAULT_SAGE_CUSTOM_CA_BUNDLE_PATH = DEFAULT_SAGE_CERTS_DIR / "bundle.pem"
PHASE5_SCHEMA_VERSION = 1
DEFAULT_SAMPLES_PER_CELL = 5
PRIMARY_POLICY_MODE = policy.POLICY_HYBRID
REAL_POLICY_MODES = (policy.POLICY_HYBRID, policy.POLICY_LLM)
REQUIRED_TREATMENTS = ("weak", "strong")
REQUIRED_WEAK_MODEL = "bedrock-claude-4-5-haiku"
REQUIRED_STRONG_MODEL = "bedrock-claude-4-6-sonnet"
ANNOTATION_ABLATIONS = (
    "intact",
    "without_reason",
    "without_prior_decisions",
    "without_reason_and_prior_decisions",
)
DETERMINISTIC_CONTROL_NAMES = (
    "symbolic",
    "always-first",
    "always-second",
    "always-last",
    "seeded-random",
    "shortest-wait",
    "lowest-risk",
    "effect-reading",
    "visible-cost",
    "modeled-reachability",
)
POSITIONAL_CONTROL_NAMES = ("always-first", "always-second", "always-last")
NONPOSITIONAL_FALSIFIER_NAMES = (
    "shortest-wait",
    "lowest-risk",
    "effect-reading",
    "visible-cost",
    "modeled-reachability",
)
SYSTEM_PROMPT = (
    "You are evaluating an authorized cyber-range policy decision. Do not execute tools. "
    "Use only the supplied request and choose one currently admissible action. Return exactly "
    "one JSON object satisfying response_schema, with no markdown fences or extra text. "
    "If the request is ambiguous or no action is safe, return a stop disposition."
)


class Phase5EvaluationError(ValueError):
    """Raised when the Phase 5 evaluation contract cannot be executed."""


@dataclass(frozen=True)
class ModelTreatment:
    name: str
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.0

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
        }


@dataclass(frozen=True)
class _SyntheticResponse:
    content: str
    response_metadata: dict[str, Any]


@dataclass(frozen=True)
class _CaseContext:
    case_id: str
    scenario: str
    metric_direction: str
    tie_tolerance: float
    packet: dict[str, Any]
    frontier: tuple[dict[str, Any], ...]
    semantic_ids: tuple[str, ...]
    observed_metrics: dict[str, float]
    preferred_ids: tuple[str, ...]
    branch_outcome_scope: dict[str, Any]
    modeled_transactions: dict[str, int | None]
    retained_policy_replays: dict[str, dict[str, Any]]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_json(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Phase5EvaluationError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Phase5EvaluationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise Phase5EvaluationError(f"{path} must contain a JSON object")
    return value


def _candidate(item: dict[str, Any]) -> Any:
    return SimpleNamespace(
        name=str(item.get("name") or ""),
        target=str(item.get("target") or ""),
        preconditions=list(item.get("preconditions") or []),
        effects=list(item.get("effects") or []),
        operational_cost=dict(item.get("operational_cost") or {}),
        reason=str(item.get("reason") or ""),
    )


def _semantic_id(item: dict[str, Any]) -> str:
    return policy.semantic_candidate_id(_candidate(item))


def _wait_seconds(item: dict[str, Any]) -> int:
    return selector_experiment._wait_seconds(item)  # type: ignore[attr-defined]


def _blocked_effects(packet: dict[str, Any]) -> set[str]:
    return selector_experiment._recent_blocked_effects(packet)  # type: ignore[attr-defined]


def _effect_overlap_count(packet: dict[str, Any], item: dict[str, Any]) -> int:
    effects = selector_experiment._candidate_effects(item)  # type: ignore[attr-defined]
    return len(effects & _blocked_effects(packet))


def _risk_tuple(item: dict[str, Any], index: int) -> tuple[int, int, int, int]:
    cost = item.get("operational_cost")
    cost = cost if isinstance(cost, dict) else {}
    interaction = str(cost.get("interaction_class") or "").strip().casefold()
    interaction_rank = {
        "direct": 0,
        "read-only": 0,
        "collection": 0,
        "propagation-bound": 1,
    }.get(interaction, 2)
    requires_wait = 1 if bool(cost.get("requires_propagation_wait")) else 0
    return interaction_rank, requires_wait, _wait_seconds(item), index


def _metric_regret(selected: float | None, preferred: float | None, *, direction: str) -> float | None:
    if selected is None or preferred is None:
        return None
    if direction == "higher_is_better":
        return max(0.0, preferred - selected)
    return max(0.0, selected - preferred)


def _preferred_metric(context: _CaseContext) -> float | None:
    values = [context.observed_metrics[item] for item in context.preferred_ids if item in context.observed_metrics]
    if not values:
        return None
    return values[0]


def _best_observed_ids(
    observed_metrics: dict[str, float],
    *,
    direction: str,
    tolerance: float,
) -> tuple[str, ...]:
    if not observed_metrics:
        return ()
    best = (
        max(observed_metrics.values())
        if direction == "higher_is_better"
        else min(observed_metrics.values())
    )
    return tuple(sorted(
        candidate_id
        for candidate_id, value in observed_metrics.items()
        if abs(value - best) <= tolerance
    ))


def _observed_metrics(case: dict[str, Any], frontier: list[dict[str, Any]]) -> dict[str, float]:
    replays = case.get("policy_replays")
    replays = replays if isinstance(replays, dict) else {}
    tolerance = float(case.get("tie_tolerance", 0.0) or 0.0)
    result: dict[str, float] = {}
    for replay in replays.values():
        replay = replay if isinstance(replay, dict) else {}
        index = corpus._resolve_choice(frontier, replay)  # type: ignore[attr-defined]
        if index is None or replay.get("metric_mean") is None:
            continue
        candidate_id = _semantic_id(frontier[index])
        value = float(replay["metric_mean"])
        prior = result.get(candidate_id)
        if prior is not None and abs(prior - value) > tolerance:
            raise Phase5EvaluationError(
                f"{case.get('id')}: live policies disagree on metric for {candidate_id}"
            )
        result[candidate_id] = value
    return result


def _modeled_transactions(packet: dict[str, Any], frontier: list[dict[str, Any]]) -> dict[str, int | None]:
    state = unseen_evaluator._packet_state(packet)  # type: ignore[attr-defined]
    values: dict[str, int | None] = {}
    for item in frontier:
        modeled = unseen_evaluator._modeled_branch_score(state, item)  # type: ignore[attr-defined]
        raw = modeled.get("modeled_transactions")
        values[_semantic_id(item)] = int(raw) if raw is not None else None
    return values


def _retained_policy_replays(case: dict[str, Any], frontier: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    replays = case.get("policy_replays")
    replays = replays if isinstance(replays, dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for policy_mode in calibration.POLICY_MODES:
        replay = replays.get(policy_mode)
        replay = replay if isinstance(replay, dict) else {}
        index = corpus._resolve_choice(frontier, replay)  # type: ignore[attr-defined]
        selected_id = _semantic_id(frontier[index]) if isinstance(index, int) else ""
        result[policy_mode] = {
            "selected_source_index": index,
            "selected_candidate_id": selected_id,
            "observed_metric": replay.get("metric_mean"),
            "scored_from_independently_observed_branch": replay.get("metric_mean") is not None,
        }
    return result


def _case_context(case: dict[str, Any]) -> _CaseContext:
    packet = case.get("decision_packet")
    packet = dict(packet) if isinstance(packet, dict) else {}
    frontier = corpus._packet_frontier(packet) if packet else []  # type: ignore[attr-defined]
    metric = case.get("metric")
    metric = metric if isinstance(metric, dict) else {}
    direction = str(metric.get("direction") or "")
    if direction not in {"lower_is_better", "higher_is_better"}:
        raise Phase5EvaluationError(f"{case.get('id')}: unsupported metric direction {direction!r}")
    observed = _observed_metrics(case, frontier)
    preferred = _best_observed_ids(
        observed,
        direction=direction,
        tolerance=float(case.get("tie_tolerance", 0.0) or 0.0),
    )
    if not preferred:
        raise Phase5EvaluationError(f"{case.get('id')}: no independently observed preferred branch")
    return _CaseContext(
        case_id=str(case.get("id") or ""),
        scenario=str(case.get("scenario") or ""),
        metric_direction=direction,
        tie_tolerance=float(case.get("tie_tolerance", 0.0) or 0.0),
        packet=packet,
        frontier=tuple(dict(item) for item in frontier),
        semantic_ids=tuple(_semantic_id(item) for item in frontier),
        observed_metrics=observed,
        preferred_ids=preferred,
        branch_outcome_scope=dict(case.get("branch_outcome_scope") or {}),
        modeled_transactions=_modeled_transactions(packet, frontier),
        retained_policy_replays=_retained_policy_replays(case, frontier),
    )


def _load_contexts(
    *,
    corpus_path: str | Path | None = None,
    source_manifest_path: str | Path | None = None,
    results_root: str | Path | None = None,
    verify_hashes: bool = True,
) -> tuple[dict[str, Any], list[_CaseContext], dict[str, Any]]:
    corpus_file = Path(corpus_path) if corpus_path is not None else DEFAULT_CORPUS_PATH
    try:
        validation = corpus.validate_corpus(
            corpus_path=corpus_file,
            source_manifest_path=source_manifest_path,
            results_root=results_root,
            verify_hashes=verify_hashes,
        )
    except (calibration.CalibrationError, corpus.CorpusError) as exc:
        raise Phase5EvaluationError(str(exc)) from exc
    if not validation["passes_gate"]:
        raise Phase5EvaluationError("packet corpus validation gate must pass before Phase 5 evaluation")
    stored = _load_json(corpus_file)
    cases = stored.get("cases")
    if not isinstance(cases, list) or not cases:
        raise Phase5EvaluationError("packet corpus must declare at least one case")
    contexts = [_case_context(case) for case in cases if isinstance(case, dict)]
    if len(contexts) != len(cases):
        raise Phase5EvaluationError("packet corpus cases must be objects")
    return stored, contexts, validation


def _permutation_rows(context: _CaseContext) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ordinal, order in enumerate(permutations(range(len(context.frontier))), start=1):
        presented_ids = [context.semantic_ids[index] for index in order]
        preferred_positions = [
            position
            for position, candidate_id in enumerate(presented_ids)
            if candidate_id in context.preferred_ids
        ]
        rows.append({
            "permutation_id": f"{context.case_id}:perm-{ordinal}",
            "source_indices": list(order),
            "presented_candidate_ids": presented_ids,
            "ordered_frontier_hash": _hash_json(presented_ids),
            "preferred_positions": preferred_positions,
        })
    return rows


def _presented_frontier(context: _CaseContext, source_indices: Iterable[int]) -> list[dict[str, Any]]:
    return [dict(context.frontier[index]) for index in source_indices]


def _control_index(
    name: str,
    *,
    context: _CaseContext,
    source_indices: list[int],
) -> tuple[int, list[dict[str, Any]]]:
    presented = _presented_frontier(context, source_indices)
    packet = {**context.packet, "admissible_frontier": presented}
    if name in {"symbolic", "always-first"}:
        return 0, [{"frontier_index": index} for index in range(len(presented))]
    if name == "always-second":
        index = 1 if len(presented) > 1 else 0
        return index, [{"frontier_index": item} for item in range(len(presented))]
    if name == "always-last":
        index = len(presented) - 1
        return index, [{"frontier_index": item} for item in range(len(presented))]
    if name == "seeded-random":
        seed_material = f"{context.case_id}|{'|'.join(context.semantic_ids[index] for index in source_indices)}"
        seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
        index = random.Random(seed).randrange(len(presented))
        return index, [{"frontier_index": item, "seed": seed} for item in range(len(presented))]
    if name == "shortest-wait":
        ranking = [
            {"frontier_index": index, "expected_wait_seconds": _wait_seconds(item)}
            for index, item in enumerate(presented)
        ]
        index = min(range(len(presented)), key=lambda item: (_wait_seconds(presented[item]), item))
        return index, ranking
    if name == "lowest-risk":
        ranking = [
            {
                "frontier_index": index,
                "risk_tuple": list(_risk_tuple(item, index)),
            }
            for index, item in enumerate(presented)
        ]
        index = min(range(len(presented)), key=lambda item: _risk_tuple(presented[item], item))
        return index, ranking
    if name == "effect-reading":
        ranking = [
            {
                "frontier_index": index,
                "blocked_effect_overlap_count": _effect_overlap_count(packet, item),
            }
            for index, item in enumerate(presented)
        ]
        index = min(
            range(len(presented)),
            key=lambda item: (_effect_overlap_count(packet, presented[item]), item),
        )
        return index, ranking
    if name == "visible-cost":
        index, ranking = selector_experiment.select_blocked_effect_aware_visible_cost(packet)
        return index, ranking
    if name == "modeled-reachability":
        modeled_by_presented_index = {
            index: context.modeled_transactions.get(context.semantic_ids[source_index])
            for index, source_index in enumerate(source_indices)
        }
        index, ranking = hillclimb_iteration.select_modeled_reachability_aware_visible_cost(
            packet,
            modeled_transactions_by_index=modeled_by_presented_index,
        )
        return index, ranking
    raise Phase5EvaluationError(f"unsupported deterministic control: {name}")


def _selection_report(
    *,
    context: _CaseContext,
    source_indices: list[int],
    selected_presented_index: int | None,
    selector: str,
    ranking: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected_source_index = (
        source_indices[selected_presented_index]
        if isinstance(selected_presented_index, int) and 0 <= selected_presented_index < len(source_indices)
        else None
    )
    selected_id = (
        context.semantic_ids[selected_source_index]
        if isinstance(selected_source_index, int)
        else ""
    )
    metric = context.observed_metrics.get(selected_id)
    preferred_metric = _preferred_metric(context)
    return {
        "selector": selector,
        "selected_presented_index": selected_presented_index,
        "selected_source_index": selected_source_index,
        "selected_candidate_id": selected_id,
        "selected_is_empirical_preferred": selected_id in context.preferred_ids,
        "observed_metric": metric,
        "scored_from_independently_observed_branch": metric is not None,
        "empirical_regret": _metric_regret(metric, preferred_metric, direction=context.metric_direction),
        "ranking": list(ranking or []),
    }


def _raw_admissible_audit(context: _CaseContext) -> dict[str, Any]:
    packet = context.packet
    raw_frontier = packet.get("raw_frontier")
    rejected = packet.get("rejected_candidates")
    raw_available = isinstance(raw_frontier, list)
    rejected_available = isinstance(rejected, list)
    rejection_reasons = []
    if rejected_available:
        for item in rejected:
            if not isinstance(item, dict):
                continue
            rejection_reasons.append({
                "candidate_id": str(item.get("candidate_id") or ""),
                "reason": str(item.get("reason") or ""),
            })
    reason_codes = []
    if not raw_available:
        reason_codes.append("legacy_packet_missing_raw_frontier")
    if not rejected_available:
        reason_codes.append("legacy_packet_missing_rejection_reasons")
    return {
        "raw_frontier_available": raw_available,
        "raw_candidate_count": len(raw_frontier) if raw_available else None,
        "admissible_candidate_count": len(context.frontier),
        "rejection_reasons_available": rejected_available,
        "rejection_reasons": rejection_reasons,
        "reason_codes": reason_codes,
        "passes_gate": raw_available and rejected_available,
    }


def _control_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    countable = [row for row in rows if row["scored_from_independently_observed_branch"]]
    regrets = [float(row["empirical_regret"]) for row in countable if row["empirical_regret"] is not None]
    return {
        "selection_count": len(rows),
        "countable_selection_count": len(countable),
        "diagnostic_only_selection_count": len(rows) - len(countable),
        "empirical_best_accuracy": (
            sum(1 for row in countable if row["selected_is_empirical_preferred"]) / len(countable)
            if countable else None
        ),
        "mean_empirical_regret": (sum(regrets) / len(regrets)) if regrets else None,
        "zero_regret": bool(regrets) and all(value == 0.0 for value in regrets),
    }


def _deterministic_falsifier(contexts: list[_CaseContext]) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    all_control_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in DETERMINISTIC_CONTROL_NAMES}
    for context in contexts:
        permutation_rows = _permutation_rows(context)
        for permutation_row in permutation_rows:
            source_indices = list(permutation_row["source_indices"])
            controls: dict[str, Any] = {}
            for name in DETERMINISTIC_CONTROL_NAMES:
                index, ranking = _control_index(
                    name,
                    context=context,
                    source_indices=source_indices,
                )
                report = _selection_report(
                    context=context,
                    source_indices=source_indices,
                    selected_presented_index=index,
                    selector=name,
                    ranking=ranking,
                )
                controls[name] = report
                all_control_rows[name].append(report)
            permutation_row["controls"] = controls
        preferred_positions = sorted({
            position
            for row in permutation_rows
            for position in row["preferred_positions"]
        })
        raw_audit = _raw_admissible_audit(context)
        cases.append({
            "id": context.case_id,
            "scenario": context.scenario,
            "frontier_size": len(context.frontier),
            "semantic_candidate_ids": list(context.semantic_ids),
            "observed_metrics_by_candidate_id": dict(context.observed_metrics),
            "empirical_preferred_candidate_ids": list(context.preferred_ids),
            "branch_outcome_scope": context.branch_outcome_scope,
            "modeled_transactions_by_candidate_id": dict(context.modeled_transactions),
            "retained_policy_replays": dict(context.retained_policy_replays),
            "raw_admissible_audit": raw_audit,
            "permutation_count": len(permutation_rows),
            "expected_permutation_count": _factorial(len(context.frontier)),
            "preferred_positions_covered": preferred_positions,
            "expected_preferred_positions": list(range(len(context.frontier))),
            "permutations": permutation_rows,
            "checks": {
                "all_permutations_enumerated": len(permutation_rows) == _factorial(len(context.frontier)),
                "preferred_action_appears_at_every_position": preferred_positions == list(range(len(context.frontier))),
                "semantic_ids_are_unique": len(set(context.semantic_ids)) == len(context.semantic_ids),
                "only_observed_branches_are_scored": all(
                    report["scored_from_independently_observed_branch"]
                    == (report["selected_candidate_id"] in context.observed_metrics)
                    for row in permutation_rows
                    for report in row["controls"].values()
                ),
                "raw_and_rejection_evidence_available": raw_audit["passes_gate"],
            },
        })
    aggregates = {
        name: _control_aggregate(rows)
        for name, rows in all_control_rows.items()
    }
    deterministic_reproducers = [
        name
        for name in NONPOSITIONAL_FALSIFIER_NAMES
        if aggregates[name]["empirical_best_accuracy"] == 1.0
        and aggregates[name]["zero_regret"] is True
    ]
    best_positional_name = max(
        POSITIONAL_CONTROL_NAMES,
        key=lambda name: (
            aggregates[name]["empirical_best_accuracy"] or -1.0,
            -(aggregates[name]["mean_empirical_regret"] or 0.0),
            name,
        ),
    )
    checks = {
        "cases_present": bool(cases),
        "all_cases_enumerate_every_permutation": all(
            case["checks"]["all_permutations_enumerated"] for case in cases
        ),
        "preferred_action_placed_at_every_position": all(
            case["checks"]["preferred_action_appears_at_every_position"] for case in cases
        ),
        "semantic_ids_unique_per_case": all(case["checks"]["semantic_ids_are_unique"] for case in cases),
        "only_independently_observed_branches_scored": all(
            case["checks"]["only_observed_branches_are_scored"] for case in cases
        ),
        "raw_and_rejection_evidence_available": all(
            case["checks"]["raw_and_rejection_evidence_available"] for case in cases
        ),
        "no_nonpositional_control_reproduces_empirical_best": not deterministic_reproducers,
    }
    return {
        "cases": cases,
        "control_aggregates": aggregates,
        "best_positional_control": {
            "name": best_positional_name,
            **aggregates[best_positional_name],
        },
        "deterministic_reproducers": deterministic_reproducers,
        "benchmark_discriminating": not deterministic_reproducers,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def _factorial(value: int) -> int:
    result = 1
    for item in range(2, max(2, value + 1)):
        result *= item
    return result


class _SeamState:
    def __init__(self) -> None:
        self.effects: set[str] = set()

    def achieved_effects(self) -> set[str]:
        return set(self.effects)


@dataclass(frozen=True)
class _SeamAction:
    name: str
    target: str
    effects: tuple[str, ...] = ("objective:phase5-null-seam",)
    preconditions: tuple[str, ...] = ()
    reason: str = "phase5 branch seam fixture"


def _run_seam_controller(backend: Any, actions: list[_SeamAction]) -> dict[str, Any]:
    state = _SeamState()
    executed: list[str] = []

    def execute(selected: _SeamAction, _decision: policy.PolicyDecision) -> dict[str, Any]:
        executed.append(selected.target)
        state.effects.update(selected.effects)
        return {"ok": True}

    result = asyncio.run(controller.AutonomousController(
        observe=lambda: state,
        execute=execute,
        objective_met=lambda current: "objective:phase5-null-seam" in current.achieved_effects(),
        frontier_fn=lambda _state: list(actions),
        policy_backend=backend,
        objective="prove phase5 null seam",
        episode_id="episode-phase5-null-seam",
        config=controller.ControllerConfig(max_cycles=2),
    ).run())
    return {
        "controller_status": result.status,
        "controller_reason": result.reason,
        "executed_targets": executed,
        "telemetry": result.to_dict(),
    }


def run_null_branch_seam() -> dict[str, Any]:
    branch_actions = [
        _SeamAction("test-capability", "target-a"),
        _SeamAction("test-capability", "target-b"),
    ]
    singleton_actions = [_SeamAction("test-capability", "target-singleton")]

    null_branch = _run_seam_controller(
        policy.HybridPolicy(None, provider="null", model_id="null"),
        branch_actions,
    )

    selected_id = policy.semantic_candidate_id(branch_actions[1])

    def enabled_decide(_request: dict[str, Any]) -> _SyntheticResponse:
        return _SyntheticResponse(
            content=json.dumps({
                "disposition": "select",
                "candidate_id": selected_id,
                "rationale": "fixture branch selection",
                "confidence": 1.0,
                "expected_evidence": "fixture",
            }),
            response_metadata={
                "model_provider": "fixture",
                "model_name": "enabled-branch-model",
            },
        )

    enabled_branch = _run_seam_controller(
        policy.HybridPolicy(enabled_decide, provider="fixture", model_id="enabled-branch-model"),
        branch_actions,
    )
    null_singleton = _run_seam_controller(
        policy.HybridPolicy(None, provider="null", model_id="null"),
        singleton_actions,
    )
    null_branch_decisions = list((null_branch.get("telemetry") or {}).get("decisions") or [])
    enabled_decisions = list((enabled_branch.get("telemetry") or {}).get("decisions") or [])
    singleton_decisions = list((null_singleton.get("telemetry") or {}).get("decisions") or [])
    checks = {
        "null_branch_executes_no_action": null_branch["executed_targets"] == [],
        "null_branch_stops": bool(null_branch_decisions)
        and null_branch_decisions[0].get("disposition") == "stop",
        "null_branch_is_model_owned_opportunity": bool(null_branch_decisions)
        and null_branch_decisions[0].get("decision_owner") == "model_branch",
        "enabled_branch_selects_semantic_candidate": enabled_branch["executed_targets"] == ["target-b"]
        and bool(enabled_decisions)
        and enabled_decisions[0].get("selected_candidate_id") == selected_id,
        "singleton_remains_kernel_owned": null_singleton["executed_targets"] == ["target-singleton"]
        and bool(singleton_decisions)
        and singleton_decisions[0].get("decision_owner") == "kernel_singleton",
    }
    return {
        "kind": "phase5_null_branch_seam",
        "null_branch": null_branch,
        "enabled_branch": enabled_branch,
        "null_singleton": null_singleton,
        "checks": checks,
        "passes_gate": all(checks.values()),
    }


def _history_from_packet(packet: dict[str, Any], *, ablation: str) -> list[Any]:
    if ablation in {"without_prior_decisions", "without_reason_and_prior_decisions"}:
        return []
    history = []
    for item in list(packet.get("prior_decisions") or []):
        if not isinstance(item, dict):
            continue
        history.append(SimpleNamespace(
            decision_id=str(item.get("decision_id") or ""),
            disposition=str(item.get("disposition") or ""),
            selected_capability=str(item.get("capability") or ""),
            selected_target=str(item.get("target") or ""),
        ))
    return history


def _apply_ablation(items: list[dict[str, Any]], *, ablation: str) -> list[dict[str, Any]]:
    if ablation not in ANNOTATION_ABLATIONS:
        raise Phase5EvaluationError(f"unsupported annotation ablation: {ablation}")
    result = copy.deepcopy(items)
    if ablation in {"without_reason", "without_reason_and_prior_decisions"}:
        for item in result:
            item["reason"] = ""
    return result


def _catalog(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    catalog: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        catalog.append({"name": name, "description": "admissible capability"})
    return catalog


def _row_schema_ok(decision: policy.PolicyDecision) -> bool:
    if decision.raw_disposition != "select":
        return False
    return bool(decision.selected_candidate_id and decision.disposition == "select")


def _run_policy_row(
    *,
    context: _CaseContext,
    source_indices: list[int],
    policy_mode: str,
    treatment: ModelTreatment,
    ablation: str,
    sample_index: int,
    invoke: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    presented_source_items = _presented_frontier(context, source_indices)
    ablated_items = _apply_ablation(presented_source_items, ablation=ablation)
    candidates = [_candidate(item) for item in ablated_items]
    state = unseen_evaluator._packet_state(context.packet)  # type: ignore[attr-defined]
    history = _history_from_packet(context.packet, ablation=ablation)
    budgets = dict(context.packet.get("budgets") or {})
    captured_requests: list[dict[str, Any]] = []

    def capture(request: dict[str, Any]) -> Any:
        captured_requests.append(copy.deepcopy(request))
        return invoke(request)

    if policy_mode == policy.POLICY_HYBRID:
        backend = policy.HybridPolicy(capture, provider=treatment.provider, model_id=treatment.model)
    elif policy_mode == policy.POLICY_LLM:
        backend = policy.LLMPolicy(
            capture,
            provider=treatment.provider,
            model_id=treatment.model,
            catalog=_catalog(ablated_items),
        )
    else:
        raise Phase5EvaluationError(f"unsupported model policy mode: {policy_mode}")
    decision = asyncio.run(backend.select(
        episode_id=f"phase5:{context.case_id}:{policy_mode}:{treatment.name}:{ablation}:{sample_index}",
        objective=str(context.packet.get("objective") or ""),
        state=state,
        candidates=candidates,
        history=history,
        budgets=budgets,
    ))
    request = captured_requests[0] if captured_requests else {}
    selected_presented_index = decision.selected_index
    selected_source_index = (
        source_indices[selected_presented_index]
        if isinstance(selected_presented_index, int) and 0 <= selected_presented_index < len(source_indices)
        else None
    )
    selected_source_id = (
        context.semantic_ids[selected_source_index]
        if isinstance(selected_source_index, int)
        else ""
    )
    observed_metric = context.observed_metrics.get(selected_source_id)
    preferred_metric = _preferred_metric(context)
    return {
        "case_id": context.case_id,
        "scenario": context.scenario,
        "policy_mode": policy_mode,
        "treatment": treatment.name,
        "configured_provider": treatment.provider,
        "configured_model": treatment.model,
        "temperature": treatment.temperature,
        "ablation": ablation,
        "sample_index": sample_index,
        "presented_candidate_ids": [context.semantic_ids[index] for index in source_indices],
        "presented_ablated_candidate_ids": [policy.semantic_candidate_id(candidate) for candidate in candidates],
        "selected_presented_index": selected_presented_index,
        "selected_source_index": selected_source_index,
        "selected_candidate_id": selected_source_id,
        "selected_ablated_candidate_id": decision.selected_candidate_id,
        "selected_is_empirical_preferred": selected_source_id in context.preferred_ids,
        "observed_metric": observed_metric,
        "scored_from_independently_observed_branch": observed_metric is not None,
        "empirical_regret": _metric_regret(observed_metric, preferred_metric, direction=context.metric_direction),
        "disposition": decision.disposition,
        "raw_disposition": decision.raw_disposition,
        "schema_ok": _row_schema_ok(decision),
        "request_hash": _hash_json(request) if request else "",
        "request_schema_hash": decision.request_schema_hash,
        "selection_contract": decision.selection_contract,
        "selection_contract_hash": decision.selection_contract_hash,
        "candidate_set_hash": decision.candidate_set_hash,
        "ordered_frontier_hash": decision.ordered_frontier_hash,
        "effective_backend": decision.effective_backend,
        "effective_model_provider": decision.effective_model_provider,
        "effective_model_id": decision.effective_model_id,
        "backend_provenance_source": decision.backend_provenance_source,
        "response_metadata": dict(decision.response_metadata),
        "response_derived_backend_present": bool(
            decision.effective_backend
            and decision.backend_provenance_source
            and decision.backend_provenance_source != "unavailable"
        ),
        "model_response_observed": decision.model_response_observed,
    }


def _group_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        values = []
        for item in keys:
            value = row.get(item)
            if isinstance(value, list):
                value = tuple(value)
            elif isinstance(value, dict):
                value = _canonical_json(value)
            values.append(value)
        key = tuple(values)
        grouped.setdefault(key, []).append(row)
    return grouped


def _model_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    countable = [row for row in rows if row["scored_from_independently_observed_branch"]]
    regrets = [float(row["empirical_regret"]) for row in countable if row["empirical_regret"] is not None]
    return {
        "row_count": len(rows),
        "countable_row_count": len(countable),
        "diagnostic_only_row_count": len(rows) - len(countable),
        "schema_success_rate": sum(1 for row in rows if row["schema_ok"]) / len(rows) if rows else 0.0,
        "provenance_success_rate": (
            sum(1 for row in rows if row["response_derived_backend_present"]) / len(rows)
            if rows else 0.0
        ),
        "empirical_best_accuracy": (
            sum(1 for row in countable if row["selected_is_empirical_preferred"]) / len(countable)
            if countable else None
        ),
        "mean_empirical_regret": (sum(regrets) / len(regrets)) if regrets else None,
        "effective_backends": sorted({
            str(row["effective_backend"])
            for row in rows
            if str(row["effective_backend"])
        }),
    }


def _annotation_ablation_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    intact_rows = {
        (
            row["case_id"],
            row["policy_mode"],
            row["treatment"],
            row["sample_index"],
            tuple(row["presented_candidate_ids"]),
        ): row
        for row in rows
        if row["ablation"] == "intact"
    }
    report: dict[str, Any] = {}
    for ablation in ANNOTATION_ABLATIONS:
        if ablation == "intact":
            continue
        comparisons = []
        for row in rows:
            if row["ablation"] != ablation:
                continue
            key = (
                row["case_id"],
                row["policy_mode"],
                row["treatment"],
                row["sample_index"],
                tuple(row["presented_candidate_ids"]),
            )
            intact = intact_rows.get(key)
            if intact is None:
                continue
            comparisons.append({
                "case_id": row["case_id"],
                "policy_mode": row["policy_mode"],
                "treatment": row["treatment"],
                "same_semantic_selection": row["selected_candidate_id"] == intact["selected_candidate_id"],
                "intact_candidate_id": intact["selected_candidate_id"],
                "ablated_candidate_id": row["selected_candidate_id"],
            })
        report[ablation] = {
            "comparison_count": len(comparisons),
            "semantic_selection_agreement_rate": (
                sum(1 for item in comparisons if item["same_semantic_selection"]) / len(comparisons)
                if comparisons else None
            ),
            "changed_selection_count": sum(1 for item in comparisons if not item["same_semantic_selection"]),
            "comparisons": comparisons,
        }
    return report


def _semantic_order_invariance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = _group_rows(
        rows,
        ("case_id", "policy_mode", "treatment", "ablation", "sample_index"),
    )
    reports: list[dict[str, Any]] = []
    for key, grouped_rows in grouped.items():
        selections = sorted({
            str(row["selected_candidate_id"])
            for row in grouped_rows
            if str(row["selected_candidate_id"])
        })
        reports.append({
            "case_id": key[0],
            "policy_mode": key[1],
            "treatment": key[2],
            "ablation": key[3],
            "sample_index": key[4],
            "permutation_count": len(grouped_rows),
            "selected_candidate_ids": selections,
            "passes": len(selections) == 1 and all(row["schema_ok"] for row in grouped_rows),
        })
    return {
        "group_count": len(reports),
        "passing_group_count": sum(1 for item in reports if item["passes"]),
        "rate": (
            sum(1 for item in reports if item["passes"]) / len(reports)
            if reports else 0.0
        ),
        "groups": reports,
    }


def _matched_packet_schema_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = _group_rows(
        rows,
        ("case_id", "policy_mode", "ablation", "sample_index", "presented_candidate_ids"),
    )
    reports: list[dict[str, Any]] = []
    for key, grouped_rows in grouped.items():
        treatments = {str(row["treatment"]) for row in grouped_rows}
        request_hashes = {str(row["request_hash"]) for row in grouped_rows}
        schema_hashes = {str(row["request_schema_hash"]) for row in grouped_rows}
        reports.append({
            "case_id": key[0],
            "policy_mode": key[1],
            "ablation": key[2],
            "sample_index": key[3],
            "treatments": sorted(treatments),
            "request_hashes": sorted(request_hashes),
            "request_schema_hashes": sorted(schema_hashes),
            "passes": treatments >= set(REQUIRED_TREATMENTS)
            and len(request_hashes) == 1
            and len(schema_hashes) == 1
            and "" not in request_hashes
            and "" not in schema_hashes,
        })
    return {
        "group_count": len(reports),
        "passing_group_count": sum(1 for item in reports if item["passes"]),
        "rate": (
            sum(1 for item in reports if item["passes"]) / len(reports)
            if reports else 0.0
        ),
        "groups": reports,
    }


def run_model_strength_matrix(
    *,
    contexts: list[_CaseContext],
    treatments: list[ModelTreatment],
    invoker_factory: Callable[[ModelTreatment], Callable[[dict[str, Any]], Any]],
    samples_per_cell: int = DEFAULT_SAMPLES_PER_CELL,
    ablations: tuple[str, ...] = ANNOTATION_ABLATIONS,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if samples_per_cell <= 0:
        raise Phase5EvaluationError("samples_per_cell must be positive")
    by_name = {treatment.name: treatment for treatment in treatments}
    missing = [name for name in REQUIRED_TREATMENTS if name not in by_name]
    if missing:
        raise Phase5EvaluationError(f"missing required model treatments: {', '.join(missing)}")
    if any(float(treatment.temperature) != 0.0 for treatment in treatments):
        raise Phase5EvaluationError("Phase 5 real-backend treatments must use temperature zero")
    for ablation in ablations:
        if ablation not in ANNOTATION_ABLATIONS:
            raise Phase5EvaluationError(f"unsupported annotation ablation: {ablation}")
    log = progress or (lambda _message: None)
    invokers = {treatment.name: invoker_factory(treatment) for treatment in treatments}
    rows: list[dict[str, Any]] = []
    total = sum(
        _factorial(len(context.frontier))
        * len(REAL_POLICY_MODES)
        * len(treatments)
        * len(ablations)
        * samples_per_cell
        for context in contexts
    )
    completed = 0
    for context in contexts:
        for permutation_row in _permutation_rows(context):
            source_indices = list(permutation_row["source_indices"])
            for ablation in ablations:
                for policy_mode in REAL_POLICY_MODES:
                    for treatment in treatments:
                        for sample_index in range(samples_per_cell):
                            completed += 1
                            log(
                                f"[phase5-t3] {completed}/{total} "
                                f"{context.case_id} {policy_mode} {treatment.name} {ablation} sample={sample_index + 1}"
                            )
                            rows.append(_run_policy_row(
                                context=context,
                                source_indices=source_indices,
                                policy_mode=policy_mode,
                                treatment=treatment,
                                ablation=ablation,
                                sample_index=sample_index,
                                invoke=invokers[treatment.name],
                            ))
    summaries = {
        treatment.name: {
            policy_mode: {
                ablation: _model_summary([
                    row for row in rows
                    if row["treatment"] == treatment.name
                    and row["policy_mode"] == policy_mode
                    and row["ablation"] == ablation
                ])
                for ablation in ablations
            }
            for policy_mode in REAL_POLICY_MODES
        }
        for treatment in treatments
    }
    weak_rows = [row for row in rows if row["treatment"] == "weak"]
    strong_rows = [row for row in rows if row["treatment"] == "strong"]
    weak_backends = sorted({row["effective_backend"] for row in weak_rows if row["effective_backend"]})
    strong_backends = sorted({row["effective_backend"] for row in strong_rows if row["effective_backend"]})
    matched = _matched_packet_schema_report(rows)
    order = _semantic_order_invariance(rows)
    ablation_report = _annotation_ablation_report(rows)
    primary_weak = summaries["weak"][PRIMARY_POLICY_MODE]["intact"]
    primary_strong = summaries["strong"][PRIMARY_POLICY_MODE]["intact"]
    checks = {
        "temperature_zero": all(float(treatment.temperature) == 0.0 for treatment in treatments),
        "five_samples_per_real_cell": samples_per_cell == DEFAULT_SAMPLES_PER_CELL,
        "matched_packets_and_schemas": matched["rate"] == 1.0,
        "response_schema_success_rate_100_percent": all(row["schema_ok"] for row in rows),
        "response_provenance_success_rate_100_percent": all(
            row["response_derived_backend_present"] for row in rows
        ),
        "effective_weak_and_strong_backends_are_distinct": bool(weak_backends)
        and bool(strong_backends)
        and not (set(weak_backends) & set(strong_backends)),
        "semantic_order_invariance_100_percent": order["rate"] == 1.0,
        "strong_chooses_empirical_best_at_least_80_percent": (
            primary_strong["empirical_best_accuracy"] is not None
            and primary_strong["empirical_best_accuracy"] >= 0.80
        ),
    }
    return {
        "kind": "phase5_model_strength_t3",
        "samples_per_cell": samples_per_cell,
        "ablations": list(ablations),
        "treatments": [treatment.public_dict() for treatment in treatments],
        "policy_modes": list(REAL_POLICY_MODES),
        "rows": rows,
        "summaries": summaries,
        "matched_packet_schema": matched,
        "semantic_order_invariance": order,
        "annotation_ablations": ablation_report,
        "effective_backend_identity": {
            "weak": weak_backends,
            "strong": strong_backends,
        },
        "primary_hybrid": {
            "weak": primary_weak,
            "strong": primary_strong,
        },
        "checks": checks,
    }


def _default_typed_verdict(*, deterministic_discriminating: bool, t3_checks: dict[str, Any] | None) -> dict[str, Any]:
    t3_checks = t3_checks or {}
    return TypedVerdict(
        artifact_integrity_passed=bool(t3_checks.get("matched_packets_and_schemas", deterministic_discriminating)),
        boundary_passed=True,
        policy_identity_passed=bool(t3_checks.get("response_schema_success_rate_100_percent", deterministic_discriminating)),
        causal_model_contribution_passed=False,
        backend_provenance_passed=bool(t3_checks.get("effective_weak_and_strong_backends_are_distinct", False)),
        candidate_efficacy_passed=False,
        non_regression_passed=False,
        transfer_passed=False,
        reason_codes=("benchmark_nondiscriminating",) if not deterministic_discriminating else (),
    ).to_dict()


def build_phase5_report(
    *,
    corpus_path: str | Path | None = None,
    source_manifest_path: str | Path | None = None,
    results_root: str | Path | None = None,
    verify_hashes: bool = True,
    run_model_matrix: bool = False,
    treatments: list[ModelTreatment] | None = None,
    invoker_factory: Callable[[ModelTreatment], Callable[[dict[str, Any]], Any]] | None = None,
    samples_per_cell: int = DEFAULT_SAMPLES_PER_CELL,
    ablations: tuple[str, ...] = ANNOTATION_ABLATIONS,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    stored, contexts, validation = _load_contexts(
        corpus_path=corpus_path,
        source_manifest_path=source_manifest_path,
        results_root=results_root,
        verify_hashes=verify_hashes,
    )
    null_seam = run_null_branch_seam()
    deterministic = _deterministic_falsifier(contexts)
    model_matrix: dict[str, Any]
    if run_model_matrix:
        if not treatments or invoker_factory is None:
            raise Phase5EvaluationError("run_model_matrix requires treatments and invoker_factory")
        model_matrix = run_model_strength_matrix(
            contexts=contexts,
            treatments=treatments,
            invoker_factory=invoker_factory,
            samples_per_cell=samples_per_cell,
            ablations=ablations,
            progress=progress,
        )
    else:
        model_matrix = {
            "kind": "phase5_model_strength_t3",
            "disposition": (
                "skipped_current_packets_nondiscriminating"
                if not deterministic["benchmark_discriminating"]
                else "not_run"
            ),
            "reason": (
                "Required deterministic controls already reproduce the empirical best on the current corpus."
                if not deterministic["benchmark_discriminating"]
                else "Model matrix was not requested."
            ),
            "checks": {},
        }
    best_positional = deterministic["best_positional_control"]
    t3_checks = dict(model_matrix.get("checks") or {})
    if run_model_matrix:
        primary = model_matrix["primary_hybrid"]
        strong = primary["strong"]
        weak = primary["weak"]
        accuracy_delta_positional = (
            float(strong["empirical_best_accuracy"]) - float(best_positional["empirical_best_accuracy"])
            if strong["empirical_best_accuracy"] is not None
            and best_positional["empirical_best_accuracy"] is not None
            else None
        )
        regret_delta_positional = (
            float(best_positional["mean_empirical_regret"]) - float(strong["mean_empirical_regret"])
            if strong["mean_empirical_regret"] is not None
            and best_positional["mean_empirical_regret"] is not None
            else None
        )
        accuracy_delta_weak = (
            float(strong["empirical_best_accuracy"]) - float(weak["empirical_best_accuracy"])
            if strong["empirical_best_accuracy"] is not None
            and weak["empirical_best_accuracy"] is not None
            else None
        )
        regret_delta_weak = (
            float(weak["mean_empirical_regret"]) - float(strong["mean_empirical_regret"])
            if strong["mean_empirical_regret"] is not None
            and weak["mean_empirical_regret"] is not None
            else None
        )
        t3_checks["strong_beats_best_positional_control"] = bool(
            (accuracy_delta_positional is not None and accuracy_delta_positional >= 0.20)
            or (regret_delta_positional is not None and regret_delta_positional >= 0.20)
        )
        t3_checks["strong_beats_weak"] = bool(
            (accuracy_delta_weak is not None and accuracy_delta_weak >= 0.15)
            or (regret_delta_weak is not None and regret_delta_weak >= 0.15)
        )
        t3_checks["no_required_control_reproduces_claimed_learned_advantage"] = not bool(
            deterministic["deterministic_reproducers"]
        )
        model_matrix["primary_hybrid_comparisons"] = {
            "best_positional_control": best_positional,
            "strong_minus_best_positional_accuracy": accuracy_delta_positional,
            "best_positional_minus_strong_regret": regret_delta_positional,
            "strong_minus_weak_accuracy": accuracy_delta_weak,
            "weak_minus_strong_regret": regret_delta_weak,
        }
        model_matrix["checks"] = t3_checks
    checks = {
        "corpus_validation_passes": validation["passes_gate"],
        "null_branch_seam_passes": null_seam["passes_gate"],
        "all_permutations_enumerated": deterministic["checks"]["all_cases_enumerate_every_permutation"],
        "preferred_action_placed_at_every_position": deterministic["checks"]["preferred_action_placed_at_every_position"],
        "semantic_id_mapping_present": deterministic["checks"]["semantic_ids_unique_per_case"],
        "only_independently_observed_branches_scored": deterministic["checks"]["only_independently_observed_branches_scored"],
        "raw_and_rejection_evidence_available": deterministic["checks"]["raw_and_rejection_evidence_available"],
        "deterministic_controls_do_not_reproduce_empirical_best": deterministic["checks"]["no_nonpositional_control_reproduces_empirical_best"],
        "model_matrix_run_when_requested": not run_model_matrix or bool(model_matrix.get("rows")),
    }
    if run_model_matrix:
        checks.update({
            "model_matrix_temperature_zero": t3_checks.get("temperature_zero") is True,
            "model_matrix_five_samples_per_cell": t3_checks.get("five_samples_per_real_cell") is True,
            "model_matrix_matched_packets_and_schemas": t3_checks.get("matched_packets_and_schemas") is True,
            "model_matrix_schema_provenance_order_invariance": all(
                t3_checks.get(name) is True
                for name in (
                    "response_schema_success_rate_100_percent",
                    "response_provenance_success_rate_100_percent",
                    "semantic_order_invariance_100_percent",
                )
            ),
            "model_matrix_distinct_weak_strong_backends": t3_checks.get("effective_weak_and_strong_backends_are_distinct") is True,
            "strong_empirical_best_gate": t3_checks.get("strong_chooses_empirical_best_at_least_80_percent") is True,
            "strong_vs_positional_gate": t3_checks.get("strong_beats_best_positional_control") is True,
            "strong_vs_weak_gate": t3_checks.get("strong_beats_weak") is True,
        })
    benchmark_nondiscriminating = bool(deterministic["deterministic_reproducers"])
    verdict = (
        "benchmark_nondiscriminating"
        if benchmark_nondiscriminating
        else "rejected_offline"
        if not all(checks.values())
        else "eligible_for_live_canary"
    )
    return {
        "schema_version": PHASE5_SCHEMA_VERSION,
        "kind": "phase5_full_frontier_t3",
        "corpus_name": stored.get("name"),
        "corpus_hash": validation["corpus_hash"],
        "selection_contract": {
            "hybrid": policy.SELECTION_CONTRACT_HYBRID,
            "llm": policy.SELECTION_CONTRACT_LLM,
            "symbolic": policy.SELECTION_CONTRACT_SYMBOLIC,
        },
        "required_treatments": {
            "null": "no-decision seam",
            "weak": REQUIRED_WEAK_MODEL,
            "strong": REQUIRED_STRONG_MODEL,
            "production_gpt": "optional only when response metadata proves a distinct backend",
        },
        "scoring_contract": (
            "Only independently observed branch metrics from the frozen packet corpus are countable. "
            "Unobserved branches remain order/contract diagnostics."
        ),
        "annotation_ablation_contract": {
            "variants": list(ANNOTATION_ABLATIONS),
            "semantic_identity_preserved": True,
            "note": (
                "Only non-semantic reason text and prior-decision context are ablated; effects, target, "
                "preconditions, and operational cost remain part of semantic action identity."
            ),
        },
        "null_seam": null_seam,
        "deterministic_falsifier": deterministic,
        "model_strength_t3": model_matrix,
        "typed_verdict": _default_typed_verdict(
            deterministic_discriminating=deterministic["benchmark_discriminating"],
            t3_checks=t3_checks if run_model_matrix else None,
        ),
        "checks": checks,
        "verdict": verdict,
        "promotion_evidence_authorized": False,
        "next_step": (
            "Proceed to the sealed LAPS family-transfer holdout; current packets are nondiscriminating."
            if verdict == "benchmark_nondiscriminating"
            else "Do not proceed until the failed Phase 5 gates are resolved."
        ),
    }


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise Phase5EvaluationError(f"evaluation route file missing: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def configure_sage_ssl_cert_file(
    *,
    combined_bundle_path: str | Path | None = None,
    custom_bundle_path: str | Path | None = None,
) -> str:
    """Reuse Sage's existing local trust bundle for eval-provider clients.

    `Payload_Type/sage/main.py` already combines system CAs with the gitignored
    custom bundle into `certs/combined-bundle.pem` for the running Sage service.
    The standalone Phase 5 evaluator does not import `main.py`, so it must opt in
    to the same trust material before LangChain/OpenAI constructs its HTTP client.

    Explicit operator configuration wins. When no combined bundle exists, the
    custom bundle is still a valid explicit trust input; the helper does not
    weaken verification or invent a new certificate path.
    """

    configured = str(os.environ.get("SSL_CERT_FILE") or "").strip()
    if configured:
        return configured
    combined = Path(combined_bundle_path) if combined_bundle_path is not None else DEFAULT_SAGE_COMBINED_CA_BUNDLE_PATH
    custom = Path(custom_bundle_path) if custom_bundle_path is not None else DEFAULT_SAGE_CUSTOM_CA_BUNDLE_PATH
    for candidate in (combined, custom):
        if candidate.is_file():
            resolved = str(candidate.resolve())
            os.environ["SSL_CERT_FILE"] = resolved
            return resolved
    return ""


def load_default_treatments(path: str | Path | None = None) -> list[ModelTreatment]:
    route_path = Path(path) if path is not None else DEFAULT_ROUTE_ENV_PATH
    values = _parse_env_file(route_path)
    required = (
        "SAGE_EVAL_PROVIDER",
        "SAGE_EVAL_API_ENDPOINT",
        "SAGE_EVAL_API_KEY",
        "SAGE_EVAL_HAIKU_MODEL",
        "SAGE_EVAL_SONNET_MODEL",
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise Phase5EvaluationError(f"evaluation route file is missing values for: {', '.join(missing)}")
    endpoint = values["SAGE_EVAL_API_ENDPOINT"]
    host = (urlparse(endpoint).hostname or "").strip().casefold()
    if host in {"127.0.0.1", "localhost", "::1"}:
        raise Phase5EvaluationError(
            "weak/strong T3 treatments may not use a loopback proxy because configured labels "
            "would not prove distinct effective backends"
        )
    weak_model = values["SAGE_EVAL_HAIKU_MODEL"]
    strong_model = values["SAGE_EVAL_SONNET_MODEL"]
    if weak_model != REQUIRED_WEAK_MODEL or strong_model != REQUIRED_STRONG_MODEL:
        raise Phase5EvaluationError(
            "default Phase 5 route must use the preregistered Haiku and Sonnet treatment models"
        )
    common = {
        "provider": values["SAGE_EVAL_PROVIDER"].strip().casefold(),
        "api_key": values["SAGE_EVAL_API_KEY"],
        "base_url": endpoint,
        "temperature": 0.0,
    }
    return [
        ModelTreatment(name="weak", model=weak_model, **common),
        ModelTreatment(name="strong", model=strong_model, **common),
    ]


def make_langchain_policy_invoker(treatment: ModelTreatment) -> Callable[[dict[str, Any]], Any]:
    configure_sage_ssl_cert_file()
    from langchain.chat_models import init_chat_model
    from langchain_core.messages import HumanMessage, SystemMessage

    kwargs: dict[str, Any] = {
        "model_provider": treatment.provider,
        "model": treatment.model,
        "temperature": treatment.temperature,
    }
    if treatment.api_key is not None:
        kwargs["api_key"] = treatment.api_key
    if treatment.base_url is not None:
        kwargs["base_url"] = treatment.base_url
    try:
        llm = init_chat_model(**kwargs)
    except TypeError:
        kwargs.pop("temperature", None)
        llm = init_chat_model(**kwargs)

    def invoke(request: dict[str, Any]) -> Any:
        return llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=(
                "Policy request:\n"
                f"{json.dumps(request, indent=2, sort_keys=True, ensure_ascii=True)}\n\n"
                "Return only the JSON object."
            )),
        ])

    return invoke


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "phase5-full-frontier-t3",
        help="run Phase 5 full-frontier permutation falsifiers and optional real-backend T3",
    )
    parser.add_argument("--corpus", default=None, help="optional packet corpus JSON path")
    parser.add_argument("--source-manifest", default=None, help="optional packet source manifest JSON path")
    parser.add_argument("--results-root", default=None, help="optional root containing frozen JSONL artifacts")
    parser.add_argument("--output", default=None, help="optional JSON report path")
    parser.add_argument("--skip-hash-check", action="store_true", help="allow frozen source artifact drift")
    parser.add_argument("--run-model-matrix", action="store_true", help="call the preregistered weak/strong routes")
    parser.add_argument("--route-env", default=str(DEFAULT_ROUTE_ENV_PATH), help="route env for weak/strong T3")
    parser.add_argument("--samples-per-cell", type=int, default=DEFAULT_SAMPLES_PER_CELL)
    parser.set_defaults(func=_cmd_phase5_full_frontier_t3)


def _cmd_phase5_full_frontier_t3(args: Any) -> int:
    try:
        treatments = load_default_treatments(args.route_env) if args.run_model_matrix else None
        report = build_phase5_report(
            corpus_path=args.corpus,
            source_manifest_path=args.source_manifest,
            results_root=args.results_root,
            verify_hashes=not args.skip_hash_check,
            run_model_matrix=bool(args.run_model_matrix),
            treatments=treatments,
            invoker_factory=make_langchain_policy_invoker if args.run_model_matrix else None,
            samples_per_cell=int(args.samples_per_cell),
            progress=(lambda message: print(message, file=sys.stderr, flush=True)) if args.run_model_matrix else None,
        )
    except Phase5EvaluationError as exc:
        print(f"phase5-full-frontier-t3: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"\nVERDICT: {report['verdict']}  "
        f"(deterministic_reproducers={report['deterministic_falsifier']['deterministic_reproducers']})",
        flush=True,
    )
    return 0 if report["verdict"] in {"benchmark_nondiscriminating", "eligible_for_live_canary"} else 1
