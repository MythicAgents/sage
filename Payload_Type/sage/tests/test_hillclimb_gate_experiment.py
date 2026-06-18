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
