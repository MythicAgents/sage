import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time

import pytest


ROOT = Path(__file__).parents[3]
SOURCE = ROOT / "skills/sage-eval-gauge/scripts/diagnostic_service_isolation.py"
SPEC = importlib.util.spec_from_file_location("diagnostic_service_isolation", SOURCE)
service = importlib.util.module_from_spec(SPEC); assert SPEC.loader is not None; SPEC.loader.exec_module(service)


def _sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def _config(tmp_path, *, active=2, cleanup=5, require_wasm=True):
    context = tmp_path / "context"; context.mkdir(mode=0o700)
    empty = context / "phoenix.toml"; empty.write_bytes(b""); os.chmod(empty, 0o600)
    active_at, cleanup_at = time.monotonic() + active, time.monotonic() + cleanup
    relay = context / "relay.json"; relay.write_text(json.dumps({
        "schema": "sage-diagnostic-loopback-relay-v1", "context_directory": str(context),
        "ports": [5672, 7443], "deadline_monotonic": cleanup_at}))
    relay_source = ROOT / "skills/sage-eval-gauge/scripts/diagnostic_loopback_relay.py"
    isolation_source = ROOT / "skills/sage-eval-gauge/scripts/diagnostic_isolation.py"
    discovered = os.environ.get("SAGE_DIAGNOSTIC_TEST_WASM_PATH")
    source = Path(discovered) if discovered else ROOT / "Payload_Type/sage/.phoenix/wasm/python-3.12.0.wasm"
    wasm = context / "approved-phoenix.wasm"
    if source.is_file(): shutil.copyfile(source, wasm)
    elif require_wasm: pytest.skip("approved Phoenix WASM fixture is unavailable")
    config = context / "service.json"; config.write_text(json.dumps({"schema": service.SCHEMA,
        "context_directory": str(context), "relay_config": {"path": str(relay), "sha256": _sha(relay)},
        "relay_source": {"path": str(relay_source), "sha256": _sha(relay_source)},
        "isolation_source": {"path": str(isolation_source), "sha256": _sha(isolation_source)},
        "phoenix_client_config": {"path": str(empty), "sha256": _sha(empty)},
        "writable_paths": [str(context)], "active_deadline_monotonic": active_at,
        "cleanup_deadline_monotonic": cleanup_at,
        "wasm": {"path": str(wasm), "sha256": service.WASM_SHA256}}))
    return config, context


def test_complete_command_is_namespaced_relay_ready_nonlocal_blocked_and_descendants_die(tmp_path):
    config, context = _config(tmp_path); payload = context / "payload.json"
    script = r'''
import json,os,signal,socket,subprocess,sys,threading,time
s=socket.socket();s.bind(("127.0.0.1",0));s.listen(1)
def serve():
 c,_=s.accept();data=c.recv(16);c.sendall(b"ok" if data==b"local" else b"bad");c.close()
t=threading.Thread(target=serve);t.start();c=socket.create_connection(("127.0.0.1",s.getsockname()[1]));c.sendall(b"local");local=c.recv(16);c.close();t.join()
try:socket.create_connection(("192.0.2.1",9),.2);outbound="connected"
except OSError as e:outbound=e.errno
g=subprocess.Popen([sys.executable,"-P","-c","import os,signal,time;os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"])
nspid=next(line.split()[1:] for line in open(f"/proc/{g.pid}/status") if line.startswith("NSpid:"))
open(os.environ["RESULT"],"w").write(json.dumps({"local":local.decode(),"outbound":outbound,"inner_grandchild":int(nspid[-1])}));time.sleep(30)
'''
    receipt = context / "outer-receipt.json"
    result = service.execute(config, _sha(config), [sys.executable, "-P", "-c", script],
        {"RESULT": str(payload), "PYTHONDONTWRITEBYTECODE": "1"}, receipt)
    observed = json.loads(payload.read_text())
    assert observed["local"] == "ok" and observed["outbound"] != "connected" and result["timed_out"] is True
    assert 0 < result["namespace"]["inner_supervisor_pid"] < result["namespace"]["command_inner_pid"]
    assert observed["inner_grandchild"] > result["namespace"]["command_inner_pid"]
    assert result["namespace"]["listener_started_receipt"]["listener_ready"] is True
    assert result["owned_processes_dead"] is True and result["relay_receipt"]["listener_ready"] is False
    assert not list(context.glob("*.sock")) and all(not Path(f"/proc/{pid}").exists()
                                                  for pid in result["descendant_host_pids"])


