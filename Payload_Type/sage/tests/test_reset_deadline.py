import importlib.util
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-eval-gauge/scripts/orchestrate.py"
SPEC = importlib.util.spec_from_file_location("m3_reset_deadline_orchestrate", SCRIPT)
orchestrate = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(orchestrate)


def test_capture_deadline_kills_owned_sleeper_process_group(tmp_path):
    marker = tmp_path / "escaped"
    script = tmp_path / "sleeper.sh"
    script.write_text(f"#!/bin/bash\n(sleep 0.5; : > {marker}) &\nwait\n")
    script.chmod(0o700)
    deadline = orchestrate._ResetDeadline(0.1)
    with pytest.raises(SystemExit, match="total deadline"):
        orchestrate._run("bounded", [str(script)], tmp_path, 10, deadline=deadline)
    time.sleep(0.6)
    assert not marker.exists()


def test_cleanup_kills_owned_group_after_direct_parent_already_exited(tmp_path):
    marker, grandchild_pid = tmp_path / "escaped", tmp_path / "grandchild.pid"
    script = tmp_path / "exited_parent.py"
    script.write_text(
        "import os,signal,time\n"
        "pid=os.fork()\n"
        f"\nif pid:\n open({str(grandchild_pid)!r},'w').write(str(pid));os._exit(0)\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"time.sleep(1);open({str(marker)!r},'w').write('escaped')\n"
    )
    process = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    process.wait(timeout=2)
    deadline = orchestrate._ResetDeadline(5)
    deadline.terminate(process)
    time.sleep(1.1)
    assert not marker.exists()


def test_expired_deadline_refuses_child_before_popen(monkeypatch, tmp_path):
    clock = [0.0]
    deadline = orchestrate._ResetDeadline(1, now=lambda: clock[0])
    clock[0] = 1.0
    monkeypatch.setattr(orchestrate.subprocess, "Popen", lambda *_a, **_k: pytest.fail("child started"))
    with pytest.raises(SystemExit, match="before next dispatch"):
        orchestrate._run("expired", ["ignored"], tmp_path, 10, deadline=deadline)


def test_interrupted_communication_cleans_owned_group(monkeypatch, tmp_path):
    process = object()
    class InterruptedProcess:
        def communicate(self, timeout):
            raise KeyboardInterrupt
    process = InterruptedProcess()
    monkeypatch.setattr(orchestrate.subprocess, "Popen", lambda *_a, **_k: process)
    deadline = orchestrate._ResetDeadline(10)
    cleaned = []
    monkeypatch.setattr(deadline, "terminate", cleaned.append)
    with pytest.raises(KeyboardInterrupt):
        deadline.run(["unused"], cwd=tmp_path, cap=5)
    assert cleaned == [process]


def test_poll_reuses_one_fake_clock_budget_for_children_and_waits(monkeypatch, tmp_path):
    clock, child_caps, waits = [0.0], [], []
    deadline = orchestrate._ResetDeadline(
        10, now=lambda: clock[0], sleep=lambda value: (waits.append(value), clock.__setitem__(0, clock[0] + value))
    )

    class Process:
        returncode = 0
        pid = 999999
        def communicate(self, timeout):
            child_caps.append(timeout); clock[0] += 4
            return "not-ready", ""

    monkeypatch.setattr(orchestrate.subprocess, "Popen", lambda *_a, **_k: Process())
    with pytest.raises(SystemExit, match="total deadline"):
        orchestrate._poll("shared", ["ignored"], tmp_path, lambda _out: False,
                          timeout=100, interval=3, deadline=deadline)
    assert child_caps == [10.0, 3.0] and waits == [3.0]


def test_poll_success_and_noncapture_run_preserve_old_subprocess_path(monkeypatch, tmp_path):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs)); return subprocess.CompletedProcess(argv, 0, "ready", "")
    monkeypatch.setattr(orchestrate.subprocess, "run", run)
    monkeypatch.setattr(orchestrate.subprocess, "Popen", lambda *_a, **_k: pytest.fail("Popen used"))
    orchestrate._run("legacy", ["ordinary"], tmp_path, 17)
    assert orchestrate._poll("legacy-poll", ["probe"], tmp_path, lambda value: value == "ready",
                             timeout=30) == "ready"
    assert calls[0][1]["timeout"] == 17
    assert "timeout" not in calls[1][1]


