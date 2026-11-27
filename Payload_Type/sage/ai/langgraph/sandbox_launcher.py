"""Confine one Sandbox snippet and run it. Standard library only; this file imports nothing from Sage.

``sandbox_executor.run_local`` starts it with the base interpreter:
``python -I sandbox_launcher.py STATUS_FD RUN_DIR LANGUAGE TIMEOUT CODE``.

This process stays outside the confinement as a small reaper. It registers as a child subreaper and forks the snippet
into its own process group. It enforces the wall timeout, then kills that group and reaps every descendant, so no
orphan is left to PID 1 (Sage, in the container). It writes ``child <pid>`` and then one JSON line
``{"exit_code", "timed_out"}`` to STATUS_FD.

The child applies, in order: resource limits, ``no_new_privs``, a Landlock ruleset and a seccomp filter. Then it
execs the snippet. If any step fails, it writes ``CONFINEMENT_FAILED`` and the reason to stderr and exits 125 before
any snippet code runs.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import resource
import select
import signal
import stat
import struct
import sys
import time

CONFINEMENT_FAILED = "sage-sandbox: confinement failed: "
REQUIRED_ABI = 6  # signal and abstract-socket scoping protect Sage, which shares this uid
PROCESS_HEADROOM = 64
ADDRESS_SPACE = 1 << 30
FILE_SIZE = 64 << 20
OPEN_FILES = 64

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long
_LL_CREATE, _LL_ADD, _LL_RESTRICT = 444, 445, 446  # the same on x86_64 and aarch64
_FS_ALL = (1 << 16) - 1  # every filesystem right through ABI 8
_FS_FILE = 1 | 2 | 4 | (1 << 14) | (1 << 15)  # execute, write, read, truncate, ioctl_dev: the rights a file can take
_FS_READ_EXEC = 1 | 4 | 8  # execute, read file, read directory
# Audit arch and the calls that return EPERM: socket, ptrace, setpgid, setsid, add_key, request_key, keyctl,
# perf_event_open, process_vm_readv, process_vm_writev, bpf, userfaultfd, io_uring_setup/enter/register.
_SECCOMP = {
    "x86_64": (0xC000003E, (41, 101, 109, 112, 248, 249, 250, 298, 310, 311, 321, 323, 425, 426, 427)),
    "aarch64": (0xC00000B7, (198, 117, 154, 157, 217, 218, 219, 241, 270, 271, 280, 282, 425, 426, 427)),
}


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("fs", ctypes.c_uint64), ("net", ctypes.c_uint64), ("scoped", ctypes.c_uint64)]


class _Fprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _sys(*args) -> int:
    rc = _libc.syscall(*args)
    if rc < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return rc


def _prctl(option: int, arg2: int, arg3: ctypes.c_void_p | None = None) -> None:
    if _libc.prctl(ctypes.c_int(option), ctypes.c_ulong(arg2), arg3 or ctypes.c_ulong(0), ctypes.c_ulong(0),
                   ctypes.c_ulong(0)) != 0:
        raise OSError(ctypes.get_errno(), f"prctl({option}): {os.strerror(ctypes.get_errno())}")


def landlock_abi() -> int:
    """The kernel's Landlock ABI version, or 0 when Landlock is absent or disabled."""
    rc = _libc.syscall(ctypes.c_long(_LL_CREATE), None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    return int(rc) if rc > 0 else 0


def _landlock(run_dir: str) -> None:
    abi = landlock_abi()
    if abi < REQUIRED_ABI:
        raise OSError(0, f"Landlock ABI {abi} is below {REQUIRED_ABI}")
    attr = _RulesetAttr(_FS_ALL, 3, 3)  # both TCP rights, both scopes
    ruleset = _sys(ctypes.c_long(_LL_CREATE), ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)),
                   ctypes.c_uint32(0))
    system = (os.path.dirname(os.path.realpath(sys.executable)), sys.base_prefix, "/usr", "/bin", "/lib", "/lib64")
    rules = [(path, _FS_READ_EXEC) for path in dict.fromkeys(system)]
    rules += [("/etc/ld.so.cache", 4), ("/dev/urandom", 4), ("/dev/null", _FS_FILE & ~1), (run_dir, _FS_ALL)]
    for path, access in rules:
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except FileNotFoundError:
            continue
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                access &= _FS_FILE
            _sys(ctypes.c_long(_LL_ADD), ctypes.c_int(ruleset), ctypes.c_int(1),
                 ctypes.create_string_buffer(struct.pack("=Qi", access, fd)), ctypes.c_uint32(0))
        finally:
            os.close(fd)
    _sys(ctypes.c_long(_LL_RESTRICT), ctypes.c_int(ruleset), ctypes.c_uint32(0))
    os.close(ruleset)


