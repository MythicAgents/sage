"""Log-only probe for LangChain callback dispatch, loaded via PYTHONPATH.

Answers one question about a Sage run: when a graph node executes, does the callback manager that
dispatches its chain event still carry the OpenInference tracer handler?

Three outcomes are distinguishable from the log this writes, and they point at different defects:

  A. ``on_chain_start`` dispatched, tracer NOT among the handlers
     -> the tracer is not reaching that manager. Handler-attachment defect.
  B. ``on_chain_start`` never dispatched at all
     -> the graph is not emitting chain events; the tracer never had a chance.
  C. dispatched WITH the tracer present, yet no CHAIN span lands in phoenix.db
     -> the loss is inside the tracer or the export path, not the callback wiring.

The probe is observation only. Every hook calls through to the original, returns exactly what the
original returned, and swallows its own exceptions — a bug in here must never change what Sage does.

Enable::

    skills/sage-goad-reset/scripts/sage_restart.sh \
        PYTHONPATH=<repo>/skills/sage-trace-analysis/scripts/callback_probe \
        SAGE_CALLBACK_PROBE=1

With ``SAGE_CALLBACK_PROBE`` unset this module imports and does nothing, so leaving the PYTHONPATH
entry in place is harmless.

Environment:
  SAGE_CALLBACK_PROBE       "1" to arm. Anything else disables every hook.
  SAGE_CALLBACK_PROBE_LOG   output path. Default .sage_history/diagnostics/callback-probe/<pid>.jsonl
  SAGE_CALLBACK_PROBE_MAX   per-event-type record cap. Default 2000. Counters keep running after.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
from collections import Counter
from pathlib import Path

_ENABLED = os.environ.get("SAGE_CALLBACK_PROBE") == "1"

_MAX_PER_EVENT = int(os.environ.get("SAGE_CALLBACK_PROBE_MAX") or 2000)
_TRACER_MARKER = "OpenInferenceTracer"

_lock = threading.Lock()
_counts: Counter = Counter()
_written: Counter = Counter()
_log_path: Path | None = None
_probe_errors = 0


def _default_log_path() -> Path:
    # .../skills/sage-trace-analysis/scripts/callback_probe/sitecustomize.py -> parents[4] == repo root
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / ".sage_history" / "diagnostics" / "callback-probe" / f"{os.getpid()}.jsonl"


def _emit(event: str, **fields: object) -> None:
    """Append one JSONL record, capped per event type. Never raises."""
    global _probe_errors
    try:
        with _lock:
            _counts[event] += 1
            if _written[event] >= _MAX_PER_EVENT:
                return
            _written[event] += 1
            record = {"event": event, "pid": os.getpid(), "seq": _counts[event], **fields}
            assert _log_path is not None
            with _log_path.open("a") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
    except Exception:  # a probe failure must stay invisible to Sage
        _probe_errors += 1


def _handler_names(manager: object) -> list[str]:
    names: list[str] = []
    for attr in ("handlers", "inheritable_handlers"):
        for handler in getattr(manager, attr, None) or []:
            names.append(type(handler).__name__)
    return names


def _dispatch_hook(cls: type, method_name: str, event: str) -> None:
    """Wrap a manager-level dispatch method to record whether the tracer is attached."""
    original = getattr(cls, method_name, None)
    if original is None or getattr(original, "_sage_probe", False):
        return

    def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            names = _handler_names(self)
            _emit(
                event,
                manager=type(self).__name__,
                tracer_present=any(_TRACER_MARKER in n for n in names),
                handlers=sorted(set(names)),
                parent_run_id=str(getattr(self, "parent_run_id", None)),
            )
        except Exception:
            pass
        return original(self, *args, **kwargs)

    wrapper._sage_probe = True  # type: ignore[attr-defined]
    setattr(cls, method_name, wrapper)


def _install_hooks() -> None:
    """Attach the dispatch hooks. Called after LangChainInstrumentor has finished instrumenting."""
    from langchain_core.callbacks import manager as cb_manager

    targets = [
        ("CallbackManager", "on_chain_start", "chain_start"),
        ("AsyncCallbackManager", "on_chain_start", "chain_start"),
        ("CallbackManager", "on_llm_start", "llm_start"),
        ("AsyncCallbackManager", "on_llm_start", "llm_start"),
        ("CallbackManager", "on_chat_model_start", "chat_model_start"),
        ("AsyncCallbackManager", "on_chat_model_start", "chat_model_start"),
        ("CallbackManager", "on_tool_start", "tool_start"),
        ("AsyncCallbackManager", "on_tool_start", "tool_start"),
    ]
    installed = []
    for cls_name, method_name, event in targets:
        cls = getattr(cb_manager, cls_name, None)
        if cls is None:
            continue
        _dispatch_hook(cls, method_name, event)
        installed.append(f"{cls_name}.{method_name}")
    _emit("probe_installed", hooks=installed, log=str(_log_path))


def _arm() -> None:
    """Wrap LangChainInstrumentor._instrument so hooks land immediately after instrumentation."""
    from openinference.instrumentation.langchain import LangChainInstrumentor

    original = LangChainInstrumentor._instrument
    if getattr(original, "_sage_probe", False):
        return

    def wrapper(self, **kwargs):  # type: ignore[no-untyped-def]
        result = original(self, **kwargs)
        try:
            _install_hooks()
        except Exception as exc:  # never block instrumentation
            _emit("probe_install_failed", error=f"{type(exc).__name__}: {exc}")
        return result

    wrapper._sage_probe = True  # type: ignore[attr-defined]
    LangChainInstrumentor._instrument = wrapper


def _report() -> None:
    try:
        with _lock:
            summary = dict(_counts)
            errors = _probe_errors
        assert _log_path is not None
        with _log_path.open("a") as fh:
            fh.write(json.dumps({"event": "probe_summary", "pid": os.getpid(),
                                 "counts": summary, "probe_errors": errors}) + "\n")
    except Exception:
        pass


if _ENABLED:
    try:
        _log_path = Path(os.environ.get("SAGE_CALLBACK_PROBE_LOG") or _default_log_path())
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _arm()
        atexit.register(_report)
    except Exception as exc:
        # Arming failed; say so on stderr and let Sage start normally.
        print(f"[sage-callback-probe] disabled, arming failed: {type(exc).__name__}: {exc}")
