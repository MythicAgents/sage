#!/usr/bin/env python3
"""Bounded standalone Phoenix startup diagnostic for development capture stages A and B."""
from __future__ import annotations

import argparse, hashlib, importlib.util, ipaddress, json, os, select, shutil, signal, socket, stat, subprocess, sys, tempfile, time, traceback
from importlib.metadata import distribution
import threading
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "Payload_Type/sage"
SOURCE_SCHEMA = "sage-m3-phoenix-startup-source-v1"
RESULT_SCHEMA = "sage-m3-phoenix-startup-result-v1"
_ROLES = {
    "startup_probe": Path(__file__).resolve(),
    "phoenix_compat": RUNTIME / "ai/phoenix_process_compat.py",
    "observation_channel": RUNTIME / "ai/observation_channel.py",
    "process_observer": ROOT / "skills/sage-eval-gauge/scripts/process_observer.py",
    "observation_receiver": ROOT / "skills/sage-eval-gauge/scripts/observation_receiver.py",
    "resource_budget": RUNTIME / "ai/resource_budget.py",
    "resource_http": RUNTIME / "ai/resource_http.py",
    "diagnostic_isolation": ROOT / "skills/sage-eval-gauge/scripts/diagnostic_isolation.py",
    "diagnostic_isolation_binary": Path("/usr/bin/bwrap"),
    "archive_runtime": ROOT / "skills/sage-goad-reset/scripts/archive_runtime_dbs.py",
}
_PHOENIX_DIST = distribution("arize-phoenix")
for _role,_relative in {"phoenix_services":"phoenix/services.py","phoenix_session":"phoenix/session/session.py","phoenix_server_main":"phoenix/server/main.py","phoenix_server_app":"phoenix/server/app.py","phoenix_config":"phoenix/config.py","phoenix_serve":"phoenix/server/cli/commands/serve.py","phoenix_wasm_download":"phoenix/server/sandbox/_download.py"}.items():
    _ROLES[_role]=Path(_PHOENIX_DIST.locate_file(_relative)).resolve()
for _name in ("qualify_capture_lifecycle","capture_composite_bridge","capture_episode_compiler","capture_risk_inventory","capture_ledger_source","capture_event_buffer","capture_event_projection","capture_os_network","capture_os_writes","capture_redaction","capture_sink_sources","capture_live_facts","native_inventory","capture_tradecraft_facts"):
    _ROLES[_name]=ROOT/"skills/sage-eval-gauge/scripts"/(_name+".py")
for _name in ("composite_source_adapter","composite_gauge","evaluator_evidence","fitness","safe_success","safe_success_v2","tradecraft_evidence"):
    _ROLES["composite_"+_name]=RUNTIME/"ai/hillclimb"/(_name+".py")
_CHILD_MARKER = b"SAGE_PHOENIX_DIAGNOSTIC="

class ProbeError(RuntimeError): pass
class _RetainedScratch:
    def __enter__(self):
        self.path=Path(tempfile.mkdtemp(prefix="sage-phoenix-diagnostic-"));os.chmod(self.path,0o700);return self.path
    def __exit__(self,*_a):return None
def _sha(raw: bytes) -> str:return hashlib.sha256(raw).hexdigest()
def _json(path: Path, digest: str) -> dict[str, Any]:
    try: raw=path.read_bytes(); value=json.loads(raw); meta=path.stat(follow_symlinks=False)
    except (OSError,UnicodeError,json.JSONDecodeError):raise ProbeError("diagnostic binding differs") from None
    if not path.is_absolute() or len(digest)!=64 or _sha(raw)!=digest or not stat.S_ISREG(meta.st_mode) or not isinstance(value,dict):raise ProbeError("diagnostic binding differs")
    return value

