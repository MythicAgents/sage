"""Opt-in process attempt telemetry for the local observation channel.

These audit records show attempted Python-visible effects.  They do not attest
success, completeness, or the behavior of native child implementations.
"""
from __future__ import annotations

import atexit
import math
import os
from pathlib import Path
import sys
import threading
from typing import Any
from collections.abc import Mapping

from ai import observation_channel


_lock = threading.Lock()
_local = threading.local()
_hook_registered = False
_atexit_registered = False
_started_pids: set[int] = set()
_ended_pids: set[int] = set()
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return {"object_type": type(value).__name__}


def _environment_names(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    return sorted(str(_safe(key)) for key in value)


def _after_fork() -> None:
    global _lock, _local
    _lock, _local = threading.Lock(), threading.local()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _emit(kind: str, payload: dict[str, Any]) -> None:
    if getattr(_local, "active", False):
        return
    _local.active = True
    try:
        observation_channel.emit(kind, {"observation_class": "ATTEMPT", **payload})
    finally:
        _local.active = False


def _selected_event(event: str, args: tuple[Any, ...]) -> tuple[str, dict[str, Any]] | None:
    payload: dict[str, Any] = {"audit_event": event, "attempted": True}
    if event in {"socket.connect", "socket.bind"}:
        payload["address"] = _safe(args[1] if len(args) > 1 else None)
        return "control_request", payload
    if event == "socket.getaddrinfo":
        payload["host"] = _safe(args[0] if args else None)
        payload["port"] = _safe(args[1] if len(args) > 1 else None)
        return "control_request", payload
    if event == "subprocess.Popen":
        payload.update(executable=_safe(args[0] if args else None),
                       argv=_safe(args[1] if len(args) > 1 else None),
                       cwd=_safe(args[2] if len(args) > 2 else None),
                       environment_names=_environment_names(args[3] if len(args) > 3 else None))
        return "tool_event", payload
    if event in {"os.exec", "os.posix_spawn"}:
        payload.update(executable=_safe(args[0] if args else None),
                       argv=_safe(args[1] if len(args) > 1 else None),
                       environment_names=_environment_names(args[2] if len(args) > 2 else None))
        return "tool_event", payload
    if event == "os.system":
        payload["command"] = _safe(args[0] if args else None)
        return "tool_event", payload
    if event in {"os.putenv", "os.unsetenv"}:
        payload["environment_name"] = _safe(args[0] if args else None)
        return "config_snapshot", payload
    if event == "open":
        mode, flags = args[1] if len(args) > 1 else None, args[2] if len(args) > 2 else 0
        write = (isinstance(mode, str) and any(value in mode for value in "wax+")) or (
            isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        )
        if not write:
            return None
        payload.update(path=_safe(args[0] if args else None), mode=_safe(mode), flags=_safe(flags))
        return "tool_event", payload
    if event in {"os.rename", "os.replace"}:
        payload.update(source=_safe(args[0] if args else None),
                       destination=_safe(args[1] if len(args) > 1 else None))
        return "tool_event", payload
    if event in {"os.remove", "os.unlink", "os.rmdir", "os.mkdir"}:
        payload["path"] = _safe(args[0] if args else None)
        return "tool_event", payload
    return None


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if getattr(_local, "active", False) or observation_channel.internal_activity():
        return
    selected = _selected_event(event, args)
    if selected is not None:
        if not os.environ.get(observation_channel.ENV_NAME, ""):
            raise observation_channel.ObservationChannelError("process observation delivery failed")
        _emit(*selected)


def _process_end() -> None:
    pid = os.getpid()
    with _lock:
        if pid not in _started_pids or pid in _ended_pids:
            return
        _ended_pids.add(pid)
    _emit("process_end", {"pid": pid, "attempted": False})


def install() -> bool:
    global _hook_registered, _atexit_registered
    if not os.environ.get(observation_channel.ENV_NAME, ""):
        return False
    pid = os.getpid()
    with _lock:
        if pid in _started_pids:
            return True
        if not _hook_registered:
            sys.addaudithook(_audit_hook)
            _hook_registered = True
        _emit("process_start", {
            "pid": pid,
            "parent_pid": os.getppid(),
            "interpreter": sys.executable,
            "implementation": sys.implementation.name,
            "version": ".".join(str(value) for value in sys.version_info[:3]),
            "attempted": False,
        })
        _started_pids.add(pid)
        if not _atexit_registered:
            atexit.register(_process_end)
            _atexit_registered = True
    return True
