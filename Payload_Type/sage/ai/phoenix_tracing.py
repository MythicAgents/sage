"""Sage's Phoenix tracer registration for the normal (non-capture) runtime.

Phoenix runs inside the Sage process, and its own server traces each request it handles (FastAPI
telemetry, GraphQL) through the process-global tracer provider. Sage exports every span synchronously
(``batch=False``) so the last spans of a crashing request are not lost. If that synchronous exporter were
the global provider, the Phoenix server thread would export its own request span over gRPC to the
collector that the same blocked thread serves, and the UI would deadlock on its first request.

So Sage's provider stays out of the global slot, and Sage's span producers are given it explicitly: the
OpenInference LangChain instrumentor and the kernel tracer.
"""

from __future__ import annotations

from typing import Any


def register_sage_tracing() -> Any:
    """Register Sage's exporting tracer provider and attach Sage's instrumentation to it."""
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from phoenix.otel import register

    from ai.langgraph import kernel_tracing

    tracer_provider = register(
        project_name="Sage",
        auto_instrument=False,  # LangChain is instrumented explicitly below
        batch=False,
        set_global_tracer_provider=False,
    )
    LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
    kernel_tracing.use_tracer_provider(tracer_provider)
    return tracer_provider
