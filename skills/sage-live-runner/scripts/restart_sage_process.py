#!/usr/bin/env python3
"""Restart the local Sage dev process when it is not running under the sage tmux session."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


SAGE_CWD = Path("/home/john/dev/sage/Payload_Type/sage")
VENV_PY = Path("/home/john/dev/sage/.venv/bin/python")
LOG = Path("/tmp/sage-restart-process.log")


def _read_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _read_cwd(pid: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except Exception:
        return None


def _read_env(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    env: dict[str, str] = {}
    for item in raw.split(b"\x00"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def _find_sage_pid() -> int:
    candidates: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        pid = int(proc.name)
        cmd = _read_cmdline(pid)
        if "main.py" not in cmd:
            continue
        if _read_cwd(pid) != SAGE_CWD:
            continue
        candidates.append(pid)
    if not candidates:
        raise SystemExit("no local Sage main.py process found")
    return sorted(candidates)[0]


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.25)
    return False


def main() -> None:
    pid = _find_sage_pid()
    env = _read_env(pid)
    env["SAGE_ENGAGEMENT_GATE"] = env.get("SAGE_ENGAGEMENT_GATE", "1") or "1"
    env["SAGE_BLOODHOUND_MCP_DIR"] = env.get("SAGE_BLOODHOUND_MCP_DIR", "/home/john/dev/bloodhound_mcp")

    subprocess.run([str(VENV_PY), "-c", "import mythic_container"], check=True)
    print(f"restarting Sage pid={pid} cwd={SAGE_CWD}")
    os.kill(pid, signal.SIGINT)
    if not _wait_gone(pid, 12):
        print(f"pid {pid} still alive after SIGINT; sending SIGTERM")
        os.kill(pid, signal.SIGTERM)
        _wait_gone(pid, 5)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    log_handle = LOG.open("ab", buffering=0)
    proc = subprocess.Popen(
        [str(VENV_PY), "-u", "main.py"],
        cwd=str(SAGE_CWD),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(2)
    print(f"new Sage pid={proc.pid} log={LOG}")


if __name__ == "__main__":
    main()
