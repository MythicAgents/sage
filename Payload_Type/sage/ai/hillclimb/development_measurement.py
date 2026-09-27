"""Offline finite-sample diagnostics for human-guided evaluation.

These reducers consume measurements, not proof. Source admission, split access,
independent outcome verification and human disposition remain separate. Nothing
here issues a task, opens a protected corpus or authorizes a promotion.
"""
from __future__ import annotations

import math
from statistics import NormalDist, mean
from typing import Any, Mapping, Sequence

from . import composite_gauge, composite_source_adapter
from .gate_experiment import bootstrap_spearman_ci, spearman
from .tradecraft_evidence import canonical_sha256

VERSION = "m3-measurement-v1"


def _probability(value: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1:
        raise ValueError(f"{name} must be finite and strictly between zero and one")
    return float(value)


def _count(value: int, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _binomial_cdf(k: int, n: int, p: float) -> float:
    if p <= 0:
        return 1.0
    if p >= 1:
        return float(k >= n)
    terms = [math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1)
             + j * math.log(p) + (n - j) * math.log1p(-p) for j in range(k + 1)]
    peak = max(terms)
    return min(1.0, math.exp(peak) * math.fsum(math.exp(x - peak) for x in terms))


def binomial_upper(failures: int, trials: int, *, alpha: float) -> float | None:
    """Exact one-sided Clopper-Pearson upper bound, including zero failures."""
    _count(trials, "trials")
    _count(failures, "failures")
    _probability(alpha, "alpha")
    if failures > trials:
        raise ValueError("failures exceeds trials")
    if trials == 0:
        return None
    if failures == trials:
        return 1.0
    if failures == 0:
        return -math.expm1(math.log(alpha) / trials)
    lo, hi = 0.0, 1.0
    for _ in range(70):
        middle = (lo + hi) / 2
        if _binomial_cdf(failures, trials, middle) > alpha:
            lo = middle
        else:
            hi = middle
    return hi


def minimum_zero_failure_trials(*, tolerance: float, alpha: float, comparisons: int = 1) -> int:
    """Smallest n whose multiplicity-adjusted upper bound is strictly below tolerance."""
    _probability(tolerance, "tolerance")
    _probability(alpha, "alpha")
    _count(comparisons, "comparisons", minimum=1)
    return math.floor(math.log(alpha / comparisons) / math.log1p(-tolerance)) + 1


def _interval(successes: int, n: int, alpha: float) -> dict[str, float]:
    upper = binomial_upper(successes, n, alpha=alpha / 2)
    other = binomial_upper(n - successes, n, alpha=alpha / 2)
    return {"lower": 0.0 if other is None else 1 - other,
            "upper": 1.0 if upper is None else upper}


def paired_binary_precision(left: Sequence[bool], right: Sequence[bool], *,
                            practical_delta: float, alpha: float = .05,
                            target_power: float = .8) -> dict[str, Any]:
    """Bound paired rate differences using the two discordant-cell probabilities.

    Bonferroni exact binomial intervals give a conservative finite-sample effect
    interval. The MDE is a normal design diagnostic using an exact upper bound on
    discordance (a variance upper bound), never zero empirical variance alone.
    It is not a claim of exact achieved power.
    """
    _probability(practical_delta, "practical_delta")
    _probability(alpha, "alpha")
    _probability(target_power, "target_power")
    if target_power <= .5:
        raise ValueError("target_power must exceed .5")
    if len(left) != len(right) or any(type(x) is not bool for x in (*left, *right)):
        raise ValueError("paired outcomes must be equal-length Boolean sequences")
    n = len(left)
    positive = sum(a and not b for a, b in zip(left, right))
    negative = sum(b and not a for a, b in zip(left, right))
    # Half of alpha covers the effect interval; half covers discordance. Their
    # joint event supplies the variance bound used for the design diagnostic.
    positive_ci = _interval(positive, n, alpha / 4)
    negative_ci = _interval(negative, n, alpha / 4)
    effect_ci = {"lower": positive_ci["lower"] - negative_ci["upper"],
                 "upper": positive_ci["upper"] - negative_ci["lower"]}
    effect = (positive - negative) / n if n else None
    discordance_upper = binomial_upper(positive + negative, n, alpha=alpha / 2)
    minimum_absolute_effect = (0.0 if effect_ci["lower"] <= 0 <= effect_ci["upper"]
                               else min(abs(x) for x in effect_ci.values()))
    variance_upper = (max(0., discordance_upper - minimum_absolute_effect ** 2)
                      if discordance_upper is not None else None)
    z = NormalDist().inv_cdf(1 - alpha / 2) + NormalDist().inv_cdf(target_power)
    mde = z * math.sqrt(variance_upper / n) if n else None
    radius = max(effect - effect_ci["lower"], effect_ci["upper"] - effect) if n else None
    saturated = bool(n and len(set(left)) == 1 and len(set(right)) == 1)
    reasons = []
    if n < 5:
        reasons.append("fewer_than_five_pairs")
    if saturated:
        reasons.append("saturated_binary_arms")
    if mde is None or mde > practical_delta:
        reasons.append("mde_not_actionable")
    if radius is None or radius > practical_delta:
        reasons.append("exact_interval_not_actionable")
    return {"paired_n": n, "positive_discordance": positive, "negative_discordance": negative,
            "effect": effect, "effect_interval": effect_ci, "alpha": alpha,
            "sample_variance": ((positive + negative - n * effect ** 2) / (n - 1)
                                if n > 1 else None),
            "discordance_upper": discordance_upper, "variance_upper": variance_upper,
            "conservative_planning_mde": mde,
            "mde_method": "normal_design_with_joint_exact_variance_upper_bound",
            "practical_delta": practical_delta, "target_power": target_power,
            "informative": not reasons, "reason_codes": reasons}


def anchor_reliability(first: Mapping[str, Sequence[float]], second: Mapping[str, Sequence[float]], *,
                       minimum_rho: float, minimum_ci_lower: float,
                       bootstrap_samples: int = 2000) -> dict[str, Any]:
    """Diagnostic rank stability across independently collected live batches.

    Caller must establish batch independence and temporal order from the ledger.
    This function cannot authenticate them from arrays.
    """
    _probability(minimum_rho, "minimum_rho")
    _probability(minimum_ci_lower, "minimum_ci_lower")
    _count(bootstrap_samples, "bootstrap_samples", minimum=100)
    if set(first) != set(second) or len(first) < 5:
        raise ValueError("anchor batches must contain the same >=5 working variants")
    names = sorted(first)
    for batch in (first, second):
        for name in names:
            values = batch[name]
            if len(values) < 2 or any(type(x) not in (int, float) or not math.isfinite(x)
                                       or not 0 <= x <= 1 for x in values):
                raise ValueError("anchor variants require >=2 finite scores in [0,1]")
    a, b = [mean(first[x]) for x in names], [mean(second[x]) for x in names]
    rho = spearman(a, b)
    ci = bootstrap_spearman_ci(a, b, samples=bootstrap_samples)
    return {"rho": rho, "bootstrap_interval": ci, "variant_ids": names,
            "statistically_stable": rho is not None and ci is not None
                and rho >= minimum_rho and ci["lower"] >= minimum_ci_lower,
            "requires_independent_batches_and_prior_ledger_admission": True}


def decision_concordance(trials: Sequence[Mapping[str, Any]], *,
                         tolerance: float, family_alpha: float, comparisons: int,
                         anchor_admitted_before_decisions: bool) -> dict[str, Any]:
    """One predeclared comparison; each row is an independent decision block.

    Several pairwise comparisons from a common live batch are not independent
    trials. Inconclusive live references conservatively count against a proposed
    promotion. A gauge that never promotes cannot claim a measured false-promotion
    rate. The caller supplies the prospectively declared total comparison count.
    """
    _probability(tolerance, "tolerance")
    _probability(family_alpha, "family_alpha")
    _count(comparisons, "comparisons", minimum=1)
    if anchor_admitted_before_decisions is not True:
        raise ValueError("independent live anchor must be admitted before decision concordance")
    ids, promoted, false, unresolved, disagreements = set(), 0, 0, 0, 0
    for row in trials:
        if set(row) != {"independent_block_id", "gauge_decision", "live_decision"}:
            raise ValueError("decision trial fields differ from schema")
        key = row["independent_block_id"]
        if not isinstance(key, str) or not key.strip() or key in ids:
            raise ValueError("independent block IDs must be non-empty and unique")
        ids.add(key)
        gauge, live = row["gauge_decision"], row["live_decision"]
        if gauge not in {"promote", "reject", "inconclusive"} or live not in {"promote", "reject", "inconclusive"}:
            raise ValueError("unknown decision")
        disagreements += gauge != live
        if gauge == "promote":
            promoted += 1
            false += live == "reject"
            unresolved += live == "inconclusive"
    alpha = family_alpha / comparisons
    upper = binomial_upper(false + unresolved, promoted, alpha=alpha)
    # The conditional bound above is descriptive. The two decision-opportunity
    # metrics share the requested family error budget; a zero-promotion comparison
    # can be honestly inconclusive without fabricating a conditional estimate.
    error_alpha = alpha / 2
    opportunity_upper = binomial_upper(false + unresolved, len(ids), alpha=error_alpha)
    disagreement_upper = binomial_upper(disagreements, len(ids), alpha=error_alpha)
    return {"independent_blocks": len(ids), "proposed_promotions": promoted,
            "false_promotions": false, "unresolved_promotions": unresolved,
            "decision_disagreements": disagreements, "adjusted_alpha": alpha,
            "false_promotion_upper_bound": upper, "tolerance": tolerance,
            "false_promotion_bound_passed": upper is not None and upper < tolerance,
            "opportunity_error_adjusted_alpha": error_alpha,
            "unsupported_promotion_per_opportunity_upper_bound": opportunity_upper,
            "decision_disagreement_upper_bound": disagreement_upper,
            "decision_error_bounds_passed": opportunity_upper is not None
                and disagreement_upper is not None
                and opportunity_upper < tolerance and disagreement_upper < tolerance,
            "requires_independently_resolved_improvement_and_gauge_sensitivity": True,
            "minimum_zero_failure_promotions": minimum_zero_failure_trials(
                tolerance=tolerance, alpha=family_alpha, comparisons=comparisons),
            "requires_source_admission_and_near_delta_coverage": True}


def stratified_binary_precision(strata: Mapping[str, Mapping[str, Sequence[bool]]], *,
                                practical_delta: float, alpha: float = .05,
                                target_power: float = .8) -> dict[str, Any]:
    """Equal-family inference without treating different case rates as IID.

    Joint exact intervals cover each stratum; weighted bounds then cover the
    prospectively equal-family estimand. Independence across paired repetitions
    and strata must be established separately from these arrays.
    """
    _probability(alpha, "alpha")
    if not strata or any(not isinstance(key, str) or not key for key in strata):
        raise ValueError("non-empty named strata are required")
    if any(set(row) != {"left", "right"} for row in strata.values()):
        raise ValueError("each stratum requires exactly left and right paired outcomes")
    reports = {key: paired_binary_precision(row["left"], row["right"],
                  practical_delta=practical_delta, alpha=alpha / len(strata),
                  target_power=target_power) for key, row in strata.items()}
    if any(row["paired_n"] == 0 for row in reports.values()):
        raise ValueError("empty strata cannot estimate the equal-family effect")
    count = len(reports)
    effect = mean(row["effect"] for row in reports.values())
    interval = {side: mean(row["effect_interval"][side] for row in reports.values())
                for side in ("lower", "upper")}
    variance_of_mean_upper = sum(row["variance_upper"] / row["paired_n"]
                                 for row in reports.values()) / count ** 2
    z = NormalDist().inv_cdf(1 - alpha / 2) + NormalDist().inv_cdf(target_power)
    mde = z * math.sqrt(variance_of_mean_upper)
    radius = max(effect - interval["lower"], interval["upper"] - effect)
    reasons = []
    if any(row["paired_n"] < 5 for row in reports.values()):
        reasons.append("fewer_than_five_pairs_in_a_stratum")
    if all("saturated_binary_arms" in row["reason_codes"] for row in reports.values()):
        reasons.append("saturated_binary_arms_in_all_strata")
    if mde > practical_delta:
        reasons.append("mde_not_actionable")
    if radius > practical_delta:
        reasons.append("exact_interval_not_actionable")
    return {"estimand": "equal_stratum_paired_probability_difference", "strata": reports,
            "effect": effect, "effect_interval": interval, "alpha": alpha,
            "variance_of_mean_upper": variance_of_mean_upper,
            "conservative_planning_mde": mde, "informative": not reasons,
            "reason_codes": reasons, "requires_independent_source_admission": True}


def gradient_fidelity(pairs: Sequence[Mapping[str, Any]], *, practical_delta: float,
                      maximum_effect_error: float, family_alpha: float) -> dict[str, Any]:
    """Paired effect diagnostic, with simultaneous finite-sample intervals.

    Each pair supplies independent block differences in [-1,1]. Gauge and live
    effects share the same prospectively normalized scale. No post-hoc rescaling.
    Binary outcome differences use exact discordant-cell bounds; other bounded
    scores use Hoeffding bounds. Near-delta evidence requires the entire live
    interval within +/-2*min_delta.
    """
    _probability(practical_delta, "practical_delta")
    _probability(maximum_effect_error, "maximum_effect_error")
    _probability(family_alpha, "family_alpha")
    if not pairs:
        raise ValueError("at least one prospectively adjacent pair is required")
    results, names = [], set()
    for pair in pairs:
        if set(pair) != {"pair_id", "gauge_deltas", "live_deltas"}:
            raise ValueError("gradient pair fields differ from schema")
        name, gauge, live = pair["pair_id"], pair["gauge_deltas"], pair["live_deltas"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("gradient pair IDs must be unique")
        names.add(name)
        if len(gauge) != len(live) or len(live) < 2 or any(
            type(x) not in (int, float) or not math.isfinite(x) or not -1 <= x <= 1
            for x in (*gauge, *live)
        ):
            raise ValueError("gradient differences must be paired finite scores in [-1,1]")
        adjusted = family_alpha / (2 * len(pairs))
        def effect_interval(values):
            if all(x in (-1, 0, 1) for x in values):
                pos = _interval(sum(x == 1 for x in values), len(values), adjusted / 2)
                neg = _interval(sum(x == -1 for x in values), len(values), adjusted / 2)
                return {"lower": pos["lower"] - neg["upper"],
                        "upper": pos["upper"] - neg["lower"]}
            radius = math.sqrt(2 * math.log(2 / adjusted) / len(values))
            return {"lower": max(-1., mean(values) - radius),
                    "upper": min(1., mean(values) + radius)}
        g, l = mean(gauge), mean(live)
        interval, gauge_interval = effect_interval(live), effect_interval(gauge)
        error_upper = max(abs(gauge_interval["lower"] - interval["upper"]),
                          abs(gauge_interval["upper"] - interval["lower"]))
        results.append({"pair_id": name, "gauge_effect": g, "live_effect": l,
                        "live_interval": interval, "gauge_interval": gauge_interval,
                        "effect_error_upper": error_upper,
                        "near_delta_supported": max(abs(x) for x in interval.values()) <= 2 * practical_delta,
                        "effect_fidelity_supported": error_upper <= maximum_effect_error,
                        "flat_over_steep": abs(l) >= practical_delta and abs(g) < .25 * abs(l)})
    order_checks = []
    for index, a in enumerate(results):
        for b in results[index + 1:]:
            live_gap = a["live_effect"] - b["live_effect"]
            gauge_gap = a["gauge_effect"] - b["gauge_effect"]
            reversed_order = live_gap * gauge_gap < 0
            live_separated = (a["live_interval"]["lower"] > b["live_interval"]["upper"]
                              or b["live_interval"]["lower"] > a["live_interval"]["upper"])
            gauge_separated_same_order = (
                a["gauge_interval"]["lower"] > b["gauge_interval"]["upper"] if live_gap > 0
                else b["gauge_interval"]["lower"] > a["gauge_interval"]["upper"]
            )
            order_checks.append({"pairs": [a["pair_id"], b["pair_id"]],
                                 "observed_order_reversed": reversed_order,
                                 "live_order_resolved": live_separated,
                                 "ordering_supported": not reversed_order and (
                                     not live_separated or gauge_separated_same_order)})
    return {"pairs": results, "effect_ordering": order_checks,
            "near_delta_coverage": any(x["near_delta_supported"] for x in results),
            "gradient_supported": all(x["effect_fidelity_supported"] and not x["flat_over_steep"] for x in results)
                and all(x["ordering_supported"] for x in order_checks),
            "method": "simultaneous_exact_binary_or_bounded_mean_intervals_no_posthoc_rescaling"}


def compose_source_measurement(bundles: Sequence[Mapping[str, Any]], *, repo_root,
                               report_arguments: Mapping[str, Any],
                               uncertainty_alpha: float = .05) -> dict[str, Any]:
    """Exercise explicit source/scorer version admission and finite-sample comparison.

    Rejected inputs remain listed, and prevent an informative complete-batch claim.
    This never invents missing evidence or rewrites the legacy capability report.
    """
    admissions = [composite_source_adapter.admit_composite_source(x, repo_root=repo_root) for x in bundles]
    records = [x["composite_record"] for x in admissions if x["composite_record"] is not None]
    rejected = [i for i, row in enumerate(admissions) if row["composite_record"] is None]
    report, precision = None, None
    if records and not rejected:
        report = composite_gauge.build_paired_report(records, **report_arguments)
        left, right = report["evaluated_arm"], report["baseline_arm"]
        outcomes = report["pair_outcomes"]
        a = [x["rows"][left]["safe_success"] for x in outcomes]
        b = [x["rows"][right]["safe_success"] for x in outcomes]
        if all(type(x) is bool for x in (*a, *b)):
            precision = paired_binary_precision(a, b,
                practical_delta=report_arguments["smallest_relevant_effects"]["safe_success"],
                alpha=uncertainty_alpha)
    payload = {"schema": VERSION, "admissions": admissions, "rejected_indices": rejected,
               "legacy_composite_report": report, "finite_sample_precision": precision,
               "measurement_informative": bool(precision and precision["informative"]),
               "evidence_admission_required": True, "promotion_authorized": False,
               "runtime_application_authorized": False, "m3_complete": False}
    return {**payload, "report_hash": canonical_sha256(payload)}


def sample_plan(*, practical_delta: float, tolerance: float, family_alpha: float,
                comparisons: int, target_power: float = .8) -> dict[str, Any]:
    """Prospective worst-variance planning, not an allocation or a power guarantee."""
    _probability(practical_delta, "practical_delta")
    _probability(target_power, "target_power")
    if target_power <= .5:
        raise ValueError("target_power must exceed .5")
    minimum = minimum_zero_failure_trials(tolerance=tolerance, alpha=family_alpha,
                                          comparisons=comparisons)
    alpha = family_alpha / comparisons
    z = NormalDist().inv_cdf(1 - alpha / 2) + NormalDist().inv_cdf(target_power)
    return {"schema": VERSION, "practical_delta": practical_delta, "false_promotion_tolerance": tolerance,
            "family_alpha": family_alpha, "comparison_count": comparisons,
            "minimum_independent_zero_false_promotions_per_comparison": minimum,
            "worst_variance_normal_design_pairs": math.ceil((z / practical_delta) ** 2),
            "planning_approximation_only": True, "live_authorized": False,
            "protected_access_authorized": False, "composite_execution_authorized": False}


def main(argv=None) -> int:
    """JSON-in/JSON-out local-only interface; no network or live execution surface."""
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "precision", "stratified-precision", "anchor", "concordance", "gradient", "compose"))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args(argv)
    reducers = {"plan": sample_plan, "precision": paired_binary_precision,
                "stratified-precision": stratified_binary_precision,
                "anchor": anchor_reliability, "concordance": decision_concordance,
                "gradient": gradient_fidelity, "compose": compose_source_measurement}
    try:
        inputs = json.loads(args.input.read_text())
        if not isinstance(inputs, dict):
            raise ValueError("input must be a JSON object")
        if args.operation == "compose":
            if args.source_root is None:
                raise ValueError("compose requires an explicit --source-root")
            if "repo_root" in inputs:
                raise ValueError("source root must come from --source-root")
            inputs["repo_root"] = args.source_root
        elif args.source_root is not None:
            raise ValueError("--source-root applies only to compose")
        result = reducers[args.operation](**inputs)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        print(json.dumps({"schema": VERSION, "status": "refused", "reason": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
