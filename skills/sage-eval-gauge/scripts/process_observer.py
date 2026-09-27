#!/usr/bin/env python3
"""Bounded passive strace observer for one caller-spawned process tree."""
from __future__ import annotations

import argparse
import base64
from collections import Counter, deque
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class ProcessObserverError(RuntimeError):
    pass


EXEC_CONFIG_ENV = "SAGE_PROCESS_OBSERVER_CONFIG"
EXEC_CONFIG_SHA256_ENV = "SAGE_PROCESS_OBSERVER_CONFIG_SHA256"
_EXEC_CONFIG_SCHEMA = "sage-process-observer-exec-config-v1"
_RECEIPT_SCHEMA = "sage-process-observer-preexec-receipt-v1"


def _descriptor_streams(pid: int) -> tuple[dict[str, Any], dict[str, Any]]:
    transient = {"event": "observed_process_streams", "tracee_pid": pid, "streams": []}
    durable = {"count": 0, "descriptors": []}
    for descriptor in (1, 2):
        path = f"/proc/{pid}/fd/{descriptor}"
        try:
            target = os.readlink(path)
            metadata = os.stat(path)
            stream_type = ("pipe" if stat.S_ISFIFO(metadata.st_mode) else "character" if stat.S_ISCHR(metadata.st_mode)
                           else "socket" if stat.S_ISSOCK(metadata.st_mode) else "regular" if stat.S_ISREG(metadata.st_mode)
                           else "other")
            transient["streams"].append({"fd": descriptor, "target": target, "device": metadata.st_dev,
                                           "inode": metadata.st_ino, "type": stream_type})
            durable["descriptors"].append({"fd": descriptor,
                "target_sha256": hashlib.sha256(os.fsencode(target)).hexdigest(),
                "device": metadata.st_dev, "inode": metadata.st_ino, "type": stream_type})
        except OSError:
            transient["streams"].append({"fd": descriptor, "missing": True})
            durable["descriptors"].append({"fd": descriptor, "missing": True})
    durable["count"] = len(durable["descriptors"])
    return transient, durable


_LINE = re.compile(rb"^\s*(?P<pid>\d+)\s+\d+(?:\.\d+)?\s+(?P<call>[a-z][a-z0-9_]*)\(")
_EXIT = re.compile(rb"^\s*(?P<pid>\d+)\s+\d+(?:\.\d+)?\s+\+\+\+ exited")
_SIGNALED = re.compile(rb"^\s*(?P<pid>\d+)\s+\d+(?:\.\d+)?\s+\+\+\+ killed by SIG[A-Z0-9]+ \+\+\+")
_RESUMED = re.compile(rb"^\s*(?P<pid>\d+)\s+\d+(?:\.\d+)?\s+<\.\.\. (?P<call>[a-z][a-z0-9_]*) resumed>")
_PROCESS = frozenset({"execve", "clone", "clone3", "fork", "vfork", "setsid"})
_NETWORK = frozenset({"socket", "socketpair", "connect", "bind", "listen", "accept", "accept4", "sendto", "sendmsg"})
_FILE = frozenset({"rename", "renameat", "renameat2", "unlink", "unlinkat", "mkdir", "mkdirat"})
_SIMPLE_WRITES = frozenset({"write", "writev", "pwrite64", "pwritev", "pwritev2", "mmap", "mmap2"})
_UNSUPPORTED_TRANSFERS = frozenset({
    "sendfile", "splice", "tee", "vmsplice", "copy_file_range", "msync",
})