def test_child_signal_before_deadline_is_not_timeout(tmp_path):
    config, context = _config(tmp_path); receipt = context / "signal.json"
    result = service.execute(config, _sha(config),
        [sys.executable, "-P", "-c", "import os,signal;os.kill(os.getpid(),signal.SIGTERM)"],
        {"PYTHONDONTWRITEBYTECODE": "1"}, receipt)
    assert result["namespace"]["command_returncode"] == -signal.SIGTERM
    assert result["namespace"]["timed_out"] is False and result["timed_out"] is False


@pytest.mark.parametrize("case", ["config-hash", "wasm", "phoenix", "port"])
def test_bound_configuration_drift_fails_before_dispatch(monkeypatch, tmp_path, case):
    config, context = _config(tmp_path, require_wasm=case not in {"config-hash", "wasm"})
    value = json.loads(config.read_text())
    digest = _sha(config)
    if case == "config-hash": digest = "0" * 64
    elif case == "wasm":
        value["wasm"]["sha256"] = "0" * 64; config.write_text(json.dumps(value)); digest = _sha(config)
    else:
        if case == "phoenix": Path(value["phoenix_client_config"]["path"]).write_text("endpoint=remote")
        else:
            relay_path = Path(value["relay_config"]["path"])
            relay_value = json.loads(relay_path.read_text()); relay_value["ports"] = [5672]
            relay_path.write_text(json.dumps(relay_value)); value["relay_config"]["sha256"] = _sha(relay_path)
            config.write_text(json.dumps(value)); digest = _sha(config)
    monkeypatch.setattr(service.subprocess, "Popen", lambda *_a, **_k: pytest.fail("dispatch reached"))
    with pytest.raises(service.DiagnosticServiceIsolationError):
        service.execute(config, digest, [sys.executable, "-c", "pass"], {}, context / "receipt.json")


def test_pinned_restart_arguments_are_last_writer_values(tmp_path):
    config, _context = _config(tmp_path)
    values = dict(item.split("=", 1) for item in service.pinned_restart_arguments(config, _sha(config)))
    assert values["RABBITMQ_HOST"] == values["MYTHIC_SERVER_HOST"] == "127.0.0.1"
    assert values["RABBITMQ_PORT"] == "5672" and values["PHOENIX_GRPC_PORT"] == "4317"
    assert values["PHOENIX_CLIENT_CONFIG"].endswith("phoenix.toml")
    assert values["PHOENIX_WASM_BINARY_PATH"].endswith("approved-phoenix.wasm")


def test_outer_interruption_after_dispatch_contains_namespace_and_preserves_receipt(monkeypatch, tmp_path):
    config, context = _config(tmp_path, active=3, cleanup=6); started = context / "started"
    script = ("import os,subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"
              "'import os,time;os.setsid();time.sleep(30)']);open(os.environ['STARTED'],'w').write('1');time.sleep(30)")
    real_descendants = service._descendants; calls = [0]
    def interrupted(pid):
        calls[0] += 1
        value = real_descendants(pid)
        if calls[0] == 20: raise KeyboardInterrupt()
        return value
    monkeypatch.setattr(service, "_descendants", interrupted)
    receipt = context / "interrupted.json"
    with pytest.raises(service.DiagnosticServiceIsolationError):
        service.execute(config, _sha(config), [sys.executable, "-P", "-c", script],
            {"STARTED": str(started), "PYTHONDONTWRITEBYTECODE": "1"}, receipt)
    assert started.exists(); value = json.loads(receipt.read_text())
    assert value["failure_type"] == "KeyboardInterrupt" and value["owned_processes_dead"] is True
    assert value["relay_receipt"]["listener_ready"] is False and not list(context.glob("*.sock"))
