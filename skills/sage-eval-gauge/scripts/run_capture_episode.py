#!/usr/bin/env python3
"""Execute one source-bound development capture episode through maintained production seams."""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import stat
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "Payload_Type" / "sage"
SCHEMA = "sage-m3-capture-episode-run-v1"
SOURCE_BINDING_SCHEMA = "sage-m3-capture-execution-binding-v1"
_CAPTURE_FILE_ROLES = frozenset({
    "capture_driver", "gauge_orchestrator", "native_gauge", "capture_compiler",
    "capture_risk_inventory", "process_observer", "observation_channel",
    "capture_event_buffer", "native_inventory", "native_action_extractor",
    "resource_budget", "reset_controller", "payload_bootstrap", "capture_service_probe",
    "capture_composite_bridge", "capture_live_facts", "capture_ledger_source",
    "capture_os_network", "capture_os_writes", "capture_sink_sources",
    "capture_tradecraft_facts", "engagement_ledger", "live_native_selection",
})
_C_INNER_STAGE_PATH_ENV="SAGE_M3_C_INNER_STAGE_PATH"
_C_INNER_STAGE_SHA_ENV="SAGE_M3_C_INNER_STAGE_SHA256"


class CaptureEpisodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrozenCaptureSpec:
    path: Path
    sha256: str
    value: dict[str, Any]


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bound_file(value: Any, digest: Any, name: str) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        path = ROOT / path
    try:
        raw = path.read_bytes()
    except OSError:
        raise CaptureEpisodeError(f"{name} is unavailable") from None
    if not path.is_file() or len(str(digest or "")) != 64 or _digest(raw) != digest:
        raise CaptureEpisodeError(f"{name} binding differs")
    return path.resolve()


def _resolved(value: Any) -> Path:
    path = Path(str(value or ""))
    return (ROOT / path if not path.is_absolute() else path).resolve()


def load_frozen_spec(path: str | Path, expected_sha256: str) -> FrozenCaptureSpec:
    source = Path(path)
    if not source.is_absolute():
        source = ROOT / source
    raw = source.read_bytes()
    if _digest(raw) != expected_sha256:
        raise CaptureEpisodeError("capture specification differs")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise CaptureEpisodeError("capture specification is malformed") from None
    return FrozenCaptureSpec(source.resolve(), expected_sha256, value)


