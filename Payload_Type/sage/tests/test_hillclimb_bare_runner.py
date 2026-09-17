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
import effect_registry as er  # noqa: E402
import fitness  # noqa: E402
import probes as probe_mod  # noqa: E402
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


@pytest.mark.parametrize("ceiling", [0, -1, False, True, 1.5, "100"])
def test_invalid_explicit_token_ceiling_is_rejected(ceiling):
    with pytest.raises(ValueError, match="token ceiling"):
        br.BareModelRunner(lambda *_: {}, lambda _: None, token_ceiling=ceiling)


@pytest.mark.parametrize("timeout", [True, -1, False, float("nan"), float("inf"), float("-inf")])
def test_invalid_explicit_wall_limit_is_rejected(timeout):
    with pytest.raises(ValueError, match="timeout"):
        br.BareModelRunner(lambda *_: {}, lambda _: None, timeout=timeout)


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


def test_provider_timeout_retains_attempt_and_marks_cost_incomplete():
    events = []

    def provider_timeout(*_args):
        raise TimeoutError("request deadline")

    result = br.BareModelRunner(
        provider_timeout,
        lambda _call: pytest.fail("tool dispatch attempted"),
        timeout=1.0,
        event_sink=events.append,
    ).run("obj")

    assert result.stopped == "timeout"
    assert result.termination == {"cause": "wall_time", "subtype": "wrapper_timeout"}
    assert result.model_attempts == 1
    assert result.model_calls == 0
    assert result.model_response_records == []
    assert result.total_tokens == 0
    assert result.token_usage_complete is False
    assert events == [{"event": "model_request_started", "model_attempt": 1}]


def test_response_is_durably_emitted_before_validation_and_cannot_dispatch():
    events = []
    tool_calls = []

    def reject(record):
        assert events[-1]["event"] == "model_response_retained"
        raise ValueError("wrong route")

    result = br.BareModelRunner(
        lambda *_args: {
            "tool": "issue_command",
            "args": {},
            "_usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            "_backend": {"effective_backend": "openai:wrong"},
        },
        lambda call: tool_calls.append(call),
        token_ceiling=100,
        require_complete_usage=True,
        event_sink=events.append,
        response_validator=reject,
    ).run("obj")

    assert result.stopped == "measurement_error"
    assert result.total_tokens == 3
    assert tool_calls == []
    assert [event["event"] for event in events] == [
        "model_request_started", "model_response_retained",
    ]
    assert result.model_calls == 1
    assert result.steps == 0
    assert tool_calls == []


def test_completed_call_token_boundary_retains_crossing_response_without_dispatch():
    tool_calls = []
    decisions = iter([
        {
            "tool": "issue_command",
            "args": {"command": "whoami"},
            "_usage": {"input_tokens": 4, "output_tokens": 2},
        },
        {
            "tool": "issue_command",
            "args": {"command": "hostname"},
            "_usage": {"input_tokens": 4, "output_tokens": 1},
        },
    ])

    result = br.BareModelRunner(
        lambda *_args: next(decisions),
        lambda call: tool_calls.append(call) or "ok",
        token_ceiling=10,
        require_complete_usage=True,
    ).run("obj")

    assert result.stopped == "token_budget_overshoot"
    assert result.total_tokens == 11
    assert result.model_calls == 2
    assert result.steps == 1
    assert [call["args"]["command"] for call in tool_calls] == ["whoami"]
    assert [item["response_id"] for item in result.model_response_records] == [
        "bare-model-call-1",
        "bare-model-call-2",
    ]
    assert result.action_response_ids == ["bare-model-call-1"]
    assert [item["response_id"] for item in result.transcript] == ["bare-model-call-1"]


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": "9", "output_tokens": "2", "total_tokens": "11"},
        {"input_tokens": 9.9, "output_tokens": 2.9, "total_tokens": 11.9},
    ],
)
def test_bare_runner_does_not_coerce_malformed_usage_values(usage):
    tool_calls = []
    result = br.BareModelRunner(
        lambda *_args: {
            "tool": "issue_command",
            "args": {"command": "whoami"},
            "_usage": usage,
        },
        lambda call: tool_calls.append(call),
        token_ceiling=10,
        require_complete_usage=True,
    ).run("obj")

    assert result.stopped == "measurement_error"
    assert result.token_usage_complete is False
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
    assert res.termination == {"cause": "other_declared_boundary", "subtype": "model_error"}


def test_typed_context_limit_failure_is_reported_without_message_inference():
    class ContextBoundary(RuntimeError):
        termination_cause = "per_request_context_limit"
        termination_subtype = "provider_typed_code"

    result = br.BareModelRunner(
        lambda *_args: (_ for _ in ()).throw(ContextBoundary("opaque")),
        lambda _call: "unused",
    ).run("obj")

    assert result.termination == {
        "cause": "per_request_context_limit",
        "subtype": "provider_typed_code",
    }


@pytest.mark.parametrize(
    ("final", "subtype"),
    [("OBJECTIVE_MET", "objective_met"), ("STUCK", "stuck"), ("done", "other_final")],
)
def test_explicit_raw_terminal_decisions_have_separate_subtypes(final, subtype):
    result = br.BareModelRunner(lambda *_args: {"final": final}, lambda _call: "unused").run("obj")
    assert result.termination == {"cause": "explicit_terminal_decision", "subtype": subtype}


