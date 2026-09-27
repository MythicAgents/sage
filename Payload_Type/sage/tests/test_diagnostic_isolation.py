from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import psutil
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "skills/sage-eval-gauge/scripts/diagnostic_isolation.py"
SPEC = importlib.util.spec_from_file_location("m3_diagnostic_isolation", SOURCE)
assert SPEC and SPEC.loader
isolation = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(isolation)


def _wait(path: Path, timeout: float = 8) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try: return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError): time.sleep(.02)
    raise AssertionError("sandbox result unavailable")


def test_isolated_child_has_loopback_no_egress_and_pid_namespace_containment(tmp_path):
    scratch = tmp_path / "scratch"; scratch.mkdir(mode=0o700); result = scratch / "result.json"
    script = r'''
import http.server,json,os,signal,socket,subprocess,sys,threading,time
class H(http.server.BaseHTTPRequestHandler):
 def do_GET(self):self.send_response(200);self.send_header("Content-Length","2");self.end_headers();self.wfile.write(b"ok")
 def log_message(self,*_):pass
s=http.server.HTTPServer(("127.0.0.1",0),H);t=threading.Thread(target=s.serve_forever,daemon=True);t.start()
c=socket.create_connection(("127.0.0.1",s.server_port),1);c.sendall(b"GET / HTTP/1.0\r\nHost: local\r\n\r\n");loopback=b"200 OK" in c.recv(256);c.close()
try:socket.create_connection(("192.0.2.1",9),.2);outbound="connected"
except OSError as e:outbound=e.errno
g=subprocess.Popen([sys.executable,"-P","-c","import os,signal,time;os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"])
nspid=next(x.split()[1:] for x in open(f"/proc/{g.pid}/status") if x.startswith("NSpid:"))
open(os.environ["RESULT"]+".pending","w").write(json.dumps({"loopback":loopback,"outbound":outbound,"grandchild_inner":int(nspid[-1]),"default_route":any(x.split()[1]=="00000000" for x in open("/proc/net/route").read().splitlines()[1:] if x.split())}))
os.replace(os.environ["RESULT"]+".pending",os.environ["RESULT"]);time.sleep(30)
'''
    command = isolation.isolated_command(
        [str(Path(sys.executable).resolve()), "-P", "-c", script], writable_paths=[scratch],
        environment={"RESULT": str(result), "PYTHONDONTWRITEBYTECODE": "1"}, cwd=scratch,
    )
    wrapper = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    value = _wait(result); descendants = psutil.Process(wrapper.pid).children(recursive=True)
    host_pids = [process.pid for process in descendants]
    host_nspids = {process.pid: next((line.split()[1:] for line in
        Path(f"/proc/{process.pid}/status").read_text().splitlines() if line.startswith("NSpid:")), [])
        for process in descendants}
    assert value["loopback"] is True and value["outbound"] != "connected"
    assert value["default_route"] is False
    assert any(parts and int(parts[-1]) == value["grandchild_inner"] and len(parts) >= 2
               for parts in host_nspids.values())
    os.killpg(wrapper.pid, signal.SIGTERM); wrapper.wait(timeout=8)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() for pid in host_pids): time.sleep(.02)
    assert all(not Path(f"/proc/{pid}").exists() for pid in host_pids)


def test_builder_rejects_ambient_environment_and_broad_write(tmp_path):
    try:
        isolation.isolated_command([sys.executable, "-c", "pass"], writable_paths=[Path("/")],
            environment={"bad-key": "value"}, cwd=tmp_path)
    except isolation.DiagnosticIsolationError:
        pass
    else:
        assert False


def test_process_observer_strace_uses_fresh_sandbox_devices(tmp_path):
    scratch = tmp_path / "observer"; scratch.mkdir(mode=0o700); result = scratch / "result.json"
    observer = ROOT / "skills/sage-eval-gauge/scripts/process_observer.py"
    script = r'''
import importlib.util,json,os
s=importlib.util.spec_from_file_location("isolated_observer",os.environ["OBSERVER"])
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
fd=os.open("/dev/null",os.O_RDWR);os.close(fd)
v=m.observe(["/bin/true"],lambda *_:{},timeout_seconds=2,max_events=256,
 max_line_bytes=4096,max_trace_bytes=1048576,stdout_mode="devnull",stderr_mode="devnull")
open(os.environ["RESULT"]+".pending","w").write(json.dumps({"complete":v["complete"],"failures":v["failures"],"returncode":v["returncode"]}))
os.replace(os.environ["RESULT"]+".pending",os.environ["RESULT"])
'''
    command = isolation.isolated_command(
        [str(Path(sys.executable).resolve()), "-P", "-c", script], writable_paths=[scratch],
        read_only_paths=[ROOT],
        environment={"OBSERVER": str(observer), "RESULT": str(result),
                     "PYTHONDONTWRITEBYTECODE": "1"}, cwd=scratch,
    )
    wrapper = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=8, check=False)
    assert wrapper.returncode == 0
    assert _wait(result) == {"complete": True, "failures": [], "returncode": 0}


def test_temporary_source_tree_is_readonly_with_private_state_and_tmp(tmp_path):
    source = tmp_path / "source"; source.mkdir(mode=0o700)
    state = tmp_path / "state"; state.mkdir(mode=0o700); result = state / "result.json"
    unrelated = tmp_path / "unrelated"; unrelated.write_text("host-only")
    socket_path = tmp_path / "host.sock"; host_socket = __import__("socket").socket(
        __import__("socket").AF_UNIX, __import__("socket").SOCK_STREAM)
    host_socket.bind(str(socket_path))
    script = source / "probe.py"; script.write_text(r'''import json,os,pathlib,socket
p=pathlib.Path(__file__)
try:p.write_text("changed");source="writable"
except OSError:source="readonly"
pathlib.Path(os.environ["STATE"]).write_text("state")
try:socket.create_connection(("192.0.2.1",9),.2);outbound="connected"
except OSError as e:outbound=e.errno
pathlib.Path(os.environ["RESULT"]).write_text(json.dumps({"source":source,"state":pathlib.Path(os.environ["STATE"]).read_text(),"unrelated":pathlib.Path(os.environ["UNRELATED"]).exists() or pathlib.Path(os.environ["SOCKET"]).exists(),"outbound":outbound}))
''')
    command = isolation.isolated_command([str(Path(sys.executable).resolve()), "-P", str(script)],
        writable_paths=[state], read_only_paths=[source],
        environment={"STATE": str(state / "allowed"), "RESULT": str(result),
            "UNRELATED": str(unrelated), "SOCKET": str(socket_path),
            "PYTHONDONTWRITEBYTECODE": "1"}, cwd=source)
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=5)
    assert completed.returncode == 0
    value = _wait(result); host_socket.close()
    assert value["source"] == "readonly" and value["state"] == "state"
    assert value["unrelated"] is False and value["outbound"] != "connected"
