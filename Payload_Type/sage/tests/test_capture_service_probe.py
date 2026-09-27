import importlib.util
import json
import os
from pathlib import Path
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import asyncio
import time
import subprocess
import pytest


ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "skills/sage-eval-gauge/scripts/capture_service_probe.py"
SPEC = importlib.util.spec_from_file_location("capture_service_probe", SCRIPT)
service = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = service
assert SPEC.loader is not None; SPEC.loader.exec_module(service)


def test_producer_exit_stops_registration_without_waiting_for_timeout(monkeypatch):
    cancelled = []
    async def pending(*_a, **_kw):
        try: await asyncio.sleep(30)
        finally: cancelled.append(True)
    monkeypatch.setattr(service, "_chat_registration_after", pending)
    monkeypatch.setattr(service, "_pid_alive", lambda pid: False)
    with pytest.raises(service.ServiceProbeError, match="owned Sage exited"):
        asyncio.run(service._registration_with_producer(None, 30, 123, service._ServiceHandler(), time.time()+900))


def test_registration_timeout_is_distinct_from_service_deadline(monkeypatch):
    async def pending(*_a, **_kw): await asyncio.sleep(30)
    monkeypatch.setattr(service, "_chat_registration_after", pending)
    monkeypatch.setattr(service, "_pid_alive", lambda pid: True)
    with pytest.raises(service.ServiceProbeError, match="registration timed out"):
        asyncio.run(service._registration_with_producer(None, .01, 123, service._ServiceHandler(), time.time()+900))


def test_operation_timeout_does_not_claim_an_exhausted_allocation():
    assert service._safe_exception(TimeoutError("secret")) == (
        "ServiceOperationTimeout", "service operation timed out")
    assert service._safe_exception(service.ServiceProbeError("service active deadline exhausted")) == (
        "ServiceProbeError", "service active deadline exhausted")


def test_restart_failure_diagnostic_retains_cause_without_output():
    completed = service.subprocess.CompletedProcess([], 1, b"operator-secret",
        b"ERR: Sage relaunch did not produce a Sage/observer child under tmux pane 8\n")
    value = service._restart_failure_diagnostic(completed)
    assert value["error_category"] == "launch_child_not_detected"
    assert value["returncode"] == 1 and value["stdout_bytes"] == len(b"operator-secret")
    assert value["stderr_bytes"] > 0 and value["output_complete"] is True
    assert "operator-secret" not in json.dumps(value)

    unrelated = service.subprocess.CompletedProcess([], 2, b"usage: hidden secret", b"")
    other = service._restart_failure_diagnostic(unrelated)
    assert other["error_category"] == "sage_restart_failed" and other["returncode"] == 2
    assert "hidden" not in json.dumps(other)


def test_actual_restart_preflight_error_is_safely_projected(tmp_path):
    isolation = service._load_module(service.ROOT / "skills/sage-eval-gauge/scripts/diagnostic_isolation.py",
                                     "capture_restart_error_test_isolation")
    runtime = tmp_path / "runtime"; runtime.mkdir()
    missing = tmp_path / "operator-sensitive-interpreter"
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ["HOME"],
        "USER": os.environ.get("USER", "sage"), "LOGNAME": os.environ.get("LOGNAME", "sage"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb", "SAGE_CWD": str(runtime),
        "SAGE_VENV_PY": str(missing), "SAGE_STARTUP_DISCOVERY_SECONDS": "1"}
    command = isolation.isolated_command(
        ["/bin/bash", str(service.ROOT / "skills/sage-goad-reset/scripts/sage_restart.sh")],
        writable_paths=[tmp_path], read_only_paths=[service.ROOT], environment=environment,
        cwd=service.ROOT)
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=5)
    value = service._restart_failure_diagnostic(completed)
    assert value["error_category"] == "runtime_interpreter_missing" and value["returncode"] == 1
    assert value["stdout_bytes"] == len(completed.stdout) and value["stderr_bytes"] == len(completed.stderr)
    assert str(missing) not in json.dumps(value) and "operator-sensitive" not in json.dumps(value)


def test_capture_restart_detects_observer_supervisor_in_actual_tmux_namespace(tmp_path):
    isolation = service._load_module(service.ROOT / "skills/sage-eval-gauge/scripts/diagnostic_isolation.py",
                                     "capture_restart_test_isolation")
    runtime = tmp_path / "runtime"; runtime.mkdir(); (tmp_path / "bloodhound").mkdir()
    python = tmp_path / "fake-python"
    python.write_text('#!/bin/bash\nif [[ "$1" == "-c" ]]; then exit 0; fi\n'
                      'exec -a process_observer.py /bin/sleep 30\n'); python.chmod(0o700)
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ["HOME"],
        "USER": os.environ.get("USER", "sage"), "LOGNAME": os.environ.get("LOGNAME", "sage"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb",
        "SAGE_CWD": str(runtime), "SAGE_VENV_PY": str(python),
        "SAGE_BLOODHOUND_MCP_DIR": str(tmp_path / "bloodhound"),
        "SAGE_STARTUP_DISCOVERY_SECONDS": "2", "SAGE_STARTUP_STABILITY_SECONDS": "1",
        "RABBITMQ_PASSWORD": "fixture", "SAGE_LOCAL_MYTHIC_HOSTS": "0"}
    command = isolation.isolated_command(
        ["/bin/bash", str(service.ROOT / "skills/sage-goad-reset/scripts/sage_restart.sh")],
        writable_paths=[tmp_path], read_only_paths=[service.ROOT], environment=environment,
        cwd=service.ROOT)
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=8)
    assert completed.returncode == 0
    assert b"Sage relaunch verified" in completed.stdout
    assert b"process_observer.py" not in completed.stdout


