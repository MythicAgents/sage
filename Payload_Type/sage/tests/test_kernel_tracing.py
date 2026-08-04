"""Kernel tracing: spans are emitted, they nest, and emission changes nothing.

The load-bearing test here is not "spans appear" but `test_seam_contract_is_identical_with_tracing_on_and_off`.
Instrumentation that alters a controller outcome is worse than no instrumentation, and `start_as_current_span`
does mutate ambient context, so neutrality is asserted rather than assumed.

Each positive assertion has a control that proves it can fail: a disabled-tracing run that must produce no
spans, and a seam that raises, which must be recorded as an error rather than silently looking successful.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai.langgraph import kernel_tracing


@pytest.fixture
def spans(monkeypatch):
    """Install an in-memory tracer provider and hand back its finished-span accessor."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace_api, "get_tracer", lambda *a, **k: provider.get_tracer("test"))
    monkeypatch.setattr(kernel_tracing, "_tracer_cache", None)
    monkeypatch.setenv("SAGE_KERNEL_TRACING", "1")
    yield exporter
    kernel_tracing._tracer_cache = None


def _names(exporter):
    return [s.name for s in exporter.get_finished_spans()]


def test_kernel_span_emits(spans):
    with kernel_tracing.kernel_span("sage.kernel.test", **{"sage.k": "v"}):
        pass
    finished = spans.get_finished_spans()
    assert [s.name for s in finished] == ["sage.kernel.test"]
    assert finished[0].attributes["sage.k"] == "v"


def test_kernel_span_declares_its_openinference_kind(spans):
    """Without this attribute Phoenix renders every kernel span as UNKNOWN."""
    with kernel_tracing.kernel_span("sage.kernel.test"):
        pass
    assert spans.get_finished_spans()[0].attributes["openinference.span.kind"] == "CHAIN"


def test_caller_attributes_win_over_defaults(spans):
    """Control: the default kind must not clobber an explicit one."""
    with kernel_tracing.kernel_span("sage.kernel.test", **{"openinference.span.kind": "TOOL"}):
        pass
    assert spans.get_finished_spans()[0].attributes["openinference.span.kind"] == "TOOL"


def test_disabled_emits_nothing(spans, monkeypatch):
    """Control: with tracing off the same call must produce zero spans."""
    monkeypatch.setenv("SAGE_KERNEL_TRACING", "0")
    monkeypatch.setattr(kernel_tracing, "_tracer_cache", None)
    with kernel_tracing.kernel_span("sage.kernel.test"):
        pass
    assert spans.get_finished_spans() == ()


def test_no_opentelemetry_degrades_to_null_span(monkeypatch):
    """With the API absent the helpers must still work and accept the same calls."""
    monkeypatch.setattr(kernel_tracing, "_trace_api", None)
    monkeypatch.setattr(kernel_tracing, "_tracer_cache", None)
    assert kernel_tracing.tracing_enabled() is False
    with kernel_tracing.kernel_span("sage.kernel.test") as span:
        span.set_attribute("k", "v")
        span.set_status("ignored")
        assert span.is_recording() is False


def test_nested_span_parents_child(spans):
    """The whole point: a span opened inside a kernel span nests under it via ambient context."""
    with kernel_tracing.kernel_span("sage.kernel.outer"):
        trace_api.get_tracer("inner").start_span("child").end()
    finished = {s.name: s for s in spans.get_finished_spans()}
    assert finished["child"].parent is not None
    assert finished["child"].parent.span_id == finished["sage.kernel.outer"].context.span_id


def test_seam_outcome_records_error(spans):
    with kernel_tracing.kernel_span("sage.kernel.seam.x") as span:
        kernel_tracing.record_seam_outcome(span, "timeout", "x exceeded seam_timeout_s=5")
    finished = spans.get_finished_spans()[0]
    assert finished.attributes["sage.seam.status"] == "timeout"
    assert "seam_timeout_s" in finished.attributes["sage.seam.detail"]
    assert finished.status.status_code is trace_api.StatusCode.ERROR


