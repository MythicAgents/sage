#!/usr/bin/env python3
"""Contain an assembled local Sage diagnostic in one network/PID namespace."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[3]
SCHEMA = "sage-diagnostic-service-isolation-v1"
WASM_SHA256 = "e5dc5a398b07b54ea8fdb503bf68fb583d533f10ec3f930963e02b9505f7a763"
WASM_SIZE = 26_267_204
RELAY_PORTS = [5672, 7443]
FINAL_RECEIPT_MARGIN = 5


class DiagnosticServiceIsolationError(RuntimeError):
    pass


def _bound(path_value, digest, name, *, empty=False) -> Path:
    try:
        path = Path(path_value); metadata = os.stat(path, follow_symlinks=False); raw = path.read_bytes()
    except (OSError, TypeError):
        raise DiagnosticServiceIsolationError(f"{name} is unavailable") from None
    if (not path.is_absolute() or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest
            or empty and raw):
        raise DiagnosticServiceIsolationError(f"{name} differs")
    return path.resolve()


def load_config(path: Path, sha256: str) -> dict:
    raw_path = _bound(path, sha256, "service isolation configuration")
    try: value = json.loads(raw_path.read_text())
    except (UnicodeError, json.JSONDecodeError):
        raise DiagnosticServiceIsolationError("service isolation configuration is invalid") from None
    fields = {"schema", "context_directory", "relay_config", "relay_source", "isolation_source",
              "phoenix_client_config", "writable_paths", "active_deadline_monotonic",
              "cleanup_deadline_monotonic", "wasm"}
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != SCHEMA:
        raise DiagnosticServiceIsolationError("service isolation configuration is invalid")
    context = Path(value.get("context_directory", ""))
    try: context_meta = os.stat(context, follow_symlinks=False)
    except OSError: raise DiagnosticServiceIsolationError("service isolation context is invalid") from None
    active, cleanup = value.get("active_deadline_monotonic"), value.get("cleanup_deadline_monotonic")
    if (not context.is_absolute() or not stat.S_ISDIR(context_meta.st_mode)
            or context_meta.st_uid != os.getuid() or stat.S_IMODE(context_meta.st_mode) != 0o700
            or type(active) not in (int, float) or type(cleanup) not in (int, float)
            or not math.isfinite(active) or not math.isfinite(cleanup)
            or not time.monotonic() < active <= cleanup or cleanup - active > 120):
        raise DiagnosticServiceIsolationError("service isolation configuration is invalid")
    wasm = value["wasm"]
    if not isinstance(wasm, dict) or set(wasm) != {"path", "sha256"} or wasm.get("sha256") != WASM_SHA256:
        raise DiagnosticServiceIsolationError("Phoenix WASM differs")
    wasm_path = _bound(wasm.get("path"), wasm.get("sha256"), "Phoenix WASM")
    if wasm_path.stat().st_size != WASM_SIZE:
        raise DiagnosticServiceIsolationError("Phoenix WASM differs")
    refs = {}
    for key, name in (("relay_config", "relay configuration"), ("relay_source", "relay source"),
                      ("isolation_source", "isolation source")):
        ref = value[key]
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            raise DiagnosticServiceIsolationError("service isolation configuration is invalid")
        refs[key] = _bound(ref["path"], ref["sha256"], name)
    try: relay_value = json.loads(refs["relay_config"].read_text())
    except (UnicodeError, json.JSONDecodeError):
        raise DiagnosticServiceIsolationError("relay configuration differs") from None
    if (not isinstance(relay_value, dict)
            or relay_value.get("schema") != "sage-diagnostic-loopback-relay-v1"
            or relay_value.get("ports") != RELAY_PORTS
            or relay_value.get("context_directory") != str(context.resolve())
            or relay_value.get("deadline_monotonic") != cleanup):
        raise DiagnosticServiceIsolationError("relay configuration differs")
    phoenix = value["phoenix_client_config"]
    if not isinstance(phoenix, dict) or set(phoenix) != {"path", "sha256"}:
        raise DiagnosticServiceIsolationError("service isolation configuration is invalid")
    refs["phoenix_client_config"] = _bound(
        phoenix["path"], phoenix["sha256"], "Phoenix client configuration", empty=True)
    writable = value["writable_paths"]
    if (not isinstance(writable, list) or not writable or len(set(writable)) != len(writable)
            or any(not isinstance(item, str) for item in writable)
            or str(context.resolve()) not in writable):
        raise DiagnosticServiceIsolationError("service isolation writable paths differ")
    value.update(refs); value["wasm"] = wasm_path; value["context_directory"] = context.resolve()
    return value


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise DiagnosticServiceIsolationError("service isolation dependency is unavailable")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def pinned_restart_arguments(config_path: Path, config_sha256: str) -> list[str]:
    """Arguments the C integration appends to sage_restart.sh after sourced environment."""
    config = load_config(config_path, config_sha256)
    values = {"RABBITMQ_HOST": "127.0.0.1", "RABBITMQ_PORT": "5672",
        "MYTHIC_SERVER_HOST": "127.0.0.1", "NGINX_HOST": "127.0.0.1",
        "SAGE_LOCAL_MYTHIC_HOSTS": "0",
        "PHOENIX_HOST": "127.0.0.1", "PHOENIX_PORT": "6006", "PHOENIX_GRPC_PORT": "4317",
        "PHOENIX_COLLECTOR_ENDPOINT": "http://127.0.0.1:4317",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4317",
        "PHOENIX_CLIENT_CONFIG": str(config["phoenix_client_config"]),
        "PHOENIX_WASM_BINARY_PATH": str(config["wasm"])}
    return [f"{key}={values[key]}" for key in sorted(values)]


def _descendants(pid: int) -> set[int]:
    found, pending = set(), [pid]
    while pending:
        parent = pending.pop()
        try: children = [int(item) for item in Path(f"/proc/{parent}/task/{parent}/children").read_text().split()]
        except OSError: children = []
        for child in children:
            if child not in found: found.add(child); pending.append(child)
    return found


def _write_receipt(path: Path, value: Mapping) -> None:
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":"))); os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _inner(config_path: Path, config_sha256: str, receipt_path: Path, argv: Sequence[str]) -> int:
    config = load_config(config_path, config_sha256)
    relay = _module(config["relay_source"], "sage_service_inner_relay")
    started = time.monotonic(); command = None; timed_out = False
    with relay.InnerRelay(config["relay_config"], _digest(config["relay_config"].read_bytes())) as inner:
        command = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, start_new_session=False)
        first = {"schema": SCHEMA, "inner_supervisor_pid": os.getpid(), "command_inner_pid": command.pid,
                 "listener_started_receipt": inner.receipt()}
        _write_receipt(receipt_path, first)
        try: returncode = command.wait(timeout=max(.001, config["active_deadline_monotonic"] - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            command.terminate()
            try: returncode = command.wait(timeout=max(.001, config["cleanup_deadline_monotonic"] - time.monotonic()))
            except subprocess.TimeoutExpired: command.kill(); returncode = command.wait(timeout=2)
    first.update(command_returncode=returncode,
                 elapsed_seconds=max(0, time.monotonic() - started), timed_out=timed_out,
                 listener_final_receipt=inner.receipt())
    _write_receipt(receipt_path, first); return returncode


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def execute(config_path: Path, config_sha256: str, argv: Sequence[str], environment: Mapping[str, str],
            receipt_path: Path, *, deadline_provider: Callable[[], float | None] | None = None) -> dict:
    """Run the complete C driver under one namespace and return containment-only facts."""
    config = load_config(config_path, config_sha256)
    isolation = _module(config["isolation_source"], "sage_service_isolation_builder")
    relay = _module(config["relay_source"], "sage_service_outer_relay")
    if (not argv or not Path(argv[0]).is_absolute() or any(not isinstance(item, str) or not item for item in argv)
            or receipt_path.parent.resolve() != config["context_directory"]):
        raise DiagnosticServiceIsolationError("service isolation command is invalid")
    child_receipt = config["context_directory"] / "namespace-receipt.json"
    if child_receipt.exists() or receipt_path.exists():
        raise DiagnosticServiceIsolationError("service isolation receipt is unavailable")
    child_env = dict(environment)
    for argument in pinned_restart_arguments(config_path, config_sha256):
        key, value = argument.split("=", 1); child_env[key] = value
    child_env["SAGE_M3_C_NAMESPACE_INNER"] = "1"
    child_argv = [sys.executable, "-P", str(Path(__file__).resolve()), "--inner", str(config_path),
                  config_sha256, str(child_receipt), "--", *argv]
    wrapped = isolation.isolated_command(child_argv,
        writable_paths=[Path(item) for item in config["writable_paths"]], environment=child_env,
        cwd=config["context_directory"], read_only_paths=[ROOT, config["wasm"]])
    known = set(); timed_out = False; failure = None; process = None
    outer = relay.OuterRelay(config["relay_config"], _digest(config["relay_config"].read_bytes()))
    effective_cleanup = config["cleanup_deadline_monotonic"]
    def cutoff() -> float:
        nonlocal effective_cleanup
        if callable(deadline_provider):
            dynamic = deadline_provider()
            if dynamic is not None:
                if type(dynamic) not in (int, float) or not math.isfinite(dynamic):
                    raise DiagnosticServiceIsolationError("service isolation deadline is invalid")
                effective_cleanup = min(effective_cleanup, dynamic)
                return effective_cleanup - FINAL_RECEIPT_MARGIN
        return config["active_deadline_monotonic"]
    try:
        outer.__enter__()
        process = subprocess.Popen(wrapped, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        while process.poll() is None and time.monotonic() < cutoff():
            known.update(_descendants(process.pid)); time.sleep(.02)
        if process.poll() is None: timed_out = True
    except BaseException as exc:
        failure = type(exc).__name__
    finally:
        try:
            if process is not None:
                try: known.update(_descendants(process.pid))
                except BaseException: pass
                if process.poll() is None:
                    try: os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                    try: process.wait(timeout=max(.001,
                        effective_cleanup - time.monotonic() - FINAL_RECEIPT_MARGIN))
                    except subprocess.TimeoutExpired:
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                        try: process.wait(timeout=max(.001, min(.25, effective_cleanup - time.monotonic())))
                        except subprocess.TimeoutExpired: pass
                try: known.update(_descendants(process.pid))
                except BaseException: pass
        finally:
            outer.close()
    deadline = effective_cleanup
    while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() for pid in known): time.sleep(.02)
    child = json.loads(child_receipt.read_text()) if child_receipt.exists() else None
    if isinstance(child, Mapping) and child.get("timed_out") is True: timed_out = True
    result = {"schema": SCHEMA, "wrapper_host_pid": process.pid if process is not None else None,
        "descendant_host_pids": sorted(known), "namespace": child,
        "returncode": process.returncode if process is not None else None, "timed_out": timed_out,
        "failure_type": failure,
        "owned_processes_dead": (process is None or not Path(f"/proc/{process.pid}").exists())
            and all(not Path(f"/proc/{pid}").exists() for pid in known),
        "relay_receipt": outer.receipt(), "cleanup_seconds_limit": 120}
    _write_receipt(receipt_path, result)
    if failure is not None: raise DiagnosticServiceIsolationError("service isolation execution failed")
    return result


def main(argv=None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) < 5 or values[0] != "--inner" or "--" not in values:
        raise SystemExit("service isolation invocation is invalid")
    divider = values.index("--")
    return _inner(Path(values[1]), values[2], Path(values[3]), values[divider + 1:])


if __name__ == "__main__": raise SystemExit(main())