def _validate(approval: Path, approval_sha: str, config: Path, ledger: Path, manifest: Path, manifest_sha: str, stage: str, output: Path,probe_id:str,wasm_artifact:Path|None=None,wasm_sha:str|None=None) -> dict[str,Any]:
    auth=_json(approval,approval_sha); source=_json(manifest,manifest_sha)
    allocation=auth.get("diagnostic_session")
    v4=(auth.get("schema")=="sage-resource-recovery-amendment-v3" and auth.get("amendment_ordinal")==3
        and allocation=={"stages":["A","B","C"],"starts_per_stage":2,"max_stage_starts":6,"stage_active_seconds":120,"stage_total_seconds":180})
    v5=(auth.get("schema")=="sage-resource-recovery-amendment-v4" and auth.get("amendment_ordinal")==4
        and allocation=={"stages":["A","B","C"],"max_stage_starts":24,"pooled_active_seconds":7200,"stage_active_seconds":600,"stage_cleanup_seconds":120,"stage_total_seconds":720,"calendar_deadlines_inherited":True})
    if (not (v4 or v5) or auth.get("status")!="approved"
            or _sha(config.read_bytes())!=auth.get("base_config_sha256") or not ledger.is_absolute()
            or source.get("schema")!=SOURCE_SCHEMA or stage not in {"A","B"} or not output.is_absolute() or not probe_id):raise ProbeError("diagnostic binding differs")
    files=source.get("files")
    if not isinstance(files,list):raise ProbeError("diagnostic source binding differs")
    seen=set()
    for row in files:
        if not isinstance(row,dict) or set(row)!={"role","path","sha256"}:raise ProbeError("diagnostic source binding differs")
        role=row["role"]; path=Path(row["path"])
        if role in seen or not isinstance(role,str):raise ProbeError("diagnostic source binding differs")
        try: raw=path.read_bytes()
        except OSError:raise ProbeError("diagnostic source binding differs") from None
        if not path.is_absolute() or _sha(raw)!=row["sha256"]:raise ProbeError("diagnostic source binding differs")
        seen.add(role)
    if seen!=set(_ROLES) or any(next(Path(r["path"]) for r in files if r["role"]==k)!=v for k,v in _ROLES.items()):raise ProbeError("diagnostic source binding differs")
    if (wasm_artifact is None)!=(wasm_sha is None):raise ProbeError("diagnostic WASM binding differs")
    if wasm_artifact is not None:
        try:wasm_raw=wasm_artifact.read_bytes();wasm_meta=wasm_artifact.stat(follow_symlinks=False)
        except OSError:raise ProbeError("diagnostic WASM binding differs") from None
        if not wasm_artifact.is_absolute() or not stat.S_ISREG(wasm_meta.st_mode) or wasm_meta.st_mode&0o022 \
                or wasm_sha!=_sha(wasm_raw) or len(wasm_raw)!=26267204:
            raise ProbeError("diagnostic WASM binding differs")
    return auth

def _minimal_env(config:Path,ledger:Path,stage:str,probe_id:str,observation:str="")->dict[str,str]:
    pybin=str(Path(sys.executable).absolute().parent); env={"PATH":pybin+":/usr/bin:/bin","PYTHONPATH":str(RUNTIME),"LANG":"C.UTF-8",
        "PHOENIX_HOST":"127.0.0.1","PHOENIX_PORT":"16006","PHOENIX_GRPC_PORT":"14317",
        "PHOENIX_DISABLE_AGENT_ASSISTANT":"true","PHOENIX_ENABLE_MCP_SERVER":"false","PHOENIX_ENABLE_PROMETHEUS":"false",
        "PHOENIX_TELEMETRY_ENABLED":"false","PHOENIX_ALLOW_EXTERNAL_RESOURCES":"false","SAGE_DIAGNOSTIC_CHILD":"1",
        "SAGE_RESOURCE_BUDGET_CONFIG":str(config),"SAGE_RESOURCE_BUDGET_LEDGER":str(ledger),"SAGE_RESOURCE_SERVICE_PROBE_ID":probe_id,
        "SAGE_RESOURCE_BUDGET_PHASE":"active","SAGE_DIAGNOSTIC_STAGE":stage}
    if observation:env["SAGE_OBSERVATION_DIRECTORY"]=observation
    return env

def _listener(port:int)->set[int]:
    sockets=set()
    for name in ("tcp","tcp6"):
        for line in Path("/proc/net"+"/"+name).read_text().splitlines()[1:]:
            f=line.split()
            if len(f)>9 and f[3]=="0A" and int(f[1].split(":")[1],16)==port:sockets.add(f[9])
    owners=set()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():continue
        try:
            if any(os.readlink(fd)[8:-1] in sockets for fd in (d/"fd").iterdir() if os.readlink(fd).startswith("socket:[")):owners.add(int(d.name))
        except OSError:pass
    return owners

def _socket_evidence(owners:set[int])->dict[str,Any]:
    """Project owned TCP sockets without retaining endpoint values."""
    sockets=set()
    for pid in owners:
        try:
            for fd in (Path("/proc")/str(pid)/"fd").iterdir():
                target=os.readlink(fd)
                if target.startswith("socket:["):sockets.add(target[8:-1])
        except OSError:pass
    classes=[];identity=hashlib.sha256()
    for family,name in ((socket.AF_INET,"tcp"),(socket.AF_INET6,"tcp6")):
        try: lines=Path("/proc/net") .joinpath(name).read_text().splitlines()[1:]
        except OSError:continue
        for line in lines:
            f=line.split()
            if len(f)<=9 or f[9] not in sockets:continue
            local,remote=f[1].split(":"),f[2].split(":")
            def address(raw):
                b=bytes.fromhex(raw)
                b=b[::-1] if family==socket.AF_INET else b"".join(b[i:i+4][::-1] for i in range(0,16,4))
                return ipaddress.ip_address(b)
            try: local_ip=address(local[0]);remote_ip=address(remote[0]);local_port=int(local[1],16)
            except (ValueError,OSError):continue
            mapped=remote_ip.ipv4_mapped if isinstance(remote_ip,ipaddress.IPv6Address) else None
            remote_loopback=remote_ip.is_loopback or bool(mapped and mapped.is_loopback)
            local_fixed=local_port in {16006,14317}
            direction="inbound_local" if f[3]=="01" and local_fixed else "outbound_local"
            if f[3]=="0A":direction="listening"
            if f[3] not in {"01","0A"}:direction="other"
            if not remote_loopback and f[3]=="01":direction="nonlocal"
            classes.append(direction);identity.update(f"{name}:{f[9]}:{f[3]}:{local_fixed}".encode())
    return {"counts":{name:classes.count(name) for name in ("listening","inbound_local","outbound_local","nonlocal","other")},"socket_identity_sha256":identity.hexdigest(),"nonlocal":classes.count("nonlocal")>0}