def test_v5_shape_and_active_end_precede_reduced_cleanup(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_M3_C_NAMESPACE_INNER", "1")
    approval = {
        "schema": "sage-resource-recovery-amendment-v4", "amendment_ordinal": 4,
        "limit_changes": {"capture_correction_cycles": {"old": 6, "new": 6}},
        "engineering_seconds": 43200, "engineering_cleanup_seconds": 900,
        "diagnostic_session": {"stages": ["A", "B", "C"], "max_stage_starts": 24,
            "pooled_active_seconds": 7200, "stage_active_seconds": 600,
            "stage_cleanup_seconds": 120, "stage_total_seconds": 720,
            "calendar_deadlines_inherited": True},
        "capture_recovery": {"max_attempts": 6, "max_before_first_composite": 4,
            "consume_episode_and_setup_retry": True, "interpretable_outcome_replacement": False},
    }
    assert service._recovery_shape(approval)
    frozen, _, _, _ = _sealed(tmp_path)
    validated = service.validate(frozen, live=True)
    validated["amendment"] = approval
    now = __import__("time").time()
    stage = {"stage": "C", "attempt": 1, "started_epoch": now,
             "active_deadline_epoch": now + 600, "total_deadline_epoch": now + 720,
             "active_ended_epoch": None, "closed_epoch": None}
    order = []
    class Ledger:
        def status(self):
            return {"outstanding_reservations": [], "unknown_provider_usage": False,
                "provider_overrun": False, "service_probes": {validated["probe_id"]: {
                    "stages": [stage], "active_control_requests": 0, "cleanup_control_requests": 0}}}
        def end_diagnostic_active(self, probe_id):
            order.append("active-end")
            return {**stage, "active_ended_epoch": now + 1, "total_deadline_epoch": now + 121}
    class Receiver:
        def __init__(self, record, handler, temporary_parent):
            self.directory = temporary_parent / "fifo"; self.directory.mkdir(); Path(record).write_text("receipt\n")
        def __enter__(self): return self
        def __exit__(self, *_a): pass
    monkeypatch.setattr(service, "_load_module", lambda *_a, **_k: type("M", (), {
        "ObservationReceiver": Receiver,
        "pinned_restart_arguments": staticmethod(lambda *_a: [])}))
    validated.update(service_isolation=tmp_path / "isolation.json", service_isolation_sha256="a" * 64)
    monkeypatch.setattr(service, "_Pump", lambda *_a, **_k: type("P", (), {"start": lambda self: None,
        "finish": lambda self, *_a: None})())
    monkeypatch.setattr(service, "_local_sage_pids", lambda: set())
    monkeypatch.setattr(service, "_listener_owners", lambda _port: set())
    monkeypatch.setattr(service, "require_applied_amendment", lambda *_a, **_kw: approval)
    monkeypatch.setattr(service.subprocess, "run", lambda *_a, **_kw: type("R", (), {
        "returncode": 1, "stdout": b"", "stderr": b""})())
    monkeypatch.setattr(service, "_cleanup", lambda env, deadline: order.append(("cleanup", deadline)) or {
        "complete": True, "steps": []})
    result = service.execute(frozen, validated, Ledger(), diagnostic_stage=stage)
    assert result["service_ready"] is False and order == ["active-end", ("cleanup", now + 121)]


def test_c_outer_dispatches_full_inner_driver_and_requires_containment(monkeypatch, tmp_path):
    from tests.test_diagnostic_service_isolation import _config as isolation_config
    isolation_path, context = isolation_config(tmp_path, active=2, cleanup=8)
    canonical_result = context / "service-result.json"
    inner_result = service.inner_result_path(canonical_result)
    fake = context / "inert-c-driver.py"
    fake.write_text("""import hashlib,json,os,pathlib,sys
p=pathlib.Path(os.environ['SAGE_M3_C_INNER_STAGE_PATH']);assert hashlib.sha256(p.read_bytes()).hexdigest()==os.environ['SAGE_M3_C_INNER_STAGE_SHA256']
assert os.environ['SAGE_M3_C_NAMESPACE_INNER']=='1' and os.environ['SAGE_LOCAL_MYTHIC_HOSTS']=='0' and os.environ['RABBITMQ_HOST']=='127.0.0.1'
pathlib.Path(os.environ['RESULT']).write_text(json.dumps({'schema':'sage-m3-capture-service-result-v1','probe_id':'c-fixture','service_ready':True,'error_type':None,'error_code':None,'cleanup':{'complete':True,'steps':[]},'model_requests':0}))
""")
    spec_path = context / "service-spec.json"; spec_path.write_text("{}")
    frozen = service.FrozenServiceProbe(spec_path, service._digest(spec_path.read_bytes()), {
        "amendment": {"path": str(context / "amendment"), "sha256": "a" * 64, "id": "v5"}})
    now = time.time(); stage = {"stage": "C", "attempt": 1, "started_epoch": now,
        "active_deadline_epoch": now + 600, "total_deadline_epoch": now + 720,
        "active_ended_epoch": None, "closed_epoch": None}
    class Ledger:
        calls = 0
        def status(self):
            self.calls += 1
            current = stage if self.calls == 1 else {
                **stage, "active_ended_epoch": now + .1, "total_deadline_epoch": now + 120.1}
            return {"outstanding_reservations": [], "unknown_provider_usage": False,
                "provider_overrun": False, "service_probes": {"c-fixture": {"stages": [current]}}}
    validated = {"probe_id": "c-fixture", "config": context / "config",
        "ledger": context / "ledger", "service_isolation": isolation_path,
        "service_isolation_sha256": service._digest(isolation_path.read_bytes()),
        "result_path": canonical_result}
    monkeypatch.setattr(service, "require_applied_amendment", lambda *_a, **_k: {})
    monkeypatch.setitem(service._REQUIRED_FILE_ROLES, "capture_driver", fake)
    monkeypatch.setenv("RESULT", str(inner_result))
    result = service._execute_c_isolated(frozen, validated, Ledger(), stage)
    assert result["service_ready"] is True and result["inner_result_retained"] is True
    assert result["diagnostic_stage_active_ended"] is True
    assert len(result["inner_result_sha256"]) == 64 and result["isolation_receipt"]["sha256"]
    receipt = json.loads(Path(result["isolation_receipt"]["path"]).read_text())
    assert receipt["owned_processes_dead"] is True and receipt["relay_receipt"]["listener_ready"] is False


def test_exact_c_stage_ends_active_before_prelaunch_failure_with_actual_ledger(monkeypatch, tmp_path):
    from tests.test_resource_recovery_amendment import _continuation, _continuation_close
    budget, approval, _clock = _continuation(tmp_path)
    budget.start_service_probe("c-preflight", "continuation-v5")
    budget.start_diagnostic_stage("c-preflight", "A")
    _continuation_close(budget, tmp_path, "c-preflight", "A")
    budget.start_diagnostic_stage("c-preflight", "B")
    _continuation_close(budget, tmp_path, "c-preflight", "B")
    stage = budget.start_diagnostic_stage("c-preflight", "C")
    frozen = type("Frozen", (), {"value": {"observation": {"phoenix_url": "http://127.0.0.1:6006/"},
        "amendment": {}}})()
    validated = {"probe_id": "c-preflight", "config": budget.config_path,
        "ledger": budget.ledger_path, "record_path": tmp_path / "events.jsonl",
        "amendment": json.loads(approval.read_text()), "service_isolation": tmp_path / "isolation.json",
        "service_isolation_sha256": "a" * 64}
    monkeypatch.setenv("SAGE_M3_C_NAMESPACE_INNER", "1")
    class Receiver:
        def __init__(self, record, handler, temporary_parent):
            self.directory = temporary_parent / "receiver"; self.directory.mkdir()
        def __enter__(self): return self
        def __exit__(self, *_a): pass
    monkeypatch.setattr(service, "_load_module", lambda *_a, **_k: type("M", (), {
        "ObservationReceiver": Receiver,
        "pinned_restart_arguments": staticmethod(lambda *_a: [])}))
    monkeypatch.setattr(service, "_local_sage_pids", lambda: {123})
    launched = []; cleaned = []
    monkeypatch.setattr(service.subprocess, "run", lambda *_a, **_k: launched.append(True))
    monkeypatch.setattr(service, "_cleanup", lambda *_a, **_k: cleaned.append(True))
    result = service.execute(frozen, validated, budget, diagnostic_stage=stage)
    current = budget.status()["service_probes"]["c-preflight"]["stages"][-1]
    assert result["service_ready"] is False and not launched and not cleaned
    assert current["stage"] == "C" and current["attempt"] == stage["attempt"]
    assert current["active_ended_epoch"] is not None
    assert current["total_deadline_epoch"] <= current["active_ended_epoch"] + 120


def test_outer_c_rejects_missing_canonical_active_end(monkeypatch, tmp_path):
    result_path = tmp_path / "result.json"; now = time.time(); stage = {"stage": "C", "attempt": 1,
        "started_epoch": now, "active_deadline_epoch": now + 600, "total_deadline_epoch": now + 720,
        "active_ended_epoch": None, "closed_epoch": None}
    inner = service.inner_result_path(result_path); inner.write_text(json.dumps({
        "schema": service.RESULT_SCHEMA, "probe_id": "probe", "service_ready": True,
        "error_type": None, "error_code": None, "cleanup": {"complete": True, "steps": []}}))
    class Isolation:
        @staticmethod
        def load_config(*_a): return {"context_directory": tmp_path,
            "writable_paths": [str(tmp_path)], "active_deadline_monotonic": time.monotonic() + 100,
            "cleanup_deadline_monotonic": time.monotonic() + 100}
        @staticmethod
        def execute(_path, _sha, _command, _environment, receipt_path, **_kw):
            receipt_path.write_text("{}")
            return {"failure_type": None, "returncode": 0, "owned_processes_dead": True,
                "relay_receipt": {"listener_ready": False}, "namespace": {
                    "listener_started_receipt": {"listener_ready": True},
                    "listener_final_receipt": {"listener_ready": False}}}
    class Ledger:
        def status(self): return {"outstanding_reservations": [], "unknown_provider_usage": False,
            "provider_overrun": False, "service_probes": {"probe": {"stages": [stage]}}}
    monkeypatch.setattr(service, "_load_module", lambda *_a, **_k: Isolation)
    monkeypatch.setattr(service, "require_applied_amendment", lambda *_a, **_k: {})
    frozen = service.FrozenServiceProbe(tmp_path / "spec", "a" * 64,
        {"amendment": {"path": str(tmp_path / "approval"), "sha256": "b" * 64, "id": "v5"}})
    validated = {"probe_id": "probe", "config": tmp_path / "config", "ledger": tmp_path / "ledger",
        "service_isolation": tmp_path / "isolation", "service_isolation_sha256": "c" * 64,
        "result_path": result_path}
    result = service._execute_c_isolated(frozen, validated, Ledger(), stage)
    assert result["service_ready"] is False and result["diagnostic_stage_active_ended"] is False
    assert result["error_type"] == "ResourceBudgetError"


def test_service_retains_typed_child_failure_and_classifies_audit_attempt():
    handler = service._ServiceHandler()
    row = handler({"kind": "tool_event", "payload": {"event": "phoenix_process_startup_diagnostic",
        "status": "failure", "error_category": "argument_parser_rejected", "returncode": 2,
        "output_sha256": "a" * 64, "raw_stderr": "secret-do-not-retain"}})
    assert row["error_category"] == "argument_parser_rejected" and row["returncode"] == 2
    assert "secret" not in json.dumps(row)
    audit = handler({"kind": "control_request", "payload": {"audit_event": "socket.bind",
        "address": "private", "attempted": True}})
    assert audit["application_request"] is False and audit["audit_event"] == "socket.bind"
    assert not handler.control_requests and "private" not in json.dumps(audit)


def test_service_stream_identity_binds_first_valid_root_and_retains_child_record():
    handler=service._ServiceHandler()
    assert "tracee_pid" not in handler({"kind":"tool_event","payload":{"event":"observed_process_streams","tracee_pid":0,"streams":[]}})
    assert handler.tracee_pid is None
    root=handler({"kind":"tool_event","payload":{"event":"observed_process_streams","tracee_pid":122,"streams":[]}})
    child=handler({"kind":"tool_event","payload":{"event":"observed_process_streams","tracee_pid":167,"streams":[]}})
    assert root["tracee_pid"]==122 and child["tracee_pid"]==167 and handler.tracee_pid==122


def _sealed(tmp_path):
    parent = tmp_path / "approved-execution.json"
    parent.write_text(json.dumps({"status": "active", "live_authorized": True}))
    document = tmp_path / "M3_RECOVERY_AMENDMENT_V2.md"; document.write_text("approved bounded recovery\n")
    config = tmp_path / "budget.json"
    config.write_text(json.dumps({"schema": "sage-shared-resource-budget-v1", "resources": {},
                                  "calendar_seconds": 1, "cleanup_seconds": 1}))
    amendment = tmp_path / "approved-recovery-amendment-v2.json"
    amendment.write_text(json.dumps({"schema": "sage-resource-recovery-amendment-v1", "status": "approved",
        "amendment_id": "m3-recovery-v2-test", "service_probe": service._SERVICE_LIMITS,
        "limit_changes": {"capture_correction_cycles": {"old": 2, "new": 4}},
        "schedule_id": "m3-order-v1:00001", "exceptional_retry_ordinal": 2,
        "conditional_continuation_authorized": True, "original_aggregate_limits_unchanged": True,
        "live_authorized": "conditional per document", "base_config_sha256": service._digest(config.read_bytes()),
        "document": {"path": str(document), "sha256": service._digest(document.read_bytes())},
        "parent_authority": {"path": str(parent), "sha256": service._digest(parent.read_bytes())}}))
    source = tmp_path / "service-source.json"
    roles = {name: str(path) for name, path in service._REQUIRED_FILE_ROLES.items()}
    source.write_text(json.dumps({"schema": service.SOURCE_SCHEMA, "amendment_path": str(amendment),
        "amendment_sha256": service._digest(amendment.read_bytes()), "file_roles": roles,
        "files": {path: service._digest(Path(path).read_bytes()) for path in roles.values()}}))
    ledger = tmp_path / "shared-resource-ledger.jsonl"
    observation = tmp_path / "observation.jsonl"; result = tmp_path / "result.json"
    value = {"schema": service.SCHEMA,
        "amendment": {"path": str(amendment), "sha256": service._digest(amendment.read_bytes()),
                      "id": "m3-recovery-v2-test"},
        "source_binding": {"path": str(source), "sha256": service._digest(source.read_bytes())},
        "resources": {"config": str(config), "config_sha256": service._digest(config.read_bytes()),
                      "ledger": str(ledger), "probe_id": "service-test-001"},
        "observation": {"max_events": 1000, "max_trace_bytes": 1_048_576,
                        "record_path": str(observation), "result_path": str(result),
                        "phoenix_url": "http://127.0.0.1:6006/"}}
    probe = tmp_path / "service.json"; probe.write_text(json.dumps(value))
    return service.load(probe, service._digest(probe.read_bytes())), amendment, config, ledger


def test_service_plan_binds_approved_amendment_base_config_and_exact_sources(tmp_path):
    frozen, _amendment, _config, _ledger = _sealed(tmp_path)
    validated = service.validate(frozen, live=True)
    assert validated["probe_id"] == "service-test-001"
    changed = frozen.path.parent / "changed.json"
    value = json.loads(frozen.path.read_text()); value["observation"]["phoenix_url"] = "https://127.0.0.1:6006/"
    changed.write_text(json.dumps(value))
    try: service.validate(service.load(changed, service._digest(changed.read_bytes())), live=True)
    except service.ServiceProbeError as exc: assert "observation" in str(exc)
    else: assert False


def test_generated_service_observer_config_uses_supported_source_string_limit(monkeypatch, tmp_path):
    frozen, _amendment, _config, _ledger = _sealed(tmp_path)
    frozen.value["observation"]["max_trace_bytes"] = 2_097_152
    source = Path(frozen.value["source_binding"]["path"])
    work = tmp_path / "observer-work"; work.mkdir(mode=0o700)
    receiver_mod = service._load_module(service._REQUIRED_FILE_ROLES["observation_receiver"], "service_config_receiver")
    with receiver_mod.ObservationReceiver(tmp_path / "records.jsonl", lambda _event: {},
                                          temporary_parent=work) as receiver:
        config_path, config_sha, _receipt = service._observer_config(
            frozen, {"source_binding": source, "source_sha256": service._digest(source.read_bytes())},
            receiver.directory, work)
        config = json.loads(config_path.read_text())
        monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", str(receiver.directory))
        process = service._load_module(service._REQUIRED_FILE_ROLES["process_observer"], "service_config_process_observer")
        loaded = process.load_exec_config(config_path, config_sha, cwd=config["cwd"], python=config["python"])
    assert loaded["limits"]["max_line_bytes"] == 1_048_576
    assert loaded["limits"]["max_events"] == frozen.value["observation"]["max_events"]
    assert loaded["limits"]["max_trace_bytes"] == frozen.value["observation"]["max_trace_bytes"]


def test_replacement_service_binds_installed_phoenix_and_prior_authority(tmp_path):
    frozen, prior, config, ledger = _sealed(tmp_path)
    value = json.loads(prior.read_text())
    value.update(schema="sage-resource-recovery-amendment-v2", amendment_id="m3-recovery-v3-test",
        amendment_ordinal=2, replacement_service_probe=True, engineering_seconds=7200,
        correction_engineering_seconds=1500,
        prior_amendment={"path": str(prior), "sha256": service._digest(prior.read_bytes())},
        limit_changes={"capture_correction_cycles": {"old": 4, "new": 5}})
    amendment = tmp_path / "replacement.json"; amendment.write_text(json.dumps(value))
    spec = json.loads(frozen.path.read_text()); source_path = Path(spec["source_binding"]["path"])
    source = json.loads(source_path.read_text())
    source.update(amendment_path=str(amendment), amendment_sha256=service._digest(amendment.read_bytes()))
    installed = service.distribution("arize-phoenix")
    paths = [str(Path(installed.locate_file(rel)).resolve()) for rel in (
        "phoenix/services.py", "phoenix/session/session.py", "phoenix/server/main.py", "phoenix/server/cli/commands/serve.py")]
    dependency_files = {p: service._digest(Path(p).read_bytes()) for p in paths}
    source["files"].update(dependency_files)
    source["phoenix_dependency"] = {"version": service.version("arize-phoenix"), "files": dependency_files}
    source_path.write_text(json.dumps(source))
    spec["amendment"] = {"path": str(amendment), "sha256": service._digest(amendment.read_bytes()),
                         "id": value["amendment_id"]}
    spec["source_binding"]["sha256"] = service._digest(source_path.read_bytes())
    frozen.path.write_text(json.dumps(spec))
    assert service.validate(service.load(frozen.path, service._digest(frozen.path.read_bytes())), live=True)
    source["phoenix_dependency"]["version"] = "wrong"
    source_path.write_text(json.dumps(source)); spec["source_binding"]["sha256"] = service._digest(source_path.read_bytes())
    frozen.path.write_text(json.dumps(spec))
    with pytest.raises(service.ServiceProbeError, match="dependency"):
        service.validate(service.load(frozen.path, service._digest(frozen.path.read_bytes())), live=True)


def test_service_action_uses_one_start_owned_registration_and_service_only_cleanup(monkeypatch, tmp_path):
    frozen, amendment, config, ledger_path = _sealed(tmp_path); validated = service.validate(frozen, live=True)
    tracee_pid = os.getpid()
    class FakeLedger:
        def __init__(self): self.started = self.closed = False; self.calls = []
        def status(self):
                return {"outstanding_reservations": [], "unknown_provider_usage": False,
                    "provider_overrun": False, "service_probes": {validated["probe_id"]: {
                    "closed_epoch": 2 if self.closed else None, "active_control_requests": 1,
                    "cleanup_control_requests": 0}} if self.started else {}}
        def apply_amendment(self, path, digest): self.calls.append(("amend", Path(path), digest))
        def start_service_probe(self, probe_id, amendment_id):
            self.started = True; self.calls.append(("start", probe_id, amendment_id))
            now = __import__("time").time()
            return {"started_epoch": now, "active_deadline_epoch": now + 900,
                    "total_deadline_epoch": now + 1200,
                    "limits": service._SERVICE_LIMITS}
        def close_service_probe(self, probe_id): self.closed = True; self.calls.append(("close", probe_id))
    fake_ledger = FakeLedger()
    class Receiver:
        def __init__(self, _record, handler, temporary_parent):
            self.handler = handler; self.directory = temporary_parent / "fifo"; self.directory.mkdir()
            Path(_record).write_text("event receipt\n")
        def __enter__(self): return self
        def __exit__(self, *_a): pass
        def serve_one(self, _timeout):
            raise type("ObservationReceiverTimeout", (Exception,), {})()
    class Pump:
        def __init__(self, _receiver, handler): self.handler = handler
        def start(self):
            self.handler.tracee_pid = tracee_pid
            self.handler({"kind": "control_request", "pid": tracee_pid,
                "payload": {"request_id": "registration", "transport": "amqp"}})
            self.handler({"kind": "control_response", "pid": tracee_pid,
                "payload": {"request_id": "registration", "outcome": "returned", "reservation_id": "r1"}})
        def finish(self, _timeout=0): pass
    real_loader = service._load_module
    monkeypatch.setattr(service, "_load_module", lambda path, name: type("M", (), {"ObservationReceiver": Receiver})
                        if Path(path).name == "observation_receiver.py" else real_loader(path, name))
    monkeypatch.setattr(service, "_Pump", Pump)
    monkeypatch.setattr(service, "_local_sage_pids", lambda: set())
    monkeypatch.setattr(service, "require_applied_amendment", lambda *_a, **_kw: {})
    listeners = [set(), {tracee_pid}]; monkeypatch.setattr(service, "_listener_owners",
        lambda _port: listeners.pop(0) if listeners else set())
    monkeypatch.setattr(service, "_is_descendant", lambda pid, ancestor: pid == ancestor == tracee_pid)
    monkeypatch.setattr(service, "_startup_identity", lambda: {"pid": tracee_pid, "cwd": str(service.RUNTIME),
        "recorded_at": "9999-01-01T00:00:00+00:00"})
    chat_timeouts = []
    async def registration(_started, timeout):
        chat_timeouts.append(timeout)
        assert os.environ["SAGE_RESOURCE_SERVICE_PROBE_ID"] == "service-test-001"
        return {"id": 9, "container_running": True}
    monkeypatch.setattr(service, "_chat_registration_after", registration)
    monkeypatch.setattr(service, "_phoenix_liveness", lambda _url, timeout: {
        "status_code": 200, "ready": timeout <= 5})
    operations = []; timeouts = []
    def run(argv, **kwargs):
        class Completed: returncode = 0; stdout = b""; stderr = b""
        names = {Path(item).name for item in argv}
        name = ("restart" if "sage_restart.sh" in names else "stop" if "sage_stop.sh" in names
                else "archive" if "archive_runtime_dbs.py" in names else "unexpected")
        operations.append(name); timeouts.append(kwargs["timeout"])
        if name == "restart":
            assert sum(item.endswith("sage_restart.sh") for item in argv) == 1
            assert any(item == "SAGE_RESOURCE_SERVICE_PROBE_ID=service-test-001" for item in argv)
        if name == "stop":
            config_path = Path(kwargs["env"]["SAGE_PROCESS_OBSERVER_CONFIG"])
            receipt_path = Path(json.loads(config_path.read_text())["receipt_path"])
            value = json.loads(config_path.read_text())
            receipt_path.write_text(json.dumps({"schema": "sage-process-observer-preexec-receipt-v1",
                "config_sha256": service._digest(config_path.read_bytes()), "entry_sha256": value["entry_sha256"],
                "source_binding_sha256": value["source_binding_sha256"], "tracer_attested": True,
                "tracee_pid": tracee_pid, "failures": ["unsupported_sink_transfer"],
                "observed_pids": [tracee_pid], "ended_pids": [tracee_pid]}))
        return Completed()
    monkeypatch.setattr(service.subprocess, "run", run)
    result = service.execute(frozen, validated, fake_ledger)
    assert result["error_code"] is None, (result["error_code"], result["cleanup"], result["process_observer_receipt"])
    assert result["service_ready"] is True, result
    assert operations == ["restart", "stop", "archive"]
    assert 0 < timeouts[0] <= 30 and 0 < timeouts[1] <= 120 and 0 < timeouts[2] <= 180
    assert chat_timeouts and 60 < chat_timeouts[0] <= service._SERVICE_LIMITS["active_seconds"]
    assert [item[0] for item in fake_ledger.calls] == ["start", "close"]
    assert result["model_requests"] == 0 and result["resource_scope"]["closed_epoch"] == 2


@pytest.mark.parametrize("mode,expected_ready",[("complete",True),("missing",False),("early-error",False),("startup-mismatch",False)])
def test_receiver_survives_owned_preexec_settlement_before_terminal_cardinality(monkeypatch,tmp_path,mode,expected_ready):
    frozen,_amendment,_config,_ledger_path=_sealed(tmp_path);validated=service.validate(frozen,live=True)
    handler=service._ServiceHandler();monkeypatch.setattr(service,"_ServiceHandler",lambda:handler)
    observer=ROOT/"skills/sage-eval-gauge/scripts/process_observer.py";channel=ROOT/"Payload_Type/sage/ai/observation_channel.py"
    entry=tmp_path/"settlement.py";stop=tmp_path/"stop"
    entry.write_text("import importlib.util,os,pathlib,subprocess,time\n"
        "s=importlib.util.spec_from_file_location('settlement_channel',os.environ['CHANNEL_PATH']);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
        "time.sleep(.1);child=subprocess.Popen(['/bin/sleep','.2']);m.emit('tool_event',{'event':'observed_process_streams','tracee_pid':child.pid,'streams':[]});child.wait()\n"
        "ids=[str(i) for i in range(69)]\n"
        "for i in ids:m.emit('control_request',{'request_id':i,'transport':'fixture','application_request':False})\n"
        "while not pathlib.Path(os.environ['STOP']).exists():time.sleep(.01)\n"
        "if os.environ['MODE']!='missing':\n"
        " for i in ids:m.emit('control_response',{'request_id':i,'outcome':'returned','reservation_id':'reservation-'+i})\n")
    def observer_config(_f,_v,directory,work):
        receipt=work/"service-process-observer-receipt.json"
        value={"schema":"sage-process-observer-exec-config-v1","observer_path":str(observer),"observer_sha256":service._digest(observer.read_bytes()),
            "entry_path":str(entry),"entry_sha256":service._digest(entry.read_bytes()),"source_binding_path":str(validated["source_binding"]),"source_binding_sha256":validated["source_sha256"],
            "observation_channel_path":str(channel),"observation_channel_sha256":service._digest(channel.read_bytes()),"strace_path":"/usr/bin/strace","strace_sha256":service._digest(Path('/usr/bin/strace').read_bytes()),
            "python":sys.executable,"cwd":str(tmp_path),"argv":[sys.executable,"-P",str(entry)],"limits":{"timeout_seconds":20,"max_events":10000,"max_line_bytes":65536,"max_trace_bytes":16*1024*1024},
            "observation_directory":str(directory),"receipt_path":str(receipt)}
        path=work/"observer.json";path.write_text(json.dumps(value));os.chmod(path,0o600);return path,service._digest(path.read_bytes()),receipt
    monkeypatch.setattr(service,"_observer_config",observer_config)
    monkeypatch.setitem(service._REQUIRED_FILE_ROLES,"sage_main",entry)
    class Ledger:
        closed=False
        def status(self):return {"outstanding_reservations":[],"unknown_provider_usage":False,"provider_overrun":False,"service_probes":{validated["probe_id"]:{"closed_epoch":2 if self.closed else None,"active_control_requests":69,"cleanup_control_requests":0}}}
        def start_service_probe(self,*_a):
            now=time.time();return {"started_epoch":now,"active_deadline_epoch":now+30,"total_deadline_epoch":now+60,"limits":service._SERVICE_LIMITS}
        def close_service_probe(self,_id):self.closed=True
    launched=[]
    def restart(*_a,**_kw):
        config=Path(_kw.get("env",{}).get("SAGE_PROCESS_OBSERVER_CONFIG","") or next(x.split('=',1)[1] for x in _a[0] if x.startswith('SAGE_PROCESS_OBSERVER_CONFIG=')))
        environment=dict(os.environ,CHANNEL_PATH=str(channel),STOP=str(stop),MODE=mode,PYTHONDONTWRITEBYTECODE="1")
        p=subprocess.Popen([sys.executable,str(observer),"--exec-config",str(config),"--exec-config-sha256",service._digest(config.read_bytes()),"--cwd",str(tmp_path),"--python",sys.executable],cwd=tmp_path,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE);launched.append(p)
        deadline=time.monotonic()+8
        while len(handler.control_requests)<69 and time.monotonic()<deadline:time.sleep(.02)
        return type("Completed",(),{"returncode":0,"stdout":b"","stderr":b""})()
    monkeypatch.setattr(service.subprocess,"run",restart);monkeypatch.setattr(service,"_local_sage_pids",lambda:set())
    listener_calls=[set(),None];monkeypatch.setattr(service,"_listener_owners",lambda _p:listener_calls.pop(0) if listener_calls and listener_calls[0] is not None else {handler.tracee_pid})
    monkeypatch.setattr(service,"_is_descendant",lambda pid,ancestor:pid==ancestor)
    monkeypatch.setattr(service,"_startup_identity",lambda:{"pid":launched[0].pid+(mode=="startup-mismatch"),"cwd":str(service.RUNTIME),"recorded_at":"9999-01-01T00:00:00+00:00"})
    async def registration(*_a,**_kw):return {"id":9,"container_running":True}
    monkeypatch.setattr(service,"_registration_with_producer",registration)
    def liveness(*_a,**_kw):
        if mode=="early-error":raise service.ServiceProbeError("fresh native Chat registration is unavailable")
        return {"status_code":200,"ready":True}
    monkeypatch.setattr(service,"_phoenix_liveness",liveness);monkeypatch.setattr(service,"require_applied_amendment",lambda *_a,**_k:{})
    def cleanup(_env,_deadline):stop.write_text("stop");launched[0].wait(12);return {"complete":True,"steps":[]}
    monkeypatch.setattr(service,"_cleanup",cleanup)
    result=service.execute(frozen,validated,Ledger())
    assert result["service_ready"] is expected_ready and not Path(f"/proc/{launched[0].pid}").exists()
    assert isinstance(result["process_observer_receipt"],dict)
    assert "receiver_error" not in result["process_observer_receipt"].get("failures",[])
    if mode!="missing":assert len(handler.control_requests)==len(handler.control_responses)==69
    else:assert handler.control_responses==set()


def test_stale_service_blocks_before_amendment_and_start(monkeypatch, tmp_path):
    frozen, _amendment, _config, _ledger = _sealed(tmp_path); validated = service.validate(frozen, live=True)
    class FakeLedger:
        def status(self): return {"outstanding_reservations": [], "service_probes": {}}
        def apply_amendment(self, *_a): raise AssertionError("must fail before amendment")
    monkeypatch.setattr(service, "_local_sage_pids", lambda: {55})
    monkeypatch.setattr(service, "_listener_owners", lambda _port: set())
    class Receiver:
        def __init__(self, _record, _handler, temporary_parent):
            self.directory = temporary_parent / "fifo"; self.directory.mkdir()
        def __enter__(self): return self
        def __exit__(self, *_a): pass
    real_loader = service._load_module
    monkeypatch.setattr(service, "_load_module", lambda path, name: type("M", (), {"ObservationReceiver": Receiver})
                        if Path(path).name == "observation_receiver.py" else real_loader(path, name))
    result = service.execute(frozen, validated, FakeLedger())
    assert result["service_ready"] is False and result["error_type"] == "ServiceProbeError"


def test_chat_registration_timestamps_are_utc_and_reject_stale_rows():
    from datetime import datetime, timezone
    started = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
    base = {"name": "sage", "type": "chat", "container_running": True, "deleted": False}
    naive = service._fresh_chat_timestamp({**base, "updated_at": "2026-09-22T14:00:01"}, started)
    aware = service._fresh_chat_timestamp({**base, "updated_at": "2026-09-22T09:00:01-05:00"}, started)
    assert naive == aware == datetime(2026, 9, 22, 14, 0, 1, tzinfo=timezone.utc)
    assert service._fresh_chat_timestamp({**base, "updated_at": "2026-09-22T13:59:59"}, started) is None


def test_unexpected_secret_error_is_redacted_and_failure_cleanup_runs_once(monkeypatch, tmp_path, capsys):
    frozen, _amendment, _config, _ledger = _sealed(tmp_path); validated = service.validate(frozen, live=True)
    tracee_pid = os.getpid(); cleanup_calls = []
    class FakeLedger:
        closed = False
        def status(self):
            return {"outstanding_reservations": [], "service_probes": {validated["probe_id"]: {
                "closed_epoch": 2 if self.closed else None}}}
        def start_service_probe(self, *_a):
            now = __import__("time").time()
            return {"started_epoch": now, "active_deadline_epoch": now + 900,
                    "total_deadline_epoch": now + 1200, "limits": service._SERVICE_LIMITS}
        def close_service_probe(self, _probe_id): self.closed = True
    class Receiver:
        def __init__(self, record, _handler, temporary_parent):
            Path(record).write_text("event\n"); self.directory = temporary_parent / "fifo"; self.directory.mkdir()
        def __enter__(self): return self
        def __exit__(self, *_a): pass
    class Pump:
        def __init__(self, _receiver, handler): self.handler = handler
        def start(self): self.handler.tracee_pid = tracee_pid
        def finish(self, _timeout=0): pass
    real_loader = service._load_module
    monkeypatch.setattr(service, "_load_module", lambda path, name: type("M", (), {"ObservationReceiver": Receiver})
                        if Path(path).name == "observation_receiver.py" else real_loader(path, name))
    monkeypatch.setattr(service, "_Pump", Pump); monkeypatch.setattr(service, "_local_sage_pids", lambda: set())
    monkeypatch.setattr(service, "_listener_owners", lambda _port: set())
    monkeypatch.setattr(service, "require_applied_amendment", lambda *_a, **_kw: {})
    monkeypatch.setattr(service, "_startup_identity", lambda: {"pid": tracee_pid, "cwd": str(service.RUNTIME),
        "recorded_at": "9999-01-01T00:00:00+00:00"})
    monkeypatch.setattr(service.subprocess, "run", lambda *_a, **_kw: type("C", (), {
        "returncode": 0, "stdout": b"", "stderr": b""})())
    secret = "sdk-password=do-not-retain"
    async def fail(*_a, **_kw): raise RuntimeError(secret)
    monkeypatch.setattr(service, "_chat_registration_after", fail)
    monkeypatch.setattr(service, "_cleanup", lambda env, deadline: cleanup_calls.append((env, deadline)) or {
        "complete": False, "steps": []})
    monkeypatch.setattr(service, "_read_process_receipt", lambda *_a, **_kw: None)
    result = service.execute(frozen, validated, FakeLedger())
    retained = validated["result_path"]; service.retain(retained, result)
    print(json.dumps({"error_type": result["error_type"], "error_code": result["error_code"]}))
    assert len(cleanup_calls) == 1 and result["error_type"] == "UnexpectedError"
    assert secret not in retained.read_text() and secret not in capsys.readouterr().out


def test_expired_active_deadline_starts_no_readiness_or_cleanup(monkeypatch, tmp_path):
    frozen, _amendment, _config, _ledger = _sealed(tmp_path); validated = service.validate(frozen, live=True)
    calls = []
    class FakeLedger:
        def status(self): return {"outstanding_reservations": [], "service_probes": {}}
        def start_service_probe(self, *_a):
            now = __import__("time").time() - 901
            return {"started_epoch": now, "active_deadline_epoch": now + 900,
                    "total_deadline_epoch": now + 1200, "limits": service._SERVICE_LIMITS}
        def close_service_probe(self, *_a): calls.append("close")
    class Receiver:
        def __init__(self, _record, _handler, temporary_parent):
            self.directory = temporary_parent / "fifo"; self.directory.mkdir()
        def __enter__(self): return self
        def __exit__(self, *_a): pass
    class Pump:
        def __init__(self, *_a): pass
        def start(self): pass
        def finish(self, *_a): pass
    real_loader = service._load_module
    monkeypatch.setattr(service, "_load_module", lambda path, name: type("M", (), {"ObservationReceiver": Receiver})
                        if Path(path).name == "observation_receiver.py" else real_loader(path, name))
    monkeypatch.setattr(service, "_Pump", Pump); monkeypatch.setattr(service, "_local_sage_pids", lambda: set())
    monkeypatch.setattr(service, "_listener_owners", lambda _port: set())
    monkeypatch.setattr(service, "require_applied_amendment", lambda *_a, **_kw: {})
    monkeypatch.setattr(service.subprocess, "run", lambda *_a, **_kw: calls.append("subprocess"))
    result = service.execute(frozen, validated, FakeLedger())
    assert result["error_code"] == "service active deadline exhausted"
    assert calls == ["close"]


def test_phoenix_loopback_liveness_uses_metered_httpx_without_proxy(monkeypatch):
    if str(service.RUNTIME) not in sys.path: sys.path.insert(0, str(service.RUNTIME))
    from ai import observation_channel, resource_budget, resource_http
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *_a): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    calls = []; events = []
    class Ledger:
        def reserve(self, amounts, operation):
            calls.append((amounts, operation)); return f"reservation-{len(calls)}"
    monkeypatch.setattr(resource_budget, "configured_ledger", lambda: Ledger())
    monkeypatch.setattr(resource_http.observation_channel, "emit", lambda kind, payload: events.append((kind, payload)))
    monkeypatch.setenv(observation_channel.ENV_NAME, "/synthetic/fifo")
    try:
        result = service._phoenix_liveness(f"http://127.0.0.1:{server.server_port}/", timeout=2)
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
    assert result == {"status_code": 200, "ready": True}
    assert calls == [({"control_requests": 1}, "service-phoenix-readiness")]
    assert [kind for kind, _payload in events] == ["control_request", "control_response"]
    assert events[0][1]["request_id"] == events[1][1]["request_id"]
    assert events[1][1]["reservation_id"] == "reservation-1" and events[1][1]["outcome"] == "returned"
