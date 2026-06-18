"""Bare-model runner tests (eval gauge Phase 0).

Pins the agent loop (done/budget/error/tool-error), probe-based scoring, and the bare-vs-harness
comparator's noise-floor judgement. The LLM and Mythic are injected fakes — no lab, no API key.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import bare_runner as br  # noqa: E402
import fitness  # noqa: E402
from range_state import Milestone, GroundTruth  # noqa: E402
from scenarios import goad_scenarios  # noqa: E402

_RUN = {"score": 0.5, "status": "pass", "tool_calls": 5, "model_calls": 9, "errors": []}


def _gt(furthest):
    ms = {m: (m.value <= furthest.value) for m in Milestone if m.value <= furthest.value}
    ms[Milestone.FOOTHOLD] = True
    return GroundTruth("scn", ms, furthest)


def _card(furthest):
    return fitness.score(_RUN, _gt(furthest))


def test_loop_runs_until_final():
    calls = {"n": 0}

    def model_fn(system, tools, history):
        calls["n"] += 1
        return {"final": "done"} if calls["n"] > 3 else {"tool": "mythic", "args": {"cmd": "whoami"}}

    res = br.BareModelRunner(model_fn, lambda c: "ok", tools_spec=[{"name": "mythic"}]).run("get DA")
    assert res.steps == 3 and res.stopped == "done"
    assert len(res.transcript) == 3 and res.transcript[0]["obs"] == "ok"


def test_loop_hits_budget():
    res = br.BareModelRunner(lambda s, t, h: {"tool": "x", "args": {}}, lambda c: "ok", max_steps=5).run("obj")
    assert res.stopped == "budget" and res.steps == 5


def test_model_error_stops():
    def boom(s, t, h):
        raise RuntimeError("api down")
    res = br.BareModelRunner(boom, lambda c: "ok").run("obj")
    assert res.stopped == "error" and res.steps == 0


def test_tool_error_is_captured_not_fatal():
    seq = iter([{"tool": "x", "args": {}}, {"final": "done"}])

    def model_fn(s, t, h):
        return next(seq)

    def bad_tool(call):
        raise ValueError("mythic timeout")

    res = br.BareModelRunner(model_fn, bad_tool).run("obj")
    assert res.stopped == "done" and "[tool error]" in res.transcript[0]["obs"]


def test_score_bare_run_uses_probes():
    scn = goad_scenarios()[1]
    res = br.BareModelResult(objective="obj", steps=4, stopped="done")
    probes = {Milestone.GRAPH_COLLECTED: lambda: True, Milestone.SYSTEM_ON_HOST: lambda: True}
    card = br.score_bare_run(res, scn, probes)
    assert card.furthest_milestone == "SYSTEM_ON_HOST"
    assert card.substring_score == 0.0          # substring eval doesn't apply to a bare model


def test_compare_harness_better_when_gap_exceeds_noise():
    cmp = br.compare_bare_vs_harness("child-da", [_card(Milestone.GRAPH_COLLECTED)] * 3,
                                     [_card(Milestone.KRBTGT_DUMPED)] * 3)
    assert cmp.delta > 0 and cmp.real is True and cmp.verdict == "harness-better"


def test_compare_indistinguishable_when_equal():
    cmp = br.compare_bare_vs_harness("child-da", [_card(Milestone.DA_CHILD)] * 3,
                                     [_card(Milestone.DA_CHILD)] * 3)
    assert cmp.delta == 0 and cmp.verdict == "indistinguishable"
