"""Confinement and limit probes for the local Sandbox executor (standard library only).

Shared by ``test_sandbox_executor.py`` and its opt-in container run, which executes this file inside the built Sage
image (``python3 -I sandbox_checks.py <sage_root>``) and reads the JSON it prints. No pytest import here.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

_PROBE = r'''
import ctypes, errno, json, os, socket
out = {}
def probe(name, fn):
    try:
        out[name] = "allowed:" + str(fn())[:80]
    except OSError as exc:
        out[name] = "denied:" + errno.errorcode.get(exc.errno, str(exc.errno))
def ptrace():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.ptrace(16, PARENT, None, None) != 0:
        raise OSError(ctypes.get_errno(), "ptrace")
    return "attached"
probe("compute", lambda: 2 + 2)
probe("write_run_dir", lambda: (open("out.txt", "w").write("ok"), open("out.txt").read())[1])
probe("read_secret", lambda: open(SECRET).read())
probe("list_root", lambda: os.listdir("/"))
probe("proc_1_environ", lambda: open("/proc/1/environ", "rb").read())
probe("proc_self_environ", lambda: open("/proc/self/environ", "rb").read())
probe("tcp_loopback", lambda: socket.create_connection(("127.0.0.1", PORT), timeout=2))
probe("udp_send", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", PORT)))
probe("unix_connect", lambda: socket.socket(socket.AF_UNIX).connect(UNIX))
probe("signal_parent", lambda: os.kill(PARENT, 0))
probe("ptrace_parent", ptrace)
probe("planted_env", lambda: os.environ.get("SAGE_SANDBOX_PLANTED"))
print(json.dumps(out))
'''

EXPECTED_CONFINEMENT = {
    "compute": "allowed:4",
    "write_run_dir": "allowed:ok",
    "read_secret": "denied:EACCES",
    "list_root": "denied:EACCES",
    "proc_1_environ": "denied:EACCES",
    "proc_self_environ": "denied:EACCES",
    "tcp_loopback": "denied:EPERM",
    "udp_send": "denied:EPERM",
    "unix_connect": "denied:EPERM",
    "signal_parent": "denied:EPERM",
    "ptrace_parent": "denied:EPERM",
    "planted_env": "allowed:None",
}


def confinement_outcomes(run_local) -> dict:
    """E1: run the probe snippet with a stand-in secret, live listeners and a planted variable in place."""
    scratch = tempfile.mkdtemp(prefix="sbx-chk-")
    secret = os.path.join(scratch, "secret.txt")
    with open(secret, "w") as handle:
        handle.write("stand-in secret")
    unix_path = os.path.join(scratch, "s.sock")
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen()
    unix = socket.socket(socket.AF_UNIX)
    unix.bind(unix_path)
    unix.listen()
    os.environ["SAGE_SANDBOX_PLANTED"] = "planted-value"
    try:
        code = (
            f"SECRET = {secret!r}\nPORT = {tcp.getsockname()[1]}\nUNIX = {unix_path!r}\nPARENT = {os.getpid()}\n"
            + _PROBE
        )
        result = run_local("python", code, 20)
        shell = run_local("shell", "printf 'a\\nb\\n' | wc -l; ls / >/dev/null 2>&1 || echo root-denied", 20)
    finally:
        os.environ.pop("SAGE_SANDBOX_PLANTED", None)
        tcp.close()
        unix.close()
    probes = json.loads(result["stdout"]) if result.get("exit_code") == 0 else {"error": result}
    return {"probes": probes, "shell": shell["stdout"].split(), "shell_exit": shell["exit_code"]}


def _gone(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def limit_outcomes(run_local) -> dict:
    """E2: timeout, background child, fork bomb, output flood and file-size limit."""
    out = {}
    started = time.monotonic()
    timed = run_local("shell", "sleep 30 & echo $!; sleep 30", 2)
    out["timeout"] = {
        "timed_out": timed["timed_out"],
        "elapsed_ok": time.monotonic() - started < 10,
        "background_child_gone": _gone(int(timed["stdout"].split()[0])),
    }
    finished = run_local("shell", "sleep 30 & echo $!", 10)
    out["main_exit_ends_run"] = {
        "timed_out": finished["timed_out"],
        "exit_code": finished["exit_code"],
        "background_child_gone": _gone(int(finished["stdout"].split()[0])),
    }
    started = time.monotonic()
    bomb = run_local(
        "python",
        "import os, sys\nprint(os.getpid(), flush=True)\nwhile True:\n    try:\n        os.fork()\n"
        "    except OSError:\n        pass\n",
        3,
    )
    group = int(bomb["stdout"].split()[0])
    try:
        os.killpg(group, 0)
        group_gone = False
    except ProcessLookupError:
        group_gone = True
    responsive = subprocess.run(["true"], timeout=10).returncode == 0
    out["fork_bomb"] = {
        "timed_out": bomb["timed_out"],
        "elapsed_ok": time.monotonic() - started < 15,
        "group_gone": group_gone,
        "parent_responsive": responsive,
    }
    flood = run_local("python", "import sys\nsys.stdout.write('x' * 5_000_000)\n", 30)
    out["flood"] = {"truncated": flood["truncated"], "stdout_len": len(flood["stdout"])}
    big = run_local(
        "python",
        "import errno\nchunk = b'0' * (1 << 20)\ntry:\n    with open('big.bin', 'wb') as f:\n"
        "        for _ in range(256):\n            f.write(chunk)\n    print('wrote-all')\n"
        "except OSError as exc:\n    print(errno.errorcode.get(exc.errno))\n",
        30,
    )
    out["file_size"] = {"stdout": big["stdout"].strip()}
    return out


EXPECTED_LIMITS = {
    "timeout": {"timed_out": True, "elapsed_ok": True, "background_child_gone": True},
    "main_exit_ends_run": {"timed_out": False, "exit_code": 0, "background_child_gone": True},
    "fork_bomb": {"timed_out": True, "elapsed_ok": True, "group_gone": True, "parent_responsive": True},
    "flood": {"truncated": True, "stdout_len": 20000},
    "file_size": {"stdout": "EFBIG"},
}


if __name__ == "__main__":
    sys.path.insert(0, sys.argv[1])
    from ai.langgraph import sandbox_executor

    print(json.dumps({
        "abi": sandbox_executor.landlock_abi(),
        "confinement": confinement_outcomes(sandbox_executor.run_local),
        "limits": limit_outcomes(sandbox_executor.run_local),
    }))
