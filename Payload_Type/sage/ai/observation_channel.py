"""Opt-in generic event delivery through operator-owned local FIFOs."""
from __future__ import annotations

from collections.abc import Mapping
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import stat
import threading
import time
from typing import Any
from uuid import uuid4

ENV_NAME = "SAGE_OBSERVATION_DIRECTORY"
EVENT_SCHEMA = "sage-observation-event-v2"
MAX_FRAME_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 5.0
_ALLOWED_KINDS = frozenset({"process_start", "process_end", "model_request", "model_response", "control_request", "control_response", "log_record", "tool_event", "config_snapshot"})
_process_pid = os.getpid()
_process_uuid = uuid4().hex
_sequence = 0
_lock = threading.Lock()
_local = threading.local()


def _defer_main_sigint():
    """Defer capture teardown until the current frame has a definite outcome."""
    if threading.current_thread() is not threading.main_thread():
        return None
    previous = signal.getsignal(signal.SIGINT)
    pending: list[tuple[int, Any]] = []

    def defer(signum, frame):
        if not pending:
            pending.append((signum, frame))

    signal.signal(signal.SIGINT, defer)
    return previous, pending


def _restore_main_sigint(state, *, transport_failed: bool) -> None:
    if state is None:
        return
    previous, pending = state
    signal.signal(signal.SIGINT, previous)
    if not pending or previous is signal.SIG_IGN:
        return
    signum, frame = pending[0]
    if previous is signal.SIG_DFL:
        if not transport_failed:
            signal.raise_signal(signum)
        return
    try:
        previous(signum, frame)
    except BaseException:
        if not transport_failed:
            raise

class ObservationChannelError(RuntimeError):
    def __init__(self, message: str, *, stage: str = "unknown") -> None:
        super().__init__(message)
        self.stage = stage

def _reset_after_fork() -> None:
    global _process_pid, _process_uuid, _sequence, _lock, _local
    _process_pid, _process_uuid, _sequence, _lock, _local = (
        os.getpid(), uuid4().hex, 0, threading.Lock(), threading.local()
    )

def internal_activity() -> bool:
    """Return whether this thread is executing channel transport internals."""
    return bool(getattr(_local, "active", False))

if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)

def _metadata(path: Path, *, kind: str, mode: int):
    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError:
        raise ObservationChannelError("observation channel configuration is invalid", stage="config") from None
    expected = {"directory": stat.S_ISDIR, "fifo": stat.S_ISFIFO, "regular": stat.S_ISREG}[kind]
    if not expected(value.st_mode) or value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != mode:
        raise ObservationChannelError("observation channel configuration is invalid", stage="config")
    return value

def _paths() -> tuple[Path, Path, Path]:
    raw = os.environ.get(ENV_NAME, "")
    if not raw or not Path(raw).is_absolute():
        raise ObservationChannelError("observation channel configuration is invalid", stage="config")
    directory = Path(raw)
    _metadata(directory, kind="directory", mode=0o700)
    events, acks, lock = directory / "events.fifo", directory / "acks.fifo", directory / "lock"
    _metadata(events, kind="fifo", mode=0o600)
    _metadata(acks, kind="fifo", mode=0o600)
    _metadata(lock, kind="regular", mode=0o600)
    return events, acks, lock

def _open_checked(path: Path, flags: int, *, fifo: bool, stage: str) -> int:
    try:
        descriptor = os.open(path, flags | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        raise ObservationChannelError("observation delivery failed", stage=stage) from None
    metadata = os.fstat(descriptor)
    if (not (stat.S_ISFIFO(metadata.st_mode) if fifo else stat.S_ISREG(metadata.st_mode))
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600):
        os.close(descriptor)
        raise ObservationChannelError("observation delivery failed", stage=stage)
    return descriptor

def _acquire_lock(descriptor: int, deadline: float) -> None:
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ObservationChannelError("observation delivery failed", stage="lock") from None
            time.sleep(0.01)
        except OSError:
            raise ObservationChannelError("observation delivery failed", stage="lock") from None

def _write_all(descriptor: int, frame: bytes, deadline: float) -> None:
    offset = 0
    while offset < len(frame):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [descriptor], [], remaining)[1]:
            raise ObservationChannelError("observation delivery failed", stage="write")
        try:
            written = os.write(descriptor, frame[offset:])
        except BlockingIOError:
            continue
        except OSError:
            raise ObservationChannelError("observation delivery failed", stage="write") from None
        if written <= 0:
            raise ObservationChannelError("observation delivery failed", stage="write")
        offset += written

def _read_ack(descriptor: int, deadline: float) -> bytes:
    value = b""
    while len(value) < 33:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise ObservationChannelError("observation acknowledgement failed", stage="ack")
        try:
            part = os.read(descriptor, 34 - len(value))
        except BlockingIOError:
            continue
        except OSError:
            raise ObservationChannelError("observation acknowledgement failed", stage="ack") from None
        if not part:
            raise ObservationChannelError("observation acknowledgement failed", stage="ack")
        value += part
    return value

def emit(kind: str, payload: Mapping[str, Any]) -> bool:
    global _process_pid, _process_uuid, _sequence
    if not os.environ.get(ENV_NAME, ""):
        return False
    if kind not in _ALLOWED_KINDS or not isinstance(payload, Mapping):
        raise ObservationChannelError("observation event is invalid", stage="config")
    previous = internal_activity()
    _local.active = True
    deferred_sigint = _defer_main_sigint()
    transport_failed = False
    try:
        with _lock:
            if os.getpid() != _process_pid:
                _reset_after_fork()
            _sequence += 1
            event_id = uuid4().hex
            event = {"schema": EVENT_SCHEMA, "event_id": event_id, "process_uuid": _process_uuid, "pid": os.getpid(), "seq": _sequence, "time_ns": time.time_ns(), "kind": kind, "payload": payload}
            try:
                encoded = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise ObservationChannelError("observation payload is not JSON-serializable", stage="config") from None
            if len(encoded) > MAX_FRAME_BYTES:
                raise ObservationChannelError("observation event exceeds the frame limit", stage="config")
            events_path, acks_path, lock_path = _paths()
            lock_fd = events_fd = acks_fd = -1
            deadline = time.monotonic() + TIMEOUT_SECONDS
            try:
                lock_fd = _open_checked(lock_path, os.O_RDWR, fifo=False, stage="lock")
                _acquire_lock(lock_fd, deadline)
                events_fd = _open_checked(events_path, os.O_WRONLY, fifo=True, stage="write")
                acks_fd = _open_checked(acks_path, os.O_RDONLY, fifo=True, stage="ack")
                _write_all(events_fd, len(encoded).to_bytes(8, "big") + encoded, deadline)
                acknowledgement = _read_ack(acks_fd, deadline)
                if acknowledgement != b"1" + event_id.encode("ascii"):
                    raise ObservationChannelError("observation acknowledgement failed", stage="ack")
                return True
            finally:
                for descriptor in (acks_fd, events_fd, lock_fd):
                    if descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
    except BaseException:
        transport_failed = True
        raise
    finally:
        _local.active = previous
        _restore_main_sigint(deferred_sigint, transport_failed=transport_failed)
