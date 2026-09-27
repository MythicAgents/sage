from __future__ import annotations

import math
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai.hillclimb import development_measurement as measurement
from Payload_Type.sage.tests.test_hillclimb_composite_source_adapter import _fixture


ARMS = ("shipping_sage", "true_naive")
ARM_CONTRACTS = {
    "shipping_sage": {
        "wrapper_id": "shipping_native_sage",
        "wrapper_revision": "tree:fixture-v1",
        "execution_path": "supervisor_worker",
    },
    "true_naive": {
        "wrapper_id": "true_naive_raw_tools",
        "wrapper_revision": "tree:fixture-v1",
        "execution_path": "true_naive_raw_tools",
    },
}


def _direct_binomial_upper(k: int, n: int, alpha: float) -> float:
    def cdf(p: float) -> float:
        return sum(math.comb(n, j) * p**j * (1 - p) ** (n - j) for j in range(k + 1))

    lo, hi = 0.0, 1.0
    for _ in range(100):
        midpoint = (lo + hi) / 2
        if cdf(midpoint) > alpha:
            lo = midpoint
        else:
            hi = midpoint
    return hi


def _decision_rows(count: int, *, live: str = "promote"):
    return [
        {
            "independent_block_id": f"block-{index}",
            "gauge_decision": "promote",
            "live_decision": live,
        }
        for index in range(count)
    ]


def _valid_bundle(tmp_path, *, arm: str, objective: bool):
    root = tmp_path / arm
    root.mkdir()
    wrapper = ARM_CONTRACTS[arm]["wrapper_id"]
    execution_path = ARM_CONTRACTS[arm]["execution_path"]
    bundle = _fixture(
        root,
        arm=arm,
        wrapper=wrapper,
        execution_path=execution_path,
        objective=objective,
    )
    for binding in bundle["source_bindings"].values():
        binding["path"] = f"{arm}/{binding['path']}"
    return bundle


def test_binomial_upper_zero_and_terminal_cases_and_small_n_inversion():
    assert measurement.binomial_upper(0, 5, alpha=0.05) == pytest.approx(1 - 0.05**0.2)
    assert measurement.binomial_upper(0, 0, alpha=0.05) is None
    assert measurement.binomial_upper(5, 5, alpha=0.05) == 1.0
    assert measurement.binomial_upper(2, 7, alpha=0.05) == pytest.approx(
        _direct_binomial_upper(2, 7, 0.05), abs=1e-14
    )


@pytest.mark.parametrize(
    "failures,trials,alpha",
    [(-1, 1, 0.05), (2, 1, 0.05), (True, 1, 0.05), (0, False, 0.05), (0, 1, math.inf)],
)
def test_binomial_upper_rejects_invalid_counts_booleans_and_infinities(failures, trials, alpha):
    with pytest.raises(ValueError):
        measurement.binomial_upper(failures, trials, alpha=alpha)


def test_minimum_zero_failure_trials_obeys_strict_multiplicity_adjusted_boundary():
    n = measurement.minimum_zero_failure_trials(tolerance=0.1, alpha=0.05, comparisons=10)
    assert measurement.binomial_upper(0, n, alpha=0.05 / 10) < 0.1
    assert measurement.binomial_upper(0, n - 1, alpha=0.05 / 10) >= 0.1


def test_paired_binary_precision_rejects_saturated_tie_and_five_clear_pairs():
    tie = measurement.paired_binary_precision([False] * 5, [False] * 5, practical_delta=0.1)
    assert tie["sample_variance"] == 0
    assert tie["informative"] is False
    assert "saturated_binary_arms" in tie["reason_codes"]

    five = measurement.paired_binary_precision([True] * 5, [False] * 5, practical_delta=0.1)
    assert five["informative"] is False
    assert set(five["reason_codes"]) >= {"mde_not_actionable", "exact_interval_not_actionable"}