def _child()->int:
    probe=os.environ.get("SAGE_RESOURCE_SERVICE_PROBE_ID","");stage=os.environ.get("SAGE_DIAGNOSTIC_STAGE","")
    sys.path.insert(0,str(RUNTIME));from ai.resource_budget import ResourceLedger
    status=ResourceLedger(os.environ["SAGE_RESOURCE_BUDGET_CONFIG"],os.environ["SAGE_RESOURCE_BUDGET_LEDGER"],service_probe_id=probe).status();bound=status.get("service_probes",{}).get(probe,{})
    if not bound.get("stages") or bound["stages"][-1].get("stage")!=stage or bound["stages"][-1].get("closed_epoch") is not None:raise ProbeError("diagnostic child is not authorized")
    stage_state=bound["stages"][-1];active_deadline=float(stage_state["active_deadline_epoch"])
    working=Path(os.environ["SAGE_SYNTHETIC_PHOENIX_DIR"]);os.environ["PHOENIX_WORKING_DIR"]=str(working);os.environ["PHOENIX_SQL_DATABASE_URL"]="sqlite:///"+str(working/"phoenix.db")
    try:
        import phoenix as px
        from ai.phoenix_process_compat import PhoenixProcessStartupError,child_startup_diagnostic,launch_capture_phoenix
        session=launch_capture_phoenix(px)
    except BaseException as exc:
        safe=getattr(exc,"diagnostic",None) if type(exc).__name__=="PhoenixProcessStartupError" else None
        if not isinstance(safe,dict):
            from ai.phoenix_process_compat import project_child_output
            safe=project_child_output(traceback.format_exc().encode(),None)
        os.write(1,_CHILD_MARKER+json.dumps({"ready":False,"diagnostic":safe},separators=(",",":")).encode()+b"\n");return 1
    stopped=False;failed=False
    def stop(*_a):
        nonlocal stopped
        if not stopped:
            stopped=True
            try:session.end()
            except Exception:pass
    signal.signal(signal.SIGTERM,stop);child=session.app_service.child;child_pid=child.pid
    snapshot=child_startup_diagnostic(child,category="alive_but_unready")
    while time.time()<active_deadline and snapshot["alive"]:
        owned=_descendants(os.getpid());network=_socket_evidence(owned)
        if network["nonlocal"]:
            safe=child_startup_diagnostic(child,category="alive_but_unready")
            os.write(1,_CHILD_MARKER+json.dumps({"ready":False,"diagnostic":safe,"pid":child_pid,"network":network},separators=(",",":")).encode()+b"\n");failed=True;break
        if child_pid in _listener(16006) and child_pid in _listener(14317):
            if stage=="B":
                start_path=Path(os.environ["SAGE_DIAGNOSTIC_MEASUREMENT_PATH"])
                os.write(1,_CHILD_MARKER+json.dumps({"awaiting_measurement":True,"pid":child_pid,"listeners":{"http":True,"grpc":True},"network":network},separators=(",",":")).encode()+b"\n")
                while not start_path.exists() and time.time()<active_deadline and snapshot["alive"]:
                    network=_socket_evidence(_descendants(os.getpid()))
                    if network["nonlocal"]:break
                    time.sleep(.05);snapshot=child_startup_diagnostic(child,category="alive_but_unready")
                if not start_path.exists() or network["nonlocal"] or not snapshot["alive"]:
                    category="alive_but_unready" if snapshot["alive"] else "child_exited";safe=child_startup_diagnostic(child,category=category)
                    os.write(1,_CHILD_MARKER+json.dumps({"ready":False,"diagnostic":safe,"pid":child_pid,"network":network},separators=(",",":")).encode()+b"\n");failed=True;break
            from ai import resource_http
            try:
                transport=resource_http.MeteredHTTPTransport("control",ledger=ResourceLedger(os.environ["SAGE_RESOURCE_BUDGET_CONFIG"],os.environ["SAGE_RESOURCE_BUDGET_LEDGER"],service_probe_id=probe),transport=resource_http.httpx.HTTPTransport(trust_env=False),operation="diagnostic-phoenix-readiness")
                with resource_http.httpx.Client(transport=transport,timeout=min(5,max(.1,active_deadline-time.time())),trust_env=False) as client:response=client.get("http://127.0.0.1:16006")
                http={"status_code":response.status_code,"ready":200<=response.status_code<400}
            except Exception:http={"ready":False,"error_category":"local_http_failure"}
            network=_socket_evidence(_descendants(os.getpid()))
            os.write(1,_CHILD_MARKER+json.dumps({"ready":http["ready"],"pid":child_pid,"listeners":{"http":True,"grpc":True},"http":http,"network":network},separators=(",",":")).encode()+b"\n")
            failed=not http["ready"] or network["nonlocal"];break
        time.sleep(.1);snapshot=child_startup_diagnostic(child,category="alive_but_unready")
    else:
        category="alive_but_unready" if snapshot["alive"] else "child_exited";safe=child_startup_diagnostic(child,category=category)
        os.write(1,_CHILD_MARKER+json.dumps({"ready":False,"diagnostic":safe,"pid":child_pid},separators=(",",":")).encode()+b"\n");failed=True
    while not stopped:signal.pause()
    return 1 if failed else 0

