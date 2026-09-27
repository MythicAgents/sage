#!/usr/bin/env python3
"""One-start, source-bound service-readiness action for the development capture driver."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
from importlib.metadata import distribution, version
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "Payload_Type" / "sage"
SCHEMA = "sage-m3-capture-service-probe-v1"
SOURCE_SCHEMA = "sage-m3-capture-service-source-v1"
RESULT_SCHEMA = "sage-m3-capture-service-result-v1"
_REQUIRED_FILE_ROLES = {
    "capture_driver": ROOT / "skills/sage-eval-gauge/scripts/run_capture_episode.py",
    "capture_service_probe": Path(__file__).resolve(),
    "diagnostic_service_isolation": ROOT / "skills/sage-eval-gauge/scripts/diagnostic_service_isolation.py",
    "diagnostic_loopback_relay": ROOT / "skills/sage-eval-gauge/scripts/diagnostic_loopback_relay.py",
    "diagnostic_isolation": ROOT / "skills/sage-eval-gauge/scripts/diagnostic_isolation.py",
    "bwrap": Path("/usr/bin/bwrap"),
    "process_observer": ROOT / "skills/sage-eval-gauge/scripts/process_observer.py",
    "observation_receiver": ROOT / "skills/sage-eval-gauge/scripts/observation_receiver.py",
    "observation_channel": RUNTIME / "ai/observation_channel.py",
    "observation_process": RUNTIME / "ai/observation_process.py",
    "resource_budget": RUNTIME / "ai/resource_budget.py",
    "resource_clients": RUNTIME / "ai/resource_clients.py",
    "resource_http": RUNTIME / "ai/resource_http.py",
    "resource_rpc": RUNTIME / "ai/resource_rpc.py",
    "sage_main": RUNTIME / "main.py",
    "phoenix_process_compat": RUNTIME / "ai/phoenix_process_compat.py",
    "sage_restart": ROOT / "skills/sage-goad-reset/scripts/sage_restart.sh",
    "sage_relaunch": ROOT / "skills/sage-goad-reset/scripts/_sage_relaunch.py",
    "sage_stop": ROOT / "skills/sage-goad-reset/scripts/sage_stop.sh",
    "startup_identity": ROOT / "skills/sage-goad-reset/scripts/readiness_contract.py",
    "archive_runtime": ROOT / "skills/sage-goad-reset/scripts/archive_runtime_dbs.py",
    "chat_registration": ROOT / "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py",
}
_SERVICE_LIMITS = {
    "allowed_resources": ["control_requests"], "provider_authorized": False,
    "payload_tasks_authorized": False, "range_mutations_authorized": False,
    "max_starts": 1, "active_seconds": 900, "total_seconds": 1200,
    "control_requests": 1024, "active_control_requests": 896,
    "cleanup_control_requests": 128,
}
_V3_SERVICE_LIMITS = {**_SERVICE_LIMITS, "active_seconds": 2580, "total_seconds": 2700}
_V5_SERVICE_LIMITS = {
    "allowed_resources": ["control_requests"], "provider_authorized": False,
    "payload_tasks_authorized": False, "range_mutations_authorized": False,
    "max_starts": 24, "active_seconds": 15465600, "total_seconds": 15552000,
    "control_requests": 8192, "active_control_requests": 7168,
    "cleanup_control_requests": 1024,
}
_CRITICAL_OBSERVER_FAILURES = frozenset({
    "deadline_exceeded", "trace_byte_limit_exceeded", "event_limit_exceeded", "receiver_error",
    "collector_error", "containment_unverified", "tracer_not_attested", "lost_syscall_data",
    "missing_root_process_end", "missing_process_end", "missing_resumed_syscall", "partial_trace_frame",
})
_SAFE_EXECUTION_ERROR_CODES = frozenset({
    "canonical ledger cannot start service probe", "approved recovery amendment is missing",
    "approved recovery amendment differs", "approved recovery lineage differs",
    "approved recovery amendment is not applied", "Sage or Phoenix predates service probe",
    "service ledger deadline is invalid", "service active deadline exhausted",
    "service total deadline exhausted", "one-start Sage relaunch failed",
    "local Sage startup identity is unavailable", "observer and local startup identity differ",
    "service start timestamp is invalid", "fresh native Chat registration is unavailable",
    "Phoenix listener is not owned by launched Sage", "Phoenix service is not ready",
    "service application request evidence is incomplete", "service cleanup or process evidence is incomplete",
    "service observation receiver failed", "service dependency is unavailable",
    "owned Sage exited before service readiness", "native Chat registration timed out",
    "Phoenix startup failed",
})


class ServiceProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrozenServiceProbe:
    path: Path
    sha256: str
    value: dict[str, Any]


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_json(path: Path, digest: str, label: str) -> dict[str, Any]:
    if not path.is_absolute() or len(digest) != 64:
        raise ServiceProbeError(f"{label} binding differs")
    try:
        raw = path.read_bytes(); value = json.loads(raw)
        metadata = path.stat(follow_symlinks=False)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ServiceProbeError(f"{label} binding differs") from None
    if not stat.S_ISREG(metadata.st_mode) or _digest(raw) != digest or not isinstance(value, dict):
        raise ServiceProbeError(f"{label} binding differs")
    return value


def _bound_file(path: Path, digest: str, label: str) -> bytes:
    if not path.is_absolute() or len(digest) != 64:
        raise ServiceProbeError(f"{label} binding differs")
    try: raw = path.read_bytes(); metadata = path.stat(follow_symlinks=False)
    except OSError: raise ServiceProbeError(f"{label} binding differs") from None
    if not stat.S_ISREG(metadata.st_mode) or _digest(raw) != digest:
        raise ServiceProbeError(f"{label} binding differs")
    return raw


def load(path: str | Path, expected_sha256: str) -> FrozenServiceProbe:
    source = Path(path).resolve(); value = _load_json(source, expected_sha256, "service probe")
    return FrozenServiceProbe(source, expected_sha256, value)


def _recovery_shape(value: Mapping[str, Any]) -> bool:
    if value.get("schema") == "sage-resource-recovery-amendment-v4":
        return (value.get("limit_changes") == {"capture_correction_cycles": {"old": 6, "new": 6}}
            and value.get("amendment_ordinal") == 4 and value.get("engineering_seconds") == 43200
            and value.get("engineering_cleanup_seconds") == 900
            and value.get("diagnostic_session") == {"stages": ["A", "B", "C"],
                "max_stage_starts": 24, "pooled_active_seconds": 7200,
                "stage_active_seconds": 600, "stage_cleanup_seconds": 120,
                "stage_total_seconds": 720, "calendar_deadlines_inherited": True}
            and value.get("capture_recovery") == {"max_attempts": 6,
                "max_before_first_composite": 4, "consume_episode_and_setup_retry": True,
                "interpretable_outcome_replacement": False})
    if value.get("schema") == "sage-resource-recovery-amendment-v3":
        return (value.get("limit_changes") == {"capture_correction_cycles": {"old": 5, "new": 6}}
            and value.get("amendment_ordinal") == 3 and value.get("engineering_seconds") == 7200
            and value.get("correction_engineering_seconds") == 1500
            and value.get("diagnostic_session") == {"stages": ["A", "B", "C"], "starts_per_stage": 2,
                "max_stage_starts": 6, "stage_active_seconds": 120, "stage_total_seconds": 180})
    if value.get("schema") == "sage-resource-recovery-amendment-v1":
        return value.get("limit_changes") == {"capture_correction_cycles": {"old": 2, "new": 4}}
    return (value.get("schema") == "sage-resource-recovery-amendment-v2"
        and value.get("limit_changes") == {"capture_correction_cycles": {"old": 4, "new": 5}}
        and value.get("amendment_ordinal") == 2 and value.get("replacement_service_probe") is True
        and value.get("engineering_seconds") == 7200 and value.get("correction_engineering_seconds") == 1500)


def validate(frozen: FrozenServiceProbe, *, live: bool) -> dict[str, Any]:
    value = _load_json(frozen.path, frozen.sha256, "service probe")
    if value != frozen.value or set(value) != {
        "schema", "amendment", "source_binding", "resources", "observation"
    } or value.get("schema") != SCHEMA:
        raise ServiceProbeError("service probe changed or has invalid fields")
    amendment_ref, source_ref, resources, observation = (
        value.get(name) for name in ("amendment", "source_binding", "resources", "observation")
    )
    if not all(isinstance(item, Mapping) for item in (amendment_ref, source_ref, resources, observation)):
        raise ServiceProbeError("service probe has invalid fields")
    amendment_path = Path(str(amendment_ref.get("path") or "")).resolve()
    amendment_sha = str(amendment_ref.get("sha256") or "")
    amendment = _load_json(amendment_path, amendment_sha, "approved recovery amendment")
    expected_service_limits = (_V5_SERVICE_LIMITS if amendment.get("schema") == "sage-resource-recovery-amendment-v4"
        else _V3_SERVICE_LIMITS if amendment.get("schema") == "sage-resource-recovery-amendment-v3" else _SERVICE_LIMITS)
    if (not _recovery_shape(amendment)
            or amendment.get("status") != "approved"
            or amendment.get("amendment_id") != amendment_ref.get("id")
            or amendment.get("service_probe") != expected_service_limits
            or (amendment.get("schema") != "sage-resource-recovery-amendment-v4" and
                (amendment.get("schedule_id") != "m3-order-v1:00001" or amendment.get("exceptional_retry_ordinal") != 2))
            or amendment.get("conditional_continuation_authorized") is not True
            or amendment.get("original_aggregate_limits_unchanged") is not True):
        raise ServiceProbeError("approved recovery amendment differs")
    document, parent = amendment.get("document"), amendment.get("parent_authority")
    if not isinstance(document, Mapping) or not isinstance(parent, Mapping):
        raise ServiceProbeError("approved recovery lineage differs")
    _bound_file(Path(str(document.get("path") or "")).resolve(), str(document.get("sha256") or ""),
                "approved recovery document")
    parent_value = _load_json(Path(str(parent.get("path") or "")).resolve(),
                              str(parent.get("sha256") or ""), "parent authority")
    if parent_value.get("status") != "active" or parent_value.get("live_authorized") is not True:
        raise ServiceProbeError("parent authority differs")
    if amendment.get("schema") in {"sage-resource-recovery-amendment-v2", "sage-resource-recovery-amendment-v3", "sage-resource-recovery-amendment-v4"}:
        prior_ref = amendment.get("prior_amendment", {})
        prior = _load_json(Path(str(prior_ref.get("path") or "")), str(prior_ref.get("sha256") or ""),
                           "prior recovery amendment")
        expected_prior = ({"sage-resource-recovery-amendment-v4": "sage-resource-recovery-amendment-v3",
            "sage-resource-recovery-amendment-v3": "sage-resource-recovery-amendment-v2"}
            .get(amendment.get("schema"), "sage-resource-recovery-amendment-v1"))
        if (prior.get("schema") != expected_prior or not _recovery_shape(prior)
                or prior.get("status") != "approved" or prior.get("parent_authority") != parent):
            raise ServiceProbeError("approved recovery lineage differs")
    config_path = Path(str(resources.get("config") or "")).resolve()
    ledger_path = Path(str(resources.get("ledger") or "")).resolve()
    config_sha = str(resources.get("config_sha256") or "")
    probe_id = resources.get("probe_id")
    _load_json(config_path, config_sha, "base resource configuration")
    if (config_sha != amendment.get("base_config_sha256")
            or ledger_path.parent != amendment_path.parent
            or ledger_path.name != "shared-resource-ledger.jsonl"
            or not isinstance(probe_id, str) or not probe_id.strip()):
        raise ServiceProbeError("service resource binding differs")
    source_path = Path(str(source_ref.get("path") or "")).resolve()
    source_sha = str(source_ref.get("sha256") or "")
    source = _load_json(source_path, source_sha, "service source")
    files, roles = source.get("files"), source.get("file_roles")
    if (source.get("schema") != SOURCE_SCHEMA or source.get("amendment_path") != str(amendment_path)
            or source.get("amendment_sha256") != amendment_sha
            or not isinstance(files, Mapping) or not isinstance(roles, Mapping)
            or set(roles) != set(_REQUIRED_FILE_ROLES)):
        raise ServiceProbeError("service source binding differs")
    for file_path, digest in files.items():
        _bound_file(Path(str(file_path)).resolve(), str(digest), "service source file")
    if amendment.get("schema") in {"sage-resource-recovery-amendment-v2", "sage-resource-recovery-amendment-v3", "sage-resource-recovery-amendment-v4"}:
        dependency = source.get("phoenix_dependency", {})
        installed = distribution("arize-phoenix")
        required_paths = {str(Path(installed.locate_file(relative)).resolve()) for relative in (
            "phoenix/services.py", "phoenix/session/session.py", "phoenix/server/main.py",
            "phoenix/server/cli/commands/serve.py")}
        if (dependency.get("version") != version("arize-phoenix")
                or not isinstance(dependency.get("files"), dict)
                or set(dependency["files"]) != required_paths
                or any(files.get(path) != digest for path, digest in dependency["files"].items())):
            raise ServiceProbeError("service dependency is unavailable")
    for role, exact_path in _REQUIRED_FILE_ROLES.items():
        bound_path = Path(str(roles.get(role) or "")).resolve()
        if bound_path != exact_path.resolve() or bound_path not in [Path(str(item)).resolve() for item in files]:
            raise ServiceProbeError("service source binding differs")
        if _digest(bound_path.read_bytes()) != files.get(str(bound_path)):
            raise ServiceProbeError("service source binding differs")
    phoenix_url = observation.get("phoenix_url")
    try: parsed = urlsplit(str(phoenix_url or "")); parsed_port = parsed.port
    except ValueError: raise ServiceProbeError("service observation binding differs") from None
    output_path = Path(str(observation.get("result_path") or "")).resolve()
    record_path = Path(str(observation.get("record_path") or "")).resolve()
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed_port is None or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path != "/"
            or any(item.exists() or not item.parent.is_dir() for item in (output_path, record_path))
            or not all(type(observation.get(name)) is int for name in ("max_events", "max_trace_bytes"))
            or not 100 <= observation["max_events"] <= 1_000_000
            or not 1_048_576 <= observation["max_trace_bytes"] <= 1_073_741_824):
        raise ServiceProbeError("service observation binding differs")
    isolation_ref = observation.get("service_isolation")
    if amendment.get("schema") == "sage-resource-recovery-amendment-v4":
        if not isinstance(isolation_ref, Mapping) or set(isolation_ref) != {"path", "sha256"}:
            raise ServiceProbeError("service isolation binding differs")
        isolation_path = Path(str(isolation_ref.get("path") or "")).resolve()
        isolation_sha = str(isolation_ref.get("sha256") or "")
        _bound_file(isolation_path, isolation_sha, "service isolation configuration")
        isolation_mod = _load_module(_REQUIRED_FILE_ROLES["diagnostic_service_isolation"],
                                     "sage_m3_service_isolation_validation")
        isolation_value = isolation_mod.load_config(isolation_path, isolation_sha)
        if (isolation_value.get("relay_source") != _REQUIRED_FILE_ROLES["diagnostic_loopback_relay"].resolve()
                or isolation_value.get("isolation_source") != _REQUIRED_FILE_ROLES["diagnostic_isolation"].resolve()):
            raise ServiceProbeError("service isolation binding differs")
    else:
        isolation_path = isolation_sha = None
    if live and amendment.get("live_authorized") != "conditional per document":
        raise ServiceProbeError("service probe is not authorized")
    return {"amendment": amendment, "amendment_path": amendment_path,
            "source_binding": source_path, "source_sha256": source_sha,
            "config": config_path, "ledger": ledger_path, "probe_id": probe_id,
            "record_path": record_path, "result_path": output_path,
            "observation": dict(observation), "service_isolation": isolation_path,
            "service_isolation_sha256": isolation_sha}


def require_applied_amendment(reference: Mapping[str, Any], *, config: Path,
                              ledger_path: Path, parent_reference: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(reference, Mapping):
        raise ServiceProbeError("approved recovery amendment is missing")
    path = Path(str(reference.get("path") or "")).resolve(); digest = str(reference.get("sha256") or "")
    value = _load_json(path, digest, "approved recovery amendment")
    expected_service_limits = (_V5_SERVICE_LIMITS if value.get("schema") == "sage-resource-recovery-amendment-v4"
        else _V3_SERVICE_LIMITS if value.get("schema") == "sage-resource-recovery-amendment-v3" else _SERVICE_LIMITS)
    if (not _recovery_shape(value) or value.get("status") != "approved"
            or value.get("amendment_id") != reference.get("id")
            or value.get("base_config_sha256") != _digest(config.read_bytes())
            or value.get("service_probe") != expected_service_limits
            or (value.get("schema") != "sage-resource-recovery-amendment-v4" and
                (value.get("schedule_id") != "m3-order-v1:00001" or value.get("exceptional_retry_ordinal") != 2))
            or value.get("conditional_continuation_authorized") is not True
            or value.get("original_aggregate_limits_unchanged") is not True):
        raise ServiceProbeError("approved recovery amendment differs")
    if parent_reference is not None:
        parent = value.get("parent_authority")
        if (not isinstance(parent, Mapping)
                or Path(str(parent.get("path") or "")).resolve()
                    != Path(str(parent_reference.get("path") or "")).resolve()
                or parent.get("sha256") != parent_reference.get("sha256")):
            raise ServiceProbeError("approved recovery lineage differs")
    if str(RUNTIME) not in sys.path: sys.path.insert(0, str(RUNTIME))
    from ai.resource_budget import ResourceLedger
    status = ResourceLedger(config, ledger_path).status()
    expected = {"amendment_id": value["amendment_id"], "approval_path": str(path),
                "approval_sha256": digest}
    if not status.get("amendments") or expected != status["amendments"][-1]:
        raise ServiceProbeError("approved recovery amendment is not applied")
    return value


class _ServiceHandler:
    def __init__(self) -> None:
        self.tracee_pid: int | None = None
        self.control_requests: set[str] = set()
        self.control_responses: set[str] = set()
        self.control_processes: set[int] = set()
        self.control_failures = 0
        self.model_requests = 0
        self.diagnostics: list[dict[str, Any]] = []

    def __call__(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
        kind = event.get("kind"); payload = event.get("payload")
        safe: dict[str, Any] = {"kind": kind}
        if not isinstance(payload, Mapping): return safe
        if kind == "tool_event":
            name = payload.get("event"); safe["event"] = name
            if name == "observed_process_streams" and type(payload.get("tracee_pid")) is int \
                    and payload["tracee_pid"] > 0 and isinstance(payload.get("streams"),list):
                safe["tracee_pid"] = payload["tracee_pid"]
                if self.tracee_pid is None:self.tracee_pid = payload["tracee_pid"]
            if name == "sage_chat_registration_diagnostic":
                safe.update({key: payload[key] for key in ("status", "attempt_count", "error_category") if key in payload})
                self.diagnostics.append(dict(safe))
            if name == "phoenix_process_startup_diagnostic":
                safe["status"] = "failure"
                category = payload.get("error_category")
                safe["error_category"] = category if category in {
                    "argument_parser_rejected", "child_exited", "inactive_process", "unsupported_command_shape",
                    "preexisting_pid_entry", "dependency_import_error", "database_startup_error", "startup_error"
                } else "unclassified_startup_failure"
                if type(payload.get("returncode")) is int: safe["returncode"] = payload["returncode"]
                if re.fullmatch(r"[a-f0-9]{64}", str(payload.get("output_sha256", ""))):
                    safe["output_sha256"] = payload["output_sha256"]
                if str(RUNTIME) not in sys.path: sys.path.insert(0, str(RUNTIME))
                from ai.phoenix_process_compat import _EXCEPTION_CLASSES
                exception_class = payload.get("exception_class")
                safe["exception_class"] = exception_class if exception_class in _EXCEPTION_CLASSES else "unknown"
                safe["frames"] = []
                for frame in payload.get("frames", []) if isinstance(payload.get("frames"), list) else []:
                    if (isinstance(frame, dict) and set(frame) == {"relative_path", "line", "source_sha256"}
                            and isinstance(frame["relative_path"], str) and not Path(frame["relative_path"]).is_absolute()
                            and ".." not in Path(frame["relative_path"]).parts
                            and frame["relative_path"].endswith(".py") and type(frame["line"]) is int
                            and re.fullmatch(r"[a-f0-9]{64}", str(frame["source_sha256"]))):
                        safe["frames"].append(frame)
                safe["frames"] = safe["frames"][-4:]
                self.diagnostics.append(dict(safe))
        elif kind == "control_request" and isinstance(payload.get("request_id"), str):
            self.control_requests.add(payload["request_id"])
            if type(event.get("pid")) is int: self.control_processes.add(event["pid"])
            safe.update({key: payload[key] for key in ("request_id", "transport", "method", "rpc_method",
                "routing_key", "endpoint") if key in payload})
        elif kind == "control_response" and isinstance(payload.get("request_id"), str):
            self.control_responses.add(payload["request_id"])
            if not isinstance(payload.get("reservation_id"), str) or payload.get("outcome") not in {"returned", "error"}:
                self.control_failures += 1
            safe.update(request_id=payload["request_id"], outcome=payload.get("outcome"),
                        reservation_id=payload.get("reservation_id"))
        elif kind in {"model_request", "model_response"}:
            self.model_requests += kind == "model_request"
        elif kind == "control_request" and payload.get("audit_event") in {
            "socket.connect", "socket.bind", "socket.getaddrinfo"
        }:
            safe.update(audit_event=payload["audit_event"], observation_class="ATTEMPT",
                        transport_class="python_socket_audit", application_request=False)
        return safe


class _Pump:
    def __init__(self, receiver: Any, handler: _ServiceHandler) -> None:
        self.receiver = receiver; self.handler = handler; self.stop = threading.Event()
        self.failure: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.is_set():
            try: self.receiver.serve_one(.2)
            except Exception as exc:
                if type(exc).__name__ == "ObservationReceiverTimeout": continue
                self.failure = exc; return

    def start(self) -> None: self.thread.start()
    def finish(self, timeout: float = 6) -> None:
        self.stop.set(); self.thread.join(max(0, timeout))
        if self.thread.is_alive() or self.failure is not None:
            raise ServiceProbeError("service observation receiver failed")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None: raise ServiceProbeError("service dependency is unavailable")
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module); return module


def _listener_owners(port: int) -> set[int]:
    inodes: set[str] = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try: lines = table.read_text().splitlines()[1:]
        except OSError: continue
        for line in lines:
            fields = line.split()
            if len(fields) >= 10 and fields[3] == "0A" and int(fields[1].split(":")[-1], 16) == port:
                inodes.add(fields[9])
    owners: set[int] = set()
    if not inodes: return owners
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit(): continue
        try:
            for descriptor in (directory / "fd").iterdir():
                target = os.readlink(descriptor)
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    owners.add(int(directory.name)); break
        except OSError: continue
    return owners


def _local_sage_pids() -> set[int]:
    found = set()
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit(): continue
        try:
            command = (directory / "cmdline").read_bytes().split(b"\0")
            cwd = (directory / "cwd").resolve()
        except OSError: continue
        if cwd == RUNTIME and any(Path(os.fsdecode(item)).name == "main.py" for item in command if item):
            found.add(int(directory.name))
    return found


def _is_descendant(pid: int, ancestor: int) -> bool:
    seen = set()
    while pid > 1 and pid not in seen:
        if pid == ancestor: return True
        seen.add(pid)
        try: fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError: return False
        pid = int(fields[1])
    return False


def _startup_identity() -> dict[str, Any]:
    path = Path("/tmp/sage_startup_identity.json")
    try: return json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ServiceProbeError("local Sage startup identity is unavailable") from None


def _observer_config(frozen: FrozenServiceProbe, validated: Mapping[str, Any], directory: Path,
                     work: Path) -> tuple[Path, str, Path]:
    observer = _REQUIRED_FILE_ROLES["process_observer"]; channel = _REQUIRED_FILE_ROLES["observation_channel"]
    entry = _REQUIRED_FILE_ROLES["sage_main"]; strace = Path(shutil.which("strace") or "").resolve()
    py = ROOT / ".venv/bin/python"; receipt = work / "service-process-observer-receipt.json"
    value = {"schema": "sage-process-observer-exec-config-v1", "observer_path": str(observer),
        "observer_sha256": _digest(observer.read_bytes()), "entry_path": str(entry),
        "entry_sha256": _digest(entry.read_bytes()), "source_binding_path": str(validated["source_binding"]),
        "source_binding_sha256": validated["source_sha256"], "observation_channel_path": str(channel),
        "observation_channel_sha256": _digest(channel.read_bytes()), "strace_path": str(strace),
        "strace_sha256": _digest(strace.read_bytes()), "python": str(py), "cwd": str(RUNTIME),
        "argv": [str(py), "-u", "main.py"], "limits": {"timeout_seconds": 1200,
            "max_events": frozen.value["observation"]["max_events"],
            # Sage startup emits bounded binary write/sendto arguments near one MiB.
            "max_line_bytes": 1_048_576,
            "max_trace_bytes": frozen.value["observation"]["max_trace_bytes"]},
        "observation_directory": str(directory), "receipt_path": str(receipt)}
    path = work / "service-process-observer-config.json"; raw = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(raw); os.chmod(path, 0o600)
    return path, _digest(raw), receipt


async def _chat_registration_after(started: datetime, timeout: float = 30) -> dict[str, Any]:
    if str(RUNTIME) not in sys.path: sys.path.insert(0, str(RUNTIME))
    from ai import resource_clients
    from ai.hillclimb import live_seams
    from mythic import mythic
    resource_clients.install(); bootstrap = _load_module(
        _REQUIRED_FILE_ROLES["chat_registration"], "sage_m3_service_chat_registration")
    started_utc = _parse_utc_timestamp(started)
    if started_utc is None: raise ServiceProbeError("service start timestamp is invalid")
    client = await live_seams._mythic_login_async_safe(); deadline = time.monotonic() + timeout
    while True:
        value = await mythic.execute_custom_query(client, bootstrap.CHAT_CONTAINER_QUERY)
        rows = value.get("consuming_container", [])
        for row in rows if isinstance(rows, list) else []:
            updated = _fresh_chat_timestamp(row, started_utc)
            if updated is not None:
                return {"id": row.get("id"), "updated_at": updated.astimezone(timezone.utc).isoformat(),
                        "container_running": True}
        if time.monotonic() >= deadline: raise ServiceProbeError("fresh native Chat registration is unavailable")
        await asyncio.sleep(1)


def _parse_utc_timestamp(value: Any) -> datetime | None:
    try: parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError: return None
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fresh_chat_timestamp(row: Mapping[str, Any], started: datetime) -> datetime | None:
    start_utc = _parse_utc_timestamp(started); updated = _parse_utc_timestamp(row.get("updated_at"))
    if (start_utc is None or updated is None or row.get("name") != "sage" or row.get("type") != "chat"
            or row.get("container_running") is not True or row.get("deleted") is not False
            or updated < start_utc):
        return None
    return updated


def _remaining(deadline_epoch: Any, cap: float, message: str) -> float:
    if not isinstance(deadline_epoch, (int, float)) or isinstance(deadline_epoch, bool):
        raise ServiceProbeError("service ledger deadline is invalid")
    remaining = float(deadline_epoch) - time.time()
    if remaining <= 0: raise ServiceProbeError(message)
    return min(cap, remaining)


def _pid_alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
    except OSError:
        return False


async def _registration_with_producer(started: datetime, timeout: float, pid: int,
                                      handler: _ServiceHandler, active_deadline: float) -> dict[str, Any]:
    task = asyncio.create_task(_chat_registration_after(started, timeout=timeout))
    deadline = time.monotonic() + timeout
    try:
        while True:
            if any(row.get("event") == "phoenix_process_startup_diagnostic" for row in handler.diagnostics):
                raise ServiceProbeError("Phoenix startup failed")
            if not _pid_alive(pid):
                raise ServiceProbeError("owned Sage exited before service readiness")
            if task.done():
                return task.result()
            if time.time() >= active_deadline:
                raise ServiceProbeError("service active deadline exhausted")
            if time.monotonic() >= deadline:
                raise ServiceProbeError("native Chat registration timed out")
            await asyncio.wait({task}, timeout=min(.1, max(0, deadline - time.monotonic())))
    finally:
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _phoenix_liveness(url: str, timeout: float = 5) -> dict[str, Any]:
    if str(RUNTIME) not in sys.path: sys.path.insert(0, str(RUNTIME))
    from ai import resource_budget, resource_http
    ledger = resource_budget.configured_ledger()
    if ledger is None: raise ServiceProbeError("service application request evidence is incomplete")
    transport = resource_http.MeteredHTTPTransport("control", ledger=ledger,
        transport=resource_http.httpx.HTTPTransport(trust_env=False), operation="service-phoenix-readiness")
    with resource_http.httpx.Client(
        transport=transport, timeout=timeout, follow_redirects=True, trust_env=False,
    ) as client:
        response = client.get(url)
    return {"status_code": response.status_code, "ready": 200 <= response.status_code < 400}


def _cleanup(env: Mapping[str, str], total_deadline_epoch: float) -> dict[str, Any]:
    cleanup_env = dict(env); cleanup_env["SAGE_RESOURCE_BUDGET_PHASE"] = "cleanup"
    steps = []
    stop_ok = None
    for name, argv, cap in (
        ("stop_sage", ["/bin/bash", "skills/sage-goad-reset/scripts/sage_stop.sh"], 120),
        ("archive_runtime", [str(ROOT / ".venv/bin/python"),
            "skills/sage-goad-reset/scripts/archive_runtime_dbs.py"], 180),
    ):
        if name == "archive_runtime" and stop_ok is not True:
            steps.append({"name": name, "ok": False, "error_type": "blocked_after_stop_failure"})
            continue
        remaining = float(total_deadline_epoch) - time.time()
        if remaining <= 0:
            steps.append({"name": name, "ok": False, "error_type": "service_deadline_exhausted"})
            continue
        try:
            completed = subprocess.run(argv, cwd=ROOT, env=cleanup_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=min(cap, remaining))
            steps.append({"name": name, "ok": completed.returncode == 0,
                "output_sha256": _digest(completed.stdout), "error_sha256": _digest(completed.stderr)})
        except Exception as exc: steps.append({"name": name, "ok": False,
            "error_type": "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "unexpected_failure"})
        if name == "stop_sage":
            wait_until=min(total_deadline_epoch,time.time()+3)
            while (_local_sage_pids() or _listener_owners(6006) or _listener_owners(4317)) and time.time()<wait_until:
                time.sleep(.05)
            writers_dead=not _local_sage_pids() and not _listener_owners(6006) and not _listener_owners(4317)
            steps[-1]["writers_dead"]=writers_dead;steps[-1]["ok"]=steps[-1]["ok"] and writers_dead;stop_ok=steps[-1]["ok"]
    return {"complete": all(row["ok"] for row in steps), "steps": steps}


def _read_process_receipt(path: Path | None, timeout: float = 10) -> Mapping[str, Any] | None:
    if path is None: return None
    deadline = time.monotonic() + timeout
    while True:
        try: value = json.loads(path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError): value = None
        if isinstance(value, Mapping): return value
        if time.monotonic() >= deadline: return None
        time.sleep(.05)


def _safe_exception(error: BaseException) -> tuple[str, str]:
    if isinstance(error, ServiceProbeError) and str(error) in _SAFE_EXECUTION_ERROR_CODES:
        return "ServiceProbeError", str(error)
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired, asyncio.TimeoutError)):
        return "ServiceOperationTimeout", "service operation timed out"
    if type(error).__name__ in {"ResourceBudgetError", "ResourceBudgetExceeded"}:
        return "ResourceBudgetError", "resource budget failure"
    return "UnexpectedError", "unexpected service failure"


def _restart_failure_diagnostic(completed: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
    stdout = completed.stdout if isinstance(completed.stdout, bytes) else b""
    stderr = completed.stderr if isinstance(completed.stderr, bytes) else b""
    output = stdout + b"\0" + stderr
    category = "sage_restart_failed"
    if b"venv python" in output and b"not executable" in output:
        category = "runtime_interpreter_missing"
    for signature, reason in (
        (b"relaunch did not produce a Sage/observer child", "launch_child_not_detected"),
        (b"relaunch exited before the positive-start window", "launch_child_exited"),
        (b"could not create tmux session", "tmux_session_creation_failed"),
        (b"cannot import mythic_container", "runtime_import_failed"),
        (b"SAGE_BLOODHOUND_MCP_DIR is not set", "external_dependency_configuration_missing"),
        (b"RABBITMQ_PASSWORD not set", "runtime_secret_missing"),
        (b"Permission denied", "permission_denied"),
        (b"Read-only file system", "filesystem_read_only"),
        (b"No such file or directory", "file_not_found"),
        (b"command not found", "command_unavailable"),
        (b"unbound variable", "shell_unbound_variable"),
    ):
        if category == "sage_restart_failed" and signature in output:
            category = reason
            break
    return {"status": "failure", "error_category": category,
        "returncode": completed.returncode, "output_sha256": _digest(output),
        "stdout_bytes": len(stdout), "stderr_bytes": len(stderr), "output_complete": True}


def _execute_c_isolated(frozen: FrozenServiceProbe, validated: Mapping[str, Any], ledger: Any,
                        diagnostic_stage: Mapping[str, Any]) -> dict[str, Any]:
    """Reinvoke the full C driver in one contained namespace after its sole reservation."""
    isolation_mod = _load_module(_REQUIRED_FILE_ROLES["diagnostic_service_isolation"],
                                 "sage_m3_diagnostic_service_isolation")
    status = ledger.status(); probe = status.get("service_probes", {}).get(validated["probe_id"], {})
    stages = probe.get("stages", []) if isinstance(probe, Mapping) else []
    if (status.get("outstanding_reservations") or status.get("unknown_provider_usage")
            or status.get("provider_overrun") or not stages or stages[-1] != diagnostic_stage
            or diagnostic_stage.get("stage") != "C" or diagnostic_stage.get("closed_epoch") is not None):
        raise ServiceProbeError("diagnostic C stage is unavailable")
    require_applied_amendment(frozen.value["amendment"], config=validated["config"],
                              ledger_path=validated["ledger"])
    context = isolation_mod.load_config(validated["service_isolation"],
                                        validated["service_isolation_sha256"])["context_directory"]
    isolation_config = isolation_mod.load_config(validated["service_isolation"],
                                                 validated["service_isolation_sha256"])
    if str(validated["ledger"].parent.resolve()) not in isolation_config["writable_paths"]:
        raise ServiceProbeError("service isolation binding differs")
    remaining_active = max(0, float(diagnostic_stage["active_deadline_epoch"]) - time.time())
    remaining_total = max(0, float(diagnostic_stage["total_deadline_epoch"]) - time.time())
    if (isolation_config["active_deadline_monotonic"] > time.monotonic() + remaining_active
            or isolation_config["cleanup_deadline_monotonic"] > time.monotonic() + remaining_total):
        raise ServiceProbeError("service ledger deadline is invalid")
    stage_path = context / f"c-stage-{diagnostic_stage['attempt']}.json"
    wrapper_receipt = context / f"c-wrapper-{diagnostic_stage['attempt']}.json"
    if stage_path.exists() or wrapper_receipt.exists():
        raise ServiceProbeError("service isolation binding differs")
    stage_path.write_text(json.dumps(dict(diagnostic_stage), sort_keys=True, separators=(",", ":")))
    os.chmod(stage_path, 0o600); stage_sha = _digest(stage_path.read_bytes())
    environment = dict(os.environ)
    environment.update({"SAGE_M3_C_INNER_STAGE_PATH": str(stage_path),
                        "SAGE_M3_C_INNER_STAGE_SHA256": stage_sha})
    def dynamic_deadline():
        latest = ledger.status().get("service_probes", {}).get(validated["probe_id"], {}).get("stages", [])
        latest = latest[-1] if latest else None
        if isinstance(latest, Mapping) and latest.get("active_ended_epoch") is not None:
            return time.monotonic() + max(0, float(latest["total_deadline_epoch"]) - time.time())
        return None
    command = [str(ROOT / ".venv/bin/python"), str(_REQUIRED_FILE_ROLES["capture_driver"]),
        "--service-readiness", str(frozen.path), "--service-readiness-sha256", frozen.sha256, "--go"]
    wrapper_error = None
    try:
        containment = isolation_mod.execute(validated["service_isolation"],
            validated["service_isolation_sha256"], command, environment, wrapper_receipt,
            deadline_provider=dynamic_deadline)
    except BaseException:
        wrapper_error = "ServiceIsolationError"
        try: containment = json.loads(wrapper_receipt.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError): containment = None
    inner_path = inner_result_path(validated["result_path"])
    try:
        inner_raw = inner_path.read_bytes(); inner = json.loads(inner_raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        inner_raw = b""; inner = None
    namespace = containment.get("namespace") if isinstance(containment, Mapping) else None
    try:
        latest_stages = ledger.status().get("service_probes", {}).get(
            validated["probe_id"], {}).get("stages", [])
        ended_stage = latest_stages[-1] if latest_stages else None
    except BaseException:
        ended_stage = None
    mutable_stage_fields = {"active_ended_epoch", "total_deadline_epoch", "closed_epoch"}
    canonical_active_end = bool(
        isinstance(ended_stage, Mapping)
        and all(ended_stage.get(key) == value for key, value in diagnostic_stage.items()
                if key not in mutable_stage_fields)
        and ended_stage.get("stage") == "C"
        and isinstance(ended_stage.get("active_ended_epoch"), (int, float))
        and not isinstance(ended_stage.get("active_ended_epoch"), bool)
        and isinstance(ended_stage.get("total_deadline_epoch"), (int, float))
        and not isinstance(ended_stage.get("total_deadline_epoch"), bool)
        and ended_stage["active_ended_epoch"] <= ended_stage["total_deadline_epoch"])
    contained = (wrapper_error is None and isinstance(containment, Mapping)
        and containment.get("failure_type") is None and containment.get("returncode") == 0
        and containment.get("owned_processes_dead") is True
        and canonical_active_end
        and containment.get("relay_receipt", {}).get("listener_ready") is False
        and isinstance(namespace, Mapping)
        and namespace.get("listener_started_receipt", {}).get("listener_ready") is True
        and namespace.get("listener_final_receipt", {}).get("listener_ready") is False)
    if not isinstance(inner, Mapping):
        inner = {"schema": RESULT_SCHEMA, "probe_id": validated["probe_id"], "service_ready": False,
            "error_type": wrapper_error or "ServiceIsolationError", "error_code": "service isolation failed",
            "cleanup": {"complete": False, "steps": []}, "model_requests": 0}
    result = dict(inner)
    result["service_ready"] = bool(inner.get("service_ready") is True and contained)
    if not contained:
        result["error_type"] = result.get("error_type") or "ServiceIsolationError"
        result["error_code"] = result.get("error_code") or "service isolation failed"
        if not canonical_active_end:
            result["error_type"] = "ResourceBudgetError"
            result["error_code"] = "diagnostic C active end missing"
    result["inner_result_retained"] = bool(inner_raw)
    result["diagnostic_stage_active_ended"] = canonical_active_end
    result["inner_result_sha256"] = _digest(inner_raw) if inner_raw else None
    result["inner_result"] = ({"path": str(inner_path), "sha256": _digest(inner_raw)} if inner_raw else None)
    result["isolation_receipt"] = ({"path": str(wrapper_receipt), "sha256": _digest(wrapper_receipt.read_bytes())}
        if wrapper_receipt.exists() else None)
    inner_cleanup = inner.get("cleanup") if isinstance(inner.get("cleanup"), Mapping) else {}
    result["cleanup"] = {"complete": bool(inner_cleanup.get("complete") is True and contained),
        "steps": list(inner_cleanup.get("steps", [])), "isolation_receipt": result["isolation_receipt"]}
    return result


def execute(frozen: FrozenServiceProbe, validated: Mapping[str, Any], ledger: Any,
            *, diagnostic_stage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if diagnostic_stage is not None and diagnostic_stage.get("stage") == "C" \
            and os.environ.get("SAGE_M3_C_NAMESPACE_INNER") != "1":
        return _execute_c_isolated(frozen, validated, ledger, diagnostic_stage)
    observation_mod = _load_module(ROOT / "skills/sage-eval-gauge/scripts/observation_receiver.py",
                                   "sage_m3_service_observation_receiver")
    old_env = os.environ.copy(); work = Path(tempfile.mkdtemp(prefix="sage-m3-service-")); os.chmod(work, 0o700)
    handler = _ServiceHandler(); pump = None; process_receipt = None; startup = chat = phoenix = None
    receiver_context = None; receiver_open = False
    receipt_path = None; observer_config_sha = None; observer_entry_sha = None
    cleanup = {"complete": False, "steps": []}; error = error_code = None
    probe_started = cleanup_attempted = launch_attempted = False
    active_deadline = total_deadline = None
    active_ended = False
    initial_controls = 0
    continuation = validated["amendment"].get("schema") == "sage-resource-recovery-amendment-v4"
    namespace_inner = os.environ.get("SAGE_M3_C_NAMESPACE_INNER") == "1"
    restart_pins = []
    if namespace_inner:
        isolation_mod = _load_module(_REQUIRED_FILE_ROLES["diagnostic_service_isolation"],
                                     "sage_m3_inner_service_isolation")
        restart_pins = isolation_mod.pinned_restart_arguments(
            validated["service_isolation"], validated["service_isolation_sha256"])
    def end_active_phase() -> bool:
        nonlocal active_ended, total_deadline
        if not continuation or active_ended:
            return True
        latest = ledger.end_diagnostic_active(validated["probe_id"])
        active_ended = True; total_deadline = latest.get("total_deadline_epoch")
        return isinstance(total_deadline, (int, float)) and not isinstance(total_deadline, bool)
    try:
        status = ledger.status()
        if diagnostic_stage is not None:
            previous = status.get("service_probes", {}).get(validated["probe_id"], {})
            initial_controls = previous.get("active_control_requests", 0) + previous.get("cleanup_control_requests", 0)
            stages = previous.get("stages", []) if isinstance(previous, Mapping) else []
            if (continuation and stages and diagnostic_stage == stages[-1]
                    and diagnostic_stage.get("stage") == "C"
                    and diagnostic_stage.get("closed_epoch") is None):
                # Outer main already reserved this exact C stage. Own its active-phase
                # accounting before any inner preflight that can return a failure.
                probe_started = True
                active_deadline = diagnostic_stage.get("active_deadline_epoch")
                total_deadline = diagnostic_stage.get("total_deadline_epoch")
        if status.get("outstanding_reservations") or status.get("unknown_provider_usage") \
                or status.get("provider_overrun"):
            raise ServiceProbeError("canonical ledger cannot start service probe")
        receiver_context=observation_mod.ObservationReceiver(validated["record_path"], handler, temporary_parent=work)
        receiver=receiver_context.__enter__();receiver_open=True
        for _receiver_lifetime in (None,):
            config_path, config_sha, receipt_path = _observer_config(frozen, validated, receiver.directory, work)
            observer_config_sha = config_sha
            observer_entry_sha = _digest(_REQUIRED_FILE_ROLES["sage_main"].read_bytes())
            env = dict(old_env); env.update({"SAGE_RESOURCE_BUDGET_CONFIG": str(validated["config"]),
                "SAGE_RESOURCE_BUDGET_LEDGER": str(validated["ledger"]),
                "SAGE_RESOURCE_SERVICE_PROBE_ID": validated["probe_id"],
                "SAGE_RESOURCE_BUDGET_PHASE": "active", "SAGE_OBSERVATION_DIRECTORY": str(receiver.directory),
                "SAGE_PROCESS_OBSERVER_CONFIG": str(config_path),
                "SAGE_PROCESS_OBSERVER_CONFIG_SHA256": config_sha,
                "SAGE_FINDINGS_SLACK_WEBHOOK_URL": "", "SAGE_FINDINGS_SLACK_CHANNEL_ID": ""})
            port = urlsplit(frozen.value["observation"]["phoenix_url"]).port
            readiness_ports = (6006, 4317) if namespace_inner else (int(port),)
            if _local_sage_pids() or any(_listener_owners(item) for item in readiness_ports):
                raise ServiceProbeError("Sage or Phoenix predates service probe")
            os.environ.update(env); os.environ.pop("SAGE_RESOURCE_EPISODE_ID", None)
            pump = _Pump(receiver, handler); pump.start()
            require_applied_amendment(frozen.value["amendment"], config=validated["config"],
                                      ledger_path=validated["ledger"])
            if validated["amendment"].get("schema") in {"sage-resource-recovery-amendment-v3", "sage-resource-recovery-amendment-v4"}:
                stages = ledger.status()["service_probes"].get(validated["probe_id"], {}).get("stages", [])
                if (not stages or diagnostic_stage != stages[-1] or diagnostic_stage.get("stage") != "C"
                        or diagnostic_stage.get("closed_epoch") is not None):
                    raise ServiceProbeError("diagnostic C stage is unavailable")
                timing = dict(diagnostic_stage)
            else:
                if diagnostic_stage is not None: raise ServiceProbeError("unexpected diagnostic stage")
                timing = ledger.start_service_probe(validated["probe_id"], validated["amendment"]["amendment_id"])
            probe_started = True; active_deadline = timing.get("active_deadline_epoch")
            total_deadline = timing.get("total_deadline_epoch")
            started_epoch = timing.get("started_epoch")
            stage_limits = validated["amendment"].get("diagnostic_session", {}) if diagnostic_stage else {}
            active_limit = stage_limits.get("stage_active_seconds", _SERVICE_LIMITS["active_seconds"])
            cleanup_limit = (stage_limits.get("stage_cleanup_seconds", 60) if diagnostic_stage
                else _SERVICE_LIMITS["total_seconds"] - _SERVICE_LIMITS["active_seconds"])
            if (not all(isinstance(item, (int, float)) and not isinstance(item, bool)
                               for item in (started_epoch, active_deadline, total_deadline))
                    or active_deadline > started_epoch + active_limit
                    or total_deadline > active_deadline + cleanup_limit):
                raise ServiceProbeError("service ledger deadline is invalid")
            restart_timeout = _remaining(active_deadline, active_limit if diagnostic_stage else 30,
                                         "service active deadline exhausted")
            start_time = datetime.now(timezone.utc)
            command = ["/bin/bash", "skills/sage-goad-reset/scripts/sage_restart.sh",
                *restart_pins,
                *[f"{key}={env[key]}" for key in ("SAGE_RESOURCE_BUDGET_CONFIG", "SAGE_RESOURCE_BUDGET_LEDGER",
                    "SAGE_RESOURCE_SERVICE_PROBE_ID", "SAGE_RESOURCE_BUDGET_PHASE", "SAGE_OBSERVATION_DIRECTORY",
                    "SAGE_PROCESS_OBSERVER_CONFIG", "SAGE_PROCESS_OBSERVER_CONFIG_SHA256",
                    "SAGE_FINDINGS_SLACK_WEBHOOK_URL", "SAGE_FINDINGS_SLACK_CHANNEL_ID")],
                "SAGE_RESOURCE_EPISODE_ID="]
            launch_attempted = True
            started = subprocess.run(command, cwd=ROOT, env=old_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=restart_timeout)
            if started.returncode != 0:
                handler.diagnostics.append(_restart_failure_diagnostic(started))
                raise ServiceProbeError("one-start Sage relaunch failed")
            deadline = time.monotonic() + _remaining(active_deadline, 8, "service active deadline exhausted")
            while handler.tracee_pid is None and time.monotonic() < deadline: time.sleep(.05)
            startup = _startup_identity()
            try: startup_time = datetime.fromisoformat(str(startup.get("recorded_at")).replace("Z", "+00:00"))
            except ValueError: startup_time = datetime.min.replace(tzinfo=timezone.utc)
            if (handler.tracee_pid is None or startup.get("pid") != handler.tracee_pid
                    or startup_time < start_time
                    or startup.get("cwd") != str(RUNTIME) or not Path(f"/proc/{handler.tracee_pid}").exists()):
                raise ServiceProbeError("observer and local startup identity differ")
            registration_timeout = _remaining(active_deadline, active_limit,
                                              "service active deadline exhausted")
            chat = asyncio.run(_registration_with_producer(start_time, registration_timeout,
                handler.tracee_pid, handler, float(active_deadline)))
            _remaining(active_deadline, 5, "service active deadline exhausted")
            listener_rows = {item: _listener_owners(item) for item in readiness_ports}
            if (any(not owners for owners in listener_rows.values())
                    or any(not all(_is_descendant(pid, handler.tracee_pid) for pid in owners)
                           for owners in listener_rows.values())):
                raise ServiceProbeError("Phoenix listener is not owned by launched Sage")
            phoenix = _phoenix_liveness(frozen.value["observation"]["phoenix_url"],
                timeout=_remaining(active_deadline, 5, "service active deadline exhausted"))
            phoenix["listener_owners"] = {str(item): sorted(owners) for item, owners in listener_rows.items()}
            if not phoenix["ready"]: raise ServiceProbeError("Phoenix service is not ready")
            if (handler.model_requests or handler.tracee_pid not in handler.control_processes
                    or handler.control_failures):
                raise ServiceProbeError("service application request evidence is incomplete")
            if not end_active_phase(): raise ServiceProbeError("service ledger deadline is invalid")
            cleanup_attempted = True; cleanup = _cleanup(env, total_deadline)
            process_receipt = _read_process_receipt(receipt_path,
                max(0, min(10, float(total_deadline) - time.time())))
            pump.finish(max(0, min(6, float(total_deadline) - time.time())))
            failures = set(process_receipt.get("failures", [])) if isinstance(process_receipt, Mapping) else _CRITICAL_OBSERVER_FAILURES
            scoped = ledger.status().get("service_probes", {}).get(validated["probe_id"])
            if (not cleanup["complete"] or not isinstance(process_receipt, Mapping)
                    or process_receipt.get("schema") != "sage-process-observer-preexec-receipt-v1"
                    or process_receipt.get("config_sha256") != observer_config_sha
                    or process_receipt.get("entry_sha256") != observer_entry_sha
                    or process_receipt.get("source_binding_sha256") != validated["source_sha256"]
                    or process_receipt.get("tracer_attested") is not True
                    or process_receipt.get("tracee_pid") != handler.tracee_pid
                    or failures & _CRITICAL_OBSERVER_FAILURES
                    or handler.model_requests or handler.control_requests != handler.control_responses
                    or handler.control_failures or not isinstance(scoped, Mapping)
                    or scoped.get("active_control_requests", 0) + scoped.get("cleanup_control_requests", 0)
                        != initial_controls + len(handler.control_requests)):
                raise ServiceProbeError("service cleanup or process evidence is incomplete")
    except BaseException as exc:
        error, error_code = _safe_exception(exc)
        if probe_started and launch_attempted and pump is not None and not cleanup_attempted:
            try: end_active_phase()
            except BaseException: pass
            if isinstance(total_deadline, (int, float)):
                cleanup_attempted = True; cleanup = _cleanup({**old_env,
                    "SAGE_RESOURCE_BUDGET_CONFIG": str(validated["config"]),
                    "SAGE_RESOURCE_BUDGET_LEDGER": str(validated["ledger"]),
                    "SAGE_RESOURCE_SERVICE_PROBE_ID": validated["probe_id"]}, float(total_deadline))
                process_receipt = _read_process_receipt(receipt_path,
                    max(0, min(10, float(total_deadline) - time.time())))
        if pump is not None:
            try: pump.finish(max(0, min(6, float(total_deadline) - time.time())) if total_deadline else 0)
            except BaseException: pass
    finally:
        if continuation and probe_started and not active_ended:
            try: end_active_phase()
            except BaseException:
                error = error or "ResourceBudgetError"; error_code = error_code or "resource budget failure"
        os.environ.clear(); os.environ.update(old_env)
        if probe_started and diagnostic_stage is None:
            try: ledger.close_service_probe(validated["probe_id"])
            except BaseException:
                error = error or "ServiceProbeCloseError"; error_code = error_code or "service probe close failed"
        if receiver_open:
            try: receiver_context.__exit__(None,None,None)
            except BaseException:
                error = error or "ServiceProbeError";error_code = error_code or "service observation receiver failed"
        shutil.rmtree(work, ignore_errors=True)
    state = ledger.status().get("service_probes", {}).get(validated["probe_id"])
    event_record_sha = (_digest(validated["record_path"].read_bytes())
                        if validated["record_path"].is_file() else None)
    complete = (error is None and cleanup["complete"] and handler.model_requests == 0
                and (not continuation or active_ended)
                and isinstance(process_receipt, Mapping) and isinstance(state, Mapping)
                and (diagnostic_stage is not None or state.get("closed_epoch") is not None) and event_record_sha is not None)
    return {"schema": RESULT_SCHEMA, "probe_id": validated["probe_id"], "service_ready": complete,
        "error_type": error, "error_code": error_code, "startup_identity": startup,
        "chat_registration": chat, "phoenix": phoenix,
        "process_observer_receipt": process_receipt, "application_controls": len(handler.control_requests),
        "model_requests": handler.model_requests, "event_record_sha256": event_record_sha,
        "cleanup": cleanup, "resource_scope": state, "startup_diagnostics": handler.diagnostics}


def retain(path: Path, result: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(result), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                         allow_nan=False).encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try: os.fchmod(descriptor, 0o600); os.write(descriptor, encoded); os.fsync(descriptor)
    finally: os.close(descriptor)
    return _digest(encoded)


def inner_result_path(path: Path) -> Path:
    return path.with_suffix(".inner.json")