def test_seam_outcome_ok_is_not_an_error(spans):
    """Control for the above: an ok seam must not be marked failed."""
    with kernel_tracing.kernel_span("sage.kernel.seam.x") as span:
        kernel_tracing.record_seam_outcome(span, "ok")
    finished = spans.get_finished_spans()[0]
    assert finished.attributes["sage.seam.status"] == "ok"
    assert finished.status.status_code is not trace_api.StatusCode.ERROR


# --- controller integration -------------------------------------------------------------------

def _controller():
    from ai.langgraph.autonomous_controller import AutonomousController

    return AutonomousController(
        observe=lambda: None,
        execute=lambda action: None,
        objective_met=lambda state: True,
        needs_collection=lambda state: False,
        collect=lambda: None,
        frontier_fn=lambda state: [],
    )


def _seam_result(controller, thunk, name, annotate=None):
    return asyncio.run(controller._seam(thunk, name, annotate=annotate))


def test_seam_emits_span_named_for_the_seam(spans):
    controller = _controller()
    status, value = _seam_result(controller, lambda: "payload", "policy_select")
    assert (status, value) == ("ok", "payload")
    assert "sage.kernel.seam.policy_select" in _names(spans)


def test_seam_records_a_raising_thunk_as_error(spans):
    """A seam converts exceptions into a status, so without recording it the span would look fine."""
    controller = _controller()

    def boom():
        raise RuntimeError("kaboom")

    status, value = _seam_result(controller, boom, "execute")
    assert status == "error" and "kaboom" in value
    finished = [s for s in spans.get_finished_spans() if s.name == "sage.kernel.seam.execute"][0]
    assert finished.attributes["sage.seam.status"] == "error"
    assert finished.status.status_code is trace_api.StatusCode.ERROR


def test_seam_contract_is_identical_with_tracing_on_and_off(spans, monkeypatch):
    """Neutrality: the (status, value) contract must not depend on whether tracing is enabled."""
    controller = _controller()

    def boom():
        raise ValueError("same either way")

    on_ok = _seam_result(controller, lambda: {"a": 1}, "observe")
    on_err = _seam_result(controller, boom, "observe")

    monkeypatch.setenv("SAGE_KERNEL_TRACING", "0")
    monkeypatch.setattr(kernel_tracing, "_tracer_cache", None)
    off_ok = _seam_result(controller, lambda: {"a": 1}, "observe")
    off_err = _seam_result(controller, boom, "observe")

    assert on_ok == off_ok
    assert on_err == off_err


def test_seam_span_carries_cycle_index(spans):
    controller = _controller()
    controller._current_cycle = 7
    _seam_result(controller, lambda: None, "execute")
    finished = [s for s in spans.get_finished_spans() if s.name == "sage.kernel.seam.execute"][0]
    assert finished.attributes["sage.cycle.index"] == 7


def test_run_emits_an_episode_span_and_returns_the_result(spans):
    controller = _controller()
    result = asyncio.run(controller.run())
    assert result is not None
    episode = [s for s in spans.get_finished_spans() if s.name == "sage.kernel.episode"]
    assert episode, "run() emitted no episode span"
    assert episode[0].attributes["sage.episode.status"] == str(result.status)
    assert episode[0].attributes["sage.episode.cycle_count"] == int(result.cycle_count)


class _EmptyState:
    """The minimum an observed state needs to get the controller into its cycle loop.

    `observe` returning None halts at "could not observe engagement state" *before* the loop, which is
    why an earlier version of this test saw no cycle span and was right to fail.
    """

    def achieved_effects(self):
        return []