def _retain(path:Path,value:Mapping[str,Any])->str:
    raw=json.dumps(dict(value),sort_keys=True,separators=(",",":")).encode();fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:os.write(fd,raw);os.fsync(fd)
    finally:os.close(fd)
    return _sha(raw)

class _Handler:
    def __init__(self):self.events=[];self.delivery_failure=None;self.requests=set();self.responses=set();self.response_failures=0;self.capture=None
    def __call__(self,event):
        payload=event.get("payload") if isinstance(event,Mapping) else None;kind=event.get("kind") if isinstance(event,Mapping) else None
        row={"kind":kind}
        if isinstance(payload,Mapping) and kind=="tool_event":row["event"]=payload.get("event")
        elif isinstance(payload,Mapping) and kind=="control_request" and isinstance(payload.get("request_id"),str):self.requests.add(payload["request_id"])
        elif isinstance(payload,Mapping) and kind=="control_response" and isinstance(payload.get("request_id"),str):
            self.responses.add(payload["request_id"]);self.response_failures += payload.get("outcome") not in {"returned","error"} or not isinstance(payload.get("reservation_id"),str)
        self.events.append(row);return self.capture(event) if callable(self.capture) else row
    def controls_ok(self):return self.requests==self.responses and self.response_failures==0
    def control_receipt(self):
        return {"request_count":len(self.requests),"response_count":len(self.responses),
                "correspondence":self.controls_ok(),"failure_count":self.response_failures,
                "identity_sha256":_sha(json.dumps({"requests":sorted(self.requests),"responses":sorted(self.responses)},sort_keys=True,separators=(",",":")).encode())}

def _reduce_final_gates(*,runtime_marker_ready:bool,http_ready:bool,listeners_ready:bool,
        marker_no_nonlocal:bool,active_end_recorded:bool,cleanup_no_nonlocal:bool,
        processes_contained:bool,archive_ok:bool,cleanup_deadline_met:bool,
        observer_receipt_valid:bool|None=None,receiver_delivery_complete:bool|None=None,
        control_correspondence:bool|None=None,compiler_admissible:bool|None=None,
        composite_admitted:bool|None=None)->dict[str,Any]:
    gates={"runtime_marker_ready":runtime_marker_ready,"http_ready":http_ready,
           "listeners_ready":listeners_ready,"marker_no_nonlocal":marker_no_nonlocal,
           "active_end_recorded":active_end_recorded,"cleanup_no_nonlocal":cleanup_no_nonlocal,
           "processes_contained":processes_contained,"archive_ok":archive_ok,
           "cleanup_deadline_met":cleanup_deadline_met}
    optional={"observer_receipt_valid":observer_receipt_valid,
              "receiver_delivery_complete":receiver_delivery_complete,
              "control_correspondence":control_correspondence,
              "compiler_admissible":compiler_admissible,"composite_admitted":composite_admitted}
    gates.update({name:value for name,value in optional.items() if value is not None})
    failed=sorted(name for name,value in gates.items() if value is not True)
    return {"schema":"sage-m3-diagnostic-final-gates-v1","gates":gates,"failed":failed,
            "passed":not failed}

def _load(path:Path,name:str):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module

