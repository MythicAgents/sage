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
import math
import os
import random
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import NormalDist
from typing import Callable, Sequence

try:
    from .experiment_contracts import READINESS_ELIGIBLE, READINESS_NOT_READY
except Exception:  # script / sys.path import
    from experiment_contracts import READINESS_ELIGIBLE, READINESS_NOT_READY  # type: ignore

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
    candidate_surface: str = ""
    smallest_relevant_effect: float | None = None
    target_power: float | None = None
    achieved_power: float | None = None
    measured_noise: float | None = None
    mde: float | None = None
    paired_effect_mean: float | None = None
    paired_effect_ci95: dict[str, float] | None = None
    rank_correlation_ci95: dict[str, float] | None = None
    high_cheap_low_live_inversion_count: int = 0
    readiness_decision: str = READINESS_NOT_READY
    readiness_failed_gates: list[str] = field(default_factory=list)
    substrate_class: str = ""
    t1_reason_code: str = ""
    paired_instance_count: int | None = None
    t0_coverage: float | None = None
    unscorable_new_behavior_rate: float | None = None
    proposer_canary_passed: bool | None = None
    provider_canary_passed: bool | None = None
    reset_automation_passed: bool | None = None
    authorization_boundary_passed: bool | None = None


@dataclass(frozen=True)
class CalibrationProtocolReport:
    candidate_surface: str
    t0_disposition: str
    t1_substrate_status: str
    t2_anchor_present: bool
    smallest_relevant_effect: float | None
    target_power: float | None
    achieved_power: float | None
    measured_noise: float | None
    mde: float | None
    paired_effect_mean: float | None
    paired_effect_ci95: dict[str, float] | None
    spearman_rho: float | None
    rank_correlation_ci95: dict[str, float] | None
    inversion_count: int
    readiness_decision: str
    failed_gates: tuple[str, ...] = field(default_factory=tuple)
    ranking_authorized: bool = False
    substrate_class: str = ""
    t1_reason_code: str = ""
    paired_instance_count: int | None = None
    t0_coverage: float | None = None
    unscorable_new_behavior_rate: float | None = None
    proposer_canary_passed: bool | None = None
    provider_canary_passed: bool | None = None
    reset_automation_passed: bool | None = None
    authorization_boundary_passed: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PairedSensitivityReport:
    """Paired design sensitivity over candidate-minus-incumbent outcomes.

    This is deliberately a measurement report, not an acceptance authority.  The
    readiness gate consumes its fields and remains fail-closed when the design is
    underpowered or has too few paired instances.
    """

    paired_instance_count: int
    smallest_relevant_effect: float | None
    target_power: float
    alpha: float
    paired_effect_mean: float | None
    measured_noise: float | None
    standard_error: float | None
    mde: float | None
    achieved_power: float | None
    paired_effect_ci95: dict[str, float] | None
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def powered(self) -> bool:
        return bool(
            not self.reason_codes
            and self.smallest_relevant_effect is not None
            and self.mde is not None
            and self.achieved_power is not None
            and self.mde <= self.smallest_relevant_effect
            and self.achieved_power >= self.target_power
        )


def _two_sided_normal_power(effect: float, standard_error: float, *, alpha: float) -> float:
    if standard_error == 0.0:
        return 1.0 if abs(effect) > 0.0 else 0.0
    normal = NormalDist()
    z_alpha = normal.inv_cdf(1.0 - alpha / 2.0)
    noncentrality = abs(effect) / standard_error
    return (
        1.0
        - normal.cdf(z_alpha - noncentrality)
        + normal.cdf(-z_alpha - noncentrality)
    )