def test_an_export_failure_is_loud_not_silent(caplog):
    """Anti-ISC-4b: a span that cannot be exported must produce a visible log line.

    The whole ISA started from a trace that looked populated while missing what mattered, so a *silent*
    export failure is the failure mode worth guarding. The OpenTelemetry SDK already logs one — this
    pins that behaviour so it cannot be quietly wrapped in a swallowing `except`, and so a future
    custom processor cannot regress it.

    Uses a raising exporter rather than a genuinely oversized span: the 4 MB gRPC limit needs a real
    collector, and what is being asserted is that *any* export failure is audible. Span-size headroom
    is ISC-12's job, separately.
    """
    import logging

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

    class _RaisingExporter(SpanExporter):
        def export(self, spans):
            raise RuntimeError("simulated export failure: span too large")

        def shutdown(self):
            return None

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_RaisingExporter()))
    tracer = provider.get_tracer("anti-isc-4b")

    with caplog.at_level(logging.ERROR):
        tracer.start_span("doomed").end()

    assert caplog.records, "an export failure produced no log record at all — it failed silently"
    assert any(
        "simulated export failure" in r.getMessage() or r.exc_info for r in caplog.records
    ), f"export failure logged nothing identifying it: {[r.getMessage() for r in caplog.records]}"


def test_a_successful_export_stays_quiet(caplog):
    """Control for the above: the assertion must not pass on noise from an ordinary export."""
    import logging

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with caplog.at_level(logging.ERROR):
        provider.get_tracer("quiet").start_span("fine").end()

    assert len(exporter.get_finished_spans()) == 1
    assert not caplog.records, f"a successful export logged errors: {[r.getMessage() for r in caplog.records]}"


def _looping_controller():
    """A controller that actually enters the cycle loop: objective unmet, frontier empty."""
    from ai.langgraph.autonomous_controller import AutonomousController

    return AutonomousController(
        observe=lambda: _EmptyState(),
        execute=lambda action: None,
        objective_met=lambda state: False,
        needs_collection=lambda state: False,
        collect=lambda: None,
        frontier_fn=lambda state: [],
    )


def test_cycle_span_is_emitted_and_parents_its_seams(spans):
    """ISC-3: one span per controller cycle, with that cycle's seams nested beneath it."""
    asyncio.run(_looping_controller().run())
    finished = spans.get_finished_spans()

    cycles = [s for s in finished if s.name == "sage.kernel.cycle"]
    assert cycles, "no sage.kernel.cycle span emitted"
    assert cycles[0].attributes["sage.cycle.index"] == 1

    episode = [s for s in finished if s.name == "sage.kernel.episode"]
    assert episode, "no episode span"
    assert cycles[0].parent is not None
    assert cycles[0].parent.span_id == episode[0].context.span_id

    cycle_ids = {s.context.span_id for s in cycles}
    seams = [s for s in finished if s.name.startswith("sage.kernel.seam.")]
    assert seams, "no seam spans to check nesting against"
    in_cycle = [s for s in seams if s.parent and s.parent.span_id in cycle_ids]
    assert in_cycle, "no seam span nested under a cycle span"


def test_cycle_count_matches_the_controller_result(spans):
    """The independent oracle: Phoenix must agree with the controller's own cycle count."""
    result = asyncio.run(_looping_controller().run())
    cycles = [s for s in spans.get_finished_spans() if s.name == "sage.kernel.cycle"]
    assert len(cycles) == result.cycle_count or len(cycles) == result.cycle_count + 1, (
        f"{len(cycles)} cycle spans vs controller cycle_count={result.cycle_count}; "
        "a cycle that halts before recording a CycleRecord may legitimately differ by one"
    )
    assert [c.attributes["sage.cycle.index"] for c in cycles] == list(range(1, len(cycles) + 1))


class _FakeDecision:
    def __init__(self, disposition, rationale, observed, capability="", target=""):
        self.disposition = disposition
        self.rationale = rationale
        self.policy_mode = "hybrid"
        self.model_response_observed = observed
        self.selected_capability = capability
        self.selected_target = target