class _ProcessOutput:
    """Continuously drain both producer streams with bounded retained bytes."""
    def __init__(self,process:subprocess.Popen):
        self.process=process;self.lock=threading.Lock();self.marker=None;self.pending=bytearray();self.rows={}
        for name,stream in (("stdout",process.stdout),("stderr",process.stderr)):
            row=self.rows[name]={"raw":bytearray(),"bytes":0,"digest":hashlib.sha256(),"eof":stream is None}
            if stream is not None:
                row["thread"]=threading.Thread(target=self._read,args=(name,stream),daemon=True);row["thread"].start()
    def _read(self,name,stream):
        row=self.rows[name]
        try:
            while True:
                part=os.read(stream.fileno(),65536)
                if not part:break
                with self.lock:
                    row["bytes"]+=len(part);row["digest"].update(part);row["raw"].extend(part[:max(0,65536-len(row["raw"]))])
                    if name=="stdout":
                        self.pending.extend(part);lines=self.pending.split(b"\n");self.pending=bytearray(lines.pop())
                        if len(self.pending)>65536:del self.pending[:-65536]
                        for line in lines:
                            if line.startswith(_CHILD_MARKER):
                                try:self.marker=json.loads(line[len(_CHILD_MARKER):])
                                except json.JSONDecodeError:pass
        except OSError:pass
        finally:
            with self.lock:row["eof"]=True
    def wait_marker(self,deadline,traffic,previous=None):
        while time.time()<deadline and self.process.poll() is None:
            with self.lock:marker=self.marker
            if marker is not None and marker is not previous:return marker
            if traffic():return {"ready":False,"diagnostic":{"status":"failure","error_category":"nonlocal_network_activity","returncode":None,"output_sha256":_sha(b""),"exception_class":"unknown","frames":[]}}
            time.sleep(.05)
        with self.lock:
            if self.marker is None and bytes(self.pending).startswith(_CHILD_MARKER):
                try:self.marker=json.loads(bytes(self.pending)[len(_CHILD_MARKER):])
                except json.JSONDecodeError:pass
            return self.marker
    def finish(self,deadline):
        for row in self.rows.values():
            thread=row.get("thread")
            if thread is not None:thread.join(max(0,deadline-time.time()))
        with self.lock:
            return {name:{"raw":bytes(row["raw"]),"bytes":row["bytes"],"sha256":row["digest"].hexdigest(),"complete":row["eof"] and row["bytes"]<=65536,"retained_bytes":len(row["raw"])} for name,row in self.rows.items()}

def _process_output(process:subprocess.Popen, deadline:float,traffic):
    capture=_ProcessOutput(process);return capture,capture.wait_marker(deadline,traffic)

def _descendants(ancestor:int)->set[int]:
    result={ancestor};changed=True
    while changed:
        changed=False
        for item in Path("/proc").iterdir():
            if not item.name.isdigit() or int(item.name) in result:continue
            try:ppid=int((item/"stat").read_text().rsplit(")",1)[1].split()[1])
            except (OSError,ValueError):continue
            if ppid in result:result.add(int(item.name));changed=True
    return result

def _nonlocal_connection(owners:set[int])->bool:return _socket_evidence(owners)["nonlocal"]

def _read_receipt(path:Path,deadline:float)->Mapping[str,Any]|None:
    while time.time()<deadline:
        try:
            value=json.loads(path.read_bytes())
            if isinstance(value,Mapping):return value
        except (OSError,UnicodeError,json.JSONDecodeError):pass
        time.sleep(.05)
    return None

def _settle_pump(stop:threading.Event,thread:threading.Thread|None,receiver:Any,
        failures:list[dict[str,Any]],deadline:float)->tuple[bool,list[dict[str,Any]]]:
    stop.set();settled=thread is None
    if thread is not None:
        thread.join(min(.5,max(0,deadline-time.time())));settled=not thread.is_alive()
    if receiver is not None:receiver.__exit__(None,None,None)
    if thread is not None and not settled:
        thread.join(min(.5,max(0,deadline-time.time())));settled=not thread.is_alive()
    projected=list(failures[:1])
    if not settled and not projected:
        projected=[{"error_class":"ObservationPumpUnsettled","protocol_stage":"ack",
                    "reason":"receiver pump did not settle","frame_bytes":None,"frame_limit_bytes":None}]
    return settled,projected

