"""Capture-only compatibility shim for Phoenix's process launcher.

Phoenix 19.19.0 builds ``main.py FLAGS serve`` even though its entry parser
requires ``main.py serve FLAGS``.  Keep Phoenix's ProcessSession and service
lifecycle, changing only that command ordering while capture is active.
"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import re
import sysconfig
import threading
from typing import Any, Sequence


class PhoenixProcessStartupError(RuntimeError):
    """A fixed, non-sensitive capture Phoenix startup failure."""

    def __init__(self, message: str, diagnostic: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


_EXPECTED_FLAGS = ("--database-url", "--host", "--port")
_MAX_DIAGNOSTIC_BYTES = 64 * 1024
_REPO_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_SOURCE = _REPO_ROOT / "Payload_Type" / "sage"
_SITE_PACKAGES = Path(sysconfig.get_paths()["purelib"]).resolve()
_DEPENDENCY_ROOTS = frozenset({
    "alembic", "anyio", "fastapi", "grpc", "opentelemetry", "phoenix",
    "pydantic", "sqlalchemy", "starlette", "strawberry", "uvicorn",
})
_EXCEPTION_CLASSES = frozenset({
    "SystemExit", "ImportError", "ModuleNotFoundError", "FileNotFoundError",
    "RuntimeError", "ValueError", "OSError", "PermissionError",
    "OperationalError", "DatabaseError", "InterfaceError", "ProgrammingError",
    "IntegrityError", "DataError", "InternalError", "NotSupportedError",
    "PendingRollbackError",
})
_DATABASE_CLASSES = frozenset({
    "OperationalError", "DatabaseError", "InterfaceError", "ProgrammingError",
    "IntegrityError", "DataError", "InternalError", "NotSupportedError",
    "PendingRollbackError",
})
_FRAME = re.compile(rb'^\s*File "(?P<path>[^"]+)", line (?P<line>[1-9][0-9]*)')
_EXCEPTION = re.compile(rb"^(?P<name>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Exit))(?:\s*:|\s*$)")
_DIAGNOSTIC_CATEGORIES = frozenset({
    "alive_but_unready", "argument_parser_rejected", "child_exited",
    "database_startup_error", "dependency_import_error", "inactive_process",
    "preexisting_pid_entry", "startup_error", "unsupported_command_shape",
})
_LIFECYCLE_STATES = frozenset({
    "running", "sleeping", "disk-sleep", "stopped", "tracing-stop", "zombie",
    "dead", "wake-kill", "waking", "parked", "idle", "locked",
})


def _source_frame(raw_path: bytes, raw_line: bytes) -> dict[str, Any] | None:
    try:
        path = Path(os.fsdecode(raw_path)).resolve(strict=False)
        line = int(raw_line)
        try:
            relative = path.relative_to(_SITE_PACKAGES)
            if not relative.parts or relative.parts[0] not in _DEPENDENCY_ROOTS:
                return None
        except ValueError:
            relative = path.relative_to(_REPO_ROOT)
            if not path.is_relative_to(_RUNTIME_SOURCE):
                return None
        if path.suffix != ".py" or not path.is_file():
            return None
        raw = path.read_bytes()
        if line > max(1, raw.count(b"\n") + 1):
            return None
        digest = sha256(raw).hexdigest()
    except (OSError, UnicodeError, ValueError):
        return None
    return {"relative_path": relative.as_posix(), "line": line, "source_sha256": digest}


def project_child_output(output: bytes, returncode: int | None) -> dict[str, Any]:
    """Reduce bounded child output to source-bound diagnostics with no raw values."""
    bounded = bytes(output[:_MAX_DIAGNOSTIC_BYTES])
    argparse_rejected = returncode == 2 and b"usage:" in bounded and b"error:" in bounded
    frames = []
    for line in bounded.splitlines():
        match = _FRAME.match(line)
        if match and (frame := _source_frame(match["path"], match["line"])) is not None:
            frames.append(frame)
    exception_class = "SystemExit" if argparse_rejected else "unknown"
    for line in reversed(bounded.splitlines()):
        match = _EXCEPTION.match(line.strip())
        if match:
            candidate = match["name"].decode("ascii").rsplit(".", 1)[-1]
            exception_class = candidate if candidate in _EXCEPTION_CLASSES else "unknown"
            break
    if argparse_rejected:
        category = "argument_parser_rejected"
    elif exception_class in {"ImportError", "ModuleNotFoundError", "FileNotFoundError"}:
        category = "dependency_import_error"
    elif exception_class in _DATABASE_CLASSES:
        category = "database_startup_error"
    elif exception_class in {"RuntimeError", "ValueError", "OSError", "PermissionError"}:
        category = "startup_error"
    else:
        category = "child_exited"
    return {
        "status": "failure", "error_category": category,
        "returncode": returncode if isinstance(returncode, int) else None,
        "output_sha256": sha256(bounded).hexdigest(), "exception_class": exception_class,
        "frames": frames[-4:],
    }


def compatible_command(command: Sequence[str]) -> list[str]:
    """Move an exact trailing ``serve`` before Phoenix's existing flags."""
    value = list(command)
    if len(value) == 9 and value[1] == "main.py":
        value.insert(1, "-P")
    if len(value) != 10 or value[1:3] != ["-P", "main.py"]:
        raise PhoenixProcessStartupError("capture Phoenix command shape is unsupported")
    if value[3] == "serve" and tuple(value[4::2]) == _EXPECTED_FLAGS:
        return value
    if value[-1] == "serve" and tuple(value[3:-1:2]) == _EXPECTED_FLAGS:
        return value[:3] + ["serve"] + value[3:-1]
    raise PhoenixProcessStartupError("capture Phoenix command shape is unsupported")


