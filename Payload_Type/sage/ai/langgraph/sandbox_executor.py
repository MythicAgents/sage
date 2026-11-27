"""Pick the Sandbox's executor and run the confined local one.

``container`` is the Docker executor (``MythicTools._run_sandbox_sync``) and needs a reachable Docker engine.
``local`` runs ``sandbox_launcher.py``, which confines the snippet with Landlock, seccomp and resource limits. It
shares the host kernel and has no cgroup limits, so it is weaker than a container or a VM. With neither, the Sandbox
is unavailable and says why.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from mythic_container.logging import logger

try:
    from . import sandbox_launcher
except ImportError:  # allow running this module directly for manual testing
    import sandbox_launcher

REQUIRED_LANDLOCK_ABI = sandbox_launcher.REQUIRED_ABI
OUTPUT_CHARS = 20000  # the Docker executor's cap, before output reaches the model
landlock_abi = sandbox_launcher.landlock_abi
_LAUNCHER = os.path.abspath(sandbox_launcher.__file__)
_SELF_TEST = (
    "import socket\nprint(2 + 2)\nfor probe in (lambda: open(OUTSIDE).read(), socket.socket):\n    try:\n"
    "        probe()\n        print('allowed')\n    except PermissionError:\n        print('denied')\n"
)


def _docker_reason() -> str:
    """'' when a Docker engine answers through the executor's own client, otherwise why not."""
    try:
        import docker
    except ImportError:
        return "the docker SDK is not installed"
    try:
        client = docker.from_env(timeout=5)
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - any failure means no usable engine
        logger.debug(f"Sandbox: Docker engine check failed: {exc!r}")
        return "no Docker engine is reachable"
    return ""


def _self_test() -> str:
    """'' when the launcher runs a computation and denies an outside read and a socket, otherwise the failure."""
    scratch = tempfile.mkdtemp(prefix="sage-sandbox-check-")
    outside = os.path.join(scratch, "outside.txt")
    try:
        with open(outside, "w") as handle:
            handle.write("outside")
        result = run_local("python", f"OUTSIDE = {outside!r}\n{_SELF_TEST}", 15)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if result["exit_code"] != 0 or result["stdout"].split() != ["4", "denied", "denied"]:
        return f"unexpected result (exit {result['exit_code']}): {(result['stdout'] + result['stderr'])[-300:]}"
    return ""


@functools.lru_cache(maxsize=1)
def select_executor() -> tuple[str | None, str]:
    """Return ``("container" | "local" | None, reason)``, once per process, with one WARNING unless ``container``."""
    docker_reason = _docker_reason()
    if not docker_reason:
        return "container", ""
    abi = landlock_abi() if sys.platform.startswith("linux") else 0
    if abi < REQUIRED_LANDLOCK_ABI:
        reason = (f"{docker_reason}, and Landlock ABI {abi} is below {REQUIRED_LANDLOCK_ABI} "
                  "(the confined local executor needs Linux 6.12 or later)")
    elif failure := _self_test():
        reason = f"{docker_reason}, and the confined local executor failed its self-test: {failure}"
    else:
        logger.warning(f"Sandbox: {docker_reason}; using the confined local executor (Landlock ABI {abi}, seccomp, "
                       "resource limits; it shares the host kernel)")
        return "local", ""
    logger.warning(f"Sandbox unavailable: {reason}")
    return None, reason


def _read_capped(proc: subprocess.Popen, deadline: float) -> tuple[dict, dict, bool]:
    """Read both pipes to EOF, keeping a bounded prefix of each; True when the deadline passed first."""
    keep = OUTPUT_CHARS * 4 + 4
    kept = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    seen = dict.fromkeys(kept, 0)
    with selectors.DefaultSelector() as selector:
        for stream in kept:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map() and time.monotonic() < deadline:
            for key, _ in selector.select(max(0.0, deadline - time.monotonic())):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                seen[key.fileobj] += len(chunk)
                buffer = kept[key.fileobj]
                buffer += chunk[: max(0, keep - len(buffer))]
        return kept, seen, bool(selector.get_map())


def _status(fd: int) -> tuple[int | None, dict | None]:
    os.set_blocking(fd, False)
    raw = b""
    with contextlib.suppress(BlockingIOError):
        while chunk := os.read(fd, 4096):
            raw += chunk
    lines = raw.decode(errors="replace").splitlines()
    child = int(lines[0].split()[1]) if lines and lines[0].startswith("child ") else None
    return child, (json.loads(lines[1]) if len(lines) > 1 else None)


def run_local(language: str, code: str, timeout: int) -> dict:
    """Run one snippet under the confined launcher. Returns the Docker executor's result fields."""
    interpreter = os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)
    run_dir = tempfile.mkdtemp(prefix="sage-sandbox-")  # mode 0700
    status_r, status_w = os.pipe()
    try:
        proc = subprocess.Popen(
            [interpreter, "-I", _LAUNCHER, str(status_w), run_dir, language, str(timeout), code],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(status_w,),
            start_new_session=True, cwd=run_dir, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": run_dir,
                                                      "LANG": "C.UTF-8"},
        )
        os.close(status_w)
        status_w = -1
        with proc:
            kept, seen, overran = _read_capped(proc, time.monotonic() + timeout + 10)
            if overran:  # the launcher outlived its own timeout: kill it and the snippet's group
                child, _ = _status(status_r)
                for group in filter(None, (child, proc.pid)):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(group, signal.SIGKILL)
            proc.wait()
        _, status = _status(status_r)
    finally:
        if status_w >= 0:
            os.close(status_w)
        os.close(status_r)
        shutil.rmtree(run_dir, ignore_errors=True)
    stdout = kept[proc.stdout].decode("utf-8", "replace")
    stderr = kept[proc.stderr].decode("utf-8", "replace")
    failure = next((line for line in stderr.splitlines() if line.startswith(sandbox_launcher.CONFINEMENT_FAILED)), "")
    if overran or status is None or (status["exit_code"] == 125 and failure):
        raise RuntimeError(failure or "the local sandbox launcher did not finish")
    return {
        "exit_code": status["exit_code"],
        "timed_out": status["timed_out"],
        "stdout": stdout[:OUTPUT_CHARS],
        "stderr": stderr[:OUTPUT_CHARS],
        "truncated": any(seen[s] > len(kept[s]) for s in kept) or max(len(stdout), len(stderr)) > OUTPUT_CHARS,
    }