def test_paired_binary_precision_large_heterogeneous_effect_is_symmetric_and_informative():
    left = [True] * 1500 + [False] * 500 + [True] * 250 + [False] * 250
    right = [True] * 1500 + [False] * 500 + [False] * 250 + [True] * 250
    result = measurement.paired_binary_precision(left, right, practical_delta=0.1)
    swapped = measurement.paired_binary_precision(right, left, practical_delta=0.1)
    assert result["paired_n"] >= 2000
    assert result["informative"] is swapped["informative"] is True
    assert all(math.isfinite(x) for x in (*result["effect_interval"].values(), result["conservative_planning_mde"]))
    assert result["effect"] == pytest.approx(-swapped["effect"])
    assert result["effect_interval"]["lower"] == pytest.approx(-swapped["effect_interval"]["upper"])
    assert result["effect_interval"]["upper"] == pytest.approx(-swapped["effect_interval"]["lower"])
    assert result["conservative_planning_mde"] == pytest.approx(swapped["conservative_planning_mde"])


def test_decision_concordance_requires_anchor_unique_blocks_and_real_promotions():
    rows = _decision_rows(1)
    with pytest.raises(ValueError, match="anchor"):
        measurement.decision_concordance(
            rows, tolerance=0.2, family_alpha=0.05, comparisons=1,
            anchor_admitted_before_decisions=False,
        )

    with pytest.raises(ValueError, match="unique"):
        measurement.decision_concordance(
            rows * 2, tolerance=0.2, family_alpha=0.05, comparisons=1,
            anchor_admitted_before_decisions=True,
        )

    zero = measurement.decision_concordance(
        [{**rows[0], "gauge_decision": "reject"}],
        tolerance=0.2, family_alpha=0.05, comparisons=1,
        anchor_admitted_before_decisions=True,
    )
    assert zero["proposed_promotions"] == 0
    assert zero["false_promotion_bound_passed"] is False


def test_decision_concordance_conservatively_counts_unresolved_and_passes_enough_matches():
    unresolved = measurement.decision_concordance(
        _decision_rows(50) + [{
            "independent_block_id": "unresolved",
            "gauge_decision": "promote",
            "live_decision": "inconclusive",
        }],
        tolerance=0.01, family_alpha=0.05, comparisons=1,
        anchor_admitted_before_decisions=True,
    )
    assert unresolved["unresolved_promotions"] == 1
    assert unresolved["false_promotion_upper_bound"] > 0
    assert unresolved["false_promotion_bound_passed"] is False

    enough = measurement.decision_concordance(
        _decision_rows(20), tolerance=0.2, family_alpha=0.05, comparisons=1,
        anchor_admitted_before_decisions=True,
    )
    assert enough["minimum_zero_failure_promotions"] <= enough["proposed_promotions"]
    assert enough["false_promotion_bound_passed"] is True


def test_decision_opportunity_bounds_cover_zero_promotion_sensitivity_and_strict_boundary():
    zero = [
        {"independent_block_id": f"block-{index}", "gauge_decision": "inconclusive",
         "live_decision": "inconclusive"}
        for index in range(64)
    ]
    passed = measurement.decision_concordance(
        zero, tolerance=0.1, family_alpha=0.025, comparisons=10,
        anchor_admitted_before_decisions=True,
    )
    assert passed["proposed_promotions"] == 0
    assert passed["false_promotion_upper_bound"] is None
    assert passed["false_promotion_bound_passed"] is False
    assert passed["opportunity_error_adjusted_alpha"] == pytest.approx(0.025 / (2 * 10))
    assert passed["decision_error_bounds_passed"] is True
    assert passed["requires_independently_resolved_improvement_and_gauge_sensitivity"] is True

    failed = measurement.decision_concordance(
        zero[:-1], tolerance=0.1, family_alpha=0.025, comparisons=10,
        anchor_admitted_before_decisions=True,
    )
    assert failed["decision_error_bounds_passed"] is False
    unsupported = measurement.decision_concordance(
        [{**zero[0], "gauge_decision": "promote"}, *zero[1:]],
        tolerance=0.1, family_alpha=0.025, comparisons=10,
        anchor_admitted_before_decisions=True,
    )
    assert unsupported["unsupported_promotion_per_opportunity_upper_bound"] > passed[
        "unsupported_promotion_per_opportunity_upper_bound"
    ]
    assert unsupported["decision_error_bounds_passed"] is False


