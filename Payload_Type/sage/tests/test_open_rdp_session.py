from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-mythic-payload-deploy/scripts/open_rdp_session.py"


def test_outer_termination_cleans_distinct_pty_process_group(tmp_path):
    binary, child_pid, marker = tmp_path / "xfreerdp3", tmp_path / "child.pid", tmp_path / "escaped"
    binary.write_text(
        "#!/usr/bin/env python3\nimport os,signal,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "pid=os.fork()\n"
        f"\nif pid:\n open({str(child_pid)!r},'w').write(str(pid));os.waitpid(pid,0)\n"
        "else:\n signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(1);"
        f"open({str(marker)!r},'w').write('escaped')\n"
    )
    binary.chmod(0o700)
    environment = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    helper = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--target-ip", "127.0.0.1", "--run-as-user", "test",
         "--run-as-password", "synthetic", "--log", str(tmp_path / "rdp.log")],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 3
    while not child_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_pid.exists()
    helper.send_signal(signal.SIGTERM)
    helper.communicate(timeout=3)
    assert helper.returncode == 128 + signal.SIGTERM
    time.sleep(1.1)
    assert not marker.exists()
