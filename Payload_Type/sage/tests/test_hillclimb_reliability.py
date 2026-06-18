"""C3 noise-floor / reliability tests (eval gauge Phase 0).

Pins: zero spread for identical runs; non-zero MDE + per-milestone label-agreement for
varying runs (ISC-12/13/14); refusal to mix verifier_hashes (ISC-9); injectable measure();
append-only record write (ISC-15).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import fitness  # noqa: E402
import reliability  # noqa: E402
from range_state import Milestone, GroundTruth  # noqa: E402

_RUN = {"score": 0.5, "status": "pass", "tool_calls": 10, "model_calls": 20,
        "recursion_deaths": 0, "total_tokens": 1000, "wall_seconds": 5.0, "errors": []}


def _gt(furthest):
    ms = {m: (m.value <= furthest.value) for m in Milestone if m.value <= furthest.value}
    ms[Milestone.FOOTHOLD] = True
    return GroundTruth("north-da", ms, furthest)


def _card(furthest, gauge_version="phase0-1"):
    return fitness.score(_RUN, _gt(furthest), gauge_version=gauge_version)


def test_identical_runs_have_zero_noise():
    rep = reliability.noise_floor([_card(Milestone.KRBTGT_DUMPED) for _ in range(5)])
    assert rep.repeats == 5
    assert rep.capability_stdev == 0.0
    assert rep.min_detectable_effect == 0.0
    assert rep.least_stable_agreement == 1.0
    assert all(v == 1.0 for v in rep.label_agreement.values())


def test_varying_runs_yield_noise_floor_and_label_agreement():
    # 3 runs reach KRBTGT, 1 stalls at GRAPH -> the SYSTEM/DA/KRBTGT milestones flip 3:1
    cards = [_card(Milestone.KRBTGT_DUMPED) for _ in range(3)] + [_card(Milestone.GRAPH_COLLECTED)]
    rep = reliability.noise_floor(cards)
    assert rep.capability_stdev > 0.0
    assert rep.min_detectable_effect > 0.0
    assert rep.least_stable_agreement == 0.75            # 3 vs 1 over 4 repeats
    assert rep.least_stable_milestone in {"SYSTEM_ON_HOST", "DA_CHILD", "KRBTGT_DUMPED"}
    assert rep.label_agreement["FOOTHOLD"] == 1.0        # stable milestones still agree
    assert rep.label_agreement["GRAPH_COLLECTED"] == 1.0


def test_refuses_to_mix_verifier_hashes():
    mixed = [_card(Milestone.KRBTGT_DUMPED, "phase0-1"), _card(Milestone.KRBTGT_DUMPED, "phase0-2")]
    try:
        reliability.noise_floor(mixed)
        assert False, "expected ValueError on mixed verifier_hash"
    except ValueError as exc:
        assert "verifier_hash" in str(exc)


def test_measure_runs_injected_fn_n_times():
    calls = []

    def fake_run(seed):
        calls.append(seed)
        return _card(Milestone.DA_CHILD)

    rep = reliability.measure(fake_run, repeats=5)
    assert calls == [0, 1, 2, 3, 4]
    assert rep.repeats == 5


def test_reliability_record_is_appended(tmp_path):
    rep = reliability.noise_floor([_card(Milestone.KRBTGT_DUMPED) for _ in range(3)])
    path = reliability.write_reliability_record(rep, results_dir=tmp_path)
    reliability.write_reliability_record(rep, results_dir=tmp_path)
    lines = Path(path).read_text().strip().splitlines()
    assert len(lines) == 2                               # append-only
    rec = json.loads(lines[0])
    assert rec["kind"] == "reliability" and rec["verifier_hash"].startswith("sha256:")