def _paired_counts(n, positive, negative):
    shared = n - positive - negative
    both_true = shared // 2
    both_false = shared - both_true
    return {
        "left": [True] * positive + [False] * negative + [True] * both_true + [False] * both_false,
        "right": [False] * positive + [True] * negative + [True] * both_true + [False] * both_false,
    }


def test_stratified_precision_uses_equal_weights_and_preserves_signed_symmetry():
    strata = {
        "small-positive": _paired_counts(1000, 1000, 0),
        "large-negative": _paired_counts(2000, 0, 2000),
    }
    result = measurement.stratified_binary_precision(strata, practical_delta=0.1)
    swapped = measurement.stratified_binary_precision(
        {key: {"left": row["right"], "right": row["left"]} for key, row in strata.items()},
        practical_delta=0.1,
    )
    assert result["effect"] == pytest.approx(0.0)
    assert result["effect"] != pytest.approx((1000 - 2000) / 3000)
    assert result["effect"] == pytest.approx(-swapped["effect"])
    assert result["effect_interval"]["lower"] == pytest.approx(-swapped["effect_interval"]["upper"])
    assert result["effect_interval"]["upper"] == pytest.approx(-swapped["effect_interval"]["lower"])
    assert result["variance_of_mean_upper"] == pytest.approx(swapped["variance_of_mean_upper"])


def test_stratified_precision_rejects_invalid_and_saturated_inputs():
    with pytest.raises(ValueError, match="non-empty"):
        measurement.stratified_binary_precision({}, practical_delta=0.1)
    with pytest.raises(ValueError, match="exactly left and right"):
        measurement.stratified_binary_precision({"bad": {"left": [], "right": [], "extra": []}}, practical_delta=0.1)
    with pytest.raises(ValueError, match="empty strata"):
        measurement.stratified_binary_precision({"empty": {"left": [], "right": []}}, practical_delta=0.1)
    with pytest.raises(ValueError, match="Boolean"):
        measurement.stratified_binary_precision({"bad": {"left": [1] * 5, "right": [False] * 5}}, practical_delta=0.1)

    saturated = measurement.stratified_binary_precision(
        {"tie": {"left": [False] * 100, "right": [False] * 100}}, practical_delta=0.1,
    )
    assert saturated["informative"] is False
    assert "saturated_binary_arms_in_all_strata" in saturated["reason_codes"]


def test_stratified_precision_is_informative_for_high_n_heterogeneous_strata():
    result = measurement.stratified_binary_precision(
        {
            "family-a": _paired_counts(10000, 1000, 500),
            "family-b": _paired_counts(10000, 500, 100),
        },
        practical_delta=0.1,
    )
    assert result["strata"]["family-a"]["effect"] != result["strata"]["family-b"]["effect"]
    assert result["conservative_planning_mde"] <= 0.1
    assert result["informative"] is True


