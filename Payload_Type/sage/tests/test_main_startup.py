from __future__ import annotations

import logging
from pathlib import Path
import runpy
import shutil
import sys
from types import ModuleType, SimpleNamespace

import pytest


SAGE_ROOT = Path(__file__).resolve().parents[1]


class _PhoenixReached(RuntimeError):
    pass


@pytest.mark.parametrize("phoenix_override", [False, True])
@pytest.mark.parametrize("metered", [False, True])
def test_main_creates_absolute_phoenix_working_dir_before_launch(monkeypatch, tmp_path, metered, phoenix_override):
    meter_calls = []
    if metered:
        monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", "fixture-config")
        meter = ModuleType("ai.resource_clients")
        meter.install = lambda: meter_calls.append("http-installed")
        monkeypatch.setitem(sys.modules, "ai.resource_clients", meter)
        rpc_meter = ModuleType("ai.resource_rpc")
        rpc_meter.install = lambda: meter_calls.append("rpc-installed")
        monkeypatch.setitem(sys.modules, "ai.resource_rpc", rpc_meter)
    else:
        monkeypatch.delenv("SAGE_RESOURCE_BUDGET_CONFIG", raising=False)
        monkeypatch.delenv("SAGE_RESOURCE_BUDGET_LEDGER", raising=False)
    monkeypatch.delenv("SAGE_OBSERVATION_DIRECTORY", raising=False)
    runtime_root = tmp_path / "sage-runtime"
    runtime_root.mkdir()
    runtime_main = runtime_root / "main.py"
    shutil.copy2(SAGE_ROOT / "main.py", runtime_main)
    # Default: the service dir's .phoenix. Override: an operator-set PHOENIX_WORKING_DIR must win so a
    # deployment on a filesystem SQLite can't lock (e.g. a macOS bind mount) can relocate the DB.
    if phoenix_override:
        expected = tmp_path / "custom-phoenix"
        monkeypatch.setenv("PHOENIX_WORKING_DIR", str(expected))
    else:
        monkeypatch.delenv("PHOENIX_WORKING_DIR", raising=False)
        expected = runtime_root / ".phoenix"

    dotenv = ModuleType("dotenv_bootstrap")
    dotenv.load_sage_dotenv = lambda: ()
    monkeypatch.setitem(sys.modules, "dotenv_bootstrap", dotenv)

    mythic = ModuleType("mythic_container")
    mythic.mythic_service = SimpleNamespace(start_and_run_forever=lambda: None)
    monkeypatch.setitem(sys.modules, "mythic_container", mythic)
    logging_module = ModuleType("mythic_container.logging")
    logging_module.logger = logging.getLogger("test-main-startup")
    monkeypatch.setitem(sys.modules, "mythic_container.logging", logging_module)
    monkeypatch.setitem(sys.modules, "sage_chat", ModuleType("sage_chat"))
    phoenix = ModuleType("phoenix")

    def launch_app(*, use_temp_dir):
        assert meter_calls == (["http-installed", "rpc-installed"] if metered else [])
        assert use_temp_dir is False
        configured = Path(__import__("os").environ["PHOENIX_WORKING_DIR"])
        assert configured == expected
        assert configured.is_absolute()
        assert configured.is_dir()
        raise _PhoenixReached

    phoenix.launch_app = launch_app
    monkeypatch.setitem(sys.modules, "phoenix", phoenix)
    phoenix_otel = ModuleType("phoenix.otel")
    phoenix_otel.register = lambda **_kwargs: object()
    monkeypatch.setitem(sys.modules, "phoenix.otel", phoenix_otel)
    instrumentation = ModuleType("openinference.instrumentation.langchain")
    instrumentation.LangChainInstrumentor = lambda: SimpleNamespace(
        instrument=lambda **_kwargs: None
    )
    monkeypatch.setitem(sys.modules, "openinference", ModuleType("openinference"))
    monkeypatch.setitem(
        sys.modules, "openinference.instrumentation", ModuleType("openinference.instrumentation")
    )
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.langchain", instrumentation)
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    try:
        with pytest.raises(_PhoenixReached):
            runpy.run_path(str(runtime_main), run_name="__main__")
    finally:
        if expected.exists():
            expected.rmdir()