def test_tool_error_is_captured_not_fatal():
    seq = iter([{"tool": "x", "args": {}}, {"final": "done"}])

    def model_fn(s, t, h):
        return next(seq)

    def bad_tool(call):
        raise ValueError("mythic timeout")

    res = br.BareModelRunner(model_fn, bad_tool).run("obj")
    assert res.stopped == "done" and "[tool error]" in res.transcript[0]["obs"]
    assert res.recoverable_tool_errors == [
        {"step": 1, "tool": "x", "category": "mythic"}
    ]


def test_each_dispatcher_error_class_is_recoverable_and_retained():
    observations = iter([
        "[tool error] read_credentials: schema drift",
        "[bloodhound tool error] domain_info: unavailable",
        "[unknown tool] 'typo'",
    ])
    decisions = iter([
        {"tool": "read_credentials", "args": {}},
        {"tool": "domain_info", "args": {}},
        {"tool": "typo", "args": {}},
        {"final": "STUCK"},
    ])

    result = br.BareModelRunner(lambda *_args: next(decisions), lambda _call: next(observations)).run("obj")

    assert result.stopped == "done"
    assert [item["category"] for item in result.recoverable_tool_errors] == [
        "mythic", "bloodhound", "unknown_tool",
    ]
    assert result.steps == 3


def test_score_bare_run_uses_probes():
    scn = goad_scenarios()[0]
    res = br.BareModelResult(objective="obj", steps=4, stopped="done")
    probes = {Milestone.SYSTEM_ON_HOST: probe_mod.registered_probe(
        Milestone.SYSTEM_ON_HOST,
        lambda: True,
        origin=er.ProofOrigin.MYTHIC_TASK_OUTPUT,
        proof_kind="system_identity",
        target=scn.domains["child"],
    )}
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


def test_single_matched_observation_does_not_resolve_overlapping_intervals():
    comparison = br.compare_bare_vs_harness("fixture", [_comparison_card(False)], [_comparison_card(True)])
    assert comparison.delta == 1.0
    assert comparison.bare_success_interval[1] > comparison.harness_success_interval[0]
    assert comparison.resolved is False


@pytest.mark.parametrize("missing", [None, "false", 0, {}, []])
def test_missing_or_malformed_objective_is_not_counted_as_failure(missing):
    comparison = br.compare_bare_vs_harness(
        "fixture", [_comparison_card(missing)], [_comparison_card(False)], allow_missing_rates=True,
    )
    assert comparison.bare_objective_success_rate is None
    assert comparison.harness_objective_success_rate == 0
    assert comparison.bare_outcomes["unmeasured"] == 1
    assert comparison.bare_outcomes["disproven"] == 0
    assert comparison.delta is None
    assert comparison.resolved is False


@pytest.mark.parametrize("disposition", ["invalid", "unmeasured", "proven", "bad", [], {}])
def test_unavailable_or_contradictory_proof_stays_out_of_success_denominator(disposition):
    card = _comparison_card(False)
    card.objective_proof_disposition = disposition
    result = br.compare_bare_vs_harness("fixture", [card], [_comparison_card(True)], allow_missing_rates=True)
    assert result.bare_outcomes["observed"] == 0
    assert result.bare_outcomes["total"] == 1
    assert result.verdict == "insufficient-outcome-evidence"


def test_expensive_observed_failure_is_retained_with_missing_other_outcome():
    failure = _comparison_card(False, total_tokens=1_000_010, cost_countable={"total_tokens": True})
    missing = _comparison_card(None, total_tokens=20, cost_countable={"total_tokens": True})
    result = br.compare_bare_vs_harness("fixture", [failure, missing], [_comparison_card(True)])
    assert result.bare_outcomes == {"total": 2, "observed": 1, "proven": 0, "disproven": 1,
                                    "unmeasured": 1, "invalid": 0}
    assert result.bare_cost_mean["total_tokens"] == 500_015
    assert result.bare_objective_success_rate == 0
    assert result.resolved is False


@pytest.mark.parametrize("outcome,disposition", [(True, "proven"), (False, "disproven"), (None, "invalid")])
def test_actual_probe_score_summary_keeps_objective_disposition(outcome, disposition):
    scenario = goad_scenarios()[0]
    probe = probe_mod.registered_probe(
        Milestone.SYSTEM_ON_HOST, lambda: outcome, origin=er.ProofOrigin.MYTHIC_TASK_OUTPUT,
        proof_kind="system_identity", target=scenario.domains["child"],
    )
    card = br.score_from_probes(scenario, {Milestone.SYSTEM_ON_HOST: probe})
    assert card.objective_proof_disposition == disposition
    result = br.compare_bare_vs_harness(scenario.name, [card], [card], allow_missing_rates=True)
    assert result.bare_outcomes[disposition] == 1


def test_actual_missing_probe_flows_to_unknown_summary():
    scenario = goad_scenarios()[0]
    card = br.score_from_probes(scenario, {})
    assert card.objective_proof_disposition == "unmeasured"
    result = br.compare_bare_vs_harness(scenario.name, [card], [card], allow_missing_rates=True)
    assert result.bare_objective_success_rate is None
    assert result.verdict == "insufficient-outcome-evidence"


def test_legacy_numeric_consumer_gets_explicit_missing_evidence_error():
    with pytest.raises(ValueError, match="objective rates are unavailable"):
        br.compare_bare_vs_harness("fixture", [_comparison_card(None)], [_comparison_card(False)])


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