class _ChildOutput:
    def __init__(self, stream: Any) -> None:
        self.stream = stream; self.lock = threading.Lock(); self.tail = bytearray()
        self.digest = sha256(); self.byte_count = 0; self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            try: descriptor = self.stream.fileno()
            except (AttributeError, OSError, ValueError): descriptor = None
            while True:
                chunk = os.read(descriptor, 65536) if descriptor is not None else self.stream.read(65536)
                if isinstance(chunk, str): chunk = chunk.encode("utf-8", errors="replace")
                if not chunk: break
                with self.lock:
                    self.digest.update(chunk); self.byte_count += len(chunk); self.tail.extend(chunk)
                    if len(self.tail) > _MAX_DIAGNOSTIC_BYTES:
                        del self.tail[:-_MAX_DIAGNOSTIC_BYTES]
        except (OSError, ValueError):
            pass
        finally:
            self.done.set()

    def snapshot(self) -> tuple[bytes, int, str, bool]:
        with self.lock:
            return bytes(self.tail), self.byte_count, self.digest.copy().hexdigest(), self.done.is_set()

    def finish(self, timeout: float = 5) -> None:
        self.thread.join(timeout)


def _startup_diagnostic(
    child: Any, *, category: str | None = None, finalize_inactive: bool = False,
) -> dict[str, Any]:
    """Snapshot continuously drained output without exposing its bytes."""
    if category is not None and category not in _DIAGNOSTIC_CATEGORIES:
        raise PhoenixProcessStartupError("capture Phoenix diagnostic category is unsupported")
    output = b""
    output_bytes = 0; output_digest = sha256(b"").hexdigest(); output_complete = True
    drain = getattr(child, "_sage_startup_output", None)
    try:
        lifecycle = child.status()
        lifecycle = lifecycle if lifecycle in _LIFECYCLE_STATES else "unknown"
    except Exception:
        lifecycle = "unknown"
    try: was_running: bool | None = bool(child.is_running())
    except Exception: was_running = None
    returncode = getattr(child, "returncode", None)
    inactive = lifecycle in {"zombie", "dead"} or was_running is False or isinstance(returncode, int)
    if finalize_inactive and inactive:
        waiter = getattr(child, "wait", None)
        if callable(waiter):
            try: waiter(timeout=1)
            except Exception: pass
        if isinstance(drain, _ChildOutput):
            drain.finish(1)
    returncode = getattr(child, "returncode", returncode)
    if returncode is None:
        try: returncode = child.poll()
        except Exception: returncode = None
    if finalize_inactive and returncode is not None and isinstance(drain, _ChildOutput):
        drain.finish(1)
    if isinstance(drain, _ChildOutput):
        output, output_bytes, output_digest, output_complete = drain.snapshot()
    stream = getattr(child, "stdout", None)
    if drain is None and stream is not None:
        try:
            descriptor = stream.fileno()
        except (AttributeError, OSError, ValueError):
            descriptor = None
        try:
            if descriptor is not None:
                os.set_blocking(descriptor, False)
                output = os.read(descriptor, _MAX_DIAGNOSTIC_BYTES)
            else:
                output = stream.read(_MAX_DIAGNOSTIC_BYTES)
            if isinstance(output, str):
                output = output.encode("utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            output = b""
        output_bytes = len(output); output_digest = sha256(output).hexdigest()
    result = project_child_output(output, returncode)
    if category is not None:
        result["error_category"] = category
    alive = returncode is None
    try: alive = alive and child.is_running()
    except (AttributeError, OSError): pass
    result.update(output_sha256=output_digest, output_bytes=output_bytes,
                  retained_bytes=len(output), output_complete=output_complete,
                  alive=bool(alive), lifecycle_status=lifecycle,
                  lifecycle_running=was_running)
    return result


def child_startup_diagnostic(child: Any, *, category: str | None = None) -> dict[str, Any]:
    """Return a nonblocking startup snapshot for a possibly live child."""
    return _startup_diagnostic(child, category=category)


def _child_diagnostic(child: Any) -> dict[str, Any]:
    return _startup_diagnostic(child, finalize_inactive=True)


def launch_capture_phoenix(px: Any) -> Any:
    """Launch Phoenix through its installed ProcessSession with a scoped shim."""
    import phoenix.services as services
    import phoenix.session.session as session_module
    from ai.observation_channel import emit

    diagnostic: dict[str, Any] | None = None

    class CompatibleAppService(services.AppService):
        @property
        def command(self) -> list[str]:
            return compatible_command(super().command)

        def start(self) -> Any:
            try:
                occupied = any(services.get_pids_path().iterdir())
            except OSError:
                occupied = True
            if occupied:
                empty = project_child_output(b"", None)
                empty["error_category"] = "preexisting_pid_entry"
                raise PhoenixProcessStartupError(
                    "capture Phoenix PID directory is occupied", diagnostic=empty
                )
            child = services.psutil.Popen(
                self.command, cwd=self.working_dir, stdout=services.subprocess.PIPE,
                stderr=services.subprocess.STDOUT, bufsize=1, text=True,
                env={**services.os.environ},
            )
            child_pid = getattr(child, "pid", None)
            if type(child_pid) is int and child_pid > 0:
                streams = []
                for fd in (1, 2):
                    try:
                        target = services.os.readlink(f"/proc/{child_pid}/fd/{fd}")
                        metadata = services.os.stat(f"/proc/{child_pid}/fd/{fd}")
                        streams.append({"fd":fd,"target":target,"device":metadata.st_dev,"inode":metadata.st_ino,"type":"pipe" if target.startswith("pipe:[") else "socket" if target.startswith("socket:[") else "regular","missing":False})
                    except OSError:
                        streams.append({"fd":fd,"target":"","device":None,"inode":None,"type":"unknown","missing":True})
                emit("tool_event",{"event":"observed_process_streams","tracee_pid":child_pid,"streams":streams})
            output = _ChildOutput(child.stdout); setattr(child, "_sage_startup_output", output)
            output.start()
            return child

        def stop(self) -> None:
            try:
                super().stop()
            finally:
                output = getattr(self.child, "_sage_startup_output", None)
                if isinstance(output, _ChildOutput): output.finish()

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal diagnostic
            super().__init__(*args, **kwargs)
            if not self.active:
                diagnostic = _child_diagnostic(self.child)

    installed = session_module.AppService
    session_module.AppService = CompatibleAppService
    try:
        result = px.launch_app(use_temp_dir=False, run_in_thread=False)
    except PhoenixProcessStartupError as error:
        safe = error.diagnostic or project_child_output(b"", None)
        if error.diagnostic is None:
            safe["error_category"] = "unsupported_command_shape"
        emit("tool_event", {"event": "phoenix_process_startup_diagnostic", **safe})
        raise
    finally:
        session_module.AppService = installed
    if result is None:
        safe = diagnostic or project_child_output(b"", None)
        if diagnostic is None:
            safe["error_category"] = "inactive_process"
        emit("tool_event", {"event": "phoenix_process_startup_diagnostic", **safe})
        raise PhoenixProcessStartupError("capture Phoenix process failed to start", diagnostic=safe)
    return result
