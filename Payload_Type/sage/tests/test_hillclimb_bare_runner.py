"""Bare-model runner tests (eval gauge Phase 0).

Pins the agent loop (done/budget/error/tool-error), probe-based scoring, and the bare-vs-harness
comparator's noise-floor judgement. The LLM and Mythic are injected fakes — no lab, no API key.
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def _comparison_card(success, *, clean_stop=True, capability=0.0, tool_calls=1, model_calls=1,
                     total_tokens=10, wall_seconds=1.0, cost_countable=None):
    return SimpleNamespace(
        objective_proven=success,
        clean_stop=clean_stop,
        capability=capability,
        tool_calls=tool_calls,
        model_calls=model_calls,
        total_tokens=total_tokens,
        wall_seconds=wall_seconds,
        cost_countable=cost_countable or {},
    )


def test_loop_runs_until_final():
    calls = {"n": 0}

    def model_fn(system, tools, history):
        calls["n"] += 1
        return {"final": "done"} if calls["n"] > 3 else {"tool": "mythic", "args": {"cmd": "whoami"}}

    res = br.BareModelRunner(model_fn, lambda c: "ok", tools_spec=[{"name": "mythic"}]).run("get DA")
    assert res.steps == 3 and res.stopped == "done"
    assert res.objective_recognized is False
    assert len(res.transcript) == 3 and res.transcript[0]["obs"] == "ok"


@pytest.mark.parametrize(
    ("terminal_text", "recognized"),
    [
        ("OBJECTIVE_MET", True),
        ("  objective_met  ", True),
        ("STUCK", False),
        ("done", False),
        ("OBJECTIVE_MET because the range changed", False),
    ],
)
def test_bare_runner_records_only_exact_objective_terminal_as_recognition(
    terminal_text, recognized,
):
    result = br.BareModelRunner(
        lambda *_args: {"final": terminal_text},
        lambda _call: "unused",
    ).run("obj")

    assert result.stopped == "done"
    assert result.objective_recognized is recognized


def test_loop_hits_budget():
    res = br.BareModelRunner(lambda s, t, h: {"tool": "x", "args": {}}, lambda c: "ok", max_steps=5).run("obj")
    assert res.stopped == "budget" and res.steps == 5


def test_model_response_after_wall_deadline_cannot_dispatch_target_tool():
    tool_calls = []

    def late_model(*_args):
        time.sleep(0.03)
        return {"tool": "issue_command", "args": {"command": "whoami"}}

    result = br.BareModelRunner(
        late_model,
        lambda call: tool_calls.append(call),
        timeout=0.01,
    ).run("obj")

    assert result.stopped == "timeout"
    assert result.model_calls == 1
    assert result.steps == 0
    assert tool_calls == []


def test_wall_deadline_is_rechecked_at_actual_tool_dispatch_boundary(monkeypatch):
    clock = iter([0.0, 0.5, 0.999, 1.001, 1.001])
    monkeypatch.setattr(br.time, "monotonic", lambda: next(clock))
    tool_calls = []

    result = br.BareModelRunner(
        lambda *_args: {"tool": "issue_command", "args": {"command": "whoami"}},
        lambda call: tool_calls.append(call),
        timeout=1.0,
    ).run("obj")

    assert result.stopped == "timeout"
    assert result.steps == 0
    assert tool_calls == []


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
    scn = goad_scenarios()[0]
    res = br.BareModelResult(objective="obj", steps=4, stopped="done")
    probes = {Milestone.GRAPH_COLLECTED: lambda: True, Milestone.SYSTEM_ON_HOST: lambda: True}
    card = br.score_bare_run(res, scn, probes)
    assert card.furthest_milestone == "SYSTEM_ON_HOST"
    assert card.substring_score == 0.0          # substring eval doesn't apply to a bare model
    assert card.request_completed is True
    assert card.clean_stop is False             # FINAL is not evaluator-owned clean-stop evidence


def test_compare_harness_better_uses_binary_objective_not_route_ladder():
    bare = [_comparison_card(False, capability=1.0)] * 10
    harness = [_comparison_card(True, capability=0.0)] * 10
    cmp = br.compare_bare_vs_harness("child-control", bare, harness)
    assert cmp.delta == 1.0 and cmp.resolved is True and cmp.verdict == "harness-better"
    assert cmp.bare_objective_success_rate == 0.0
    assert cmp.harness_objective_success_rate == 1.0


def test_compare_equal_n5_retains_uncertainty_instead_of_claiming_resolution():
    cards = [_comparison_card(True)] * 5
    cmp = br.compare_bare_vs_harness("child-control", cards, cards)
    assert cmp.delta == 0
    assert cmp.min_detectable_effect > 0
    assert cmp.resolved is False
    assert cmp.verdict == "indistinguishable-at-current-sample"


def test_compare_reports_clean_stop_separately_from_binary_success():
    unclean = [_comparison_card(True, clean_stop=False)] * 5
    clean = [_comparison_card(True, clean_stop=True)] * 5
    cmp = br.compare_bare_vs_harness("child-control", unclean, clean)
    assert cmp.bare_objective_success_rate == 1.0
    assert cmp.harness_objective_success_rate == 1.0
    assert cmp.bare_clean_stop_rate == 0.0


def test_cost_mean_is_unavailable_instead_of_silently_zero_when_not_countable():
    cards = [_comparison_card(True, total_tokens=0)] * 2
    cmp = br.compare_bare_vs_harness("child-control", cards, cards)
    assert cmp.bare_cost_mean["total_tokens"] is None
    explicit = [_comparison_card(True, total_tokens=0, cost_countable={
        "tool_calls": True,
        "model_calls": True,
        "total_tokens": True,
        "wall_seconds": True,
    })] * 2
    cmp = br.compare_bare_vs_harness("child-control", explicit, explicit)
    assert cmp.bare_cost_mean["total_tokens"] == 0.0


def test_bare_runner_records_model_calls_tokens_and_wall_time():
    decisions = iter([
        {
            "tool": "x",
            "args": {},
            "_usage": {"input_tokens": 7, "output_tokens": 3},
            "_backend": {
                "effective_backend": "provider:model-v1",
                "backend_provenance_source": "response_metadata.model_name",
            },
        },
        {
            "final": "done",
            "_usage": {"total_tokens": 4},
            "_backend": {
                "effective_backend": "provider:model-v1",
                "backend_provenance_source": "response_metadata.model_name",
            },
        },
    ])
    result = br.BareModelRunner(lambda *_args: next(decisions), lambda _call: "ok").run("obj")
    assert result.steps == 1
    assert result.model_calls == 2
    assert result.total_tokens == 14
    assert result.wall_seconds >= 0
    assert len(result.effective_backend_requests) == 2
    assert result.effective_backend_requests[1]["decision_id"] == "bare-model-call-2"
