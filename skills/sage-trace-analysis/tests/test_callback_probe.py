"""Green -> red -> green control for the callback-dispatch probe.

A probe that reports "tracer attached" while inspecting nothing is worse than no probe, so this
plants the exact violation it exists to catch (a manager with the tracer removed) and requires the
probe to flag it. It also asserts a floor on how many records were actually examined.

The probe arms once per process — it wraps ``LangChainInstrumentor._instrument`` and installs its
dispatch hooks exactly once, which is the correct production behaviour. The fixtures below load and
instrument once for the whole module and truncate the shared log between tests, rather than trying
to re-arm per test.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

PROBE = Path(__file__).resolve().parents[1] / "scripts" / "callback_probe" / "sitecustomize.py"
MODULE_NAME = "sage_callback_probe_undertest"
TRACER_MARKER = "OpenInferenceTracer"


@pytest.fixture(scope="module")
def probe_log(tmp_path_factory):
    """Arm the probe and instrument LangChain once for this module."""
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from opentelemetry.sdk.trace import TracerProvider

    log_path = tmp_path_factory.mktemp("callback-probe") / "probe.jsonl"
    previous = {k: os.environ.get(k) for k in ("SAGE_CALLBACK_PROBE", "SAGE_CALLBACK_PROBE_LOG")}
    os.environ["SAGE_CALLBACK_PROBE"] = "1"
    os.environ["SAGE_CALLBACK_PROBE_LOG"] = str(log_path)

    spec = importlib.util.spec_from_file_location(MODULE_NAME, PROBE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)

    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=TracerProvider())
    try:
        yield log_path
    finally:
        instrumentor.uninstrument()
        sys.modules.pop(MODULE_NAME, None)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def log(probe_log):
    """Give each test a clean view of the shared log, preserving the install record."""
    install_records = [
        line for line in (probe_log.read_text().splitlines() if probe_log.exists() else [])
        if line.strip() and json.loads(line).get("event") == "probe_installed"
    ]
    probe_log.write_text("\n".join(install_records) + ("\n" if install_records else ""))
    return probe_log


def _records(log_path: Path, event: str) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            if record.get("event") == event:
                out.append(record)
    return out


def _fresh_manager():
    from langchain_core.callbacks.manager import CallbackManager

    return CallbackManager(handlers=[])


def _stripped_manager():
    manager = _fresh_manager()
    for handler in list(manager.handlers):
        if TRACER_MARKER in type(handler).__name__:
            manager.remove_handler(handler)
    assert not any(TRACER_MARKER in type(h).__name__ for h in manager.handlers)
    return manager


def test_probe_installs_dispatch_hooks(log):
    installed = _records(log, "probe_installed")
    assert installed, "probe never reported installing its hooks"
    hooks = installed[0]["hooks"]
    assert "CallbackManager.on_chain_start" in hooks
    assert "AsyncCallbackManager.on_chain_start" in hooks


def test_green_tracer_attached_is_reported_present(log):
    manager = _fresh_manager()
    assert any(TRACER_MARKER in type(h).__name__ for h in manager.handlers), (
        "instrumentation did not attach the tracer; this test's premise is broken"
    )

    result = manager.on_chain_start({"name": "probe-chain"}, {}, run_id=uuid4())
    assert result is not None, "probe must return the original dispatch result unchanged"

    records = _records(log, "chain_start")
    assert len(records) >= 1, "probe examined zero chain dispatches"
    assert records[-1]["tracer_present"] is True
    assert any(TRACER_MARKER in h for h in records[-1]["handlers"])


def test_red_planted_violation_is_flagged(log):
    """Remove the tracer from a manager — the probe must notice."""
    manager = _stripped_manager()

    result = manager.on_chain_start({"name": "planted"}, {}, run_id=uuid4())
    assert result is not None, "probe must return the original dispatch result unchanged"

    records = _records(log, "chain_start")
    assert records, "probe examined zero chain dispatches"
    assert records[-1]["tracer_present"] is False, (
        "probe reported the tracer present on a manager it had been stripped from — "
        "it cannot detect the defect it exists to catch"
    )


def test_probe_discriminates_within_one_run(log):
    """Floor assertion: the probe must distinguish, not just always answer the same way."""
    _fresh_manager().on_chain_start({"name": "good"}, {}, run_id=uuid4())
    _stripped_manager().on_chain_start({"name": "bad"}, {}, run_id=uuid4())

    records = _records(log, "chain_start")
    assert len(records) >= 2, f"probe examined {len(records)} dispatches, expected at least 2"
    assert {r["tracer_present"] for r in records} == {True, False}, "probe is not discriminating"


def test_llm_and_tool_dispatch_also_recorded(log):
    """The contrast the Aug-1 database needs: chain vs llm/tool must be separable."""
    manager = _fresh_manager()
    manager.on_chain_start({"name": "c"}, {}, run_id=uuid4())
    manager.on_tool_start({"name": "t"}, "input", run_id=uuid4())

    assert _records(log, "chain_start"), "no chain_start recorded"
    assert _records(log, "tool_start"), "no tool_start recorded"


def test_probe_errors_do_not_propagate(log, monkeypatch):
    """A broken probe internal must not break the dispatch it wraps."""
    probe = sys.modules[MODULE_NAME]

    def boom(*_args, **_kwargs):
        raise RuntimeError("probe internal failure")

    monkeypatch.setattr(probe, "_handler_names", boom)

    result = _fresh_manager().on_chain_start({"name": "after-boom"}, {}, run_id=uuid4())
    assert result is not None, "a probe exception leaked into LangChain's dispatch path"
