from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from ai.observation_tracing import MetadataOnlyExporter, create_provider


MARKER = "never-export-this-credential"


def _dump(exporter: InMemorySpanExporter) -> str:
    spans = exporter.get_finished_spans()
    return "\n".join(
        json.dumps(
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [str(event) for event in span.events],
                "links": [str(link) for link in span.links],
                "status": str(span.status),
                "resource": dict(span.resource.attributes),
                "scope": str(span.instrumentation_scope),
            },
            sort_keys=True,
        )
        for span in spans
    )


def test_metadata_only_export_preserves_context_timing_kind_parent_counts_and_errors():
    sink = InMemorySpanExporter()
    provider = create_provider(sink)
    tracer = provider.get_tracer(MARKER, MARKER, attributes={MARKER: MARKER})
    with tracer.start_as_current_span("secret parent") as parent:
        with tracer.start_as_current_span("secret child") as child:
            child.set_attribute("input.value", MARKER)
            child.set_attribute("tool.description", MARKER)
            child.set_attribute("llm.token_count.prompt", 17)
            child.set_attribute("llm.token_count.prompt_details.cache_read", 5)
            child.set_attribute("llm.token_count.total", -4)
            child.set_attribute("llm.token_count.completion", True)
            child.set_attribute("openinference.span.kind", "TOOL")
            child.add_event(MARKER, {MARKER: MARKER})
            child.set_status(StatusCode.ERROR, MARKER)
    exported = sink.get_finished_spans()
    assert len(exported) == 2
    by_parent = {span.parent is None: span for span in exported}
    original_parent_id = parent.get_span_context().span_id
    child_span = by_parent[False]
    assert child_span.parent.span_id == original_parent_id
    assert child_span.context.trace_id == parent.get_span_context().trace_id
    assert child_span.start_time is not None and child_span.end_time >= child_span.start_time
    assert child_span.attributes == {
        "llm.token_count.prompt": 17,
        "llm.token_count.prompt_details.cache_read": 5,
        "openinference.span.kind": "TOOL",
    }
    assert child_span.status.status_code is StatusCode.ERROR
    assert child_span.status.description is None
    assert MARKER not in _dump(sink)


def test_installed_langchain_instrumentor_keeps_return_and_sanitizes_export(monkeypatch):
    sink = InMemorySpanExporter()
    provider = create_provider(sink)
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        from langchain_core.callbacks import BaseCallbackHandler, CallbackManager

        handler = BaseCallbackHandler()
        manager = CallbackManager([handler])
        run = manager.on_chain_start({"name": MARKER}, {"password": MARKER})
        result = {"credential": MARKER, "answer": "unchanged"}
        run.on_chain_end(result)
        assert result["answer"] == "unchanged"

        from langchain_core.tools import StructuredTool

        tool = StructuredTool.from_function(
            func=lambda value: {"echo": value},
            name=MARKER,
            description=MARKER,
        )
        assert tool.invoke({"value": MARKER}) == {"echo": MARKER}
        spans = sink.get_finished_spans()
        assert len(spans) >= 2 and all(span.name == "sage.measurement" for span in spans)
        assert all(dict(span.resource.attributes) == {"openinference.project.name": "Sage"} for span in spans)
        assert MARKER not in _dump(sink)
    finally:
        instrumentor.uninstrument()


def test_exception_marker_removed_but_exception_propagates():
    sink = InMemorySpanExporter()
    provider = create_provider(sink)
    tracer = provider.get_tracer("test")
    with pytest.raises(RuntimeError, match=MARKER):
        with tracer.start_as_current_span("failure", record_exception=True, set_status_on_exception=True):
            raise RuntimeError(MARKER)
    span = sink.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert not span.events
    assert MARKER not in _dump(sink)


def test_delegate_lifecycle_and_project_validation():
    calls = []
    delegate = SimpleNamespace(
        export=lambda spans: (calls.append(tuple(spans)), SpanExportResult.SUCCESS)[1],
        shutdown=lambda: calls.append("shutdown"),
        force_flush=lambda timeout: (calls.append(timeout), True)[1],
    )
    exporter = MetadataOnlyExporter(delegate)
    assert exporter.export(()) is SpanExportResult.SUCCESS
    assert exporter.force_flush(42)
    exporter.shutdown()
    assert calls == [(), 42, "shutdown"]
    with pytest.raises(ValueError, match="unsupported"):
        create_provider(InMemorySpanExporter(), project=MARKER)