def _read_regular(path: Path, *, owner_only: bool, limit: int) -> bytes:
    if not path.is_absolute():
        raise ProcessObserverError("process observer configuration is invalid")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ProcessObserverError("process observer configuration is invalid") from None
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or (owner_only and stat.S_IMODE(metadata.st_mode) != 0o600)
                or metadata.st_size <= 0 or metadata.st_size > limit):
            raise ProcessObserverError("process observer configuration is invalid")
        value = bytearray()
        while len(value) <= limit:
            chunk = os.read(fd, min(65_536, limit + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        if len(value) != metadata.st_size or len(value) > limit:
            raise ProcessObserverError("process observer configuration is invalid")
        return bytes(value)
    finally:
        os.close(fd)


def _digest(value: Any) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ProcessObserverError("process observer configuration is invalid")
    return value


def load_exec_config(
    config_path: str | Path,
    expected_sha256: str,
    *,
    cwd: str | Path,
    python: str | Path,
) -> dict[str, Any]:
    """Validate the private prospective binding before any observed process exists."""
    path = Path(config_path)
    raw = _read_regular(path, owner_only=True, limit=1_048_576)
    if hashlib.sha256(raw).hexdigest() != _digest(expected_sha256):
        raise ProcessObserverError("process observer configuration is invalid")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ProcessObserverError("process observer configuration is invalid") from None
    keys = {
        "schema", "observer_path", "observer_sha256", "entry_path", "entry_sha256",
        "source_binding_path", "source_binding_sha256", "observation_channel_path",
        "observation_channel_sha256", "strace_path", "strace_sha256",
        "python", "cwd", "argv", "limits",
        "observation_directory", "receipt_path",
    }
    if not isinstance(value, dict) or frozenset(value) != keys or value.get("schema") != _EXEC_CONFIG_SCHEMA:
        raise ProcessObserverError("process observer configuration is invalid")
    observed_cwd, observed_python = Path(cwd), Path(python)
    argv = value.get("argv")
    entry = Path(value.get("entry_path", ""))
    if isinstance(argv, list) and len(argv) >= 2 and all(isinstance(item, str) and item for item in argv):
        tokens = argv[1:]
        while tokens and tokens[0] in {"-u", "-P", "-S"}:
            tokens = tokens[1:]
        bound_argv_entry = (observed_cwd / tokens[0]).absolute() if tokens and not Path(tokens[0]).is_absolute() else (Path(tokens[0]) if tokens else Path())
    else:
        bound_argv_entry = Path(); tokens = []
    if (not observed_cwd.is_absolute() or not observed_python.is_absolute()
            or Path(value["cwd"]) != observed_cwd or Path(value["python"]) != observed_python
            or not isinstance(argv, list) or argv[0] != str(observed_python)
            or len(tokens) != 1 or bound_argv_entry != entry):
        raise ProcessObserverError("process observer configuration is invalid")
    source_files = (
        ("observer_path", "observer_sha256", Path(__file__).resolve()),
        ("entry_path", "entry_sha256", entry),
        ("source_binding_path", "source_binding_sha256", None),
        ("observation_channel_path", "observation_channel_sha256", None),
    )
    for path_key, digest_key, exact_path in source_files:
        candidate = Path(value[path_key])
        if exact_path is not None and candidate != exact_path:
            raise ProcessObserverError("process observer configuration is invalid")
        content = _read_regular(candidate, owner_only=False, limit=64 * 1024 * 1024)
        if hashlib.sha256(content).hexdigest() != _digest(value[digest_key]):
            raise ProcessObserverError("process observer configuration is invalid")
    strace_path = Path(value["strace_path"])
    try:
        strace_metadata = os.stat(strace_path, follow_symlinks=False)
        strace_content = strace_path.read_bytes()
    except OSError:
        raise ProcessObserverError("process observer configuration is invalid") from None
    if (not strace_path.is_absolute() or not stat.S_ISREG(strace_metadata.st_mode)
            or not os.access(strace_path, os.X_OK) or strace_metadata.st_mode & 0o022
            or len(strace_content) > 64 * 1024 * 1024
            or hashlib.sha256(strace_content).hexdigest() != _digest(value["strace_sha256"])):
        raise ProcessObserverError("process observer configuration is invalid")
    limits = value["limits"]
    if not isinstance(limits, dict) or frozenset(limits) != {
        "timeout_seconds", "max_events", "max_line_bytes", "max_trace_bytes"
    }:
        raise ProcessObserverError("process observer configuration is invalid")
    timeout = limits["timeout_seconds"]
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not 0 < timeout <= 3600 or type(limits["max_events"]) is not int
            or not 1 <= limits["max_events"] <= 1_000_000
            or type(limits["max_line_bytes"]) is not int
            or not 1024 <= limits["max_line_bytes"] <= 1_048_576
            or type(limits["max_trace_bytes"]) is not int
            or not 1024 <= limits["max_trace_bytes"] <= 1024 * 1024 * 1024):
        raise ProcessObserverError("process observer configuration is invalid")
    observation_directory = Path(value["observation_directory"])
    try:
        directory_metadata = os.stat(observation_directory, follow_symlinks=False)
        members = [
            os.stat(observation_directory / name, follow_symlinks=False)
            for name in ("events.fifo", "acks.fifo", "lock")
        ]
    except OSError:
        raise ProcessObserverError("process observer configuration is invalid") from None
    if (not observation_directory.is_absolute() or not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.getuid() or stat.S_IMODE(directory_metadata.st_mode) != 0o700
            or any(item.st_uid != os.getuid() for item in members)
            or any(not stat.S_ISFIFO(item.st_mode) or stat.S_IMODE(item.st_mode) != 0o600 for item in members[:2])
            or not stat.S_ISREG(members[2].st_mode) or stat.S_IMODE(members[2].st_mode) != 0o600):
        raise ProcessObserverError("process observer configuration is invalid")
    receipt = Path(value["receipt_path"])
    try:
        parent_metadata = os.stat(receipt.parent, follow_symlinks=False)
    except OSError:
        raise ProcessObserverError("process observer configuration is invalid") from None
    if (not receipt.is_absolute() or receipt.exists() or not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.getuid() or stat.S_IMODE(parent_metadata.st_mode) != 0o700):
        raise ProcessObserverError("process observer configuration is invalid")
    if os.environ.get("SAGE_OBSERVATION_DIRECTORY", "") != str(observation_directory):
        raise ProcessObserverError("process observer configuration is invalid")
    return value


def _tracer_pid(tracee_pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{tracee_pid}/status").read_text().splitlines():
            if line.startswith("TracerPid:"):
                value = int(line.split(":", 1)[1].strip())
                return value if value > 0 else None
    except (OSError, ValueError):
        pass
    return None


def _pidfd_dead(descriptor: int) -> bool:
    return bool(select.select([descriptor], [], [], 0)[0])


def _pidfd_kill(descriptor: int) -> bool:
    try:
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        return True
    except (OSError, ProcessLookupError):
        return _pidfd_dead(descriptor)


def _observation_protocol_operation(line: bytes, call: str | None, members: Sequence[bytes]) -> bool:
    """Identify the configured channel's exact syscall operand, never its data."""
    if call is None:
        return False
    match = _LINE.match(line)
    if match is None:
        return False
    arguments = line[match.end():]
    if call in _SIMPLE_WRITES or call in _UNSUPPORTED_TRANSFERS:
        descriptor = re.match(rb"\s*\d+<(?P<path>[^>]*)>(?:,|\))", arguments)
        return descriptor is not None and descriptor.group("path") in members
    if call == "open":
        return any(arguments.startswith(b'"' + member + b'"') for member in members)
    if call in {"openat", "openat2"}:
        path = re.match(rb'\s*[^,]+,\s*"(?P<path>(?:\\.|[^"\\])*)"(?:,|\))', arguments)
        return path is not None and path.group("path") in members
    return False


def _strace_string_truncated(line: bytes) -> bool:
    """Detect strace's ellipsis after a closing C string, not literal content."""
    quoted = escaped = False
    for index,value in enumerate(line):
        if not quoted:
            if value == ord('"'):quoted=True
        elif escaped:
            escaped=False
        elif value == ord('\\'):
            escaped=True
        elif value == ord('"'):
            quoted=False
            if line[index+1:index+4] == b"...":return True
    return False


def _truncation_sample(line: bytes, call: str | None, source_limit: int) -> dict[str, Any] | None:
    match=re.match(rb"^\s*(?P<pid>\d+)\s+(?P<timestamp>\d+(?:\.\d+)?)\s+[^\r\n]+\)\s+=\s+(?P<result>-?\d+)",line)
    if (call not in _PROCESS|_NETWORK|_FILE|_SIMPLE_WRITES|_UNSUPPORTED_TRANSFERS|{"open","openat","openat2"}
            or match is None):return None
    result=int(match.group("result"))
    return {"pid":int(match.group("pid")),"call":call,"rendered_line_bytes":len(line),
        "source_string_limit_bytes":source_limit,"raw_line_sha256":hashlib.sha256(line).hexdigest(),
        "syscall_epoch_ns":int(float(match.group("timestamp"))*1_000_000_000),
        "return_count":result if result >= 0 else None}


class _TraceSpool:
    """Continuously drain raw tracer output while the collector may block on delivery."""
    def __init__(self, read_fd: int, directory: Path, limit: int) -> None:
        self.read_fd, self.limit = read_fd, limit
        self.condition = threading.Condition(); self.digest = hashlib.sha256()
        self.total = self.buffered = 0;self.chunks: deque[bytes] = deque()
        self.eof = self.overflow = False; self.error = False
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._run, name="sage-strace-drain")

    def start(self) -> None:
        self.worker.start()

    def _run(self) -> None:
        try:
            while not self.stop.is_set():
                if read_fd_ready := select.select([self.read_fd], [], [], .05)[0]:
                    try: chunk = os.read(self.read_fd, 262_144)
                    except BlockingIOError: continue
                    if not chunk: break
                    with self.condition:
                        self.digest.update(chunk); self.total += len(chunk)
                        if self.total <= self.limit:
                            self.chunks.append(chunk);self.buffered += len(chunk)
                        else:self.overflow = True
                        self.condition.notify_all()
        except OSError:
            with self.condition:self.error = True
        finally:
            with self.condition:self.eof = True;self.condition.notify_all()

    def read(self, deadline: float) -> bytes | None:
        with self.condition:
            while not self.chunks and not self.eof and not self.overflow and not self.error:
                remaining=deadline-time.monotonic()
                if remaining <= 0:return None
                self.condition.wait(min(.05,remaining))
            if not self.chunks:return b""
            chunk=self.chunks.popleft();self.buffered-=len(chunk);return chunk

    def finish(self, deadline: float, *, stop: bool = False) -> bool:
        remaining=max(0,deadline-time.monotonic());self.worker.join(min(5,remaining))
        if self.worker.is_alive() and stop:
            self.stop.set()
            self.worker.join(min(.25,max(0,deadline-time.monotonic())))
        return not self.worker.is_alive()

    def close(self) -> None:
        self.stop.set()
        if not self.worker.is_alive():
            try:os.close(self.read_fd)
            except OSError:pass


def _collect_preexec(
    read_fd: int,
    receipt_fd: int,
    tracee_pid: int,
    tracee_pidfd: int,
    config: Mapping[str, Any],
    config_sha256: str,
) -> None:
    failures: list[str] = []
    limits = config["limits"]
    deadline = time.monotonic() + float(limits["timeout_seconds"])
    pending = b""; unfinished: dict[tuple[int, str], bytes] = {}
    event_hash = hashlib.sha256()
    event_count = suppressed_observation_io = truncated_syscall_count = 0
    truncated_syscall_samples=[]
    observed_pids: set[int] = set(); ended_pids: set[int] = set()
    kinds: Counter[str] = Counter(); calls: Counter[str] = Counter()
    observation_members = tuple(
        os.fsencode(str(Path(config["observation_directory"]) / name))
        for name in ("events.fifo", "acks.fifo", "lock")
    )
    tracer_pidfd = -1; tracer_attested = pipe_eof = False; root_exit: dict[str, Any] = {}
    stream_metadata: dict[str, Any] = {"count": 0, "missing": True}
    spool: _TraceSpool | None = None
    try:
        os.setsid()
        spool = _TraceSpool(read_fd, Path(config["receipt_path"]).parent, limits["max_trace_bytes"])
        spool.start()
        spec = importlib.util.spec_from_file_location("sage_observation_channel_preexec", config["observation_channel_path"])
        if spec is None or spec.loader is None:
            raise ProcessObserverError("observation delivery failed")
        channel = importlib.util.module_from_spec(spec); spec.loader.exec_module(channel)
        stream_event, stream_metadata = _descriptor_streams(tracee_pid)
        try:
            stream_delivered = channel.emit("tool_event", stream_event)
        except Exception:
            stream_delivered = False
        if not stream_delivered:
            stream_metadata = {"count": 0, "missing": True}
        while not pipe_eof:
            tracer = _tracer_pid(tracee_pid)
            if tracer is not None and tracer_pidfd < 0:
                try:
                    tracer_pidfd = os.pidfd_open(tracer)
                    tracer_attested = True
                except OSError:
                    failures.append("tracer_pidfd_unavailable")
            if time.monotonic() >= deadline:
                failures.append("deadline_exceeded")
                break
            chunk=spool.read(deadline)
            if spool.overflow:
                failures.append("trace_byte_limit_exceeded");break
            if spool.error:
                failures.append("collector_error");break
            if not chunk:
                pipe_eof=spool.eof
                if pipe_eof and _pidfd_dead(tracee_pidfd) and not observed_pids:
                    failures.append("lost_syscall_data")
                continue
            pending += chunk; lines = pending.split(b"\n"); pending = lines.pop()
            for line in lines:
                resumed_match = _RESUMED.match(line)
                if resumed_match:
                    key = (int(resumed_match.group("pid")), resumed_match.group("call").decode("ascii"))
                    first = unfinished.pop(key, None)
                    if first is None:
                        failures.append("orphan_resumed_syscall"); continue
                    line = first.replace(b"<unfinished ...>", b"") + b" " + line[resumed_match.end():]
                elif b"<unfinished ...>" in line:
                    initial = _LINE.match(line)
                    if initial is None:
                        failures.append("unparsed_unfinished_syscall"); continue
                    key = (int(initial.group("pid")), initial.group("call").decode("ascii"))
                    if key in unfinished: failures.append("duplicate_unfinished_syscall")
                    unfinished[key] = line; continue
                pid, kind, call = _parse(line)
                if kind is None: continue
                event_count += 1
                if event_count > limits["max_events"]:
                    failures.append("event_limit_exceeded"); break
                if pid is not None:
                    observed_pids.add(pid)
                    if kind == "process_end":
                        ended_pids.add(pid)
                        if pid == tracee_pid:
                            match = re.search(rb"exited with (\d+)", line)
                            root_exit = {"kind": "exit", "code": int(match.group(1))} if match else {"kind": call}
                kinds[kind] += 1
                if call: calls[call] += 1
                event_hash.update(kind.encode() + b"\0" + hashlib.sha256(line).digest())
                # The observed producer writes to this same FIFO. Forwarding those raw frames while
                # its lock is held creates a lock/strace-pipe cycle and recursively embeds events.
                # Keep exact descriptor/path operations in the trace/counters, but never send them
                # back through their own sink. Data that happens to name a FIFO remains observable.
                if _observation_protocol_operation(line, call, observation_members):
                    suppressed_observation_io += 1
                    continue
                if _strace_string_truncated(line):
                    failures.append("truncated_syscall_data");truncated_syscall_count+=1
                    sample=_truncation_sample(line,call,limits["max_line_bytes"])
                    if sample is not None and len(truncated_syscall_samples)<8:truncated_syscall_samples.append(sample)
                if call in _UNSUPPORTED_TRANSFERS: failures.append("unsupported_sink_transfer")
                delivery_failure = None
                try:
                    delivered = channel.emit("tool_event", {
                        "event": "os_syscall", "kind": kind,
                        "line_base64": base64.b64encode(line).decode("ascii"),
                    })
                except Exception as error:
                    stage = getattr(error, "stage", "unknown")
                    delivery_failure = {"exception_class": "ObservationChannelError" if type(error).__name__ == "ObservationChannelError" else "unknown",
                        "stage": stage if stage in {"config", "lock", "write", "ack"} else "unknown",
                        "monotonic_ns": time.monotonic_ns()}
                    delivered = False
                if not delivered:
                    first_delivery_failure = delivery_failure or {"exception_class": "none", "stage": "disabled", "monotonic_ns": time.monotonic_ns()}
                    failures.append("receiver_error"); break
            if any(item in failures for item in ("event_limit_exceeded", "receiver_error")): break
        if unfinished: failures.append("missing_resumed_syscall")
        if pending: failures.append("partial_trace_frame")
        if not tracer_attested: failures.append("tracer_not_attested")
        if not observed_pids: failures.append("lost_syscall_data")
        if tracee_pid not in ended_pids: failures.append("missing_root_process_end")
        if observed_pids - ended_pids: failures.append("missing_process_end")
        if not spool.finish(deadline):failures.append("collector_error")
        needs_containment = any(item in failures for item in (
            "deadline_exceeded", "trace_byte_limit_exceeded", "event_limit_exceeded", "receiver_error", "collector_error"
        ))
        containment = {"requested": needs_containment, "tracer_killed": False, "tracee_exited": _pidfd_dead(tracee_pidfd)}
        if needs_containment:
            containment["tracer_killed"] = tracer_pidfd >= 0 and _pidfd_kill(tracer_pidfd)
            if not containment["tracer_killed"]:
                _pidfd_kill(tracee_pidfd)
            select.select([tracee_pidfd], [], [], 5)
            containment["tracee_exited"] = _pidfd_dead(tracee_pidfd)
            if not containment["tracee_exited"] or not containment["tracer_killed"]:
                failures.append("containment_unverified")
        if not spool.finish(time.monotonic()+5,stop=True):
            raise ProcessObserverError("trace drain containment failed")
        trace_bytes=spool.total
        receipt = {
            "schema": _RECEIPT_SCHEMA,
            "config_sha256": config_sha256,
            "argv_sha256": hashlib.sha256("\0".join(config["argv"]).encode()).hexdigest(),
            "entry_sha256": config["entry_sha256"],
            "source_binding_sha256": config["source_binding_sha256"],
            "observation_channel_sha256": config["observation_channel_sha256"],
            "coverage_scope": "selected_os_syscall_stream",
            "trace_sha256": spool.digest.hexdigest(), "event_stream_sha256": event_hash.hexdigest(),
            "trace_bytes": trace_bytes, "event_count": event_count,
            "truncated_syscall_count":truncated_syscall_count,
            "truncated_syscall_samples":truncated_syscall_samples,
            "suppressed_observation_io_count": suppressed_observation_io,
            "processes_observed": len(observed_pids), "processes_ended": len(ended_pids),
            "tracee_pid": tracee_pid, "observed_pids": sorted(observed_pids), "ended_pids": sorted(ended_pids),
            "stream_metadata_count": stream_metadata.get("count", 0), "stream_metadata": stream_metadata,
            "event_kind_counts": dict(sorted(kinds.items())), "syscall_counts": dict(sorted(calls.items())),
            "tracer_attested": tracer_attested, "root_exit": root_exit,
            "complete": not failures, "failures": list(dict.fromkeys(failures)), "containment": containment,
            "first_delivery_failure": locals().get("first_delivery_failure"),
        }
    except BaseException:
        killed_descriptor=tracer_pidfd if tracer_pidfd >= 0 else tracee_pidfd
        tracer_killed=_pidfd_kill(killed_descriptor);select.select([tracee_pidfd],[],[],5)
        if spool is not None:spool.finish(time.monotonic()+5,stop=True)
        receipt = {
            "schema": _RECEIPT_SCHEMA, "config_sha256": config_sha256,
            "coverage_scope": "selected_os_syscall_stream", "complete": False,
            "tracee_pid": tracee_pid, "observed_pids": sorted(observed_pids), "ended_pids": sorted(ended_pids),
            "stream_metadata_count": stream_metadata.get("count", 0), "stream_metadata": stream_metadata,
            "failures": ["collector_error"], "containment": {"requested": True,
                "tracer_killed":tracer_killed if tracer_pidfd >= 0 else False,
                "tracee_exited":_pidfd_dead(tracee_pidfd)},
        }
    finally:
        if spool is not None:
            if spool.finish(time.monotonic()+.25,stop=True):spool.close()
        encoded = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            os.write(receipt_fd, encoded); os.fsync(receipt_fd)
        except OSError:
            pass
        for descriptor in (tracer_pidfd, tracee_pidfd, read_fd, receipt_fd):
            if descriptor >= 0:
                try: os.close(descriptor)
                except OSError: pass


def exec_prepared(config_path: str | Path, config_sha256: str, *, cwd: str, python: str) -> None:
    """Fork a collector, then preserve this PID while strace becomes the observed target."""
    config = load_exec_config(config_path, config_sha256, cwd=cwd, python=python)
    binary = str(config["strace_path"])
    capability = subprocess.run([binary, "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=2, check=False).stdout
    if len(capability) > 1_048_576 or b"--kill-on-exit" not in capability or b"--daemonize=pgroup" not in capability:
        raise ProcessObserverError("strace lacks required tracee containment")
    read_fd, write_fd = os.pipe(); os.set_blocking(read_fd, False); os.set_inheritable(write_fd, True)
    try:
        receipt_fd = os.open(config["receipt_path"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        os.close(read_fd); os.close(write_fd)
        raise ProcessObserverError("process observer configuration is invalid") from None
    os.fchmod(receipt_fd, 0o600)
    tracee_pid = os.getpid(); tracee_pidfd = os.pidfd_open(tracee_pid)
    collector_pid = os.fork()
    if collector_pid == 0:
        os.close(write_fd)
        _collect_preexec(read_fd, receipt_fd, tracee_pid, tracee_pidfd, config, config_sha256)
        os._exit(0)
    os.close(read_fd); os.close(receipt_fd); os.close(tracee_pidfd)
    limits = config["limits"]
    argv = [binary, "-DD", "-f", "-q", "-ttt", "--decode-fds=path", "-s", str(limits["max_line_bytes"]),
            "-e", "trace=%process,%network,%file,write,writev,pwrite64,pwritev,pwritev2,sendfile,splice,tee,vmsplice,copy_file_range,mmap,msync",
            "-o", f"/proc/self/fd/{write_fd}", "--kill-on-exit", "--", *config["argv"]]
    os.chdir(cwd)
    os.execve(binary, argv, os.environ)


def _parse(line: bytes) -> tuple[int | None, str | None, str | None]:
    exit_match = _EXIT.match(line)
    if exit_match:
        return int(exit_match.group("pid")), "process_end", "exit"
    signaled = _SIGNALED.match(line)
    if signaled:
        return int(signaled.group("pid")), "process_end", "signal"
    resumed = _RESUMED.match(line)
    match = resumed or _LINE.match(line)
    if not match:
        return None, None, None
    pid, call = int(match.group("pid")), match.group("call").decode("ascii")
    if call in _PROCESS: kind = "process_lifecycle"
    elif call in _NETWORK: kind = "network_attempt"
    elif call in _FILE or call in {"open", "openat", "openat2"} and any(
        flag in line for flag in (b"O_WRONLY", b"O_RDWR", b"O_CREAT", b"O_TRUNC", b"O_APPEND")
    ): kind = "file_mutation_attempt"
    elif call in _SIMPLE_WRITES or call in _UNSUPPORTED_TRANSFERS:
        kind = "write_attempt" if call not in {"mmap", "mmap2"} \
            or b"PROT_WRITE" in line and b"MAP_SHARED" in line else None
    else: kind = None
    return pid, kind, call


def observe(
    argv: Sequence[str],
    handler: Callable[[str, bytes], Mapping[str, Any] | None],
    *,
    timeout_seconds: float,
    max_events: int,
    max_line_bytes: int = 65_536,
    max_trace_bytes: int = 32 * 1024 * 1024,
    stdout_mode: str,
    stderr_mode: str,
    max_stderr_bytes: int = 65_536,
) -> dict[str, Any]:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ProcessObserverError("argv must be fixed nonempty strings")
    if not callable(handler) or not 0 < timeout_seconds <= 3600 or not 1 <= max_events <= 1_000_000 \
            or not 1024 <= max_line_bytes <= 1_048_576 or not 1024 <= max_trace_bytes <= 1024 * 1024 * 1024 \
            or not 1024 <= max_stderr_bytes <= 64 * 1024 * 1024 \
            or stdout_mode not in {"inherit", "devnull"} or stderr_mode not in {"handler", "devnull"}:
        raise ProcessObserverError("observer limits are invalid")
    binary = shutil.which("strace")
    if not binary:
        raise ProcessObserverError("strace is unavailable")
    try:
        capability = subprocess.run(
            [binary, "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=2, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        raise ProcessObserverError("strace capability could not be verified") from None
    if len(capability) > 1_048_576 or b"--kill-on-exit" not in capability:
        raise ProcessObserverError("strace lacks required tracee containment")
    read_fd, write_fd = os.pipe()
    stderr_read_fd = stderr_write_fd = -1
    if stderr_mode == "handler":
        stderr_read_fd, stderr_write_fd = os.pipe()
        os.set_blocking(stderr_read_fd, False)
        os.set_inheritable(stderr_write_fd, True)
    os.set_blocking(read_fd, False)
    os.set_inheritable(write_fd, True)
    trace_argv = [binary, "-f", "-q", "-ttt", "--decode-fds=path", "-s", str(max_line_bytes),
                  "-e", "trace=%process,%network,%file,write,writev,pwrite64,pwritev,pwritev2,sendfile,splice,tee,vmsplice,copy_file_range,mmap,msync", "-o", f"/proc/self/fd/{write_fd}",
                  "--kill-on-exit", "--"]
    if stderr_mode == "devnull":
        trace_argv.extend(["/bin/sh", "-c", 'exec 2>/dev/null; exec "$@"', "_"])
    else:
        trace_argv.extend([
            "/bin/sh", "-c", f'exec 2>/proc/self/fd/{stderr_write_fd}; exec "$@"', "_",
        ])
    trace_argv.extend(argv)
    output = {"inherit": None, "devnull": subprocess.DEVNULL}
    passed = (write_fd,) if stderr_write_fd < 0 else (write_fd, stderr_write_fd)
    process = subprocess.Popen(trace_argv, stdin=subprocess.DEVNULL, stdout=output[stdout_mode],
                               stderr=subprocess.PIPE, pass_fds=passed, start_new_session=True)
    assert process.stderr is not None
    os.set_blocking(process.stderr.fileno(), False)
    os.close(write_fd)
    if stderr_write_fd >= 0:
        os.close(stderr_write_fd)
    retained = []; pending = b""; observed = pipe_eof = False; failures = []
    observed_pids: set[int] = set(); ended_pids: set[int] = set(); observed_count = trace_bytes = 0
    truncated_syscall_count=0;truncated_syscall_samples=[]
    diagnostic_bytes = stderr_bytes = 0
    unfinished: dict[tuple[int, str], bytes] = {}
    deadline = time.monotonic() + timeout_seconds
    try:
        while process.poll() is None or not pipe_eof:
            if time.monotonic() >= deadline:
                failures.append("deadline_exceeded"); os.killpg(process.pid, signal.SIGKILL); break
            readers = [read_fd, process.stderr.fileno()]
            if stderr_read_fd >= 0:
                readers.append(stderr_read_fd)
            ready = select.select(readers, [], [], min(0.05, deadline-time.monotonic()))[0]
            if process.stderr.fileno() in ready:
                diagnostic = os.read(process.stderr.fileno(), 65_536)
                diagnostic_bytes += len(diagnostic)
                if diagnostic_bytes > 65_536:
                    failures.append("strace_diagnostic_limit_exceeded"); os.killpg(process.pid, signal.SIGKILL); break
            if stderr_read_fd >= 0 and stderr_read_fd in ready:
                remaining_stderr = max_stderr_bytes - stderr_bytes
                measured = os.read(stderr_read_fd, min(65_536, max(1, remaining_stderr)))
                stderr_bytes += len(measured)
                if measured:
                    delivered = measured[:max(0, remaining_stderr)]
                    if delivered:
                        observed_count += 1
                        if observed_count > max_events:
                            failures.append("event_limit_exceeded"); os.killpg(process.pid, signal.SIGKILL); break
                        try:
                            metadata = handler("process_stderr", delivered)
                        except Exception:
                            failures.append("receiver_error"); os.killpg(process.pid, signal.SIGKILL); break
                        if metadata is not None and not isinstance(metadata, Mapping):
                            failures.append("receiver_nonmapping"); os.killpg(process.pid, signal.SIGKILL); break
                        if metadata is not None and len(retained) < max_events:
                            retained.append(dict(metadata))
                    if stderr_bytes > max_stderr_bytes:
                        failures.append("stderr_byte_limit_exceeded"); os.killpg(process.pid, signal.SIGKILL); break
            if read_fd in ready:
                chunk = os.read(read_fd, 262_144)
                if chunk:
                    trace_bytes += len(chunk)
                    if trace_bytes > max_trace_bytes:
                        failures.append("trace_byte_limit_exceeded"); os.killpg(process.pid, signal.SIGKILL); break
                    pending += chunk
                    lines = pending.split(b"\n"); pending = lines.pop()
                    for line in lines:
                        resumed_match = _RESUMED.match(line)
                        if resumed_match:
                            key = (int(resumed_match.group("pid")), resumed_match.group("call").decode("ascii"))
                            first = unfinished.pop(key, None)
                            if first is None:
                                failures.append("orphan_resumed_syscall"); continue
                            line = first.replace(b"<unfinished ...>", b"") + b" " + line[resumed_match.end():]
                        elif b"<unfinished ...>" in line:
                            initial = _LINE.match(line)
                            if initial is None:
                                failures.append("unparsed_unfinished_syscall"); continue
                            key = (int(initial.group("pid")), initial.group("call").decode("ascii"))
                            if key in unfinished:
                                failures.append("duplicate_unfinished_syscall")
                            unfinished[key] = line
                            continue
                        pid, kind, call = _parse(line)
                        if kind is None: continue
                        observed_count += 1
                        if observed_count > max_events:
                            failures.append("event_limit_exceeded"); os.killpg(process.pid, signal.SIGKILL); break
                        observed = True
                        if pid is not None:
                            observed_pids.add(pid)
                            if kind == "process_end": ended_pids.add(pid)
                        if _strace_string_truncated(line):
                            failures.append("truncated_syscall_data");truncated_syscall_count+=1
                            sample=_truncation_sample(line,call,max_line_bytes)
                            if sample is not None and len(truncated_syscall_samples)<8:truncated_syscall_samples.append(sample)
                        if call in _UNSUPPORTED_TRANSFERS:
                            failures.append("unsupported_sink_transfer")
                        try:
                            metadata = handler(kind, line)
                        except Exception:
                            failures.append("receiver_error"); os.killpg(process.pid, signal.SIGKILL); break
                        if metadata is not None and not isinstance(metadata, Mapping):
                            failures.append("receiver_nonmapping"); os.killpg(process.pid, signal.SIGKILL); break
                        if metadata is not None and len(retained) < max_events:
                            retained.append(dict(metadata))
                    if any(item in failures for item in ("event_limit_exceeded", "receiver_error")): break
                else:
                    pipe_eof = True
        try: returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL); returncode = process.wait(); failures.append("strace_did_not_exit")
    finally:
        if process.poll() is None or any(pid not in ended_pids for pid in observed_pids):
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        os.close(read_fd)
        if stderr_read_fd >= 0:
            os.close(stderr_read_fd)
    if not observed: failures.append("lost_syscall_data")
    if unfinished: failures.append("missing_resumed_syscall")
    if pending: failures.append("partial_trace_frame")
    if diagnostic_bytes: failures.append("strace_diagnostic")
    if observed_pids - ended_pids: failures.append("missing_process_end")
    return {"schema": "sage-process-observer-result-v1", "argv_sha256": __import__("hashlib").sha256("\0".join(argv).encode()).hexdigest(),
            "returncode": returncode, "complete": not failures, "failures": list(dict.fromkeys(failures)),
            "coverage_scope": "selected_os_syscall_stream", "redacted_events": retained,
            "event_count": observed_count, "retained_event_count": len(retained),
            "truncated_syscall_count":truncated_syscall_count,
            "truncated_syscall_samples":truncated_syscall_samples,
            "stderr_sink": stderr_mode, "stderr_bytes": stderr_bytes,
            "unsupported_sink_transfers": sorted(_UNSUPPORTED_TRANSFERS)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exec-config", type=Path)
    parser.add_argument("--exec-config-sha256")
    parser.add_argument("--cwd")
    parser.add_argument("--python")
    args = parser.parse_args(argv)
    supplied = (args.exec_config, args.exec_config_sha256, args.cwd, args.python)
    if any(supplied):
        if not all(supplied):
            raise SystemExit("process observer configuration is invalid")
        try:
            exec_prepared(args.exec_config, args.exec_config_sha256, cwd=args.cwd, python=args.python)
        except ProcessObserverError as error:
            raise SystemExit(str(error)) from None
        return 0
    print("DRY RUN: import observe with fixed argv and an explicit transient redaction handler.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