def test_stratified_precision_cli_uses_the_actual_local_entrypoint(tmp_path):
    input_path = tmp_path / "strata.json"
    input_path.write_text(json.dumps({
        "strata": {
            "family-a": _paired_counts(5000, 500, 250),
            "family-b": _paired_counts(5000, 250, 50),
        },
        "practical_delta": 0.1,
    }))
    script = Path(__file__).resolve().parents[3] / "skills/sage-eval-gauge/scripts/run_development_measurement.py"
    result = subprocess.run(
        [sys.executable, str(script), "stratified-precision", "--input", str(input_path)],
        text=True, capture_output=True, check=False, timeout=20,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["estimand"] == "equal_stratum_paired_probability_difference"


def test_gradient_fidelity_rejects_flat_over_steep_and_covers_near_delta_only_with_precision():
    flat = measurement.gradient_fidelity(
        [{"pair_id": "flat", "gauge_deltas": [0.0] * 5000, "live_deltas": [0.5] * 5000}],
        practical_delta=0.1, maximum_effect_error=0.2, family_alpha=0.05,
    )
    assert flat["pairs"][0]["flat_over_steep"] is True
    assert flat["gradient_supported"] is False

    precise = measurement.gradient_fidelity(
        [{"pair_id": "near", "gauge_deltas": [0.1] * 5000, "live_deltas": [0.1] * 5000}],
        practical_delta=0.1, maximum_effect_error=0.15, family_alpha=0.05,
    )
    imprecise = measurement.gradient_fidelity(
        [{"pair_id": "near", "gauge_deltas": [0.1] * 5, "live_deltas": [0.1] * 5}],
        practical_delta=0.1, maximum_effect_error=0.15, family_alpha=0.05,
    )
    assert precise["near_delta_coverage"] is True
    assert precise["gradient_supported"] is True
    assert imprecise["near_delta_coverage"] is False
    assert imprecise["gradient_supported"] is False


def test_gradient_fidelity_rejects_resolved_cross_pair_order_reversal():
    reversed_result = measurement.gradient_fidelity(
        [
            {"pair_id": "pair-a", "gauge_deltas": [0.3] * 10000, "live_deltas": [0.2] * 10000},
            {"pair_id": "pair-b", "gauge_deltas": [0.2] * 10000, "live_deltas": [0.3] * 10000},
        ],
        practical_delta=0.1, maximum_effect_error=0.2, family_alpha=0.05,
    )
    assert reversed_result["effect_ordering"][0]["observed_order_reversed"] is True
    assert reversed_result["gradient_supported"] is False

    supported = measurement.gradient_fidelity(
        [
            {"pair_id": "pair-a", "gauge_deltas": [0.1] * 10000, "live_deltas": [0.1] * 10000},
            {"pair_id": "pair-b", "gauge_deltas": [0.3] * 10000, "live_deltas": [0.3] * 10000},
        ],
        practical_delta=0.1, maximum_effect_error=0.2, family_alpha=0.05,
    )
    assert supported["effect_ordering"][0]["observed_order_reversed"] is False
    assert supported["effect_ordering"][0]["ordering_supported"] is True
    assert supported["near_delta_coverage"] is True
    assert supported["gradient_supported"] is True


def test_source_composition_preserves_rejection_and_gate1t_reduced_states(tmp_path):
    rejected_bundle = _valid_bundle(tmp_path, arm="shipping_sage", objective=True)
    rejected_bundle["evidence_supplement"]["claims"].pop("row.boundary_violation_count")
    rejected = measurement.compose_source_measurement(
        [rejected_bundle], repo_root=tmp_path, report_arguments={}
    )
    assert rejected["admissions"][0]["status"] == "not_reusable"
    assert rejected["measurement_informative"] is False
    assert rejected["m3_complete"] is False

    valid_root = tmp_path / "valid"
    valid_root.mkdir()
    bundles = [
        _valid_bundle(valid_root, arm="shipping_sage", objective=True),
        _valid_bundle(valid_root, arm="true_naive", objective=False),
    ]
    report = measurement.compose_source_measurement(
        bundles,
        repo_root=valid_root,
        report_arguments={
            "evaluated_arm": "shipping_sage",
            "baseline_arm": "true_naive",
            "arm_contracts": ARM_CONTRACTS,
            "smallest_relevant_effects": {"capability": 0.1, "opsec": 0.1, "safe_success": 0.1},
            "noninferiority_margins": {"safe_success": 0.1, "opsec": 0.1, "critical_subgroup": 0.1},
            "critical_subgroups": (("surface-a", "family-a"),),
        },
    )
    assert report["legacy_composite_report"] is not None
    by_arm = {row["composite_row"]["identity"]["policy_arm"]: row for row in report["admissions"]}
    for admission in by_arm.values():
        assert admission["composite_row"]["dimension_states"] == {
            name: "ABSTAIN" for name in ("S1", "S2", "S5", "S7", "S8")
        }
    assert by_arm["shipping_sage"]["composite_row"]["safe_success"] == "ABSTAIN"
    assert by_arm["true_naive"]["composite_row"]["safe_success"] is False
    assert report["measurement_informative"] is False
    assert report["m3_complete"] is False