def paired_sensitivity_report(
    candidate_scores: Sequence[float],
    incumbent_scores: Sequence[float],
    *,
    smallest_relevant_effect: float | None,
    target_power: float = 0.8,
    alpha: float = 0.05,
) -> PairedSensitivityReport:
    """Calculate paired MDE, achieved power, and a normal-approximation effect CI.

    The calculation is intentionally simple and preregisterable: use paired
    candidate-minus-incumbent deltas, sample standard deviation of those deltas,
    two-sided alpha, and normal-approximation power.  It is suitable for the
    Phase 4 instrument and explicitly reports when the design is too small to
    support a sensitivity claim.
    """

    if len(candidate_scores) != len(incumbent_scores):
        raise ValueError("candidate and incumbent score vectors must have the same length")
    if not 0.0 < float(alpha) < 1.0:
        raise ValueError("alpha must be between 0 and 1")
    if not 0.0 < float(target_power) < 1.0:
        raise ValueError("target_power must be between 0 and 1")
    n = len(candidate_scores)
    differences = [float(candidate) - float(incumbent) for candidate, incumbent in zip(candidate_scores, incumbent_scores)]
    reasons: list[str] = []
    if n < 2:
        reasons.append("insufficient_paired_instances")
    if smallest_relevant_effect is None or float(smallest_relevant_effect) < 0.0:
        reasons.append("missing_smallest_relevant_effect")
    mean_effect = statistics.mean(differences) if differences else None
    measured_noise = statistics.stdev(differences) if n >= 2 else None
    standard_error = (measured_noise / math.sqrt(n)) if measured_noise is not None else None
    normal = NormalDist()
    z_alpha = normal.inv_cdf(1.0 - float(alpha) / 2.0)
    z_power = normal.inv_cdf(float(target_power))
    mde = (
        (z_alpha + z_power) * standard_error
        if standard_error is not None
        else None
    )
    achieved_power = (
        _two_sided_normal_power(float(smallest_relevant_effect), standard_error, alpha=float(alpha))
        if smallest_relevant_effect is not None and standard_error is not None
        else None
    )
    effect_ci95 = (
        {
            "lower": mean_effect - z_alpha * standard_error,
            "upper": mean_effect + z_alpha * standard_error,
        }
        if mean_effect is not None and standard_error is not None
        else None
    )
    return PairedSensitivityReport(
        paired_instance_count=n,
        smallest_relevant_effect=smallest_relevant_effect,
        target_power=float(target_power),
        alpha=float(alpha),
        paired_effect_mean=mean_effect,
        measured_noise=measured_noise,
        standard_error=standard_error,
        mde=mde,
        achieved_power=achieved_power,
        paired_effect_ci95=effect_ci95,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def high_cheap_low_live_inversions(cheap_scores: Sequence[float], live_scores: Sequence[float]) -> int:
    """Count pairwise rank inversions where cheap ranks higher but live ranks lower."""

    if len(cheap_scores) != len(live_scores):
        raise ValueError("cheap and live score vectors must have the same length")
    inversions = 0
    for left in range(len(cheap_scores)):
        for right in range(left + 1, len(cheap_scores)):
            cheap_delta = cheap_scores[left] - cheap_scores[right]
            live_delta = live_scores[left] - live_scores[right]
            if cheap_delta == 0 or live_delta == 0:
                continue
            if cheap_delta * live_delta < 0:
                inversions += 1
    return inversions


def bootstrap_spearman_ci(
    cheap_scores: Sequence[float],
    live_scores: Sequence[float],
    *,
    samples: int = 1000,
    seed: int = 0,
) -> dict[str, float] | None:
    """Return a deterministic percentile bootstrap CI for rho, or None when undefined."""

    if len(cheap_scores) != len(live_scores) or len(cheap_scores) < 3:
        return None
    rng = random.Random(seed)
    values: list[float] = []
    n = len(cheap_scores)
    for _ in range(max(1, int(samples))):
        indices = [rng.randrange(n) for _ in range(n)]
        rho = spearman([cheap_scores[index] for index in indices], [live_scores[index] for index in indices])
        if rho is not None:
            values.append(float(rho))
    if not values:
        return None
    values.sort()

    def percentile(q: float) -> float:
        index = min(len(values) - 1, max(0, int(round((len(values) - 1) * q))))
        return values[index]

    return {"lower": percentile(0.025), "upper": percentile(0.975)}


def run_calibration_protocol(
    *,
    candidate_surface: str,
    cheap_scores: Sequence[float],
    t2_scores: Sequence[float],
    t0_disposition: str,
    t1_substrate_status: str,
    t2_anchor_present: bool,
    smallest_relevant_effect: float | None,
    target_power: float | None,
    achieved_power: float | None = None,
    measured_noise: float | None,
    mde: float | None,
    rho_threshold: float = 0.7,
    bootstrap_samples: int = 1000,
    substrate_class: str = "",
    t1_reason_code: str = "",
    paired_instance_count: int | None = None,
    t0_coverage: float | None = None,
    unscorable_new_behavior_rate: float | None = None,
    proposer_canary_passed: bool | None = None,
    provider_canary_passed: bool | None = None,
    reset_automation_passed: bool | None = None,
    authorization_boundary_passed: bool | None = None,
    paired_effect_mean: float | None = None,
    paired_effect_ci95: dict[str, float] | None = None,
    paired_candidate_scores: Sequence[float] | None = None,
    paired_incumbent_scores: Sequence[float] | None = None,
    alpha: float = 0.05,
) -> CalibrationProtocolReport:
    """Instrument the T0/T1/T2 readiness gate without authorizing a campaign."""

    surface = str(candidate_surface or "").strip()
    rho = spearman(cheap_scores, t2_scores)
    ci = bootstrap_spearman_ci(
        cheap_scores,
        t2_scores,
        samples=bootstrap_samples,
    )
    inversions = high_cheap_low_live_inversions(cheap_scores, t2_scores)
    sensitivity: PairedSensitivityReport | None = None
    if paired_candidate_scores is not None or paired_incumbent_scores is not None:
        sensitivity = paired_sensitivity_report(
            paired_candidate_scores or (),
            paired_incumbent_scores or (),
            smallest_relevant_effect=smallest_relevant_effect,
            target_power=float(target_power if target_power is not None else 0.8),
            alpha=alpha,
        )
        target_power = sensitivity.target_power
        achieved_power = sensitivity.achieved_power
        measured_noise = sensitivity.measured_noise
        mde = sensitivity.mde
        paired_effect_mean = sensitivity.paired_effect_mean
        paired_effect_ci95 = sensitivity.paired_effect_ci95
        paired_instance_count = sensitivity.paired_instance_count
    failed: list[str] = []
    if not surface:
        failed.append("missing_candidate_surface")
    if str(t0_disposition or "") not in {"survived_triage", "triage_only", "unscorable_new_behavior"}:
        failed.append("t0_not_instrumented")
    if (
        str(t0_disposition or "") == "unscorable_new_behavior"
        and (str(t1_substrate_status or "") != "verified" or not t2_anchor_present)
    ):
        failed.append("unscorable_new_behavior")
    if str(t1_substrate_status or "") != "verified":
        failed.append(str(t1_substrate_status or "t1_substrate_unavailable"))
    if not t2_anchor_present:
        failed.append("t2_anchor_missing")
    if (
        smallest_relevant_effect is None
        or target_power is None
        or achieved_power is None
        or measured_noise is None
        or mde is None
    ):
        failed.append("insufficient_statistical_power")
    else:
        if (
            float(target_power) < 0.8
            or float(achieved_power) < float(target_power)
            or float(mde) > float(smallest_relevant_effect)
        ):
            failed.append("insufficient_statistical_power")
    if sensitivity is not None and sensitivity.reason_codes:
        failed.append("insufficient_statistical_power")
    if rho is None or rho < float(rho_threshold):
        failed.append("rank_correlation_below_threshold")
    if ci is None or float(ci["lower"]) <= 0.0:
        failed.append("rank_correlation_lower_bound_not_positive")
    if inversions:
        failed.append("high_cheap_low_live_inversion")
    decision = READINESS_ELIGIBLE if not failed else READINESS_NOT_READY
    return CalibrationProtocolReport(
        candidate_surface=surface,
        t0_disposition=str(t0_disposition or ""),
        t1_substrate_status=str(t1_substrate_status or ""),
        t2_anchor_present=bool(t2_anchor_present),
        smallest_relevant_effect=smallest_relevant_effect,
        target_power=target_power,
        achieved_power=achieved_power,
        measured_noise=measured_noise,
        mde=mde,
        paired_effect_mean=paired_effect_mean,
        paired_effect_ci95=paired_effect_ci95,
        spearman_rho=rho,
        rank_correlation_ci95=ci,
        inversion_count=inversions,
        readiness_decision=decision,
        failed_gates=tuple(dict.fromkeys(failed)),
        ranking_authorized=decision == READINESS_ELIGIBLE,
        substrate_class=str(substrate_class or ""),
        t1_reason_code=str(t1_reason_code or ""),
        paired_instance_count=paired_instance_count,
        t0_coverage=t0_coverage,
        unscorable_new_behavior_rate=unscorable_new_behavior_rate,
        proposer_canary_passed=proposer_canary_passed,
        provider_canary_passed=provider_canary_passed,
        reset_automation_passed=reset_automation_passed,
        authorization_boundary_passed=authorization_boundary_passed,
    )


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
    candidate_surface: str = "",
    t0_disposition: str = "",
    t1_substrate_status: str = "",
    t2_anchor_present: bool = False,
    smallest_relevant_effect: float | None = None,
    target_power: float | None = None,
    achieved_power: float | None = None,
    measured_noise: float | None = None,
    mde: float | None = None,
    bootstrap_samples: int = 1000,
    substrate_class: str = "",
    t1_reason_code: str = "",
    paired_instance_count: int | None = None,
    t0_coverage: float | None = None,
    unscorable_new_behavior_rate: float | None = None,
    proposer_canary_passed: bool | None = None,
    provider_canary_passed: bool | None = None,
    reset_automation_passed: bool | None = None,
    authorization_boundary_passed: bool | None = None,
    paired_effect_mean: float | None = None,
    paired_effect_ci95: dict[str, float] | None = None,
    paired_candidate_scores: Sequence[float] | None = None,
    paired_incumbent_scores: Sequence[float] | None = None,
    alpha: float = 0.05,
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

    calibration = run_calibration_protocol(
        candidate_surface=candidate_surface,
        cheap_scores=subs,
        t2_scores=caps,
        t0_disposition=t0_disposition,
        t1_substrate_status=t1_substrate_status,
        t2_anchor_present=t2_anchor_present,
        smallest_relevant_effect=smallest_relevant_effect,
        target_power=target_power,
        achieved_power=achieved_power,
        measured_noise=measured_noise,
        mde=mde,
        rho_threshold=rho_threshold,
        bootstrap_samples=bootstrap_samples,
        substrate_class=substrate_class,
        t1_reason_code=t1_reason_code,
        paired_instance_count=paired_instance_count,
        t0_coverage=t0_coverage,
        unscorable_new_behavior_rate=unscorable_new_behavior_rate,
        proposer_canary_passed=proposer_canary_passed,
        provider_canary_passed=provider_canary_passed,
        reset_automation_passed=reset_automation_passed,
        authorization_boundary_passed=authorization_boundary_passed,
        paired_effect_mean=paired_effect_mean,
        paired_effect_ci95=paired_effect_ci95,
        paired_candidate_scores=paired_candidate_scores,
        paired_incumbent_scores=paired_incumbent_scores,
        alpha=alpha,
    )
    report = GateExperimentReport(
        verifier_hash=vh, seeds=seeds, scenarios=len(scenarios), total_runs=total,
        per_config=per_config, spearman_rho=rho, high_eval_low_truth_count=len(danger),
        high_eval_low_truth_configs=danger, rho_threshold=rho_threshold, high_eval=high_eval,
        low_truth=low_truth, verdict=verdict, note=note,
        candidate_surface=calibration.candidate_surface,
        smallest_relevant_effect=calibration.smallest_relevant_effect,
        target_power=calibration.target_power,
        achieved_power=calibration.achieved_power,
        measured_noise=calibration.measured_noise,
        mde=calibration.mde,
        paired_effect_mean=calibration.paired_effect_mean,
        paired_effect_ci95=calibration.paired_effect_ci95,
        rank_correlation_ci95=calibration.rank_correlation_ci95,
        high_cheap_low_live_inversion_count=calibration.inversion_count,
        readiness_decision=calibration.readiness_decision,
        readiness_failed_gates=list(calibration.failed_gates),
        substrate_class=calibration.substrate_class,
        t1_reason_code=calibration.t1_reason_code,
        paired_instance_count=calibration.paired_instance_count,
        t0_coverage=calibration.t0_coverage,
        unscorable_new_behavior_rate=calibration.unscorable_new_behavior_rate,
        proposer_canary_passed=calibration.proposer_canary_passed,
        provider_canary_passed=calibration.provider_canary_passed,
        reset_automation_passed=calibration.reset_automation_passed,
        authorization_boundary_passed=calibration.authorization_boundary_passed,
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
