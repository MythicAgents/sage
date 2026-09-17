import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MYTHIC_RESET = ROOT / "skills" / "sage-goad-reset" / "scripts" / "mythic_reset.sh"
SAGE_STOP = ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_stop.sh"


def test_sage_stop_accepts_absent_tmux_session_as_already_stopped(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux = fake_bin / "tmux"
    tmux.write_text(
        "#!/usr/bin/env bash\n"
        "[[ \"$1\" == \"has-session\" ]] && exit 1\n"
        "exit 99\n"
    )
    tmux.chmod(0o755)

    stopped = subprocess.run(
        ["/bin/bash", str(SAGE_STOP)],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert stopped.returncode == 0, stopped.stderr
    assert "already stopped" in stopped.stdout
    assert "is absent" in stopped.stdout


def test_sage_stop_retries_interrupt_during_stuck_python_finalization(tmp_path):
    ready = tmp_path / "ready"
    target = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, signal, time\n"
                "count = 0\n"
                "def interrupt(_signum, _frame):\n"
                "    global count\n"
                "    count += 1\n"
                "    if count >= 3:\n"
                "        raise SystemExit(0)\n"
                "signal.signal(signal.SIGINT, interrupt)\n"
                f"open({str(ready)!r}, 'w').close()\n"
                "while True:\n"
                "    time.sleep(0.1)\n"
            ),
        ]
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        call_count = tmp_path / "interrupt-count"
        tmux = fake_bin / "tmux"
        tmux.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "case \"$1\" in\n"
            "  has-session) exit 0 ;;\n"
            "  list-panes) echo 12345; exit 0 ;;\n"
            "  send-keys)\n"
            "    count=0\n"
            "    [[ -f \"$CALL_COUNT\" ]] && count=$(<\"$CALL_COUNT\")\n"
            "    count=$((count + 1))\n"
            "    printf '%s' \"$count\" > \"$CALL_COUNT\"\n"
            "    kill -INT \"$TARGET_PID\"\n"
            "    exit 0 ;;\n"
            "esac\n"
            "exit 1\n"
        )
        tmux.chmod(0o755)
        pgrep = fake_bin / "pgrep"
        pgrep.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$TARGET_PID\"\n")
        pgrep.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CALL_COUNT": str(call_count),
            "TARGET_PID": str(target.pid),
            "SAGE_STOP_INITIAL_WAIT_SECONDS": "1",
            "SAGE_STOP_RETRY_WAIT_SECONDS": "1",
            "SAGE_STOP_FINAL_WAIT_SECONDS": "1",
        }

        stopped = subprocess.run(
            ["/bin/bash", str(SAGE_STOP)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )

        assert stopped.returncode == 0, stopped.stderr
        assert call_count.read_text() == "3"
        assert "Stopped local Sage pid" in stopped.stdout
        assert target.wait(timeout=2) == 0
    finally:
        if target.poll() is None:
            target.terminate()
            target.wait(timeout=2)


def test_mythic_reset_drops_container_only_host_overrides(tmp_path):
    log = tmp_path / "calls.log"
    cli = tmp_path / "mythic-cli"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s|%s|%s|%s\\n" "$*" "${RABBITMQ_HOST-unset}" '
        '"${MYTHIC_SERVER_HOST-unset}" "${NGINX_HOST-unset}" >> "$CALL_LOG"\n'
    )
    cli.chmod(0o755)
    env = {
        **os.environ,
        "MYTHIC_CLI": str(cli),
        "CALL_LOG": str(log),
        "RABBITMQ_HOST": "127.0.0.1",
        "MYTHIC_SERVER_HOST": "127.0.0.1",
        "NGINX_HOST": "127.0.0.1",
        # This test is about the script DROPPING container-only host variables before calling
        # mythic-cli. Its last step asks which Sage deployment is running, and that check reads real
        # Docker rather than the fake CLI above — so without this the result depends on whatever
        # happens to be running on the machine, which is why it failed on any box with a Sage
        # container up. Declaring the check out of scope rather than declaring a MODE is deliberate:
        # SAGE_DEPLOYMENT_MODE=container would make enforce run sage_stop.sh for real and kill a
        # developer's local tmux Sage as a side effect of running the test suite.
        "SAGE_RESET_SKIP_DEPLOYMENT_ENFORCE": "1",
    }

    subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"],
        cwd=ROOT,
        env=env,
        check=True,
    )

    calls = log.read_text().splitlines()
    assert calls == [
        "stop|unset|unset|unset",
        "database reset -f|unset|unset|unset",
        "start|unset|unset|unset",
    ]
