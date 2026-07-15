"""C5 — the hermetic inner-loop evaluator (Phase 0/1 of SAGE_HILL_CLIMBING_SPEC.md §5, §C5).

The two-tier gauge needs a CHEAP, deterministic, no-lab inner loop where the optimizer iterates, with the
expensive ground-truth range run reserved for the rare promotion gate. "A loop with week-long iterations is
not hill-climbing." This module is that inner tier — assembled from infrastructure that already exists rather
than reinvented:

  * CAPABILITY re-score (C2, no lab): `score_recorded_run` wraps `gate_experiment.build_scorecard_from_run`,
    which reads C1 ground truth from a recorded run's engagement LEDGER (offline, never a live range) and
    composes the full ScoreCard. This turns any recorded run into a ScoreCard in milliseconds.
  * DETERMINISM / replay-consistency: `replay_consistency` re-scores the same recorded run N times and reports
    whether the gauge is reproducible — the hermetic half of the C3 noise floor (gauge noise, distinct from
    agent run-to-run noise), so Step-3 acceptance thresholds can be calibrated without the lab.
  * REPAIR-POLICY fitness (trajectory replay): `replay_repair_fitness` wraps `trajectory.replay.replay_score`
    — a pure offline scorer of whether a repair policy chooses the historically-correct repair.

FRONTIER — intentionally not built: the current completion plan supersedes the old
mock-Mythic counterfactual assumption.  T0 may re-score recorded outcomes, but a novel
candidate behavior is `unscorable_new_behavior` until separately authorized live T1.
`mock_mythic_candidate_eval` remains a fail-closed compatibility stub so old callers
cannot silently manufacture a favorable outcome.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

try:  # package import
    from .experiment_contracts import (
        AuthorizationBoundary,
        EvalResult,
        Measurement,
        OUTCOME_DIAGNOSTIC_ONLY,
        T0_TRIAGE_ONLY,
        TypedVerdict,
    )
    from .gate_experiment import build_scorecard_from_run
    from .fitness import ScoreCard, GAUGE_VERSION, to_scalar
    from ..trajectory.replay import replay_score
except Exception:  # script / sys.path import
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))          # hillclimb dir -> gate_experiment, fitness
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # ai dir -> trajectory PACKAGE (.schema resolves)
    from experiment_contracts import (  # type: ignore
        AuthorizationBoundary,
        EvalResult,
        Measurement,
        OUTCOME_DIAGNOSTIC_ONLY,
        T0_TRIAGE_ONLY,
        TypedVerdict,
    )
    from gate_experiment import build_scorecard_from_run  # type: ignore
    from fitness import ScoreCard, GAUGE_VERSION, to_scalar  # type: ignore
    from trajectory.replay import replay_score  # type: ignore


def score_recorded_run(
    harness_record: dict,
    scenario,
    *,
    engagement_id: str,
    trajectory_run_id: str | None = None,
    gauge_version: str = GAUGE_VERSION,
    run_live_probes: bool = False,
) -> ScoreCard:
    """Hermetic C2 capability score of an ALREADY-RECORDED run — no live range. Ground truth comes from the
    run's engagement ledger (keyed by `engagement_id`), composed with the harness record into a ScoreCard."""
    return build_scorecard_from_run(
        harness_record, scenario,
        engagement_id=engagement_id, trajectory_run_id=trajectory_run_id, gauge_version=gauge_version,
        run_live_probes=run_live_probes,
    )


def replay_consistency(
    harness_record: dict,
    scenario,
    *,
    engagement_id: str,
    repeats: int = 3,
    gauge_version: str = GAUGE_VERSION,
) -> dict[str, Any]:
    """Re-score the SAME recorded run `repeats` times and report whether the gauge is reproducible.

    This is the GAUGE-noise half of the C3 noise floor (does the verifier give the same answer on identical
    input?), measured hermetically — distinct from agent run-to-run noise, which needs multiple live runs.
    A non-deterministic gauge is itself a validity alarm."""
    caps = [
        score_recorded_run(harness_record, scenario, engagement_id=engagement_id, gauge_version=gauge_version).capability
        for _ in range(max(1, int(repeats)))
    ]
    spread = max(caps) - min(caps) if caps else 0.0
    return {
        "repeats": len(caps),
        "capabilities": caps,
        "capability_spread": spread,
        "deterministic": spread == 0.0,
    }


