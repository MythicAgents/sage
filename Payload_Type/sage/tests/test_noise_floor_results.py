"""reliability.noise_floor_from_results — compute the noise floor + MDE from already-recorded gauge runs
(the seeds an orchestrate.py --seeds N batch produced), with no lab.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
import reliability  # noqa: E402


def _card(cap, h="sha256:abc"):
    # Minimal ScoreCard dict as recorded under "card" in bare_vs_harness.jsonl.
    return {
        "scenario": "cross-forest-objective", "verifier_hash": h, "furthest_milestone": "DA_CHILD",
        "capability": cap, "milestones_reached": 1, "milestones": {"FOOTHOLD": True, "DA_CHILD": cap > 0.3},
        "substring_score": 0.0, "metric_capability_gap": 0.0, "tool_calls": 0, "model_calls": 0,
        "productive_action_ratio": 0.0, "turn_efficiency": 0.0, "status": "stopped", "recursion_deaths": 0,
        "error_count": 0, "total_tokens": 0, "wall_seconds": 0.0,
    }


def _write(p, rows):
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_noise_floor_from_results_basic(tmp_path):
    p = tmp_path / "bvh.jsonl"
    _write(p, [
        {"side": "harness", "scenario": "cross-forest-objective", "card": _card(0.4)},
        {"side": "harness", "scenario": "cross-forest-objective", "card": _card(0.4)},
        {"side": "harness", "scenario": "cross-forest-objective", "card": _card(0.2)},
        {"side": "bare", "scenario": "cross-forest-objective", "card": _card(0.9)},  # other side -> excluded
    ])
    rep = reliability.noise_floor_from_results(str(p), scenario="cross-forest-objective", side="harness")
    assert rep.repeats == 3
    assert rep.capability_min == 0.2 and rep.capability_max == 0.4
    assert rep.min_detectable_effect > 0  # real spread -> non-zero MDE


def test_noise_floor_from_results_keeps_latest_hash_only(tmp_path):
    p = tmp_path / "bvh.jsonl"
    _write(p, [
        {"side": "harness", "scenario": "x", "card": _card(0.4, h="OLD")},
        {"side": "harness", "scenario": "x", "card": _card(0.5, h="NEW")},
        {"side": "harness", "scenario": "x", "card": _card(0.5, h="NEW")},
    ])
    rep = reliability.noise_floor_from_results(str(p), scenario="x", side="harness")
    assert rep.repeats == 2 and rep.verifier_hash == "NEW"


def test_noise_floor_from_results_n_limit(tmp_path):
    p = tmp_path / "bvh.jsonl"
    _write(p, [{"side": "harness", "scenario": "x", "card": _card(c)} for c in (0.1, 0.2, 0.3, 0.4)])
    rep = reliability.noise_floor_from_results(str(p), scenario="x", side="harness", n=2)
    assert rep.repeats == 2
    assert rep.capability_min == 0.3 and rep.capability_max == 0.4  # last 2


def test_noise_floor_from_results_no_records_raises(tmp_path):
    p = tmp_path / "bvh.jsonl"
    _write(p, [{"side": "bare", "scenario": "x", "card": _card(0.5)}])
    try:
        reliability.noise_floor_from_results(str(p), scenario="x", side="harness")
        assert False, "expected ValueError for no harness records"
    except ValueError:
        pass