def _module(name: str):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("sage_m3_capture_" + name, path)
    if spec is None or spec.loader is None:
        raise CaptureEpisodeError("capture dependency is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _validate(spec: FrozenCaptureSpec, *, live: bool) -> dict[str, Any]:
    try:
        raw = spec.path.read_bytes(); reread = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CaptureEpisodeError("capture specification is unavailable") from None
    if _digest(raw) != spec.sha256 or reread != spec.value:
        raise CaptureEpisodeError("capture specification changed in memory or on disk")
    value = reread
    required = {"schema", "authority", "source_binding", "identity", "range", "model",
                "resources", "observation"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema") != SCHEMA:
        raise CaptureEpisodeError("capture specification fields differ")
    for section in required - {"schema"}:
        if not isinstance(value[section], dict):
            raise CaptureEpisodeError("capture specification fields differ")
    authority = value["authority"]
    approved = _bound_file(authority.get("path"), authority.get("sha256"), "approved execution")
    approval = json.loads(approved.read_text())
    if live and (approval.get("status") != "active" or approval.get("live_authorized") is not True
                 or approval.get("authority_id") != authority.get("id")):
        raise CaptureEpisodeError("capture authority is inactive or mismatched")
    binding_path = _bound_file(
        value["source_binding"].get("path"), value["source_binding"].get("sha256"), "source binding"
    )
    binding = json.loads(binding_path.read_text())
    files = binding.get("files") if isinstance(binding, dict) else None
    roles = binding.get("file_roles") if isinstance(binding, dict) else None
    if (binding.get("schema") != SOURCE_BINDING_SCHEMA or not isinstance(files, dict) or not files
            or not isinstance(roles, dict) or not _CAPTURE_FILE_ROLES <= set(roles)
            or len({roles[name] for name in _CAPTURE_FILE_ROLES}) != len(_CAPTURE_FILE_ROLES)
            or any(path not in files for path in roles.values())):
        raise CaptureEpisodeError("source binding is malformed")
    for file_path, digest in files.items():
        _bound_file(file_path, digest, "source file")
    capture_recovery = binding.get("capture_recovery")
    if capture_recovery is not None:
        prior = capture_recovery.get("prior_disposition") \
            if isinstance(capture_recovery, dict) and set(capture_recovery) == {"prior_disposition"} else None
        if not isinstance(prior, dict) or set(prior) != {"path", "sha256"}:
            raise CaptureEpisodeError("capture recovery binding is malformed")
        prior_path = _bound_file(
            prior.get("path"), prior.get("sha256"), "capture recovery disposition"
        )
        capture_recovery = {"prior_disposition": {
            "path": prior_path, "sha256": prior["sha256"]}}
    if (binding.get("approved_execution_sha256") != spec.value["authority"].get("sha256")
            or _resolved(binding.get("approved_execution_path")) != approved):
        raise CaptureEpisodeError("source binding does not rebind the approved execution")
    package_path = _bound_file(approval.get("package_path"), approval.get("package_sha256"), "execution package")
    freeze_path = _bound_file(
        approval.get("source_freeze_path"), approval.get("source_freeze_sha256"), "package source freeze"
    )
    package = binding.get("package"); freeze = binding.get("package_source_freeze")
    if (not isinstance(package, dict) or not isinstance(freeze, dict)
            or _resolved(package.get("path")) != package_path
            or package.get("sha256") != approval.get("package_sha256")
            or _resolved(freeze.get("path")) != freeze_path
            or freeze.get("sha256") != approval.get("source_freeze_sha256")):
        raise CaptureEpisodeError("approved package binding differs")
    schedule = binding.get("schedule")
    if not isinstance(schedule, dict): raise CaptureEpisodeError("schedule binding is malformed")
    schedule_metadata_path = _bound_file(
        schedule.get("metadata_path"), schedule.get("metadata_sha256"), "schedule metadata"
    )
    schedule_metadata = json.loads(schedule_metadata_path.read_text())
    schedule_path = _bound_file(schedule.get("path"), schedule.get("sha256"), "schedule")
    if (schedule_metadata.get("package_sha256") != approval.get("package_sha256")
            or schedule_metadata.get("schedule_sha256") != schedule.get("sha256")
            or Path(str(schedule_metadata.get("schedule_file") or ""),).name != schedule_path.name):
        raise CaptureEpisodeError("schedule does not bind the approved package")
    selected = schedule.get("selected_row"); matched = []
    try:
        for line in schedule_path.read_text().splitlines():
            row = json.loads(line)
            if row.get("schedule_id") == selected.get("schedule_id"): matched.append(row)
    except (AttributeError, UnicodeError, json.JSONDecodeError):
        raise CaptureEpisodeError("schedule binding is malformed") from None
    if (len(matched) != 1 or matched[0] != selected or selected.get("execution_status") != "unstarted"
            or selected.get("stage") != "capture-canaries" or selected.get("arm") != "baseline"
            or selected.get("analysis_countable") is not False):
        raise CaptureEpisodeError("selected baseline canary differs from the schedule")
    execution = binding.get("execution"); identity = value["identity"]
    if (not isinstance(execution, dict) or execution.get("measurement_identity") != identity
            or identity.get("schedule_id") != selected.get("schedule_id")
            or identity.get("stage") != selected.get("stage")
            or identity.get("reset_key") != selected.get("reset_key")
            or identity.get("pair_id") != selected.get("matched_cell_id")
            or identity.get("arm") != selected.get("arm")
            or identity.get("logical_seed") != selected.get("reset_replicate")
            or identity.get("side") != execution.get("side")):
        raise CaptureEpisodeError("capture identity differs from the released episode")
    resources = value["resources"]
    config = _bound_file(resources.get("config"), resources.get("config_sha256"), "resource config")
    ledger = Path(str(resources.get("ledger") or ""))
    if not ledger.is_absolute(): ledger = ROOT / ledger
    if ledger.parent.resolve() != approved.parent or ledger.name != "shared-resource-ledger.jsonl":
        raise CaptureEpisodeError("capture ledger is not canonical")
    try:
        identity = value["identity"]; model = value["model"]; range_binding = value["range"]
        required_text = [identity[key] for key in ("episode_id", "protocol_id", "pair_id", "arm",
            "model_version", "source_version", "schedule_id", "stage", "reset_key")]
        required_text += [model[key] for key in ("provider", "model_id")]
        required_text += [range_binding[key] for key in ("profile", "range_id", "mcp_server", "snapshot", "scenario",
            "retained_callback_config_sha256")]
    except KeyError:
        raise CaptureEpisodeError("capture specification fields differ") from None
    if any(not isinstance(item, str) or not item.strip() for item in required_text):
        raise CaptureEpisodeError("capture specification has empty bindings")
    if identity.get("side") not in {"harness", "bare"} or model.get("policy_mode") not in {"llm", "hybrid", "symbolic"}:
        raise CaptureEpisodeError("capture execution path is invalid")
    if selected.get("family") != range_binding.get("profile") or execution.get("family") != range_binding \
            or selected.get("family") != execution.get("family", {}).get("profile"):
        raise CaptureEpisodeError("capture range differs from the released episode")
    generation = execution.get("generation")
    if not isinstance(generation, dict) or generation != model or execution.get("notification_mode") != "disabled":
        raise CaptureEpisodeError("capture generation differs from the released episode")
    if model.get("temperature") != 0 or model.get("max_retries") != 0 \
            or not isinstance(model.get("route_endpoint"), str) or not model["route_endpoint"] \
            or not isinstance(model.get("backend_identity_sha256"), str) \
            or len(model["backend_identity_sha256"]) != 64:
        raise CaptureEpisodeError("capture provider route is invalid")
    if live and identity.get("side") == "bare":
        raise CaptureEpisodeError(
            "bare capture lacks a source-bound OS observer for the evaluator/policy process"
        )
    limits = resources.get("episode")
    if not isinstance(limits, dict) or limits != {
        "action_seconds": 2700, "settlement_seconds": 300, "provider_tokens": 1_000_000,
        "per_call_reservation": 32768, "payload_tasks": 128,
    }:
        raise CaptureEpisodeError("capture episode limits differ")
    approved_per_episode = approval.get("per_episode", {})
    if (limits["action_seconds"] != approved_per_episode.get("action_seconds")
            or limits["settlement_seconds"] != approved_per_episode.get("settlement_seconds")
            or limits["provider_tokens"] != approved_per_episode.get("nominal_tokens")
            or limits["per_call_reservation"] != approved_per_episode.get("per_call_token_reservation")
            or limits["payload_tasks"] != approved_per_episode.get("payload_task_attempts")
            or model.get("max_retries") != approved_per_episode.get("automatic_provider_retries")):
        raise CaptureEpisodeError("episode limits differ from approved execution")
    if model.get("context_limit") != 32768 or model.get("max_output_tokens") != 4096:
        raise CaptureEpisodeError("capture generation limits differ")
    if not isinstance(range_binding.get("expected_vmids"), list) or not range_binding["expected_vmids"] \
            or any(type(item) is not int or item <= 0 for item in range_binding["expected_vmids"]):
        raise CaptureEpisodeError("capture range inventory is invalid")
    _bound_file(range_binding.get("retained_callback_config"),
                range_binding.get("retained_callback_config_sha256"), "retained callback config")
    config_value = json.loads(config.read_text()); limit_section = approval.get("limits", {})
    approved_limits = {name: amount for name, amount in limit_section.items()
                       if name not in {"calendar_days", "cleanup_reserve_hours"}}
    approved_reserves = approval.get("cleanup_reserves", {})
    configured = config_value.get("resources") if isinstance(config_value, dict) else None
    if (config_value.get("schema") != "sage-shared-resource-budget-v1"
            or config_value.get("calendar_seconds") != limit_section.get("calendar_days", 0) * 86400
            or config_value.get("cleanup_seconds") != limit_section.get("cleanup_reserve_hours", 0) * 3600
            or not isinstance(configured, dict) or set(configured) != set(approved_limits)
            or any(configured[name].get("limit") != approved_limits[name]
                   or configured[name].get("cleanup_reserve") != approved_reserves.get(name, 0)
                   for name in approved_limits)):
        raise CaptureEpisodeError("resource allocation differs from the approved package")
    return {"approved": approved, "source_binding": binding_path, "source": binding,
            "config": config, "ledger": ledger.resolve(), "approval": approval, "execution": execution,
            "capture_recovery": capture_recovery}


def _require_recovery_amendment(validated: Mapping[str, Any], value: Mapping[str, Any]) -> None:
    recovery = validated.get("source", {}).get("recovery_amendment")
    _module("capture_service_probe").require_applied_amendment(
        recovery, config=validated["config"], ledger_path=validated["ledger"],
        parent_reference=value["authority"],
    )


class _CaptureHandler:
    _PREFLIGHT_CHECK_NAMES=frozenset({"objective-recognizable","mythic-queries-valid",
        "llm-endpoint-authenticated","bloodhound-reachable","bloodhound-raw-surface-valid"})
    def __init__(self, buffer: Any) -> None:
        self.buffer = buffer; self.observer_started: float | None = None
        self.tracee_pid: int | None = None
        self.producer_settlement_deadline: float | None = None
        self.episode_packet:dict[str,Any]|None=None;self.native_selection:dict[str,Any]|None=None
        self.preflight_checks:list[dict[str,Any]]=[];self._preflight_names:set[str]=set()

    def __call__(self, event: Mapping[str, Any]) -> Any:
        payload = event.get("payload") if isinstance(event, Mapping) else None
        if isinstance(payload, Mapping) and event.get("kind") == "tool_event" \
                and payload.get("event") == "observed_process_streams":
            pid = payload.get("tracee_pid")
            streams = payload.get("streams")
            if type(pid) is int and pid > 0 and isinstance(streams, list) and self.observer_started is None:
                self.tracee_pid = pid; self.observer_started = time.monotonic()
        if isinstance(payload, Mapping) and event.get("kind") == "tool_event" \
                and payload.get("event") == "measurement_capture_episode":
            if self.episode_packet is not None:raise CaptureEpisodeError("duplicate capture packet")
            self.episode_packet=copy.deepcopy(dict(payload))
            native = payload.get("native_solver_result")
            settlement = native.get("producer_settlement") if isinstance(native, Mapping) else None
            deadline = settlement.get("settlement_deadline_monotonic") if isinstance(settlement, Mapping) else None
            if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
                self.producer_settlement_deadline = float(deadline)
        if isinstance(payload,Mapping) and event.get("kind")=="tool_event" and payload.get("event")=="m3_native_request_selected":
            if (set(payload)!={"event","schema","resource_episode_id","operation_id","chat_channel_id","chat_request_id"}
                    or payload.get("schema")!="sage-m3-native-request-selection-v1"
                    or not isinstance(payload.get("resource_episode_id"),str)
                    or type(payload.get("operation_id")) is not int or payload["operation_id"]<=0
                    or type(payload.get("chat_channel_id")) not in (str,int) or payload["chat_channel_id"] in ("",0)
                    or type(payload.get("chat_request_id")) not in (str,int) or payload["chat_request_id"] in ("",0)):
                raise CaptureEpisodeError("native request selection is malformed")
            selection=dict(payload)
            if self.native_selection is not None and self.native_selection!=selection:
                raise CaptureEpisodeError("native request selection differs")
            self.native_selection=selection
        if isinstance(payload,Mapping) and event.get("kind")=="tool_event" and payload.get("event")=="m3_preflight_check":
            if (set(payload)!={"event","schema","name","passed","status","error_type"}
                    or payload.get("schema")!="sage-m3-preflight-check-v1"
                    or payload.get("name") not in self._PREFLIGHT_CHECK_NAMES
                    or type(payload.get("passed")) is not bool
                    or payload.get("status") != ("passed" if payload["passed"] else "failed")
                    or (payload["passed"] and payload.get("error_type") is not None)
                    or (not payload["passed"] and payload.get("error_type") not in {
                        "PreflightCheckFailed","PreflightCheckRaised"})
                    or payload["name"] in self._preflight_names):
                raise CaptureEpisodeError("preflight check observation is malformed")
            self._preflight_names.add(payload["name"]);self.preflight_checks.append({
                "name":payload["name"],"passed":payload["passed"],"status":payload["status"],
                "error_type":payload["error_type"],"pid":event.get("pid"),"process_uuid":event.get("process_uuid"),
                "seq":event.get("seq"),"event_id":event.get("event_id")})
        return self.buffer(event)


_SETUP_STAGES=frozenset({"stop-sage","archive-dbs","reset-mythic","ludus-rollback","ludus-poweron",
    "range-boot","clock-reachability","sync-range-time","wipe-bloodhound","bloodhound-empty",
    "restart-sage","bootstrap-foothold","foothold-power","foothold-launch","readiness","callback-discovery"})


def _capture_setup_stage(records:list[dict[str,Any]],states:dict[str,str],event:Mapping[str,Any])->None:
    if (not isinstance(event,Mapping) or set(event)!={"stage","status","error_class","exit_code","timeout_seconds"}
            or event.get("stage") not in _SETUP_STAGES or event.get("status") not in {"started","passed","failed"}
            or event.get("error_class") not in {None,"SystemExit","TimeoutExpired","UnexpectedError"}
            or event.get("exit_code") is not None and (type(event["exit_code"]) is not int
                or not -255<=event["exit_code"]<=255)
            or event.get("timeout_seconds") is not None and (not isinstance(event["timeout_seconds"],(int,float))
                or isinstance(event["timeout_seconds"],bool) or not math.isfinite(event["timeout_seconds"])
                or event["timeout_seconds"]<=0)):
        raise CaptureEpisodeError("capture setup diagnostic is malformed")
    prior=states.get(event["stage"])
    if ((event["status"]=="started" and prior is not None) or
            (event["status"] in {"passed","failed"} and prior!="started") or
            (event["status"]=="started" and any(event[k] is not None for k in ("error_class","exit_code","timeout_seconds"))) or
            (event["status"]=="passed" and any(event[k] is not None for k in ("error_class","exit_code","timeout_seconds"))) or
            (event["status"]=="failed" and event["error_class"] is None)):
        raise CaptureEpisodeError("capture setup diagnostic is malformed")
    states[event["stage"]]=event["status"];records.append(dict(event))


class _Pump:
    def __init__(self, receiver: Any, handler: _CaptureHandler) -> None:
        self.receiver = receiver; self.stop = threading.Event(); self.failure: BaseException | None = None
        self.handler = handler; self.count = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            try:
                self.receiver.serve_one(0.2); self.count += 1
            except Exception as exc:
                if type(exc).__name__ == "ObservationReceiverTimeout": continue
                self.failure = exc; return

    def start(self): self.thread.start()
    def finish(self):
        self.stop.set(); self.thread.join(timeout=6)
        if self.thread.is_alive(): raise CaptureEpisodeError("observation receiver did not stop")
        if self.failure is not None: raise CaptureEpisodeError("observation receiver failed")

    @property
    def observer_started(self):
        return self.handler.observer_started


class _BoundedOutputSink:
    """Hash support output and keep only the fenced measured stream in memory."""
    def __init__(self, limit: int) -> None:
        self.limit = limit; self._saved: dict[int, int] = {}; self._read: dict[int, int] = {}
        self._threads: list[threading.Thread] = []
        self._phases = ("pre", "measured", "post")
        self._hash = {(fd, phase): hashlib.sha256() for fd in (1, 2) for phase in self._phases}
        self._bytes = {(fd, phase): 0 for fd in (1, 2) for phase in self._phases}
        self._phase = {1: "pre", 2: "pre"}; self._pending = {1: bytearray(), 2: bytearray()}
        self._awaiting: dict[int, tuple[str, bytes, threading.Event] | None] = {1: None, 2: None}
        self._content = {1: bytearray(), 2: bytearray()}; self._retain = True
        self._failures: list[str] = []; self._frozen = False; self._scanned = False
        self._capture_lock = threading.Lock()

    def _consume(self, descriptor: int, content: bytes) -> None:
        if not content: return
        phase = self._phase[descriptor]; self._bytes[(descriptor, phase)] += len(content)
        self._hash[(descriptor, phase)].update(content)
        if phase == "measured" and self._retain:
            if sum(len(value) for value in self._content.values()) + len(content) <= self.limit:
                self._content[descriptor].extend(content)
            else:
                self._failures.append("output_byte_limit_exceeded"); self._retain = False
                self._content[1].clear(); self._content[2].clear()

    def _accept(self, descriptor: int, content: bytes) -> None:
        pending = self._pending[descriptor]; pending.extend(content)
        awaited = self._awaiting[descriptor]
        if awaited is None:
            self._consume(descriptor, bytes(pending)); pending.clear(); return
        phase, marker, ready = awaited; found = pending.find(marker)
        if found < 0:
            safe = max(0, len(pending) - len(marker) + 1)
            self._consume(descriptor, bytes(pending[:safe])); del pending[:safe]; return
        self._consume(descriptor, bytes(pending[:found])); del pending[:found + len(marker)]
        self._phase[descriptor] = phase; self._awaiting[descriptor] = None; ready.set()
        self._consume(descriptor, bytes(pending)); pending.clear()

    def _drain(self, descriptor: int, read_fd: int) -> None:
        try:
            while True:
                content = os.read(read_fd, 65536)
                if not content: break
                with self._capture_lock:
                    self._accept(descriptor, content)
        except OSError:
            self._failures.append("output_receiver_error")
        finally:
            try: os.close(read_fd)
            except OSError: pass

    def _fence(self, phase: str) -> None:
        if not self._threads or phase not in {"measured", "post"} \
                or phase == "measured" and any(value != "pre" for value in self._phase.values()) \
                or phase == "post" and any(value != "measured" for value in self._phase.values()):
            raise CaptureEpisodeError("subprocess output fence is invalid")
        events = []
        for descriptor in (1, 2):
            marker = b"\nSAGE_OUTPUT_FENCE_" + phase.encode() + b"_" + os.urandom(32).hex().encode() + b"\n"
            ready = threading.Event()
            with self._capture_lock: self._awaiting[descriptor] = (phase, marker, ready)
            os.write(descriptor, marker); events.append(ready)
        if any(not event.wait(5) for event in events):
            self._failures.append("output_fence_timeout")
            raise CaptureEpisodeError("subprocess output fence was not observed")

    def start_measurement(self) -> None: self._fence("measured")
    def end_measurement(self) -> None: self._fence("post")

    def __enter__(self):
        if type(self.limit) is not int or not 1 <= self.limit <= 1024 * 1024 * 1024:
            raise CaptureEpisodeError("subprocess output limit is invalid")
        sys.stdout.flush(); sys.stderr.flush()
        for descriptor in (1, 2):
            read_fd, write_fd = os.pipe(); self._read[descriptor] = read_fd
            self._saved[descriptor] = os.dup(descriptor); os.dup2(write_fd, descriptor); os.close(write_fd)
            thread = threading.Thread(target=self._drain, args=(descriptor, read_fd), daemon=True)
            self._threads.append(thread); thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        sys.stdout.flush(); sys.stderr.flush()
        for descriptor in (1, 2):
            os.dup2(self._saved[descriptor], descriptor); os.close(self._saved[descriptor])
        for descriptor, thread in zip((1, 2), self._threads):
            thread.join(timeout=6)
            if thread.is_alive():
                self._failures.append("output_producer_still_open")
                try: os.close(self._read[descriptor])
                except OSError: pass
        with self._capture_lock:
            for descriptor in (1, 2): self._consume(descriptor, bytes(self._pending[descriptor]))
        if any(phase != "post" for phase in self._phase.values()):
            self._failures.append("output_measurement_fence_missing")
        self._frozen = True

    def scan(self, secret_index: Any) -> dict[str, Any]:
        if not self._frozen or self._scanned or not callable(getattr(secret_index, "redact_bytes", None)):
            raise CaptureEpisodeError("subprocess output is not ready for terminal scanning")
        self._scanned = True; streams = []
        try:
            for descriptor in (1, 2):
                record = {"fd": descriptor, "segments": {phase: {
                    "bytes": self._bytes[(descriptor, phase)],
                    "sha256": self._hash[(descriptor, phase)].hexdigest(),
                } for phase in self._phases}}
                measured = record["segments"]["measured"]
                measured.update({"opaque_matches": None, "redacted_base64": None})
                if self._retain and not self._failures:
                    redacted = secret_index.redact_bytes(bytes(self._content[descriptor]))
                    if not isinstance(redacted, Mapping) or not isinstance(redacted.get("redacted"), bytes) \
                            or not isinstance(redacted.get("metadata"), Mapping):
                        raise CaptureEpisodeError("subprocess output redaction failed")
                    measured["opaque_matches"] = redacted["metadata"].get("matches")
                    measured["redacted_base64"] = __import__("base64").b64encode(redacted["redacted"]).decode("ascii")
                streams.append(record)
            return {"schema": "sage-capture-controlled-output-v1", "complete": not self._failures,
                    "failures": list(dict.fromkeys(self._failures)), "streams": streams}
        finally:
            self._content[1].clear(); self._content[2].clear()

    def discard(self) -> dict[str, Any]:
        self._content[1].clear(); self._content[2].clear(); self._scanned = True
        failures = list(dict.fromkeys([*self._failures, "terminal_secret_registration_unavailable"]))
        return {"schema": "sage-capture-controlled-output-v1", "complete": False,
                "failures": failures, "streams": [{"fd": descriptor, "segments": {phase: {
                    "bytes": self._bytes[(descriptor, phase)], "sha256": self._hash[(descriptor, phase)].hexdigest(),
                    "opaque_matches": None if phase == "measured" else None,
                    "redacted_base64": None if phase == "measured" else None,
                } for phase in self._phases}} for descriptor in (1, 2)]}


@contextmanager
def _prepare_runtime(spec: Mapping[str, Any], validated: Mapping[str, Any], receiver_dir: Path,
                     work: Path, ledger: Any):
    bloodhound = spec["observation"].get("bloodhound")
    if not isinstance(bloodhound, Mapping):
        raise CaptureEpisodeError("BloodHound launcher binding is missing")
    external = _bound_file(bloodhound.get("entry"), bloodhound.get("entry_sha256"), "BloodHound entry").parent
    interpreter = _bound_file(bloodhound.get("python"), bloodhound.get("python_sha256"), "BloodHound interpreter")
    launcher = work / "metered-bloodhound"
    _module("prepare_metered_launcher")._build(SimpleNamespace(
        python=interpreter, source_root=ROOT, external_directory=external,
        entry=external / "main.py", entry_sha256=bloodhound["entry_sha256"],
        budget_config=validated["config"], budget_ledger=validated["ledger"],
        episode_id=spec["identity"]["episode_id"], observation_directory=receiver_dir,
        client_family=["requests"], output=launcher,
    ))
    docker_path = ROOT / "skills/sage-goad-reset/scripts/docker_request_meter.py"
    loader = importlib.util.spec_from_file_location("sage_m3_docker_meter", docker_path)
    if loader is None or loader.loader is None: raise CaptureEpisodeError("Docker meter unavailable")
    docker_mod = importlib.util.module_from_spec(loader); loader.loader.exec_module(docker_mod)
    with docker_mod.DockerRequestMeter(spec["resources"]["docker_socket"], ledger, 180,
                                       temporary_parent=work) as docker:
        yield {"SAGE_BLOODHOUND_MCP_COMMAND": str(launcher),
               "DOCKER_HOST": docker.docker_host, "SAGE_RESET_METERED_DOCKER": "1"}


def _validated_route_env(execution: Mapping[str, Any]) -> dict[str, str]:
    orchestrate = _module("orchestrate"); route = dict(orchestrate._load_sage_model_environment())
    generation = execution["generation"]
    if (route.get("provider") != generation.get("provider")
            or route.get("model") != generation.get("model_id")
            or route.get("API_ENDPOINT") != generation.get("route_endpoint")
            or not isinstance(route.get("API_KEY"), str) or not route["API_KEY"]):
        raise CaptureEpisodeError("configured provider route differs from the execution binding")
    route["SAGE_EVAL_TEMPERATURE"] = str(generation["temperature"])
    route["SAGE_FINDINGS_SLACK_WEBHOOK_URL"] = ""
    route["SAGE_FINDINGS_SLACK_CHANNEL_ID"] = ""
    return route


def _observer_config(frozen: FrozenCaptureSpec, validated: Mapping[str, Any], observation_dir: Path,
                     work: Path) -> tuple[Path, str, Path]:
    process_observer = ROOT / "skills/sage-eval-gauge/scripts/process_observer.py"
    channel = RUNTIME / "ai/observation_channel.py"; entry = RUNTIME / "main.py"
    strace = Path(shutil.which("strace") or "").resolve(); py = ROOT / ".venv/bin/python"
    receipt = work / "native-process-observer-receipt.json"
    value = {
        "schema": "sage-process-observer-exec-config-v1", "observer_path": str(process_observer),
        "observer_sha256": _digest(process_observer.read_bytes()), "entry_path": str(entry),
        "entry_sha256": _digest(entry.read_bytes()), "source_binding_path": str(validated["source_binding"]),
        "source_binding_sha256": frozen.value["source_binding"]["sha256"],
        "observation_channel_path": str(channel), "observation_channel_sha256": _digest(channel.read_bytes()),
        "strace_path": str(strace), "strace_sha256": _digest(strace.read_bytes()), "python": str(py),
        "cwd": str(RUNTIME), "argv": [str(py), "-u", "main.py"],
        "limits": {"timeout_seconds": 3600, "max_events": frozen.value["observation"]["max_events"],
                   # Sage startup emits bounded binary write/sendto arguments near one MiB.
                   "max_line_bytes": 1_048_576,
                   "max_trace_bytes": frozen.value["observation"]["max_trace_bytes"]},
        "observation_directory": str(observation_dir), "receipt_path": str(receipt),
    }
    path = work / "native-process-observer-config.json"; raw = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(raw); os.chmod(path, 0o600)
    return path, _digest(raw), receipt


async def _collect_native(expected_operation_id: int | None = None) -> dict[str, Any]:
    if str(RUNTIME) not in sys.path: sys.path.insert(0, str(RUNTIME))
    native_inventory = _module("native_inventory")
    from ai import resource_clients
    from ai.hillclimb import live_seams
    from mythic import mythic
    if os.environ.get("SAGE_RESOURCE_BUDGET_CONFIG"):
        resource_clients.install()
    client = await live_seams._mythic_login_async_safe()
    operation_id = getattr(client, "current_operation_id", None)
    if expected_operation_id is not None and operation_id != expected_operation_id:
        raise CaptureEpisodeError("native operation changed during episode")
    return await native_inventory.collect_snapshot_with_configured_client(
        lambda: asyncio.sleep(0, result=client), mythic.execute_custom_query,
        expected_operation_id=operation_id,
    )


def _cleanup(spec: Mapping[str, Any], env: Mapping[str, str], *, after_stop: Callable[[bool], None] | None = None) -> dict[str, Any]:
    range_binding = spec["range"]; cleanup_env = dict(env); cleanup_env.pop("SAGE_RESOURCE_EPISODE_ID", None)
    cleanup_env["SAGE_RESOURCE_BUDGET_PHASE"] = "cleanup"
    steps = [
        ("stop_sage", ["/bin/bash", "skills/sage-goad-reset/scripts/sage_stop.sh"], ROOT, 120),
        ("archive_runtime", [str(ROOT/".venv/bin/python"), "skills/sage-goad-reset/scripts/archive_runtime_dbs.py"], ROOT, 180),
        ("verify_no_tasks", [str(ROOT/".venv/bin/python"), "ai/hillclimb/run_gauge_live.py", "verify-no-running-tasks"], RUNTIME, 120),
        ("poweroff", [str(ROOT/".venv/bin/python"), "skills/sage-goad-reset/scripts/ludus.py", "poweroff", "all",
                      "--range-id", range_binding["range_id"], "--mcp-server", range_binding["mcp_server"]], ROOT, 900),
        ("status", [str(ROOT/".venv/bin/python"), "skills/sage-goad-reset/scripts/ludus.py", "status",
                    "--range-id", range_binding["range_id"], "--mcp-server", range_binding["mcp_server"]], ROOT, 120),
    ]
    result = []
    for name, argv, cwd, timeout in steps:
        try:
            completed = subprocess.run(argv, cwd=cwd, env=cleanup_env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            ok = completed.returncode == 0; safe = {}
            if name == "status" and ok:
                text = completed.stdout.decode("utf-8", "replace")
                state = re.search(r"^range #\d+ state=([^ ]+)", text, re.MULTILINE)
                rows = re.findall(r"^  (ON |off).*pmx=(\d+)$", text, re.MULTILINE)
                ids = [int(value) for _power, value in rows]
                ok = bool(state and state.group(1) == "SUCCESS" and ids == range_binding["expected_vmids"]
                          and all(power == "off" for power, _id in rows))
                safe = {"range_state": state.group(1) if state else "unknown", "vmids": ids,
                        "all_off": bool(rows) and all(power == "off" for power, _id in rows)}
            result.append({"name": name, "ok": ok, **safe,
                           "output_sha256": _digest(completed.stdout), "error_sha256": _digest(completed.stderr)})
            if name == "stop_sage" and after_stop is not None:
                try: after_stop(ok)
                except Exception as exc:
                    result.append({"name": "terminal_observation", "ok": False,
                                   "error_type": type(exc).__name__})
        except Exception as exc:
            result.append({"name": name, "ok": False, "error_type": type(exc).__name__})
            if name == "stop_sage" and after_stop is not None:
                try: after_stop(False)
                except Exception as callback_exc:
                    result.append({"name": "terminal_observation", "ok": False,
                                   "error_type": type(callback_exc).__name__})
    return {"complete": all(item["ok"] for item in result), "steps": result}


def _strict_engagement_snapshot(directory:Path,engagement_id:str,*,allow_missing:bool)->dict[str,Any]:
    safe=re.sub(r"[^A-Za-z0-9_.-]","_",engagement_id)[:128];path=directory/("state_"+safe+".json")
    try:raw=path.read_bytes()
    except FileNotFoundError:
        if allow_missing:return {"engagement_id":engagement_id,"hops":[],"graph_facts":[]}
        raise CaptureEpisodeError("engagement evidence is unavailable") from None
    except OSError:raise CaptureEpisodeError("engagement evidence is unavailable") from None
    try:value=json.loads(raw)
    except (UnicodeError,json.JSONDecodeError):raise CaptureEpisodeError("engagement evidence is invalid") from None
    if (not isinstance(value,dict) or value.get("engagement_id")!=engagement_id
            or not isinstance(value.get("hops"),list) or not isinstance(value.get("graph_facts"),list)):
        raise CaptureEpisodeError("engagement evidence is invalid")
    return copy.deepcopy(value)


def _evidence_deadline(ledger: Any, episode_id: str, limits: Mapping[str, int],
                       producer_deadline: float | None) -> float | None:
    status = ledger.status(); scope = status.get("episodes", {}).get(episode_id)
    activated = scope.get("activated_epoch") if isinstance(scope, Mapping) else None
    if (status.get("schema") != "sage-shared-resource-budget-status-v1" or not isinstance(scope, Mapping)
            or scope.get("closed_epoch") is not None
            or scope.get("action_seconds") != limits["action_seconds"]
            or scope.get("settlement_seconds") != limits["settlement_seconds"]
            or not isinstance(activated, (int, float)) or isinstance(activated, bool)):
        return None
    remaining = max(0.0, float(activated) + limits["action_seconds"]
                    + limits["settlement_seconds"] - time.time())
    if producer_deadline is not None:
        remaining = min(remaining, max(0.0, producer_deadline - time.monotonic()))
    return time.monotonic() + remaining

def _private_fresh_directory(value:Any,label:str)->Path:
    path=Path(str(value or ""))
    try:metadata=path.stat(follow_symlinks=False);entries=tuple(path.iterdir())
    except OSError:raise CaptureEpisodeError(label+" is unavailable") from None
    if (not path.is_absolute() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=os.getuid()
            or stat.S_IMODE(metadata.st_mode)!=0o700 or entries):
        raise CaptureEpisodeError(label+" is unavailable")
    return path


def execute_capture(spec: FrozenCaptureSpec, secret_index_factory: Callable, compile_observations: Callable,
                    *, run_side: Callable | None = None, inventory_collector: Callable | None = None,
                    cleanup_runner: Callable | None = None) -> dict[str, Any]:
    """Execute one arm. Raw evidence remains in this evaluator process."""
    if not isinstance(spec, FrozenCaptureSpec) or not callable(secret_index_factory) \
            or not callable(compile_observations):
        raise CaptureEpisodeError("capture integration hooks are unavailable")
    validated = _validate(spec, live=True); value = copy.deepcopy(spec.value); resources = value["resources"]
    if str(RUNTIME) not in sys.path: sys.path.insert(0, str(RUNTIME))
    from ai import resource_budget
    receiver_mod = _module("observation_receiver"); buffer_mod = _module("capture_event_buffer")
    projection_mod = _module("capture_event_projection")
    frozen_route_env = _validated_route_env(validated["execution"])
    observation = value["observation"]; record_path = Path(observation.get("receiver_receipt_path", ""))
    capture_results_path = Path(observation.get("capture_results_path", ""))
    driver_result_path = Path(observation.get("driver_result_path", ""))
    if (not record_path.is_absolute() or record_path.exists() or not record_path.parent.is_dir()
            or not capture_results_path.is_absolute() or capture_results_path.exists()
            or not capture_results_path.parent.is_dir()
            or not driver_result_path.is_absolute() or driver_result_path.exists()
            or not driver_result_path.parent.is_dir()
            or len({record_path.resolve(), capture_results_path.resolve(), driver_result_path.resolve()}) != 3):
        raise CaptureEpisodeError("capture output destination is invalid")
    if any(type(observation.get(key)) is not int or not 1 <= observation[key] <= 1024 * 1024 * 1024
           for key in ("max_events", "max_bytes", "max_trace_bytes")):
        raise CaptureEpisodeError("capture observation limits are invalid")
    ledger = resource_budget.ResourceLedger(validated["config"], validated["ledger"])
    status = ledger.status()
    if status["outstanding_reservations"] or status["unknown_provider_usage"] or status["provider_overrun"]:
        raise CaptureEpisodeError("canonical ledger cannot start an episode")
    episode_id = value["identity"]["episode_id"]; limits = resources["episode"]
    buffer = buffer_mod.CaptureEventBuffer(max_events=observation["max_events"], max_bytes=observation["max_bytes"])
    started_reset = False; first = final = None; compile_result = composite_result = process_receipt = None
    projected=[];engagement_first=engagement_final=None
    setup_stages=[];setup_states={}
    cleanup = {"complete": True, "steps": []}; captured_error = None; output_receipt = None; output_sink = None
    frozen = {"schema": "sage-event-buffer-freeze-v1", "complete": False}; old_env = os.environ.copy()
    compiler_scope=getattr(compile_observations,"scope",{});live_contract=compiler_scope.get("live_fact_contract") if isinstance(compiler_scope,Mapping) else None
    composite_dir=None
    if isinstance(live_contract,Mapping):
        composite_dir=_private_fresh_directory(compiler_scope.get("composite_evidence_directory"),"composite evidence directory")
    work = Path(tempfile.mkdtemp(prefix="sage-m3-capture-")); os.chmod(work, 0o700)
    engagement_dir=work/"engagement";engagement_dir.mkdir(mode=0o700)
    if isinstance(compiler_scope,dict):
        # This path is freshly created and owned by this driver invocation.  Do
        # not accept a caller-supplied broad runtime-state namespace.
        compiler_scope["trusted_state_prefixes"]=[str(engagement_dir)]
    registered = False
    compiler_window_started = False; compiler_window_ended = False
    try:
        ledger.register_episode(episode_id, action_seconds=limits["action_seconds"],
            settlement_seconds=limits["settlement_seconds"], provider_token_limit=limits["provider_tokens"],
            per_call_token_reservation=limits["per_call_reservation"], payload_task_limit=limits["payload_tasks"])
        registered = True
        recovery = validated.get("capture_recovery")
        if recovery is not None:
            prior = recovery["prior_disposition"]
            ledger.reserve_capture_recovery(episode_id, prior_disposition_path=prior["path"],
                prior_disposition_sha256=prior["sha256"])
        if isinstance(compiler_scope,dict):
            compiler_scope["resource_starting_seq"]=ledger.status()["next_sequence"]
        handler = _CaptureHandler(buffer)
        with receiver_mod.ObservationReceiver(record_path, handler, temporary_parent=work) as receiver:
            pump = _Pump(receiver, handler); pump.start()
            config_path, config_sha, process_receipt_path = _observer_config(
                spec, validated, receiver.directory, work
            )
            global_env = {
                "SAGE_RESOURCE_BUDGET_CONFIG": str(validated["config"]),
                "SAGE_RESOURCE_BUDGET_LEDGER": str(validated["ledger"]),
                "SAGE_ENGAGEMENT_STATE_DIR":str(engagement_dir),
                **({"SAGE_ENGAGEMENT_ID":str(live_contract.get("current_engagement_id") or "")}
                   if isinstance(live_contract,Mapping) else {}),
                **frozen_route_env,
            }
            os.environ.update(global_env)
            os.environ.pop("SAGE_RESOURCE_EPISODE_ID", None)
            with _prepare_runtime(value, validated, receiver.directory, work, ledger) as runtime_env:
                os.environ.update(runtime_env)
                orchestrate = _module("orchestrate")
                route_env = dict(frozen_route_env)
                route_env.update({
                    **runtime_env,
                    "SAGE_PROCESS_OBSERVER_CONFIG": str(config_path),
                    "SAGE_PROCESS_OBSERVER_CONFIG_SHA256": config_sha,
                })
                collect = inventory_collector or (lambda operation_id=None: asyncio.run(_collect_native(operation_id)))
                operation_id = None
                def before_reset():
                    nonlocal started_reset; started_reset = True
                def after_reset(_sage_callback_id, apollo_callback_id):
                    nonlocal first, operation_id, compiler_window_started,engagement_first
                    first = collect(None); operation_id = first.get("operation_id")
                    matches = [row for row in first.get("rows", {}).get("callback", [])
                               if row.get("display_id") == apollo_callback_id]
                    if first.get("complete") is not True or len(matches) != 1 \
                            or first.get("rows", {}).get("task"):
                        raise CaptureEpisodeError("initial native inventory is incomplete or callback-mismatched")
                    if isinstance(live_contract,Mapping):
                        engagement_first=_strict_engagement_snapshot(engagement_dir,str(live_contract.get("current_engagement_id") or ""),allow_missing=True)
                    wait_until = time.monotonic() + 5
                    while pump.observer_started is None and time.monotonic() < wait_until: time.sleep(0.02)
                    if pump.observer_started is None or time.monotonic() - pump.observer_started > 600:
                        raise CaptureEpisodeError("native observer has insufficient action and settlement window")
                    output_sink.start_measurement()
                    start_window = getattr(compile_observations, "start_window", None)
                    if callable(start_window):
                        start_window(); compiler_window_started = True
                invoke = run_side or orchestrate.run_side
                with _BoundedOutputSink(observation["max_bytes"]) as output_sink:
                    try:
                        invoke(value["range"]["scenario"], value["identity"]["side"], go=True,
                        solve_timeout=limits["action_seconds"], policy_mode=value["model"]["policy_mode"],
                        provider=value["model"]["provider"], model=value["model"]["model_id"],
                        measurement_kind="capture", protocol_id=value["identity"]["protocol_id"],
                        pair_id=value["identity"]["pair_id"], model_version=value["identity"]["model_version"],
                        logical_seed=value["identity"]["logical_seed"], measurement_arm=value["identity"]["arm"],
                        model_token_budget=limits["provider_tokens"], model_context_limit=value["model"]["context_limit"],
                        max_output_tokens=value["model"]["max_output_tokens"], source_version=value["identity"]["source_version"],
                        live_authority_id=value["authority"]["id"], resource_budget_config=validated["config"],
                        resource_budget_ledger=validated["ledger"], resource_episode_id=episode_id,
                        observation_directory=receiver.directory, capture_results_path=capture_results_path,
                        route_env=route_env, snapshot=value["range"]["snapshot"],
                        ludus_range_id=value["range"]["range_id"], ludus_mcp_server=value["range"]["mcp_server"],
                        engagement_state_directory=engagement_dir,
                        engagement_id=str(live_contract.get("current_engagement_id") or "") if isinstance(live_contract,Mapping) else None,
                        retained_callback_config=Path(value["range"]["retained_callback_config"]),
                        before_reset=before_reset, after_reset=after_reset,
                        setup_stage_callback=lambda event:_capture_setup_stage(setup_stages,setup_states,event))
                    except BaseException as exc:
                        captured_error = type(exc).__name__
                    settle_deadline = _evidence_deadline(
                        ledger, episode_id, limits, handler.producer_settlement_deadline
                    )
                    def capture_terminal(stop_ok):
                        nonlocal final, captured_error, compiler_window_ended,engagement_final
                        try:
                            if first is None or operation_id is None or not stop_ok \
                                    or settle_deadline is None or time.monotonic() >= settle_deadline:
                                captured_error = captured_error or "TerminalInventoryUnavailable"
                                return
                            prior_episode = os.environ.get("SAGE_RESOURCE_EPISODE_ID")
                            prior_observation = os.environ.get("SAGE_OBSERVATION_DIRECTORY")
                            os.environ["SAGE_RESOURCE_EPISODE_ID"] = episode_id
                            os.environ["SAGE_OBSERVATION_DIRECTORY"] = str(receiver.directory)
                            try:
                                final = collect(operation_id)
                                if isinstance(live_contract,Mapping):
                                    engagement_final=_strict_engagement_snapshot(engagement_dir,str(live_contract.get("current_engagement_id") or ""),allow_missing=False)
                            except BaseException as exc:
                                captured_error = captured_error or type(exc).__name__
                            finally:
                                if prior_episode is None: os.environ.pop("SAGE_RESOURCE_EPISODE_ID", None)
                                else: os.environ["SAGE_RESOURCE_EPISODE_ID"] = prior_episode
                                if prior_observation is None: os.environ.pop("SAGE_OBSERVATION_DIRECTORY", None)
                                else: os.environ["SAGE_OBSERVATION_DIRECTORY"] = prior_observation
                        finally:
                            end_window = getattr(compile_observations, "end_window", None)
                            if compiler_window_started and callable(end_window):
                                try: end_window(); compiler_window_ended = True
                                except BaseException as exc:
                                    captured_error = captured_error or type(exc).__name__
                            if all(phase == "measured" for phase in output_sink._phase.values()):
                                output_sink.end_measurement()
                    if started_reset:
                        if cleanup_runner is None:
                            cleanup = _cleanup(value, {**old_env, **global_env, **runtime_env}, after_stop=capture_terminal)
                        else:
                            cleanup = cleanup_runner(value, {**old_env, **global_env, **runtime_env})
                            capture_terminal(cleanup.get("complete") is True)
                    observer_deadline = (settle_deadline if handler.observer_started is not None and settle_deadline is not None
                                         else time.monotonic() + 5)
                    while True:
                        try: candidate_receipt = json.loads(process_receipt_path.read_text())
                        except (OSError, UnicodeError, json.JSONDecodeError): candidate_receipt = None
                        if isinstance(candidate_receipt, Mapping):
                            process_receipt = candidate_receipt; break
                        if time.monotonic() >= observer_deadline: break
                        time.sleep(0.05)
                    try: pump.finish()
                    except BaseException as exc: captured_error = captured_error or type(exc).__name__
                if isinstance(process_receipt, Mapping) and process_receipt.get("tracee_pid") != handler.tracee_pid:
                    captured_error = captured_error or "ProcessObserverBindingMismatch"
            try:
                frozen = buffer.freeze()
            except BaseException as exc:
                captured_error = captured_error or type(exc).__name__
                frozen = {"schema": "sage-event-buffer-freeze-v1", "complete": False}
            if first is not None and final is not None and process_receipt is not None:
                secret_index = secret_index_factory(first, final)
                secret_registrar = getattr(compile_observations, "secret_registrar", None)
                if callable(secret_registrar):
                    buffer.visit(secret_registrar(secret_index))
                output_receipt = output_sink.scan(secret_index)
                if output_receipt["complete"] is not True:
                    captured_error = captured_error or "SubprocessOutputCaptureIncomplete"
                compiler = compile_observations(value, first, final, process_receipt, output_receipt, cleanup, secret_index)
                observe = getattr(compiler, "observe", None); finish = getattr(compiler, "finish", None)
                if not callable(observe) or not callable(finish):
                    raise CaptureEpisodeError("compiler has no transient observation boundary")
                projected = buffer.project(projection_mod.CaptureEventProjection(secret_index), observer=observe)
                compile_result = finish(projected)
                if not isinstance(compile_result, Mapping): raise CaptureEpisodeError("compiler returned no disposition")
                if isinstance(live_contract,Mapping):
                    selection=handler.native_selection;packet=handler.episode_packet
                    if (not isinstance(selection,Mapping) or selection.get("resource_episode_id")!=episode_id
                            or selection.get("operation_id")!=operation_id or not isinstance(packet,Mapping)
                            or engagement_first is None or engagement_final is None):
                        raise CaptureEpisodeError("live composite source is incomplete")
                    contract={**dict(live_contract),"expected_operation_id":operation_id,
                              "selected_channel":selection["chat_channel_id"],
                              "selected_request":selection["chat_request_id"]}
                    containment={"complete":cleanup.get("complete") is True,
                        "owned_processes_dead":bool(
                            cleanup.get("complete") is True
                            and process_receipt.get("complete") is True
                            and isinstance(process_receipt.get("observed_pids"),list)
                            and isinstance(process_receipt.get("ended_pids"),list)
                            and set(process_receipt["observed_pids"])==set(process_receipt["ended_pids"])
                        ),
                        "cleanup_steps":[{"name":str(row.get("name") or ""),"result":"complete" if row.get("ok") is True else "failed"}
                                         for row in cleanup.get("steps",[]) if isinstance(row,Mapping)]}
                    evidence=composite_dir
                    bridge=_module("capture_composite_bridge")
                    try:
                        composite_result=bridge.compose_live_capture(compile_result,episode=packet,contract=contract,
                            native_first=first,native_final=final,projected_events=projected,
                            containment_receipt=containment,engagement_first=engagement_first,
                            engagement_final=engagement_final,output_dir=evidence)
                    except Exception as exc:
                        captured_error=captured_error or type(exc).__name__
            elif output_sink is not None:
                output_receipt = output_sink.discard()
            buffer.clear()
    finally:
        if output_sink is not None and not output_sink._scanned:
            output_receipt = output_sink.discard()
        os.environ.clear(); os.environ.update(old_env)
        if registered:
            ledger.close_episode(episode_id)
        shutil.rmtree(work, ignore_errors=True)
    return {"schema": "sage-m3-capture-driver-result-v1", "episode_id": episode_id,
            "capture_complete": captured_error is None and first is not None and final is not None
                and first.get("complete") is True and final.get("complete") is True
                and isinstance(process_receipt, Mapping)
                and isinstance(output_receipt, Mapping) and output_receipt.get("complete") is True
                and cleanup.get("complete") is True and isinstance(compile_result, Mapping)
                and compiler_window_started and compiler_window_ended
                and compile_result.get("admissible") is True
                and (not isinstance(live_contract,Mapping) or isinstance(composite_result,Mapping)
                    and composite_result.get("fixture_only") is False
                    and (composite_result.get("admission") or {}).get("status")=="admitted"),
            "objective_success":((composite_result or {}).get("admission") or {}).get("composite_row",{}).get("objective_proven"),
            "error_type": captured_error, "cleanup": cleanup, "event_receipt": frozen,
            "setup_stages": copy.deepcopy(setup_stages),
            "failed_setup_stage":next((row["stage"] for row in setup_stages if row["status"]=="failed"),None),
            "preflight_checks": copy.deepcopy(handler.preflight_checks),
            "process_observer_receipt": process_receipt,
            "subprocess_output_receipt": output_receipt,
            "engagement_source":({"directory":str(engagement_dir),
                "engagement_id":str(live_contract.get("current_engagement_id") or "")}
                if isinstance(live_contract,Mapping) else None),
            "compiler_result": dict(compile_result) if isinstance(compile_result, Mapping) else None,
            "composite_result":dict(composite_result) if isinstance(composite_result,Mapping) else None}


def _cli_hooks(spec: FrozenCaptureSpec):
    observation = spec.value["observation"]
    operator_inputs = observation.get("operator_inputs")
    compiler_ref = observation.get("compiler_binding")
    if not isinstance(operator_inputs, list) or not operator_inputs or not isinstance(compiler_ref, Mapping):
        raise CaptureEpisodeError("capture operator/compiler binding is missing")
    inputs_mod = _module("capture_operator_inputs")
    compiler_mod = _module("capture_episode_compiler")
    registration_mod = _module("capture_secret_registration")
    try:
        loaded = inputs_mod.load_inputs(operator_inputs, environ=os.environ)
        binding_path = _bound_file(
            compiler_ref.get("path"), compiler_ref.get("sha256"), "capture compiler binding"
        )
        binding = json.loads(binding_path.read_text())
        if set(binding) != {"schema", "scope", "static_documents", "config_paths", "artifact_paths",
                           "operator_origins", "evidence_directory"} \
                or binding.get("schema") != "sage-m3-capture-compiler-binding-v1" \
                or not isinstance(binding["scope"], Mapping) \
                or not isinstance(binding["static_documents"], list) \
                or not all(isinstance(binding[key], list)
                           for key in ("config_paths", "artifact_paths", "operator_origins")):
            raise CaptureEpisodeError("capture compiler binding is malformed")
        if isinstance(binding["scope"].get("live_fact_contract"),Mapping):
            _private_fresh_directory(binding["scope"].get("composite_evidence_directory"),"composite evidence directory")
        driver_result_path = Path(str(observation.get("driver_result_path") or ""))
        if any(Path(str(item)).resolve() == driver_result_path.resolve()
               for item in binding["artifact_paths"]):
            raise CaptureEpisodeError("driver result overlaps measured evidence")
        static_documents = {}
        for item in binding["static_documents"]:
            if not isinstance(item, Mapping) or set(item) != {"handle", "path", "sha256"} \
                    or not isinstance(item["handle"], str) or not item["handle"] \
                    or item["handle"] in static_documents:
                raise CaptureEpisodeError("capture compiler binding is malformed")
            static_documents[item["handle"]] = _bound_file(
                item["path"], item["sha256"], "static development source"
            ).read_bytes()
        compiler = compiler_mod.CaptureCompilerFactory(
            binding["scope"], static_documents, binding["config_paths"], binding["artifact_paths"],
            binding["operator_origins"], evidence_directory=Path(binding["evidence_directory"]),
        )
    except CaptureEpisodeError:
        raise
    except Exception:
        raise CaptureEpisodeError("capture operator/compiler binding is invalid") from None
    return loaded, compiler, registration_mod


def _retain_driver_result(path: Path, result: Mapping[str, Any]) -> str:
    if not path.is_absolute() or not path.parent.is_dir():
        raise CaptureEpisodeError("capture result destination is unavailable")
    encoded = json.dumps(dict(result), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                         allow_nan=False).encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600); os.write(descriptor, encoded); os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return _digest(encoded)


def _inner_diagnostic_stage(ledger:Any,probe_id:str)->Mapping[str,Any]|None:
    path_value=os.environ.get(_C_INNER_STAGE_PATH_ENV);sha=os.environ.get(_C_INNER_STAGE_SHA_ENV)
    if bool(path_value)!=bool(sha):raise CaptureEpisodeError("diagnostic stage handoff is incomplete")
    if not path_value:return None
    path=Path(path_value)
    try:raw=path.read_bytes();meta=path.stat(follow_symlinks=False);value=json.loads(raw)
    except (OSError,UnicodeError,json.JSONDecodeError):raise CaptureEpisodeError("diagnostic stage handoff differs") from None
    if (not path.is_absolute() or not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode)!=0o600
            or _digest(raw)!=sha or not isinstance(value,Mapping)):
        raise CaptureEpisodeError("diagnostic stage handoff differs")
    latest=ledger.status().get("service_probes",{}).get(probe_id,{}).get("stages",[])
    if not latest or dict(value)!=latest[-1] or value.get("stage")!="C" or value.get("closed_epoch") is not None:
        raise CaptureEpisodeError("diagnostic stage handoff differs")
    return dict(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--spec", type=Path)
    parser.add_argument("--spec-sha256"); parser.add_argument("--go", action="store_true")
    parser.add_argument("--service-readiness", type=Path)
    parser.add_argument("--service-readiness-sha256")
    parser.add_argument("--diagnostic-retry-evidence", type=Path)
    args = parser.parse_args(argv)
    service_selected = args.service_readiness is not None or args.service_readiness_sha256 is not None
    episode_selected = args.spec is not None or args.spec_sha256 is not None
    if service_selected == episode_selected or service_selected and not all(
            (args.service_readiness, args.service_readiness_sha256)) or episode_selected and not all(
                (args.spec, args.spec_sha256)):
        parser.error("select exactly one complete episode or service-readiness binding")
    if service_selected:
        service_mod = _module("capture_service_probe")
        frozen_service = service_mod.load(args.service_readiness, args.service_readiness_sha256)
        validated_service = service_mod.validate(frozen_service, live=args.go)
        if not args.go:
            print(json.dumps({"schema": service_mod.SCHEMA,
                "probe_id": validated_service["probe_id"], "action": "service-readiness"}, sort_keys=True))
            return 0
        if str(RUNTIME) not in sys.path: sys.path.insert(0, str(RUNTIME))
        from ai.resource_budget import ResourceLedger
        service_ledger = ResourceLedger(validated_service["config"], validated_service["ledger"])
        diagnostic = validated_service["amendment"].get("schema") in {
            "sage-resource-recovery-amendment-v3", "sage-resource-recovery-amendment-v4"
        }
        stage = _inner_diagnostic_stage(service_ledger,validated_service["probe_id"])
        inner_stage = stage is not None
        if diagnostic:
            if stage is None:
                retry = None
                if args.diagnostic_retry_evidence is not None:
                    retry_path = args.diagnostic_retry_evidence.resolve()
                    retry = {"path": str(retry_path), "sha256": _digest(retry_path.read_bytes())}
                stage = service_ledger.start_diagnostic_stage(validated_service["probe_id"], "C", retry)
            elif args.diagnostic_retry_evidence is not None:
                raise CaptureEpisodeError("inner diagnostic stage cannot carry retry evidence")
        elif args.diagnostic_retry_evidence is not None:
            parser.error("diagnostic retry requires a diagnostic session")
        if inner_stage:
            inner_path = service_mod.inner_result_path(validated_service["result_path"])
            if inner_path.exists() or inner_path.parent != validated_service["result_path"].parent:
                raise CaptureEpisodeError("inner service result is unavailable")
            validated_service = dict(validated_service); validated_service["result_path"] = inner_path
        service_result = service_mod.execute(frozen_service, validated_service, service_ledger,
                                            **({"diagnostic_stage": stage} if diagnostic else {}))
        if diagnostic:
            service_result.update(stage="C", passed=service_result["service_ready"],
                contained=bool(service_result["cleanup"]["complete"] and not service_mod._local_sage_pids()
                    and not service_mod._listener_owners(6006) and not service_mod._listener_owners(4317)))
        inner_retained=service_result.get("inner_result_retained")
        inner_hash=service_result.get("inner_result_sha256")
        if inner_retained is True:
            reference=service_result.get("inner_result")
            if not isinstance(reference,Mapping) or set(reference)!={"path","sha256"}:
                raise CaptureEpisodeError("inner service result handoff is incomplete")
            path=Path(str(reference["path"])).resolve()
            try:retained=path.read_bytes()
            except OSError:raise CaptureEpisodeError("inner service result is unavailable") from None
            if (path!=service_mod.inner_result_path(validated_service["result_path"]).resolve()
                    or not isinstance(inner_hash,str) or len(inner_hash)!=64 or reference["sha256"]!=inner_hash
                    or _digest(retained)!=inner_hash):
                raise CaptureEpisodeError("inner service result differs")
            result_hash=service_mod.retain(validated_service["result_path"],service_result)
        elif inner_retained is False and inner_hash is None and service_result.get("inner_result") is None:
            if service_result.get("service_ready") is not False or not service_result.get("isolation_receipt"):
                raise CaptureEpisodeError("inner service result handoff is incomplete")
            result_hash=service_mod.retain(validated_service["result_path"],service_result)
        elif inner_retained is None and inner_hash is None:
            result_hash = service_mod.retain(validated_service["result_path"], service_result)
        else:
            raise CaptureEpisodeError("inner service result handoff is incomplete")
        print(json.dumps({"schema": service_result["schema"], "probe_id": service_result["probe_id"],
            "service_ready": service_result["service_ready"], "error_type": service_result["error_type"],
            "result_sha256": result_hash}, sort_keys=True))
        return 0
    spec = load_frozen_spec(args.spec, args.spec_sha256); validated = _validate(spec, live=args.go)
    if args.go: _require_recovery_amendment(validated, spec.value)
    loaded, compiler, registration_mod = _cli_hooks(spec)
    if args.go:
        prior = {key: os.environ.get(key) for key in loaded.environment}
        os.environ.update(loaded.environment)
        try:
            result = execute_capture(spec,
                lambda first, final: registration_mod.register_secrets(loaded.operator_values, first, final),
                compiler)
        finally:
            for key, value in prior.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
        result_hash = _retain_driver_result(
            Path(spec.value["observation"]["driver_result_path"]), result
        )
        print(json.dumps({"schema": result["schema"], "episode_id": result["episode_id"],
                          "capture_complete": result["capture_complete"],
                          "error_type": result["error_type"], "result_sha256": result_hash}, sort_keys=True))
        return 0
    print(json.dumps({"schema": SCHEMA, "episode_id": spec.value["identity"]["episode_id"],
                      "scenario": spec.value["range"]["scenario"], "side": spec.value["identity"]["side"],
                      "model": spec.value["model"]["model_id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except CaptureEpisodeError as exc: print(str(exc), file=sys.stderr); raise SystemExit(2)
