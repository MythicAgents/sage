#!/usr/bin/env python3
"""Assemble one finite capture episode without persisting transient observations."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "Payload_Type" / "sage"
if str(SOURCE) not in sys.path: sys.path.insert(0, str(SOURCE))


class CaptureCompilerError(RuntimeError): pass


def _module(name: str):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("capture_compiler_" + name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module); return module


_risk = _module("capture_risk_inventory")
_ledger = _module("capture_ledger_source")


def _hash(value: Any) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping): return {str(key): _plain(item) for key,item in value.items()}
    if isinstance(value, (list,tuple)): return [_plain(item) for item in value]
    return value


def _measurement_epoch(scope:Mapping[str,Any],ledger:Mapping[str,Any])->float|int|None:
    if scope.get("resource_service_probe_id") is not None:
        value=scope.get("measurement_activated_ns")
        if type(value) is not int or value<=0 or type(scope.get("resource_starting_seq")) is not int:
            raise CaptureCompilerError("service measurement source binding missing")
        return value/1_000_000_000
    return ledger["episode_activated_epoch"]


class CaptureCompilerFactory:
    def __init__(self, scope: Mapping[str, Any], static_documents: Mapping[str, bytes],
                 config_paths: Sequence[str], artifact_paths: Sequence[str],
                 operator_origins: Sequence[Mapping[str, Any]] = (), evidence_directory: Path | None = None) -> None:
        if not isinstance(scope, Mapping) or not isinstance(static_documents, Mapping): raise CaptureCompilerError("compiler inputs invalid")
        self.scope = copy.deepcopy(dict(scope)); self.static_documents = {str(k): bytes(v) for k,v in static_documents.items()}
        self.config_paths = list(config_paths); self.artifact_paths = list(artifact_paths); self.origins = [dict(x) for x in operator_origins]
        self.evidence_directory=evidence_directory
        if evidence_directory is not None:
            try:metadata=evidence_directory.stat()
            except OSError:raise CaptureCompilerError("evidence directory unavailable") from None
            if not evidence_directory.is_absolute() or not stat.S_ISDIR(metadata.st_mode) \
                    or metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)!=0o700:
                raise CaptureCompilerError("evidence directory is unsafe")
        for name in [*self.config_paths,*self.artifact_paths]:
            path=Path(name)
            if path.exists() and not stat.S_ISREG(path.stat().st_mode): raise CaptureCompilerError("source path invalid")
            if not path.parent.exists(): raise CaptureCompilerError("source parent unavailable")
        self.config_before=None; self.config_after=None; self.artifacts=None; self.end_time_ns=None; self.window_failures=[]
        for key in ("resource_config_path", "resource_ledger_path"):
            if not isinstance(self.scope.get(key), str) or not self.scope[key]: raise CaptureCompilerError("resource source binding missing")
        scope_ids=[item for item in (self.scope.get("resource_episode_id"),self.scope.get("resource_service_probe_id")) if item is not None]
        if len(scope_ids)!=1 or not isinstance(scope_ids[0],str) or not scope_ids[0]:raise CaptureCompilerError("resource source binding missing")

    def start_window(self) -> None:
        if self.config_before is not None: raise CaptureCompilerError("capture window already started")
        self.config_before={}; self.artifacts={}
        setup=set(self.scope.get("setup_inclusive_artifact_paths",[]))
        for path in self.config_paths:
            try:self.config_before[path]=_hash(Path(path).read_bytes())
            except OSError:self.window_failures.append("config_start_missing:"+path)
        for name in self.artifact_paths:
            try:raw=Path(name).read_bytes()
            except OSError:
                if name not in setup:self.window_failures.append("artifact_start_missing:"+name)
                raw=b""
            self.artifacts[name]=(raw,len(raw),_hash(raw))

    def end_window(self) -> None:
        if self.config_before is None or self.config_after is not None: raise CaptureCompilerError("capture window state invalid")
        self.config_after={}; frozen=[]
        for path in self.config_paths:
            try:self.config_after[path]=_hash(Path(path).read_bytes())
            except OSError:self.window_failures.append("config_end_missing:"+path)
        for name,(prefix,start,_digest) in self.artifacts.items():
            try:raw=Path(name).read_bytes()
            except OSError:self.window_failures.append("artifact_end_missing:"+name);continue
            if len(raw)<start or _hash(raw[:start])!=_hash(prefix):self.window_failures.append("artifact_prefix_changed:"+name);continue
            append=raw[start:]; frozen.append({"path_handle":"artifact:"+_hash(name),"start_offset":start,
                "append_bytes":append,"append_sha256":_hash(append)})
        self.artifact_ends=frozen;self.end_time_ns=time.time_ns()

    def secret_registrar(self, secret_index: Any):
        if self.config_before is None or self.config_after is None or self.end_time_ns is None:
            raise CaptureCompilerError("capture window is incomplete")
        ledger=_ledger.read_ledger_source(self.scope["resource_config_path"],self.scope["resource_ledger_path"],episode_id=self.scope.get("resource_episode_id"),service_probe_id=self.scope.get("resource_service_probe_id"),starting_seq=self.scope.get("resource_starting_seq"),measurement_start_ns=self.scope.get("measurement_activated_ns"),measurement_end_ns=self.end_time_ns)
        activated=_measurement_epoch(self.scope,ledger)
        if activated is None:raise CaptureCompilerError("episode activation missing")
        scope=copy.deepcopy(self.scope);scope["start_ns"]=self.scope.get("measurement_activated_ns",int(activated*1_000_000_000));scope["end_ns"]=self.end_time_ns
        registrar=_risk.RiskInventory(scope=scope,secret_index=secret_index)
        method=getattr(registrar,"register_authentication_event",None)
        if not callable(method):raise CaptureCompilerError("authentication registrar unavailable")
        return lambda event: method(_plain(event))

    def __call__(self, spec: Any, first: Mapping[str, Any], final: Mapping[str, Any],
                 process_receipt: Mapping[str, Any], output_receipt: Mapping[str, Any],
                 cleanup: Mapping[str, Any], secret_index: Any):
        return EpisodeCompiler(self, spec, first, final, process_receipt, output_receipt, cleanup, secret_index)


class EpisodeCompiler:
    def __init__(self, factory: CaptureCompilerFactory, spec: Any, first: Mapping[str, Any], final: Mapping[str, Any],
                 process_receipt: Mapping[str, Any], output_receipt: Mapping[str, Any], cleanup: Mapping[str, Any], secret_index: Any):
        self.factory=factory; self.first=first; self.final=final; self.process_receipt=process_receipt; self.output_receipt=output_receipt
        self.cleanup=cleanup; self.secret_index=secret_index; self.packet=None; self.packet_error=None; self.model_ids=[]; self.phoenix_ids=[]; self.notification_id=None; self.ids=[]
        ledger = _ledger.read_ledger_source(factory.scope["resource_config_path"],factory.scope["resource_ledger_path"],episode_id=factory.scope.get("resource_episode_id"),service_probe_id=factory.scope.get("resource_service_probe_id"),starting_seq=factory.scope.get("resource_starting_seq"),measurement_start_ns=factory.scope.get("measurement_activated_ns"),measurement_end_ns=factory.end_time_ns)
        self.ledger=ledger; activated=_measurement_epoch(factory.scope,ledger)
        if activated is None: raise CaptureCompilerError("episode activation missing")
        end = factory.end_time_ns
        if factory.config_before is None or factory.config_after is None or end is None:
            raise CaptureCompilerError("capture window is incomplete")
        scope=copy.deepcopy(factory.scope); scope["start_ns"]=factory.scope.get("measurement_activated_ns",int(activated*1_000_000_000)); scope["end_ns"]=int(end)
        if not scope["start_ns"]<scope["end_ns"]:raise CaptureCompilerError("capture interval is invalid")
        scope["process_roles"]={str(pid):"runtime" for pid in process_receipt.get("observed_pids",[]) if type(pid) is int}
        scope["allowed_callback_ids"]=sorted({row["id"] for row in (first.get("rows") or {}).get("callback",[]) if isinstance(row,Mapping) and type(row.get("id")) is int})
        scope["config_paths"]=factory.config_paths
        self.risk=_risk.RiskInventory(scope=scope,secret_index=secret_index); self.risk.static_strategy(factory.static_documents)
        for origin in factory.origins: self.risk.add_operator_origin(**origin)

    def observe(self,event: Mapping[str,Any]):
        immutable=json.loads(json.dumps(_plain(event),sort_keys=True,separators=(",", ":"),ensure_ascii=True,allow_nan=False))
        self.risk.observe(immutable); self.ids.append(immutable.get("event_id"))
        kind,payload=immutable.get("kind"),immutable.get("payload",{})
        in_window=self.risk.scope["start_ns"]<=immutable.get("time_ns",0)<=self.risk.scope["end_ns"]
        if in_window and kind=="model_request" and isinstance(payload.get("request_id"),str): self.model_ids.append(payload["request_id"])
        if in_window and kind=="control_request" and isinstance(payload.get("request_id"),str):
            rule=self.risk._http_rule(payload,kind) if "url" in payload else self.risk._rpc_rule(payload)
            if rule and rule.get("sink")=="phoenix": self.phoenix_ids.append(payload["request_id"])
        if kind=="config_snapshot" and payload.get("event")=="effective_notification_config": self.notification_id=immutable["event_id"]
        if kind=="tool_event" and payload.get("event")=="measurement_capture_episode":
            if self.packet is not None: self.packet_error="duplicate_episode_packet"
            else: self.packet=copy.deepcopy(payload)

    def finish(self, projected_records: Sequence[Mapping[str,Any]]) -> dict[str,Any]:
        config_after=self.factory.config_after; artifacts=list(self.factory.artifact_ends); expected=["artifact:"+_hash(x) for x in self.factory.artifact_paths]
        log_paths=self.risk.scope.get("log_paths",[]); log_handles=["artifact:"+_hash(p) for p in log_paths]
        sink={"model":{"request_ids":self.model_ids},"native_delivery":{},"stdout_stderr":self.output_receipt,
              "phoenix":{"request_ids":self.phoenix_ids},"notification":{"startup_event_id":self.notification_id},
              "logs":{"paths":log_paths,"expected_path_handles":log_handles,"artifacts":[x for x in artifacts if x["path_handle"] in log_handles]},
              "development":{"expected_path_handles":[x for x in expected if x not in log_handles],"artifacts":[x for x in artifacts if x["path_handle"] not in log_handles]}}
        risk=self.risk.finish(process_receipts=[self.process_receipt],native_inventory=self.final,sink_inventory=sink,
            config_before=self.factory.config_before,config_after=config_after,ledger_receipt=self.ledger,native_start_inventory=self.first)
        resource_scope=self.factory.scope.get("resource_episode_id") or self.factory.scope.get("resource_service_probe_id")
        packet_ok=isinstance(self.packet,Mapping) and self.packet.get("episode_id")==resource_scope and self.packet_error is None
        reasons=list(self.factory.window_failures)
        if not packet_ok:reasons.append(self.packet_error or "episode_packet_missing_or_wrong_scope")
        projected_raw=json.dumps(list(projected_records),sort_keys=True,separators=(",", ":"),ensure_ascii=True,allow_nan=False).encode()
        retention=None
        if self.factory.evidence_directory is None:
            reasons.append("evidence_retention_not_configured")
        else:
            documents={"native-start.json":self.first,"native-final.json":self.final,
                       "projected-events.json":list(projected_records),
                       "episode-packet.json":self.packet if packet_ok else {"missing":True}}
            paths=[self.factory.evidence_directory/name for name in documents]
            if any(path.exists() for path in paths):raise CaptureCompilerError("evidence path already exists")
            manifest={}
            for name,value in documents.items():
                original=_hash(value) if not (name=="episode-packet.json" and not packet_ok) else None
                redacted=self.secret_index.redact_json(value);encoded=json.dumps(redacted,sort_keys=True,separators=(",", ":"),ensure_ascii=True,allow_nan=False).encode()
                if self.secret_index.scan(encoded):raise CaptureCompilerError("redacted evidence contains a registered value")
                path=self.factory.evidence_directory/name
                descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                try:os.fchmod(descriptor,0o600);os.write(descriptor,encoded);os.fsync(descriptor)
                finally:os.close(descriptor)
                manifest[name]={"original_source_sha256":original,"redacted_sha256":hashlib.sha256(encoded).hexdigest(),
                                "redacted_size":len(encoded),"path":str(path)}
            retention={"schema":"sage-capture-retention-manifest-v1","artifacts":manifest}
        measurement_source = {
            "start_ns": self.risk.scope["start_ns"],
            "end_ns": self.risk.scope["end_ns"],
        }
        for key in ("resource_episode_id", "resource_service_probe_id", "resource_starting_seq"):
            if key in self.risk.scope:
                measurement_source[key] = self.risk.scope[key]
        return {"schema":"sage-capture-episode-compiler-result-v1","admissible":risk["admissible"] and packet_ok and not reasons,
                "reasons":reasons,"risk":risk,"episode_packet_sha256":_hash(self.packet) if packet_ok else None,
                "redacted_episode_packet":self.secret_index.redact_json(self.packet) if packet_ok else None,
                "projected_event_count":len(projected_records),"projected_event_sha256":_hash(projected_raw),
                "measurement_source":measurement_source,"retention":retention}
