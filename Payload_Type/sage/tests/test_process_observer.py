import importlib.util
import os
import signal
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("process_observer", ROOT / "skills/sage-eval-gauge/scripts/process_observer.py")
assert SPEC and SPEC.loader
observer = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(observer)


def test_real_child_grandchild_attempts_are_transient_and_redacted(tmp_path):
    target = tmp_path / "target.py"; config = tmp_path / "config"
    target.write_text("""
import os,socket,subprocess,sys
try: socket.create_connection(('127.0.0.1',1),timeout=.1)
except OSError: pass
open(sys.argv[1],'w').write('secret-log-value')
subprocess.run([sys.executable,'-c',"import sys;open(sys.argv[1],'w').write('changed');open(sys.argv[1],'w').write('restored')",sys.argv[2]])
""")
    raw = []
    result = observer.observe(
        [sys.executable, str(target), str(tmp_path / "log"), str(config)],
        lambda kind, line: raw.append((kind, line)) or {"kind": kind},
        timeout_seconds=10, max_events=5000, stdout_mode="devnull", stderr_mode="devnull",
    )
    assert result["complete"] is True
    assert {"network_attempt", "write_attempt", "file_mutation_attempt", "process_lifecycle"}.issubset(
        {item["kind"] for item in result["redacted_events"]}
    )
    assert any(b"secret-log-value" in line for _kind, line in raw)
    assert "secret-log-value" not in str(result)


def test_receiver_error_is_incomplete():
    def fail(_kind, _line): raise RuntimeError()
    result = observer.observe([sys.executable, "-c", "print('x')"], fail,
                              timeout_seconds=5, max_events=100, stdout_mode="devnull", stderr_mode="devnull")
    assert result["complete"] is False
    assert "receiver_error" in result["failures"]


def test_limits_and_missing_strace_fail_closed(monkeypatch):
    monkeypatch.setattr(observer.shutil, "which", lambda _name: None)
    with pytest.raises(observer.ProcessObserverError, match="unavailable"):
        observer.observe(["true"], lambda *_: None, timeout_seconds=1, max_events=1, stdout_mode="devnull", stderr_mode="devnull")


def test_deadline_kills_child_that_escapes_strace_process_group(tmp_path):
    marker, pid_file = tmp_path / "escaped", tmp_path / "pid"
    target = tmp_path / "escape.py"
    target.write_text(
        "import os,signal,sys,time\n"
        "os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "open(sys.argv[1],'w').write(str(os.getpid()))\n"
        f"time.sleep(1);open({str(marker)!r},'w').write('escaped')\n"
    )
    result = observer.observe(
        [sys.executable, str(target), str(pid_file)], lambda kind, _raw: {"kind": kind},
        timeout_seconds=0.4, max_events=1000, stdout_mode="devnull", stderr_mode="devnull",
    )
    assert result["complete"] is False and "deadline_exceeded" in result["failures"]
    time.sleep(1.1)
    assert not marker.exists()


def test_handler_stderr_is_bounded_redacted_and_never_mixed_with_diagnostics(tmp_path):
    secret = "SYNTHETIC_PRIVATE_STDERR"
    target = tmp_path / "stderr.py"
    target.write_text(f"import sys\nsys.stderr.write(({secret!r}+'x'*262144));sys.stderr.flush()\n")
    observed = []
    result = observer.observe(
        [sys.executable, str(target)],
        lambda kind, raw: observed.append((kind, raw)) or ({"kind": kind} if kind != "process_stderr" else None),
        timeout_seconds=5, max_events=1000, max_stderr_bytes=4096,
        stdout_mode="devnull", stderr_mode="handler",
    )
    assert result["complete"] is False and "stderr_byte_limit_exceeded" in result["failures"]
    assert "strace_diagnostic" not in result["failures"]
    assert result["stderr_sink"] == "handler" and result["stderr_bytes"] > 4096
    assert any(kind == "process_stderr" for kind, _raw in observed)
    assert secret not in str(result)


def test_signal_terminal_frame_is_process_end_without_missing_end():
    result = observer.observe(
        [sys.executable, "-c", "import os,signal;os.kill(os.getpid(),signal.SIGKILL)"],
        lambda kind, _raw: {"kind": kind}, timeout_seconds=5, max_events=100,
        stdout_mode="devnull", stderr_mode="devnull",
    )
    assert "missing_process_end" not in result["failures"]
    assert any(event["kind"] == "process_end" for event in result["redacted_events"])