def test_policy_span_records_a_failed_model_call(spans):
    """The gap this closes: a model failure became a stop decision visible nowhere in the tree."""
    controller = _controller()
    decision = _FakeDecision("stop", "hybrid policy call failed: ConnectError: no route to host", False)
    _seam_result(controller, lambda: decision, "policy_select", annotate=controller._annotate_policy_span)

    span = [s for s in spans.get_finished_spans() if s.name == "sage.kernel.seam.policy_select"][0]
    assert span.attributes["sage.seam.status"] == "ok", "seam vocabulary must be unchanged"
    assert span.attributes["sage.policy.disposition"] == "stop"
    assert span.attributes["sage.policy.model_response_observed"] is False
    assert "ConnectError" in span.attributes["sage.policy.rationale"]


def test_policy_span_records_a_successful_selection(spans):
    """Control: a healthy decision must look different from a failed one."""
    controller = _controller()
    decision = _FakeDecision("select", "picked the first admissible hop", True, "dcsync-krbtgt", "essos.local")
    _seam_result(controller, lambda: decision, "policy_select", annotate=controller._annotate_policy_span)

    span = [s for s in spans.get_finished_spans() if s.name == "sage.kernel.seam.policy_select"][0]
    assert span.attributes["sage.policy.disposition"] == "select"
    assert span.attributes["sage.policy.model_response_observed"] is True
    assert span.attributes["sage.policy.selected_capability"] == "dcsync-krbtgt"


def test_a_broken_annotator_cannot_break_the_seam(spans):
    """Telemetry must never affect the contract it observes."""
    controller = _controller()

    def boom(span, status, value):
        raise RuntimeError("annotator exploded")

    status, value = _seam_result(controller, lambda: "payload", "policy_select", annotate=boom)
    assert (status, value) == ("ok", "payload")


def test_tracer_cache_survives_a_provider_registered_later():
    """The eval path registers Phoenix mid-process; a cached tracer must still resolve to it.

    `evals/harness.py:ensure_phoenix_instrumentation` calls `register()` long after import, so
    `kernel_tracing` may have already cached a tracer obtained with no provider installed. That is safe
    only because the OpenTelemetry API hands back a ProxyTracer that binds late. If anyone "optimises"
    `_tracer()` into an eager concrete tracer, the eval path silently stops emitting kernel spans and
    every case reads empty. Asserted in a subprocess because a global provider can only be set once.
    """
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    sage_root = _Path(__file__).resolve().parents[1]
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from ai.langgraph import kernel_tracing\n"
        "from opentelemetry import trace as t\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "from opentelemetry.sdk.trace.export import SimpleSpanProcessor\n"
        "from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter\n"
        "with kernel_tracing.kernel_span('before_provider'): pass\n"
        "exp = InMemorySpanExporter(); p = TracerProvider()\n"
        "p.add_span_processor(SimpleSpanProcessor(exp))\n"
        "t.set_tracer_provider(p)\n"
        "with kernel_tracing.kernel_span('after_provider'): pass\n"
        "print(','.join(s.name for s in exp.get_finished_spans()))\n"
    ) % str(sage_root)

    out = subprocess.run(
        [_sys.executable, "-c", script], capture_output=True, text=True, cwd=str(sage_root), timeout=180
    )
    assert out.returncode == 0, out.stderr
    captured = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    assert "after_provider" in captured, (
        f"a tracer cached before registration did not bind to the later provider; captured={captured!r}"
    )


def test_seam_spans_nest_under_the_episode(spans):
    """Ambient nesting inside the real controller, not just a synthetic span."""
    controller = _controller()
    asyncio.run(controller.run())
    finished = spans.get_finished_spans()
    episode = [s for s in finished if s.name == "sage.kernel.episode"]
    seams = [s for s in finished if s.name.startswith("sage.kernel.seam.")]
    if not seams:
        pytest.skip("this controller configuration completed without entering a seam")
    assert episode
    assert all(s.parent is not None for s in seams)