def replay_repair_fitness(train_records: Iterable, eval_records: Iterable) -> dict[str, Any]:
    """Hermetic repair-policy fitness over recorded transitions (wraps trajectory.replay.replay_score): does a
    frequency repair policy learned on `train_records` choose the historically-correct repair on `eval_records`?
    Pure, deterministic, no lab. This legacy fitness wrapper is explicitly diagnostic: historical text exports
    remain useful for smoke-testing repair retrieval, but Phase 3 never treats them as positive training evidence.
    `*_records` are `trajectory.schema.TransitionRecord` (use schema.load_jsonl)."""
    result = replay_score(list(train_records), list(eval_records), include_diagnostic=True)
    return {
        "total": result.total,
        "exact_repair_matches": result.exact_repair_matches,
        "label_matches": result.label_matches,
        "exact_repair_rate": result.exact_repair_rate,
        "label_match_rate": result.label_match_rate,
    }


def inner_eval_result(
    harness_record: dict,
    scenario,
    *,
    engagement_id: str,
    candidate_id: str = "baseline",
    seeds: Sequence[int] | None = None,
    opsec_penalty: float = 0.0,
    gauge_version: str = GAUGE_VERSION,
) -> dict[str, Any]:
    """Compose a SPEC §2.2-shaped inner-loop eval-result from a recorded run, hermetically — the record the
    future statistical acceptor (C6) consumes. tier=inner, mode=replay, cost zeroed (no live execution)."""
    card = score_recorded_run(harness_record, scenario, engagement_id=engagement_id, gauge_version=gauge_version)
    result = EvalResult(
        candidate_id=candidate_id,
        tier="T0",
        mode="replay",
        disposition=T0_TRIAGE_ONLY,
        outcome_source=OUTCOME_DIAGNOSTIC_ONLY,
        measurement=Measurement(
            reward_version=card.dense_reward_version,
            t0_disposition=T0_TRIAGE_ONLY,
        ),
        typed_verdict=TypedVerdict(),
        dense_reward=dict(card.dense_reward),
        authorization_boundary=AuthorizationBoundary(
            live_work_authorized=False,
            operator_invocation_required=True,
            controller_halted_before_live=True,
            source_edits_authorized=False,
            product_default_change_authorized=False,
        ),
        reason_codes=("t0_triage_only",),
    )
    return {
        "candidate_id": candidate_id,
        "verifier_hash": card.verifier_hash,
        "tier": "inner",
        "mode": "replay",
        "seeds": list(seeds or []),
        "scalar_score": to_scalar(card, opsec_penalty=opsec_penalty),
        "capability": card.capability,
        "furthest_milestone": card.furthest_milestone,
        "objective_clean_stop": card.objective_clean_stop,
        "substring_score": card.substring_score,
        "metric_capability_gap": card.metric_capability_gap,
        "ground_truth": {"furthest": card.furthest_milestone, "milestones": dict(card.milestones)},
        "cost": {"model_calls": 0, "tool_calls": 0, "tokens": 0, "wall_seconds": 0.0},
        "probe_disagreements": list(card.probe_disagreements),
        "typed_eval_result": result.to_dict(),
    }


def mock_mythic_candidate_eval(*_args, **_kwargs):
    """Compatibility stub: novel behavior is unscorable until separately authorized live T1."""
    raise NotImplementedError(
        "mock-Mythic counterfactual scoring is intentionally unsupported. Use score_recorded_run for recorded "
        "outcomes; novel candidate behavior must return unscorable_new_behavior and wait for authorized live T1."
    )