def _seccomp() -> None:
    arch, denied = _SECCOMP[platform.machine()]
    kill, eperm, allow = 0x80000000, 0x00050001, 0x7FFF0000

    def op(code: int, k: int, jt: int = 0, jf: int = 0) -> bytes:
        return struct.pack("=HBBI", code, jt, jf, k)

    prog = [op(0x20, 4), op(0x15, arch, 1, 0), op(0x06, kill), op(0x20, 0)]  # load arch; foreign kills; load nr
    if arch == 0xC000003E:
        prog += [op(0x35, 0x40000000, 0, 1), op(0x06, eperm)]  # x32 numbers
    for nr in denied:
        prog += [op(0x15, nr, 0, 1), op(0x06, eperm)]
    prog.append(op(0x06, allow))
    buf = ctypes.create_string_buffer(b"".join(prog))
    fprog = _Fprog(len(prog), ctypes.addressof(buf))
    _prctl(22, 2, ctypes.c_void_p(ctypes.addressof(fprog)))  # PR_SET_SECCOMP, SECCOMP_MODE_FILTER


def _user_tasks() -> int:
    """This uid's task count as the kernel keeps it: a fork succeeds only while the count is below RLIMIT_NPROC.

    In the container the uid is shared with host processes the container cannot see, so /proc would undercount.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)

    def forks_under(limit: int) -> bool:
        resource.setrlimit(resource.RLIMIT_NPROC, (limit, hard))
        try:
            pid = os.fork()
        except BlockingIOError:
            return False
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        return True

    cap = (1 << 22) if hard == resource.RLIM_INFINITY else hard
    try:
        low, high = 1, min(64, cap)
        while not forks_under(high):
            if high >= cap:
                raise OSError(0, "cannot measure this user's task count")
            low, high = high, min(high * 2, cap)
        while high - low > 1:
            mid = (low + high) // 2
            low, high = (low, mid) if forks_under(mid) else (mid, high)
        return high - 1
    finally:
        resource.setrlimit(resource.RLIMIT_NPROC, (soft, hard))


def _confine_and_exec(run_dir: str, language: str, timeout: int, tasks: int, code: str) -> None:
    try:
        os.setpgid(0, 0)
        os.nice(19)
        limits = ((resource.RLIMIT_CPU, timeout + 1), (resource.RLIMIT_AS, ADDRESS_SPACE),
                  (resource.RLIMIT_FSIZE, FILE_SIZE), (resource.RLIMIT_NOFILE, OPEN_FILES),
                  (resource.RLIMIT_CORE, 0), (resource.RLIMIT_NPROC, tasks + PROCESS_HEADROOM))
        for which, value in limits:
            hard = resource.getrlimit(which)[1]
            value = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(which, (value, value))
        _prctl(38, 1)  # PR_SET_NO_NEW_PRIVS
        _landlock(run_dir)
        _seccomp()
        argv = ["/bin/sh", "-c", code] if language == "shell" else [sys.executable, "-I", "-c", code]
        os.execve(argv[0], argv, os.environ)
    except BaseException as exc:  # noqa: BLE001 - nothing may run unconfined
        os.write(2, f"{CONFINEMENT_FAILED}{exc!r}\n".encode())
    os._exit(125)


def _kill_and_reap(group: int) -> None:
    end = time.monotonic() + 5
    while time.monotonic() < end:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        time.sleep(0.01)


def main(argv: list[str]) -> int:
    status_fd, run_dir, language, timeout, code = int(argv[1]), argv[2], argv[3], int(argv[4]), argv[5]
    try:
        if language not in {"python", "shell"}:
            raise ValueError(f"unsupported language {language!r}")
        if os.getuid() == 0:
            raise PermissionError("refusing to run as root, where the process cap does not apply")
        tasks = _user_tasks()
        _prctl(36, 1)  # PR_SET_CHILD_SUBREAPER
    except Exception as exc:  # noqa: BLE001
        os.write(2, f"{CONFINEMENT_FAILED}{exc!r}\n".encode())
        return 125
    deadline = time.monotonic() + timeout
    child = os.fork()
    if child == 0:
        os.close(status_fd)
        _confine_and_exec(run_dir, language, timeout, tasks, code)
    os.write(status_fd, f"child {child}\n".encode())
    pidfd = os.pidfd_open(child)
    timed_out = not select.select([pidfd], [], [], max(0.0, deadline - time.monotonic()))[0]
    os.close(pidfd)
    exit_code = -1
    if not timed_out:
        exit_code = os.waitstatus_to_exitcode(os.waitpid(child, 0)[1])
        exit_code = 128 - exit_code if exit_code < 0 else exit_code  # a signal reads as 128 + signal, as in Docker
    _kill_and_reap(child)
    os.write(status_fd, (json.dumps({"exit_code": exit_code, "timed_out": timed_out}) + "\n").encode())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
