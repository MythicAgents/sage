"""C5 hermetic inner-loop evaluator (hermetic.py): offline capability re-score, determinism/consistency,
repair-policy replay fitness, the spec §2.2 eval-result shape, and the explicitly-unbuilt mock-Mythic frontier.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai"))  # trajectory PACKAGE (.schema resolves)
import pytest  # noqa: E402
import hermetic  # noqa: E402
from scenarios import goad_scenarios  # noqa: E402
from trajectory.exporter import export_text_artifact  # noqa: E402


def _child():
    return next(s for s in goad_scenarios() if s.name == "child-da")


def _rec():
    return {"score": 0.3, "status": "objective-recognized", "tool_calls": 5, "model_calls": 9, "errors": []}


def test_score_recorded_run_returns_scorecard_offline():
    card = hermetic.score_recorded_run(_rec(), _child(), engagement_id="hc-test")
    assert card.scenario == "child-da"
    assert hasattr(card, "capability") and 0.0 <= card.capability <= 1.0


def test_score_recorded_run_live_probe_flag_defaults_off(monkeypatch):
    seen = []
    sentinel = object()

    def build_scorecard_from_run(harness_record, scenario, **kwargs):
        seen.append(kwargs["run_live_probes"])
        return sentinel

    monkeypatch.setattr(hermetic, "build_scorecard_from_run", build_scorecard_from_run)
    assert hermetic.score_recorded_run(_rec(), _child(), engagement_id="hc-test") is sentinel
    assert hermetic.score_recorded_run(
        _rec(), _child(), engagement_id="hc-test", run_live_probes=True
    ) is sentinel
    assert seen == [False, True]


def test_replay_consistency_is_deterministic():
    out = hermetic.replay_consistency(_rec(), _child(), engagement_id="hc-test", repeats=4)
    assert out["repeats"] == 4
    assert out["deterministic"] is True
    assert out["capability_spread"] == 0.0


def test_inner_eval_result_has_spec_shape_and_zero_cost():
    res = hermetic.inner_eval_result(_rec(), _child(), engagement_id="hc-test", candidate_id="cand-1", seeds=[11, 12])
    assert res["candidate_id"] == "cand-1"
    assert res["tier"] == "inner" and res["mode"] == "replay"
    assert res["cost"] == {"model_calls": 0, "tool_calls": 0, "tokens": 0, "wall_seconds": 0.0}
    assert res["seeds"] == [11, 12]
    assert "scalar_score" in res and "metric_capability_gap" in res and "objective_clean_stop" in res


def test_replay_repair_fitness_wraps_replay_score(tmp_path):
    log = tmp_path / "essos_da_test.out"
    log.write_text("ERROR_NOT_UNIQUE", encoding="utf-8")  # a known failure signature with a mapped repair
    records = export_text_artifact(log)
    out = hermetic.replay_repair_fitness(records, records)
    assert out["total"] >= 1
    assert out["exact_repair_rate"] == 1.0
    assert out["label_match_rate"] == 1.0


def test_mock_mythic_frontier_is_explicitly_not_implemented():
    with pytest.raises(NotImplementedError):
        hermetic.mock_mythic_candidate_eval()