@pytest.mark.parametrize("notification_configured", [False, True])
def test_capture_installs_audit_before_clients_and_uses_sanitized_provider(monkeypatch, tmp_path, notification_configured):
    calls = []
    monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", str(tmp_path / "observer"))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", "fixture-config")
    if notification_configured:
        monkeypatch.setenv("SAGE_FINDINGS_SLACK_WEBHOOK_URL", "https://secret.invalid/SYNTHETIC_SECRET")
        monkeypatch.setenv("SAGE_FINDINGS_SLACK_CHANNEL_ID", "CSECRET123")

    dotenv = ModuleType("dotenv_bootstrap"); dotenv.load_sage_dotenv = lambda: ()
    monkeypatch.setitem(sys.modules, "dotenv_bootstrap", dotenv)
    process = ModuleType("ai.observation_process")
    process.install = lambda: calls.append("audit")
    monkeypatch.setitem(sys.modules, "ai.observation_process", process)
    for name, label in (("ai.resource_clients", "http"), ("ai.resource_rpc", "rpc")):
        module = ModuleType(name); module.install = lambda label=label: calls.append(label)
        monkeypatch.setitem(sys.modules, name, module)

    mythic = ModuleType("mythic_container")
    mythic.mythic_service = SimpleNamespace(start_and_run_forever=lambda: calls.append("service"))
    monkeypatch.setitem(sys.modules, "mythic_container", mythic)
    log = ModuleType("mythic_container.logging"); log.logger = logging.getLogger("capture-main")
    monkeypatch.setitem(sys.modules, "mythic_container.logging", log)
    monkeypatch.setitem(sys.modules, "sage_chat", ModuleType("sage_chat"))
    slack = ModuleType("sage_chat.findings_slack")
    slack.SLACK_FINDINGS_WEBHOOK_ENV = "SAGE_FINDINGS_SLACK_WEBHOOK_URL"
    slack.SLACK_FINDINGS_CHANNEL_ID_ENV = "SAGE_FINDINGS_SLACK_CHANNEL_ID"
    sys.modules["sage_chat"].findings_slack = slack
    monkeypatch.setitem(sys.modules, "sage_chat.findings_slack", slack)
    observation = ModuleType("ai.observation_channel")
    observation.emit = lambda kind, payload: calls.append(("notification", kind, payload)) or True
    monkeypatch.setitem(sys.modules, "ai.observation_channel", observation)
    phoenix = ModuleType("phoenix")
    phoenix.launch_app = lambda **kw: calls.append(("phoenix", kw)) or SimpleNamespace(active=True)
    monkeypatch.setitem(sys.modules, "phoenix", phoenix)
    compatibility = ModuleType("ai.phoenix_process_compat")
    compatibility.launch_capture_phoenix = lambda px: px.launch_app(use_temp_dir=False, run_in_thread=False)
    monkeypatch.setitem(sys.modules, "ai.phoenix_process_compat", compatibility)
    phoenix_otel = ModuleType("phoenix.otel")
    phoenix_otel.register = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe register"))
    phoenix_otel.SimpleSpanProcessor = lambda: SimpleNamespace(span_exporter="phoenix-exporter")
    monkeypatch.setitem(sys.modules, "phoenix.otel", phoenix_otel)

    tracing = ModuleType("ai.observation_tracing")
    tracing.create_provider = lambda exporter: calls.append(("provider", exporter)) or "sanitized"
    monkeypatch.setitem(sys.modules, "ai.observation_tracing", tracing)
    instrumentation = ModuleType("openinference.instrumentation.langchain")
    instrumentation.LangChainInstrumentor = lambda: SimpleNamespace(
        instrument=lambda **kwargs: calls.append(("instrument", kwargs))
    )
    openinference = ModuleType("openinference")
    oi_instrumentation = ModuleType("openinference.instrumentation")
    oi_instrumentation.TraceConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "openinference", openinference)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", oi_instrumentation)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.langchain", instrumentation)
    otel = ModuleType("opentelemetry"); otel_trace = ModuleType("opentelemetry.trace")
    otel_trace.set_tracer_provider = lambda provider: calls.append(("global", provider))
    otel.trace = otel_trace
    monkeypatch.setitem(sys.modules, "opentelemetry", otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", otel_trace)

    monkeypatch.setattr("ssl_bootstrap.configure_sage_ssl_cert_file", lambda **_kw: None)
    runpy.run_path(str(SAGE_ROOT / "main.py"), run_name="__main__")
    assert calls[:4] == ["audit", "http", "rpc", ("notification", "config_snapshot", {
        "event": "effective_notification_config", "webhook_present": notification_configured,
        "channel_present": notification_configured,
    })]
    assert "SYNTHETIC_SECRET" not in repr(calls)
    assert ("phoenix", {"use_temp_dir": False, "run_in_thread": False}) in calls
    assert ("provider", "phoenix-exporter") in calls
    instrument = next(item[1] for item in calls if isinstance(item, tuple) and item[0] == "instrument")
    assert instrument["tracer_provider"] == "sanitized"
    assert instrument["config"].hide_inputs and instrument["config"].hide_outputs
    assert instrument["config"].hide_llm_invocation_parameters and instrument["config"].hide_llm_tools
    assert calls[-1] == "service"


@pytest.mark.parametrize("session", [None, SimpleNamespace(active=False)])
def test_capture_refuses_inactive_phoenix_before_mythic_service(monkeypatch, tmp_path, session):
    monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", str(tmp_path / "observer"))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", "fixture-config")
    dotenv = ModuleType("dotenv_bootstrap"); dotenv.load_sage_dotenv = lambda: ()
    monkeypatch.setitem(sys.modules, "dotenv_bootstrap", dotenv)
    for name in ("ai.observation_process", "ai.resource_clients", "ai.resource_rpc"):
        module = ModuleType(name); module.install = lambda: None; monkeypatch.setitem(sys.modules, name, module)
    calls = []
    mythic = ModuleType("mythic_container")
    mythic.mythic_service = SimpleNamespace(start_and_run_forever=lambda: calls.append("service"))
    monkeypatch.setitem(sys.modules, "mythic_container", mythic)
    log = ModuleType("mythic_container.logging"); log.logger = logging.getLogger("capture-main-fail")
    monkeypatch.setitem(sys.modules, "mythic_container.logging", log)
    sage_chat = ModuleType("sage_chat"); monkeypatch.setitem(sys.modules, "sage_chat", sage_chat)
    slack = ModuleType("sage_chat.findings_slack")
    slack.SLACK_FINDINGS_WEBHOOK_ENV = "WEBHOOK"; slack.SLACK_FINDINGS_CHANNEL_ID_ENV = "CHANNEL"
    sage_chat.findings_slack = slack; monkeypatch.setitem(sys.modules, "sage_chat.findings_slack", slack)
    observation = ModuleType("ai.observation_channel"); observation.emit = lambda *_a, **_k: True
    monkeypatch.setitem(sys.modules, "ai.observation_channel", observation)
    phoenix = ModuleType("phoenix")
    phoenix.launch_app = lambda **kw: calls.append(("phoenix", kw)) or session
    monkeypatch.setitem(sys.modules, "phoenix", phoenix)
    compatibility = ModuleType("ai.phoenix_process_compat")
    compatibility.launch_capture_phoenix = lambda px: px.launch_app(use_temp_dir=False, run_in_thread=False)
    monkeypatch.setitem(sys.modules, "ai.phoenix_process_compat", compatibility)
    phoenix_otel = ModuleType("phoenix.otel"); phoenix_otel.register = lambda **_kw: object()
    monkeypatch.setitem(sys.modules, "phoenix.otel", phoenix_otel)
    instrumentation = ModuleType("openinference.instrumentation.langchain")
    instrumentation.LangChainInstrumentor = lambda: SimpleNamespace(instrument=lambda **_kw: None)
    monkeypatch.setitem(sys.modules, "openinference", ModuleType("openinference"))
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", ModuleType("openinference.instrumentation"))
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.langchain", instrumentation)
    monkeypatch.setattr("ssl_bootstrap.configure_sage_ssl_cert_file", lambda **_kw: None)
    with pytest.raises(RuntimeError, match="Phoenix process did not become active"):
        runpy.run_path(str(SAGE_ROOT / "main.py"), run_name="__main__")
    assert calls == [("phoenix", {"use_temp_dir": False, "run_in_thread": False})]
