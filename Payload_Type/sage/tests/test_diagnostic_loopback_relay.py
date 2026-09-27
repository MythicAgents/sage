import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socketserver
import subprocess
import sys
import threading
import time

import pytest


ROOT = Path(__file__).parents[3]


def _load(name):
    path = ROOT / f"skills/sage-eval-gauge/scripts/{name}.py"
    spec = importlib.util.spec_from_file_location(name, path); module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None; spec.loader.exec_module(module); return module


relay = _load("diagnostic_loopback_relay")
isolation = _load("diagnostic_isolation")


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            value = self.request.recv(65536)
            if not value: break
            self.request.sendall(value)


def _wait(path, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try: return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError): time.sleep(.02)
    raise AssertionError("relay child did not report")


def test_namespaced_inner_relay_reaches_only_bound_host_loopback_and_cleans_up(tmp_path):
    echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
    server = threading.Thread(target=echo.serve_forever, daemon=True); server.start()
    context = tmp_path / "relay"; context.mkdir(mode=0o700)
    config = context / "relay.json"; result = context / "result.json"
    config.write_text(json.dumps({"schema": relay.SCHEMA, "context_directory": str(context),
        "ports": [echo.server_address[1]], "deadline_monotonic": time.monotonic() + 15}))
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    script = r'''
import hashlib,importlib.util,json,os,socket,threading,time
p=os.environ["RELAY"];s=importlib.util.spec_from_file_location("child_relay",p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
with m.InnerRelay(__import__('pathlib').Path(os.environ["CONFIG"]),os.environ["SHA"]) as relay:
 open(os.environ["READY"],"w").write("ready")
 c=socket.create_connection(("127.0.0.1",int(os.environ["PORT"])),2);payload=b"\x16TLS\x00AMQP\xff"*600000
 sender=threading.Thread(target=lambda:(c.sendall(payload),c.shutdown(socket.SHUT_WR)));sender.start();received=b""
 while len(received)<len(payload):
  part=c.recv(65536)
  if not part:break
  received+=part
 sender.join(5)
 try:socket.create_connection(("192.0.2.1",9),.2);outbound="connected"
 except OSError as e:outbound=e.errno
 open(os.environ["RESULT"]+".pending","w").write(json.dumps({"echo":hashlib.sha256(received).hexdigest(),"expected":hashlib.sha256(payload).hexdigest(),"sender_done":not sender.is_alive(),"outbound":outbound,"receipt":relay.receipt()}));os.replace(os.environ["RESULT"]+".pending",os.environ["RESULT"]);time.sleep(30)
'''
    source = ROOT / "skills/sage-eval-gauge/scripts/diagnostic_loopback_relay.py"
    with relay.OuterRelay(config, digest) as outer:
        assert outer.receipt()["listener_ready"] is True
        command = isolation.isolated_command([sys.executable, "-P", "-c", script],
            writable_paths=[context], read_only_paths=[ROOT],
            environment={"RELAY": str(source), "CONFIG": str(config),
            "SHA": digest, "PORT": str(echo.server_address[1]), "READY": str(context / "ready"),
            "RESULT": str(result), "PYTHONDONTWRITEBYTECODE": "1"}, cwd=context)
        wrapper = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True)
        value = _wait(result)
        assert value["echo"] == value["expected"] and value["sender_done"] is True
        assert value["outbound"] != "connected"
        assert value["receipt"]["listener_ready"] is True
        os.killpg(wrapper.pid, signal.SIGTERM); wrapper.wait(timeout=5)
    echo.shutdown(); echo.server_close(); server.join(timeout=2)
    assert not any(context.glob("*.sock"))
    assert outer.receipt()["listener_ready"] is False
    assert all(not thread.is_alive() for thread in outer.threads)


def test_missing_socket_and_invalid_configuration_fail_closed(tmp_path):
    context = tmp_path / "relay"; context.mkdir(mode=0o700)
    port = 43210; config = context / "relay.json"
    config.write_text(json.dumps({"schema": relay.SCHEMA, "context_directory": str(context),
        "ports": [port], "deadline_monotonic": time.monotonic() + 5}))
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    with pytest.raises(relay.DiagnosticLoopbackRelayError, match="socket is unavailable"):
        with relay.InnerRelay(config, digest): pass
    config.write_text(json.dumps({"schema": relay.SCHEMA, "context_directory": str(context),
        "ports": [0], "deadline_monotonic": time.monotonic() + 5}))
    with pytest.raises(relay.DiagnosticLoopbackRelayError, match="configuration is invalid"):
        relay.OuterRelay(config, hashlib.sha256(config.read_bytes()).hexdigest())


def test_failed_outer_initialization_unlinks_only_owned_socket(tmp_path):
    context = tmp_path / "relay"; context.mkdir(mode=0o700); ports = [43212, 43213]
    protected = context / f"loopback-{ports[1]}.sock"
    existing = __import__("socket").socket(__import__("socket").AF_UNIX, __import__("socket").SOCK_STREAM)
    existing.bind(str(protected)); existing.listen(1); identity = protected.stat().st_ino
    config = context / "relay.json"; config.write_text(json.dumps({"schema": relay.SCHEMA,
        "context_directory": str(context), "ports": ports,
        "deadline_monotonic": time.monotonic() + 5}))
    with pytest.raises(relay.DiagnosticLoopbackRelayError, match="socket is unavailable"):
        with relay.OuterRelay(config, hashlib.sha256(config.read_bytes()).hexdigest()): pass
    assert protected.exists() and protected.stat().st_ino == identity
    assert not (context / f"loopback-{ports[0]}.sock").exists()
    existing.close(); protected.unlink()


def test_deadline_closes_listener_without_protocol_probe(tmp_path):
    context = tmp_path / "relay"; context.mkdir(mode=0o700); config = context / "relay.json"
    config.write_text(json.dumps({"schema": relay.SCHEMA, "context_directory": str(context),
        "ports": [43211], "deadline_monotonic": time.monotonic() + .15}))
    outer = relay.OuterRelay(config, hashlib.sha256(config.read_bytes()).hexdigest()); outer.__enter__()
    assert outer.receipt()["listener_ready"] is True
    deadline = time.monotonic() + 2
    while (outer.receipt()["listener_ready"] or any(context.glob("*.sock"))) \
            and time.monotonic() < deadline: time.sleep(.02)
    assert outer.receipt()["listener_ready"] is False and not any(context.glob("*.sock"))
    outer.close()
