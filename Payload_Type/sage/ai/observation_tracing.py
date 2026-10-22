"""Metadata-only OpenTelemetry export for explicitly configured measurements.

This helper is deliberately inert until a caller constructs and instruments the
returned provider.  In particular, it does not choose an exporter or endpoint.
"""

from __future__ import annotations

import math
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace.status import Status


_PROJECT = "Sage"
_RESOURCE = Resource({"openinference.project.name": _PROJECT})
_SCOPE = InstrumentationScope("sage.measurement")
_TOKEN_ATTRIBUTES = frozenset(
    {
        "llm.token_count.prompt",
        "llm.token_count.completion",
        "llm.token_count.total",
        "llm.token_count.prompt_details.cache_input",
        "llm.token_count.prompt_details.cache_read",
        "llm.token_count.prompt_details.cache_write",
        "llm.token_count.completion_details.cache_read",
        "llm.token_count.completion_details.cache_write",
    }
)
_SPAN_KINDS = frozenset(
    {
        "TOOL", "CHAIN", "LLM", "RETRIEVER", "EMBEDDING", "AGENT",
        "RERANKER", "UNKNOWN", "GUARDRAIL", "EVALUATOR", "PROMPT",
    }
)


def _attributes(values: Any) -> dict[str, Any]:
    if not values:
        return {}
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key in _TOKEN_ATTRIBUTES:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
                continue
            out[key] = value
        elif key == "openinference.span.kind" and value in _SPAN_KINDS:
            out[key] = value
    return out


def _metadata_only(span: ReadableSpan) -> ReadableSpan:
    return ReadableSpan(
        name="sage.measurement",
        context=span.context,
        parent=span.parent,
        resource=_RESOURCE,
        attributes=_attributes(span.attributes),
        events=(),
        links=(),
        kind=span.kind,
        status=Status(status_code=span.status.status_code, description=None),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=_SCOPE,
    )


class MetadataOnlyExporter(SpanExporter):
    """Remove all dynamic trace content before delegating export."""

    def __init__(self, delegate: SpanExporter):
        if not hasattr(delegate, "export"):
            raise TypeError("delegate exporter required")
        self._delegate = delegate

    def export(self, spans: tuple[ReadableSpan, ...]) -> SpanExportResult:
        return self._delegate.export(tuple(_metadata_only(span) for span in spans))

    def shutdown(self) -> None:
        return self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)


def create_provider(delegate_exporter: SpanExporter, project: str = _PROJECT) -> TracerProvider:
    """Return a provider that exports only fixed metadata and numeric counts."""
    if project != _PROJECT:
        raise ValueError("measurement project is unsupported")
    provider = TracerProvider(resource=_RESOURCE)
    provider.add_span_processor(SimpleSpanProcessor(MetadataOnlyExporter(delegate_exporter)))
    return provider
