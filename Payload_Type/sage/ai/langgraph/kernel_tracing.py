"""First-party OpenTelemetry spans for Sage's deterministic execution kernel.

Sage's autonomous path runs the deterministic controller, not the LangGraph agent, so the
OpenInference LangChain instrumentor sees nothing but the occasional unparented tool or model call.
Before this module the product emitted no telemetry of its own at all, and an operator who ran Sage
autonomously and opened Phoenix found a scatter of BloodHound queries with no structure above them.

This module is deliberately thin: it obtains a tracer and hands back context managers. There is no
abstraction over OpenTelemetry, no span registry, and no buffering. Two properties matter:

**Emission only.** Nothing here inspects or alters a decision, an effect, an authority check, or a
control-flow outcome. A caller's behaviour must be identical with tracing on or off.

**Safe when unconfigured.** With no tracer provider installed (offline tests, a library import), the
OpenTelemetry API returns a no-op tracer and these spans cost almost nothing. If the package is
missing entirely, or `SAGE_KERNEL_TRACING` is falsey, the helpers degrade to a null span that
accepts the same calls and does nothing.

Nesting is the reason this pays for itself twice. The OpenInference LangChain tracer starts a span
with `context=None` when it has no parent run (`_tracer.py:168-181`), which makes OpenTelemetry fall
back to the *ambient* context. The kernel calls the policy model and the MCP tools without a config,
so those spans have no parent run and today land at the root. Opening a kernel span with
`start_as_current_span` puts a live span in the ambient context, so they nest underneath the step
that caused them without either call site being touched.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

try:  # the API package ships with phoenix/openinference; never make the kernel depend on it
    from opentelemetry import trace as _trace_api
except Exception:  # pragma: no cover - exercised only where opentelemetry is absent
    _trace_api = None  # type: ignore[assignment]

_TRACER_NAME = "sage.kernel"
_OFF_VALUES = {"0", "false", "no", "off"}

# Phoenix derives a span's displayed kind from this OpenInference attribute. Without it every kernel
# span renders as UNKNOWN, which is how the first instrumented run came out: correct data, unhelpful
# UI. CHAIN is the honest classification — these spans are steps that contain other spans, not model
# calls or tool calls, and the LLM/TOOL spans beneath them carry their own kinds already.
_SPAN_KIND_ATTRIBUTE = "openinference.span.kind"
_SPAN_KIND = "CHAIN"

_tracer_cache: Any = None


def tracing_enabled() -> bool:
    """False when the operator disabled tracing or the OpenTelemetry API is unavailable."""
    if _trace_api is None:
        return False
    return os.environ.get("SAGE_KERNEL_TRACING", "1").strip().lower() not in _OFF_VALUES


class _NullSpan:
    """Accepts the span calls this module makes and does nothing with them."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        return None

    def is_recording(self) -> bool:
        return False


def _tracer() -> Any:
    global _tracer_cache
    if not tracing_enabled():
        return None
    if _tracer_cache is None:
        _tracer_cache = _trace_api.get_tracer(_TRACER_NAME)
    return _tracer_cache


def _apply(span: Any, attributes: dict[str, Any]) -> None:
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            span.set_attribute(key, value if isinstance(value, (str, bool, int, float)) else str(value))
        except Exception:  # an attribute must never break the traced operation
            continue


@contextmanager
def kernel_span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open one kernel span, or yield a null span when tracing is off.

    The span is made current for the duration of the block so that LangChain-mediated calls made
    inside it — the policy model call, MCP tool invocations — nest beneath it via ambient context.
    """
    tracer = _tracer()
    if tracer is None:
        yield _NullSpan()
        return
    try:
        cm = tracer.start_as_current_span(name)
    except Exception:  # pragma: no cover - a broken provider must not break the kernel
        yield _NullSpan()
        return
    with cm as span:
        _apply(span, {_SPAN_KIND_ATTRIBUTE: _SPAN_KIND, **attributes})
        yield span


def record_seam_outcome(span: Any, status: str, detail: Any = None) -> None:
    """Record a controller seam's own status vocabulary ('ok' | 'timeout' | 'error') on its span.

    A seam converts exceptions into a status rather than raising, so without this a failed seam
    would leave a span that looks successful. `detail` carries the seam's diagnostic string.
    """
    try:
        span.set_attribute("sage.seam.status", str(status))
        if status != "ok":
            if detail is not None:
                span.set_attribute("sage.seam.detail", str(detail)[:2048])
            if _trace_api is not None:
                span.set_status(_trace_api.Status(_trace_api.StatusCode.ERROR, str(status)))
    except Exception:
        return None
