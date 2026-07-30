"""Gate-experiment orchestration tests (eval gauge Phase 0).

Pins: Spearman correctness; an aligned gauge PASSES (ρ high, no danger); a gauge with a
high-eval/low-truth config FAILS even if ρ is okay; too-few configs -> INSUFFICIENT.
Live execution is out of scope (needs the lab); the runner is injected (synthetic).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import gate_experiment as gx  # noqa: E402
from scenarios import goad_scenarios  # noqa: E402
from range_state import Milestone  # noqa: E402


def test_spearman_monotonic_and_anti():
    assert gx.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert gx.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0
    assert gx.spearman([1], [1]) is None


_ALIGNED = {
    "prod": {"substring": 0.90, "furthest": Milestone.OBJECTIVE},
    "good": {"substring": 0.60, "furthest": Milestone.KRBTGT_DUMPED},
    "weak": {"substring": 0.30, "furthest": Milestone.GRAPH_COLLECTED},
}


def test_aligned_gauge_passes():
    rep = gx.run_gate_experiment(
        list(_ALIGNED), goad_scenarios(), gx.synthetic_runner(_ALIGNED),
        seeds=2, write_record=False,
    )
    assert rep.spearman_rho == 1.0
    assert rep.high_eval_low_truth_count == 0
    assert rep.verdict == "PASS"


def test_high_eval_low_truth_forces_fail():
    broken = dict(_ALIGNED)
    broken["liar"] = {"substring": 0.95, "furthest": Milestone.GRAPH_COLLECTED}  # eval loves it, truth doesn't
    rep = gx.run_gate_experiment(
        list(broken), goad_scenarios(), gx.synthetic_runner(broken),
        seeds=2, write_record=False,
    )
    assert rep.high_eval_low_truth_count >= 1
    assert "liar" in rep.high_eval_low_truth_configs
    assert rep.verdict == "FAIL"


def test_too_few_configs_is_insufficient():
    two = {"a": {"substring": 0.9, "furthest": Milestone.OBJECTIVE},
           "b": {"substring": 0.2, "furthest": Milestone.FOOTHOLD}}
    rep = gx.run_gate_experiment(list(two), goad_scenarios(), gx.synthetic_runner(two),
                                 seeds=1, write_record=False)
    assert rep.verdict == "INSUFFICIENT"


def test_single_verifier_hash_when_frozen():
    rep = gx.run_gate_experiment(list(_ALIGNED), goad_scenarios(), gx.synthetic_runner(_ALIGNED),
                                 seeds=1, write_record=False)
    assert rep.verifier_hash.startswith("sha256:")  # one frozen gauge across all runs


def test_gate_report_carries_candidate_surface_calibration_fields():
    rep = gx.run_gate_experiment(
        list(_ALIGNED),
        goad_scenarios(),
        gx.synthetic_runner(_ALIGNED),
        seeds=1,
        write_record=False,
        candidate_surface="retrieval-v1",
        t0_disposition="survived_triage",
        t1_substrate_status="verified",
        t2_anchor_present=True,
        smallest_relevant_effect=0.2,
        target_power=0.8,
        achieved_power=0.9,
        measured_noise=0.05,
        mde=0.1,
        bootstrap_samples=50,
    )

    assert rep.candidate_surface == "retrieval-v1"
    assert rep.smallest_relevant_effect == 0.2
    assert rep.target_power == 0.8
    assert rep.rank_correlation_ci95 is not None
    assert rep.readiness_decision in {
        "auto_harness_not_ready",
        "eligible_for_supervised_artifact_campaign",
    }


def test_missing_calibration_metrics_render_as_not_estimable():
    report = gx.run_calibration_protocol(
        candidate_surface="retrieval-v1",
        cheap_scores=(),
        t2_scores=(),
        t0_disposition="triage_only",
        t1_substrate_status="unavailable",
        t2_anchor_present=False,
        smallest_relevant_effect=None,
        target_power=None,
        achieved_power=None,
        measured_noise=None,
        mde=None,
    )

    payload = report.to_dict()
    assert payload["spearman_rho"] == "not_estimable"
    assert payload["achieved_power"] == "not_estimable"
    assert payload["mde"] == "not_estimable"
    assert "statistical_power_not_estimable" in report.failed_gates
    assert "rank_correlation_not_estimable" in report.failed_gates
    assert "rank_correlation_below_threshold" not in report.failed_gates


def test_build_scorecard_threads_live_probe_flag(monkeypatch):
    seen = []
    scn = goad_scenarios()[0]

    def read_ground_truth(scenario, **kwargs):
        seen.append(kwargs["run_live_probes"])
        milestones = {Milestone.FOOTHOLD: True}
        return gx.GroundTruth(scenario.name, milestones, Milestone.FOOTHOLD)

    monkeypatch.setattr(gx, "read_ground_truth", read_ground_truth)
    monkeypatch.setattr(gx, "read_process_signals", lambda _run_id: None)
    rec = {"score": 0.1, "status": "pass", "tool_calls": 1, "model_calls": 1, "errors": []}
    gx.build_scorecard_from_run(rec, scn)
    gx.build_scorecard_from_run(rec, scn, run_live_probes=True)
    assert seen == [False, True]