def _run(a)->dict[str,Any]:
    if str(RUNTIME) not in sys.path:sys.path.insert(0,str(RUNTIME))
    from ai.resource_budget import ResourceLedger
    retry=None
    if a.retry_evidence is not None:retry={"path":str(a.retry_evidence.resolve()),"sha256":_sha(a.retry_evidence.read_bytes())}
    ledger=ResourceLedger(a.config,a.ledger,service_probe_id=a.probe_id);status=ledger.status();auth=_json(a.approval,a.approval_sha256);expected={"amendment_id":auth["amendment_id"],"approval_path":str(a.approval),"approval_sha256":a.approval_sha256}
    active=status.get("service_probes",{}).get(a.probe_id,{})
    if not status.get("amendments") or status["amendments"][-1]!=expected or active.get("started_epoch") is None or active.get("closed_epoch") is not None:raise ProbeError("diagnostic session is not active")
    state=ledger.start_diagnostic_stage(a.probe_id,a.stage,retry_evidence=retry)
    started=time.time();active=float(state["active_deadline_epoch"]);total=float(state["total_deadline_epoch"]);v5=auth.get("schema")=="sage-resource-recovery-amendment-v4";active_ended=False
    result={"schema":RESULT_SCHEMA,"probe_id":a.probe_id,"stage":a.stage,"attempt":state["attempt"],"contained":False,"diagnostic":None,"ownership":{},"http":{},"cleanup":{},"source_manifest_sha256":a.source_manifest_sha256}
    process=None;observer_receipt=None;handler=_Handler();receiver=None;pump_stop=threading.Event();pump_thread=None;pump_failure=[];pump_settled=True;delivery_failures=[];sampled_pids=set();nonlocal_seen=False;fixture=None;rawout=rawerr=b"";output_capture=None;output_state={};runtime_marker_ready=False
    with _RetainedScratch() as scratch:
        synthetic=scratch/"repo";working=synthetic/"Payload_Type/sage/.phoenix";working.mkdir(parents=True)
        env=_minimal_env(a.config,a.ledger,a.stage,a.probe_id);env["SAGE_SYNTHETIC_PHOENIX_DIR"]=str(working);env["PYTHONNOUSERSITE"]="1";env["PYTHONUSERBASE"]=str(scratch/"python-user")
        command=[str(Path(sys.executable).absolute()),"-u",str(Path(__file__).resolve())]
        try:
            if a.wasm_artifact is not None:
                cached=working/"wasm/python-3.12.0.wasm";cached.parent.mkdir(mode=0o700);shutil.copyfile(a.wasm_artifact,cached);os.chmod(cached,0o600)
                if _sha(cached.read_bytes())!=a.wasm_artifact_sha256:raise ProbeError("diagnostic WASM copy differs")
                env["PHOENIX_WASM_BINARY_PATH"]=str(cached)
            if _listener(16006) or _listener(14317):raise ProbeError("diagnostic port is occupied")
            if a.stage=="B":
                observation=_load(_ROLES["observation_receiver"],"sage_m3_diagnostic_receiver");events=a.output_dir/"events.jsonl"
                receiver=observation.ObservationReceiver(events,handler,temporary_parent=scratch);receiver.__enter__();env["SAGE_OBSERVATION_DIRECTORY"]=str(receiver.directory)
                qualifier=_load(_ROLES["qualify_capture_lifecycle"],"sage_m3_integrated_qualifier");fixture=qualifier.ServiceCaptureFixture(scratch,a.config,a.ledger,a.probe_id,events);handler.capture=fixture
                def pump():
                    while not pump_stop.is_set():
                        try:receiver.serve_one(.2)
                        except Exception as exc:
                            if type(exc).__name__!="ObservationReceiverTimeout":pump_failure.append({"error_class":type(exc).__name__,
                                "protocol_stage":getattr(exc,"stage","unknown"),"reason":getattr(exc,"reason","unknown"),
                                "frame_bytes":getattr(exc,"frame_bytes",None),"frame_limit_bytes":getattr(exc,"frame_limit_bytes",None)});break
                pump_thread=threading.Thread(target=pump,daemon=True);pump_thread.start()
                env["SAGE_DIAGNOSTIC_MEASUREMENT_PATH"]=str(scratch/"measurement-started")
                observer_config=scratch/"observer.json";receipt=scratch/"observer-receipt.json";strace=Path("/usr/bin/strace")
                cfg={"schema":"sage-process-observer-exec-config-v1","observer_path":str(_ROLES["process_observer"]),"observer_sha256":_sha(_ROLES["process_observer"].read_bytes()),"entry_path":str(Path(__file__).resolve()),"entry_sha256":_sha(Path(__file__).read_bytes()),"source_binding_path":str(a.source_manifest),"source_binding_sha256":a.source_manifest_sha256,"observation_channel_path":str(_ROLES["observation_channel"]),"observation_channel_sha256":_sha(_ROLES["observation_channel"].read_bytes()),"strace_path":str(strace),"strace_sha256":_sha(strace.read_bytes()),"python":str(Path(sys.executable).absolute()),"cwd":str(ROOT),"argv":command,"limits":{"timeout_seconds":max(1,total-started),"max_events":200000,"max_line_bytes":65536,"max_trace_bytes":67108864},"observation_directory":str(receiver.directory),"receipt_path":str(receipt)}
                raw=json.dumps(cfg,sort_keys=True).encode();observer_config.write_bytes(raw);os.chmod(observer_config,0o600)
                command=[str(Path(sys.executable).absolute()),str(_ROLES["process_observer"]),"--exec-config",str(observer_config),"--exec-config-sha256",_sha(raw),"--cwd",str(ROOT),"--python",str(Path(sys.executable).absolute())]
            isolation=_load(_ROLES["diagnostic_isolation"],"sage_m3_diagnostic_isolation")
            lock_dir=ledger._lock_path().parent;lock_dir.mkdir(mode=0o700,exist_ok=True)
            if a.stage=="B":
                env["SAGE_DIAGNOSTIC_NAMESPACE_SUPERVISOR"]="1";env["SAGE_DIAGNOSTIC_OBSERVER_COMMAND"]=json.dumps(command);env["SAGE_DIAGNOSTIC_PROCESS_RECEIPT"]=str(receipt);env["SAGE_DIAGNOSTIC_SHUTDOWN_PATH"]=str(scratch/"shutdown")
                command=[str(Path(sys.executable).absolute()),"-P",str(Path(__file__).resolve())]
            isolated=isolation.isolated_command(command,writable_paths=[scratch,a.ledger.parent,lock_dir],environment=env,cwd=ROOT,read_only_paths=[ROOT])
            process=subprocess.Popen(isolated,env={"PATH":"/usr/bin:/bin"},cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
            def traffic():
                nonlocal nonlocal_seen
                sampled_pids.update(_descendants(process.pid));nonlocal_seen = nonlocal_seen or _nonlocal_connection(sampled_pids);return nonlocal_seen
            output_capture,marker=_process_output(process,active,traffic)
            if isinstance(marker,Mapping) and marker.get("awaiting_measurement") is True and fixture is not None:
                fixture.start_measurement(receiver.directory)
                (scratch/"measurement-started").touch(mode=0o600)
                marker=output_capture.wait_marker(active,traffic,previous=marker)
            rawout=bytes(output_capture.rows["stdout"]["raw"]);rawerr=bytes(output_capture.rows["stderr"]["raw"])
            diagnostic=marker.get("diagnostic") if isinstance(marker,dict) and marker.get("ready") is False else None
            child_pid=marker.get("pid") if isinstance(marker,dict) and marker.get("ready") is True else None
            if diagnostic is None and child_pid is None and process.poll() is not None:
                from ai.phoenix_process_compat import project_child_output
                diagnostic=project_child_output(rawout+rawerr,process.returncode)
            result["diagnostic"]=diagnostic;result["ownership"]={"namespace_pid":process.pid,"child_pid":child_pid}
            if isinstance(marker,Mapping):
                result["http"]=marker.get("http",{});result["ownership"]["listeners"]=marker.get("listeners",{});result["ownership"]["socket_evidence"]=marker.get("network",{})
                runtime_marker_ready=marker.get("ready") is True
        except BaseException as exc:
            if result["diagnostic"] is None:
                from ai.phoenix_process_compat import project_child_output
                result["diagnostic"]=project_child_output(traceback.format_exc().encode(),None)
        finally:
            if v5 and not active_ended:
                try:
                    ended=ledger.end_diagnostic_active(a.probe_id);total=float(ended["total_deadline_epoch"]);active_ended=True
                except Exception:active_ended=False
            if process is not None and process.poll() is None:
                try:
                    if a.stage=="B":(scratch/"shutdown").touch(mode=0o600)
                    else:process.send_signal(signal.SIGTERM)
                    process.wait(timeout=max(.1,total-time.time()))
                except Exception:
                    try:process.kill();process.wait(timeout=max(.1,total-time.time()))
                    except Exception:pass
            if output_capture is not None:
                output_state=output_capture.finish(total);rawout=output_state["stdout"]["raw"];rawerr=output_state["stderr"]["raw"]
            pump_settled,delivery_failures=_settle_pump(pump_stop,pump_thread,receiver,pump_failure,total)
            if 'receipt' in locals():observer_receipt=_read_receipt(receipt,min(total,time.time()+3))
            env2=dict(env);env2["SAGE_RESOURCE_BUDGET_PHASE"]="cleanup"
            # Marker/observer PIDs belong to the child's PID namespace. Host
            # cleanup uses only descendants sampled from the outer namespace.
            owned=set(sampled_pids)|({process.pid} if process is not None else set())
            teardown=min(total,time.time()+3)
            while any(Path(f"/proc/{pid}").exists() for pid in owned) and time.time()<teardown:time.sleep(.05)
            contained=(process is None or process.poll() is not None) and all(not Path(f"/proc/{pid}").exists() for pid in owned)
            result["contained"]=contained;result["observer"]={"receipt":observer_receipt,"delivery_failure":delivery_failures} if a.stage=="B" else None
            try:
                arch=subprocess.run([str(Path(sys.executable).absolute()),str(_ROLES["archive_runtime"]),"--repo-root",str(synthetic)],env=env2,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=max(.1,total-time.time())) if contained else None
            except Exception:arch=None
            archived=[]
            for path in synthetic.rglob("*.db*"):
                if path.name not in {"phoenix.db","phoenix.db-wal","phoenix.db-shm"}:archived.append({"relative_path":path.relative_to(synthetic).as_posix(),"size":path.stat().st_size,"sha256":_sha(path.read_bytes())})
            active_members=[path.name for path in working.glob("phoenix.db*")]
            result["cleanup"]={"archive_ok":arch is not None and arch.returncode==0 and bool(archived) and not active_members,"archive_stdout_sha256":_sha(arch.stdout) if arch else _sha(b""),"archive_stderr_sha256":_sha(arch.stderr) if arch else _sha(b""),"active_db_absent":not active_members,"active_db_members":active_members,"scratch_path":str(scratch),"archives":archived}
            if fixture is not None and isinstance(observer_receipt,Mapping):
                try:result["integrated_fixture"]=fixture.finish(observer_receipt,output_state,{"complete":bool(contained and result["cleanup"]["archive_ok"]),"steps":[]})
                except Exception:result["integrated_fixture"]={"fixture_only":True,"compiler_admissible":False,"error_type":"integrated_fixture_failure"}
            result["output_capture"]={name:{k:v[k] for k in ("bytes","sha256","complete","retained_bytes")} for name,v in output_state.items()}
            result["ownership"]["sampled_host_pids"]=sorted(sampled_pids);result["ownership"].pop("sampled_pids",None);result["ownership"]["nonlocal_connection_observed"]=bool(nonlocal_seen or result["ownership"].get("socket_evidence",{}).get("nonlocal",False))
            result["accounting"]={"active_end_recorded":active_ended if v5 else None};finished_epoch=time.time()
            controls=handler.control_receipt();result["control_receipt"]=controls if a.stage=="B" else None
            receipt_ok=delivery_ok=compiler_ok=composite_ok=None
            if a.stage=="B":
                critical={"deadline_exceeded","trace_byte_limit_exceeded","event_limit_exceeded","receiver_error","collector_error","containment_unverified","tracer_not_attested","lost_syscall_data","missing_root_process_end","missing_process_end","missing_resumed_syscall","partial_trace_frame"}
                receipt_ok=isinstance(observer_receipt,Mapping) and observer_receipt.get("tracer_attested") is True and observer_receipt.get("source_binding_sha256")==a.source_manifest_sha256 and not critical.intersection(observer_receipt.get("failures",[]))
                delivery_ok=pump_settled and not delivery_failures;compiler_ok=isinstance(result.get("integrated_fixture"),Mapping) and result["integrated_fixture"].get("compiler_admissible") is True
                composite_ok=isinstance(result.get("integrated_fixture"),Mapping) and (result["integrated_fixture"].get("composite") or {}).get("composite",{}).get("admission",{}).get("status")=="admitted"
            result["gate_dispositions"]=_reduce_final_gates(runtime_marker_ready=runtime_marker_ready,
                http_ready=result["http"].get("ready") is True,
                listeners_ready=result["ownership"].get("listeners")=={"http":True,"grpc":True},
                marker_no_nonlocal=result["ownership"].get("socket_evidence",{}).get("nonlocal") is False,
                active_end_recorded=active_ended or not v5,
                cleanup_no_nonlocal=not result["ownership"]["nonlocal_connection_observed"],
                processes_contained=result["contained"],archive_ok=result["cleanup"]["archive_ok"],
                cleanup_deadline_met=finished_epoch<=total,observer_receipt_valid=receipt_ok,
                receiver_delivery_complete=delivery_ok,control_correspondence=controls["correspondence"] if a.stage=="B" else None,
                compiler_admissible=compiler_ok,composite_admitted=composite_ok)
            result["passed"]=result["gate_dispositions"]["passed"]
    result["elapsed_seconds"]=time.time()-started
    return result

def main(argv=None)->int:
    if os.environ.get("SAGE_DIAGNOSTIC_NAMESPACE_SUPERVISOR")=="1":
        command=json.loads(os.environ["SAGE_DIAGNOSTIC_OBSERVER_COMMAND"]);os.environ["SAGE_DIAGNOSTIC_NAMESPACE_SUPERVISOR"]="0";process=subprocess.Popen(command,stdin=subprocess.DEVNULL)
        shutdown=Path(os.environ["SAGE_DIAGNOSTIC_SHUTDOWN_PATH"]);deadline=time.time()+720
        while process.poll() is None and time.time()<deadline:
            if shutdown.exists():
                try:process.send_signal(signal.SIGTERM)
                except ProcessLookupError:pass
                break
            time.sleep(.05)
        try:returncode=process.wait(timeout=max(.1,deadline-time.time()))
        except subprocess.TimeoutExpired:process.kill();returncode=process.wait()
        receipt=_read_receipt(Path(os.environ["SAGE_DIAGNOSTIC_PROCESS_RECEIPT"]),time.time()+5)
        return returncode if receipt is not None else 70
    if os.environ.get("SAGE_DIAGNOSTIC_CHILD")=="1":return _child()
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--approval",type=Path,required=True);p.add_argument("--approval-sha256",required=True);p.add_argument("--config",type=Path,required=True);p.add_argument("--ledger",type=Path,required=True);p.add_argument("--probe-id",required=True);p.add_argument("--stage",choices=("A","B"),required=True);p.add_argument("--output-dir",type=Path,required=True);p.add_argument("--source-manifest",type=Path,required=True);p.add_argument("--source-manifest-sha256",required=True);p.add_argument("--retry-evidence",type=Path);p.add_argument("--wasm-artifact",type=Path);p.add_argument("--wasm-artifact-sha256");p.add_argument("--go",action="store_true");a=p.parse_args(argv)
    _validate(a.approval,a.approval_sha256,a.config,a.ledger,a.source_manifest,a.source_manifest_sha256,a.stage,a.output_dir,a.probe_id,a.wasm_artifact,a.wasm_artifact_sha256)
    if not a.go:print(json.dumps({"schema":RESULT_SCHEMA,"probe_id":a.probe_id,"stage":a.stage,"action":"diagnostic"},sort_keys=True));return 0
    if not a.output_dir.is_dir() or stat.S_IMODE(a.output_dir.stat().st_mode)!=0o700:raise ProbeError("diagnostic output directory is invalid")
    result=_run(a);digest=_retain(a.output_dir/"result.json",result);print(json.dumps({"schema":result["schema"],"stage":result["stage"],"passed":result["passed"],"contained":result["contained"],"result_sha256":digest},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
