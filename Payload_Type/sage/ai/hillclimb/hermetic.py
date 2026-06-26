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

FRONTIER — NOT built here (Phase 3 / GEPA): scoring a NEW candidate's *capability* hermetically requires
re-running the agent (with the candidate prompt) against a MOCKED Mythic that serves recorded/synthetic
observations — `mock_mythic_candidate_eval` marks that seam. The mocking points are proven (construct the
Model, set `MythicTools.client = object()`, patch module-level `mythic_tools.mythic.issue_task` /
`waitfor_for_task_output` to serve deterministic tool results — the pattern test_circuit_breaker.py uses),
but off-trajectory commands (a new prompt issuing actions the recording never saw) and replay ground-truth
are open research problems and must not be shipped half-built — see spec Phase 3.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

try:  # package import
    from .gate_experiment import build_scorecard_from_run
    from .fitness import ScoreCard, GAUGE_VERSION, to_scalar
    from ..trajectory.replay import replay_score
except Exception:  # script / sys.path import
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))          # hillclimb dir -> gate_experiment, fitness
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # ai dir -> trajectory PACKAGE (.schema resolves)
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
    Pure, deterministic, no lab. `*_records` are `trajectory.schema.TransitionRecord` (use schema.load_jsonl)."""
    result = replay_score(list(train_records), list(eval_records))
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
    }


def mock_mythic_candidate_eval(*_args, **_kwargs):
    """FRONTIER (NOT IMPLEMENTED — spec Phase 3 / GEPA): hermetically score a NEW candidate's capability by
    re-running the agent against a mocked Mythic serving recorded/synthetic observations. The mocking seam is
    proven (construct Model; `MythicTools.client = object()`; patch `mythic_tools.mythic.issue_task` /
    `waitfor_for_task_output`), but off-trajectory actions and replay ground-truth are unsolved and must not
    ship half-built. Until then the optimizer's hermetic capability signal comes from `score_recorded_run`
    over recorded runs; new-candidate capability is validated at the (expensive) live promotion gate."""
    raise NotImplementedError(
        "mock-Mythic candidate re-execution is the Phase-3/GEPA frontier — not implemented. Use "
        "score_recorded_run for hermetic scoring of recorded runs; validate new candidates at the live gate."
    )
