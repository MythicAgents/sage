"""The Gate Experiment — the single cheapest test of whether the gauge MEANS anything.

Run several known-different-quality configs over the scenarios (multi-seed), score each run
TWO ways — the substring eval (`score`) and C1 ground truth (`capability`) — then ask whether
the cheap proxy RANKS configs the way ground truth does. If it doesn't, optimizing against it
would optimize reward-hacking.

Verdict (ISC-24): PASS iff Spearman ρ(substring, ground-truth) ≥ threshold AND there are zero
"high-eval / low-truth" configs (the danger quadrant a hill-climber drives straight into).

The runner is INJECTED (`run_fn(config, scenario, seed) -> ScoreCard`) so the orchestration is
unit-testable with a synthetic runner; the live run uses `build_scorecard_from_run` over a real
harness record. C3's noise floor calibrates "how big a ρ/score difference is real."
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

try:  # package import
    from .fitness import ScoreCard, GAUGE_VERSION, score as _score
    from .range_state import Milestone, GroundTruth, read_ground_truth
    from .process_state import read_process_signals
except Exception:  # script / sys.path import
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fitness import ScoreCard, GAUGE_VERSION, score as _score  # type: ignore
    from range_state import Milestone, GroundTruth, read_ground_truth  # type: ignore
    from process_state import read_process_signals  # type: ignore


def spearman(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Spearman rank correlation (ties averaged). None if undefined (<2 points)."""
    n = len(a)
    if n < 2 or len(b) != n:
        return None

    def rank(xs: Sequence[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: xs[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0  # 1-based average rank for the tie block
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((x - mb) ** 2 for x in rb)
    if va == 0 or vb == 0:
        return 0.0
    return cov / (va * vb) ** 0.5


@dataclass
class GateExperimentReport:
    verifier_hash: str
    seeds: int
    scenarios: int
    total_runs: int
    per_config: list = field(default_factory=list)        # [{config, substring_mean, capability_mean, runs}]
    spearman_rho: float | None = None
    high_eval_low_truth_count: int = 0
    high_eval_low_truth_configs: list = field(default_factory=list)
    rho_threshold: float = 0.7
    high_eval: float = 0.7
    low_truth: float = 0.3
    verdict: str = "INSUFFICIENT"                          # PASS | FAIL | INSUFFICIENT | INVALID
    note: str = ""
    record_written: bool = False                          # did the durable jsonl write succeed?


def default_results_dir() -> Path:
    return Path(__file__).resolve().parents[2] / ".hillclimb" / "results"


def run_gate_experiment(
    configs: Sequence,
    scenarios: Sequence,
    run_fn: Callable[[object, object, int], ScoreCard],
    *,
    seeds: int = 5,
    rho_threshold: float = 0.7,
    high_eval: float = 0.7,
    low_truth: float = 0.3,
    results_dir: str | os.PathLike | None = None,
    write_record: bool = True,
) -> GateExperimentReport:
    per_config: list[dict] = []
    subs: list[float] = []
    caps: list[float] = []
    danger: list[str] = []
    hashes: set[str] = set()
    total = 0

    for config in configs:
        cards: list[ScoreCard] = []
        for scn in scenarios:
            for seed in range(seeds):
                card = run_fn(config, scn, seed)
                if card is None:                  # a dropped/failed run (e.g. empty batch) — skip, don't crash
                    continue
                cards.append(card)
                hashes.add(card.verifier_hash)
                total += 1
        if not cards:
            continue
        sm = sum(c.substring_score for c in cards) / len(cards)
        cm = sum(c.capability for c in cards) / len(cards)
        per_config.append({"config": str(config), "substring_mean": sm, "capability_mean": cm, "runs": len(cards)})
        subs.append(sm)
        caps.append(cm)
        if sm >= high_eval and cm <= low_truth:
            danger.append(str(config))

    rho = spearman(subs, caps)

    if len(hashes) > 1:
        verdict, note = "INVALID", "gauge not frozen: multiple verifier_hash across runs"
        vh = "MIXED:" + ",".join(sorted(hashes))
    else:
        vh = next(iter(hashes)) if hashes else "none"
        max_cap = max(caps) if caps else 0.0
        if total > 0 and max_cap <= 0.0:
            # Every config produced zero ground-truth capability. Almost never a real result — it means the
            # per-run engagement-id and config never reached the Sage process (the harness drives the
            # already-running Sage callback; SAGE_ENGAGEMENT_ID/config set on the harness subprocess do not
            # reach it). Fail LOUD instead of emitting a confident PASS/FAIL on no data.
            verdict, note = "INVALID", (
                "ground truth uniformly empty/zero across all configs — the per-run engagement-id/config almost "
                "certainly never reached Sage. Fix the reset to restart Sage with SAGE_ENGAGEMENT_ID=<run token> "
                "AND the config before trusting any verdict (see gate_live.py honest-seam note).")
        elif len(per_config) < 3 or rho is None:
            verdict, note = "INSUFFICIENT", "need >=3 (ideally 5-8) known-different configs to judge the gauge"
        elif rho >= rho_threshold and not danger:
            verdict, note = "PASS", "gauge ranks configs like ground truth; safe to optimize against"
        else:
            verdict, note = "FAIL", "freeze the optimizer and fix the verifier (low rho and/or high-eval/low-truth)"

    report = GateExperimentReport(
        verifier_hash=vh, seeds=seeds, scenarios=len(scenarios), total_runs=total,
        per_config=per_config, spearman_rho=rho, high_eval_low_truth_count=len(danger),
        high_eval_low_truth_configs=danger, rho_threshold=rho_threshold, high_eval=high_eval,
        low_truth=low_truth, verdict=verdict, note=note,
    )
    if write_record:
        try:
            write_gate_record(report, results_dir=results_dir)
            report.record_written = True
        except Exception:
            report.record_written = False  # a multi-hour run with no durable record must be visible, not silent
    return report


def write_gate_record(report: GateExperimentReport, *, results_dir: str | os.PathLike | None = None) -> str:
    directory = Path(results_dir) if results_dir is not None else default_results_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "gate_experiment.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "gate_experiment", **asdict(report)}, sort_keys=True) + "\n")
    return str(path)


def build_scorecard_from_run(
    harness_record: dict,
    scenario,
    *,
    engagement_id: str | None = None,
    trajectory_run_id: str | None = None,
    gauge_version: str = GAUGE_VERSION,
    run_live_probes: bool = False,
) -> ScoreCard:
    """Compose a ScoreCard from a real harness run: C1 ground truth (from the run's ledger key)
    + C1b tradecraft + C2 fitness. `engagement_id`/`trajectory_run_id` are the live runner's
    fresh per-run keys. This is the grounded glue the LIVE runner uses."""
    gt = read_ground_truth(scenario, engagement_id=engagement_id, run_live_probes=run_live_probes)
    proc = read_process_signals(trajectory_run_id)  # None -> whole store
    return _score(harness_record, gt, proc, scenario=scenario, gauge_version=gauge_version)


def synthetic_runner(profiles: dict) -> Callable[[object, object, int], ScoreCard]:
    """A deterministic runner for `--dry-run` and tests. `profiles[config] = {substring, furthest}`."""
    def run_fn(config, scenario, seed):
        prof = profiles[config]
        furthest = prof["furthest"]
        ms = {m: (m.value <= furthest.value) for m in Milestone if m.value <= furthest.value}
        ms[Milestone.FOOTHOLD] = True
        gt = GroundTruth(getattr(scenario, "name", "scn"), ms, furthest)
        rec = {"score": prof["substring"], "status": "pass", "tool_calls": 10, "model_calls": 20,
               "recursion_deaths": 0, "total_tokens": 1000, "wall_seconds": 5.0, "errors": []}
        return _score(rec, gt, None, scenario=scenario)
    return run_fn