def test_range_probe_refuses_when_remaining_cannot_fit_ludus_child(monkeypatch):
    clock = [0.0]
    deadline = orchestrate._ResetDeadline(30, now=lambda: clock[0], sleep=lambda _value: None)
    monkeypatch.setattr(orchestrate._READINESS, "poll_readiness", lambda *_a, **_k: pytest.fail("probe ran"))
    with pytest.raises(SystemExit, match="cannot fit"):
        orchestrate._wait_for_range_boot(range_id=None, mcp_server=None, timeout=1800, deadline=deadline)


def test_full_reset_reports_owned_child_timeout_without_raw_output(monkeypatch,tmp_path):
    monkeypatch.setattr(orchestrate,"_range_env",lambda *_a,**_k:{})
    monkeypatch.setattr(orchestrate,"RESET_STEPS",[("stop sage",[sys.executable,"-c",
        "import time;print('PLANTED-TIMEOUT-SECRET');time.sleep(2)"],tmp_path,5)])
    events=[]
    with pytest.raises(SystemExit,match="total deadline"):
        orchestrate.full_reset_and_ready(capture_total_timeout=.1,setup_stage_callback=events.append)
    assert [row["status"] for row in events]==["started","failed"]
    assert events[-1]["stage"]=="stop-sage" and events[-1]["error_class"]=="SystemExit"
    assert 0<events[-1]["timeout_seconds"]<=.1 and "PLANTED" not in str(events)


def test_mythic_reset_failure_classifies_streamed_output_without_retaining_secret(monkeypatch, tmp_path):
    script = tmp_path / "failed_reset.py"
    script.write_text(
        "import os,time\n"
        "os.write(2,b'pg_hba.')\n"
        "time.sleep(.05)\n"
        "os.write(2,b'conf: No such file or directory\\n')\n"
        "os.write(1,b'PLANTED-SECRET-'*20000)\n"
        "raise SystemExit(17)\n"
    )
    monkeypatch.setattr(orchestrate, "_range_env", lambda *_a, **_k: {})
    monkeypatch.setattr(orchestrate, "RESET_STEPS", [
        ("reset mythic", [sys.executable, str(script)], tmp_path, 10),
    ])
    events = []
    with pytest.raises(SystemExit) as captured:
        orchestrate.full_reset_and_ready(setup_stage_callback=events.append)
    assert captured.value.setup_exit_code == 17
    assert captured.value.setup_diagnostic_code == "postgres_config_missing"
    assert events[-1]["diagnostic_code"] == "postgres_config_missing"
    assert "PLANTED-SECRET" not in str(events)
    assert "PLANTED-SECRET" not in str(captured.value)


@pytest.mark.parametrize("message, expected", [
    (b"ERR: unable to remove stopped Mythic postgres container", "postgres_remove_failed"),
    (b"dependency failed to start: container unhealthy", "dependency_unhealthy"),
    (b"secret=PLANTED-SECRET and unknown error", "unclassified"),
])
def test_mythic_reset_diagnostic_is_fixed_code(message, expected):
    assert orchestrate._mythic_reset_diagnostic(message) == expected


def test_mythic_reset_timeout_kills_process_group_and_records_code(monkeypatch, tmp_path):
    marker = tmp_path / "escaped"
    script = tmp_path / "sleeper.sh"
    script.write_text(f"#!/bin/bash\n(sleep 0.4; : > {marker}) &\nwait\n")
    script.chmod(0o700)
    monkeypatch.setattr(orchestrate, "_range_env", lambda *_a, **_k: {})
    monkeypatch.setattr(orchestrate, "RESET_STEPS", [
        ("reset mythic", [str(script)], tmp_path, 5),
    ])
    events = []
    with pytest.raises(SystemExit):
        orchestrate.full_reset_and_ready(capture_total_timeout=.05, setup_stage_callback=events.append)
    time.sleep(.5)
    assert not marker.exists()
    assert events[-1]["status"] == "failed"
    assert events[-1]["diagnostic_code"] == "startup_timeout"


def test_poll_failure_exposes_only_numeric_signed_returncode(monkeypatch,tmp_path):
    monkeypatch.setattr(orchestrate.subprocess,"run",lambda *_a,**_k:
        subprocess.CompletedProcess([], -9, "", "PLANTED-POLL-SECRET"))
    with pytest.raises(SystemExit) as captured:
        orchestrate._poll("fixture",["unused"],tmp_path,lambda _out:True,timeout=1)
    assert captured.value.setup_exit_code==-9
