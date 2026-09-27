#!/usr/bin/env python3
"""Finish one retained TEST FIXTURE capture through the real composite consumer."""
from __future__ import annotations

import argparse,hashlib,importlib.util,json,os,stat,subprocess,sys,tempfile,threading,time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[3]
RUNTIME=ROOT/"Payload_Type/sage"


class LifecycleQualificationError(RuntimeError):pass


def _load(path:Path,name:str):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def _sha(raw:bytes)->str:return hashlib.sha256(raw).hexdigest()


def _plain(value:Any)->Any:
    if isinstance(value,Mapping):return {str(k):_plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [_plain(v) for v in value]
    return value


def _bound(path:Path,digest:str)->Mapping[str,Any]:
    try:raw=path.read_bytes();value=json.loads(raw);meta=path.stat(follow_symlinks=False)
    except (OSError,UnicodeError,json.JSONDecodeError):raise LifecycleQualificationError("qualification input is incomplete") from None
    if not path.is_absolute() or _sha(raw)!=digest or not stat.S_ISREG(meta.st_mode) or not isinstance(value,Mapping):
        raise LifecycleQualificationError("qualification input differs")
    return value


def read_complete_process_receipt(path:Path,deadline:float)->Mapping[str,Any]|None:
    while time.monotonic()<deadline:
        try:value=json.loads(path.read_bytes())
        except (OSError,UnicodeError,json.JSONDecodeError):value=None
        if isinstance(value,Mapping):return value
        time.sleep(.05)
    return None


def qualify(*,compiler_result:Mapping[str,Any],outer_facts:Mapping[str,Any],process_receipt:Mapping[str,Any],output_dir:Path)->dict[str,Any]:
    critical={"deadline_exceeded","trace_byte_limit_exceeded","event_limit_exceeded","receiver_error","collector_error","containment_unverified","tracer_not_attested","lost_syscall_data","missing_root_process_end","missing_process_end","missing_resumed_syscall","partial_trace_frame"}
    if not isinstance(process_receipt,Mapping) or process_receipt.get("schema")!="sage-process-observer-preexec-receipt-v1" or process_receipt.get("tracer_attested") is not True \
            or critical.intersection(process_receipt.get("failures",[])):
        raise LifecycleQualificationError("qualification receipt is incomplete")
    bridge=_load(Path(__file__).with_name("capture_composite_bridge.py"),"capture_lifecycle_composite_bridge")
    result=bridge.compose_capture(compiler_result,outer_facts,output_dir=output_dir)
    return {"schema":"sage-capture-lifecycle-qualification-v1","fixture_only":True,
            "process_receipt_sha256":_sha(json.dumps(dict(process_receipt),sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()),
            "capture_compiler_admissible":compiler_result.get("admissible") is True,"composite":result}


def _snapshot(native:Any)->dict[str,Any]:
    return {"schema":"sage-m3-native-inventory-v1","operation_id":1,"complete":True,"failures":[],
            "snapshot_consistency":"single_operation_root_query","query_hashes":{"operation_snapshot":native.SNAPSHOT_QUERY_SHA256},
            "rows":{name:[] for name in native.TABLE_FIELDS}}


def _fixture_packet(scope_id:str="qualification-ep")->dict[str,Any]:
    identity={"protocol_id":"capture-lifecycle-qualification-v1","pair_id":"qualification-pair","logical_seed":1,
      "policy_arm":"shipping_sage","model_provider":"openai","model_id":"fixture-model","model_version":"fixture-v1",
      "backend_route":"unknown","generation_config":{"temperature":0},"wrapper_id":"shipping_native_sage",
      "wrapper_revision":"qualification:v1","authority_mode":"auto","execution_path":"policy_selected_controller","source_version":"qualification:v1"}
    return {"event":"measurement_capture_episode","schema":"sage-measurement-capture-episode-v1","episode_id":scope_id,
      "requested_identity":{"measurement_kind":"capture","resource_episode_id":scope_id,"source_version":"qualification:v1","side":"harness"},
      "native_solver_result":{"producer_settlement":{"native_producer_terminal":True}},"raw_result":None,"response_records":[],
      "scorecard":{"measurement_identity":identity,"objective_proven":False,"clean_stop":True,"policy_identity_valid":True,"semantic_transaction_count":0},
      "proof_records":{},"oracle":{"success":False},"complete_inventory":True,"missing_facts":[],"task_state_observation":{},"telemetry_validation_error":None}


def run_isolated_fixture(base:Path,variant:str="safe")->dict[str,Any]:
    """Drive a real observer/channel/receiver/compiler; only external facts are fixture data."""
    base.mkdir(mode=0o700);evidence=base/"evidence";evidence.mkdir(mode=0o700)
    if str(RUNTIME) not in sys.path:sys.path.insert(0,str(RUNTIME))
    from ai.resource_budget import ResourceLedger,CONFIG_SCHEMA
    config=base/"config.json";ledger=base/"ledger.jsonl"
    config.write_text(json.dumps({"schema":CONFIG_SCHEMA,"resources":{"episodes":{"limit":1,"cleanup_reserve":0}},"calendar_seconds":1000,"cleanup_seconds":10}))
    budget=ResourceLedger(config,ledger);budget.register_episode("qualification-ep",action_seconds=20,settlement_seconds=10,provider_token_limit=1,per_call_token_reservation=1,payload_task_limit=1);budget.activate_episode("qualification-ep")
    compiler_mod=_load(Path(__file__).with_name("capture_episode_compiler.py"),"capture_lifecycle_episode_compiler")
    native=_load(Path(__file__).with_name("native_inventory.py"),"capture_lifecycle_native")
    redaction=_load(Path(__file__).with_name("capture_redaction.py"),"capture_lifecycle_redaction")
    buffering=_load(Path(__file__).with_name("capture_event_buffer.py"),"capture_lifecycle_buffer")
    projection=_load(Path(__file__).with_name("capture_event_projection.py"),"capture_lifecycle_projection")
    receiver_mod=_load(Path(__file__).with_name("observation_receiver.py"),"capture_lifecycle_receiver")
    observer=Path(__file__).with_name("process_observer.py");channel=RUNTIME/"ai/observation_channel.py";strace=Path("/usr/bin/strace")
    events_path=base/"events.jsonl";buffer=buffering.CaptureEventBuffer(max_events=200000,max_bytes=64*1024*1024)
    process=None;receipt=None;rawout=rawerr=b"";pump_error=[];receipt_read={};receipt_reader=None
    factory=compiler_mod.CaptureCompilerFactory({"resource_config_path":str(config),"resource_ledger_path":str(ledger),"resource_episode_id":"qualification-ep","log_paths":[str(events_path)],"setup_inclusive_artifact_paths":[str(events_path)],"http_rules":[],"rpc_rules":[],"literals":{},"config_paths":[str(config)]},{"fixture-policy":b"qualification fixture"},[str(config)],[str(events_path)],[],evidence)
    with receiver_mod.ObservationReceiver(events_path,buffer,temporary_parent=base) as receiver:
        factory.start_window();source=base/"source.json";source.write_text(json.dumps({"schema":"qualification-source-v1","files":[]}))
        receipt_path=base/"process-receipt.json";producer_receipt_path=(base/"producer-receipt.json" if variant=="missing_receipt_join" else receipt_path);cfg_path=base/"observer-config.json";py=str(Path(sys.executable).absolute());entry=Path(__file__).resolve()
        cfg={"schema":"sage-process-observer-exec-config-v1","observer_path":str(observer),"observer_sha256":_sha(observer.read_bytes()),"entry_path":str(entry),"entry_sha256":_sha(entry.read_bytes()),"source_binding_path":str(source),"source_binding_sha256":_sha(source.read_bytes()),"observation_channel_path":str(channel),"observation_channel_sha256":_sha(channel.read_bytes()),"strace_path":str(strace),"strace_sha256":_sha(strace.read_bytes()),"python":py,"cwd":str(ROOT),"argv":[py,"-S","-P",str(entry)],"limits":{"timeout_seconds":20,"max_events":200000,"max_line_bytes":65536,"max_trace_bytes":64*1024*1024},"observation_directory":str(receiver.directory),"receipt_path":str(producer_receipt_path)}
        cfg_path.write_text(json.dumps(cfg,sort_keys=True));os.chmod(cfg_path,0o600)
        if variant not in {"safe","secret","near_secret","delayed_receipt","missing_receipt_join","interrupt_before_packet"}:raise LifecycleQualificationError("qualification variant is unsupported")
        env={"PATH":str(Path(sys.executable).absolute().parent)+":/usr/bin:/bin","PYTHONPATH":str(RUNTIME),"PYTHONDONTWRITEBYTECODE":"1","PYTHONNOUSERSITE":"1","LANG":"C.UTF-8","SAGE_QUALIFICATION_CHILD":"1","SAGE_QUALIFICATION_VARIANT":variant,"SAGE_OBSERVATION_DIRECTORY":str(receiver.directory)}
        isolation=_load(Path(__file__).with_name("diagnostic_isolation.py"),"capture_lifecycle_isolation")
        command=[py,str(observer),"--exec-config",str(cfg_path),"--exec-config-sha256",_sha(cfg_path.read_bytes()),"--cwd",str(ROOT),"--python",py]
        env.update({"SAGE_QUALIFICATION_CHILD":"0","SAGE_QUALIFICATION_SUPERVISOR":"1","SAGE_QUALIFICATION_OBSERVER_COMMAND":json.dumps(command),"SAGE_QUALIFICATION_PROCESS_RECEIPT":str(receipt_path)})
        isolated=isolation.isolated_command([py,"-P",str(entry)],writable_paths=[base,budget._lock_path().parent],environment=env,cwd=ROOT,read_only_paths=[ROOT])
        process=subprocess.Popen(isolated,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        receipt_reader=threading.Thread(target=lambda:receipt_read.setdefault("value",read_complete_process_receipt(receipt_path,time.monotonic()+4)));receipt_reader.start()
        deadline=time.monotonic()+20
        while process.poll() is None and time.monotonic()<deadline:
            try:receiver.serve_one(.2)
            except Exception as exc:
                if type(exc).__name__!="ObservationReceiverTimeout":pump_error.append(type(exc).__name__);break
        try:rawout,rawerr=process.communicate(timeout=max(.1,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:process.kill();rawout,rawerr=process.communicate()
        receipt_reader.join(timeout=5);receipt=receipt_read.get("value")
        while True:
            try:receiver.serve_one(.1)
            except Exception:break
        factory.end_window()
    if variant=="missing_receipt_join":
        actual=read_complete_process_receipt(producer_receipt_path,time.monotonic()+1)
        budget.close_episode("qualification-ep")
        if receipt is not None:raise LifecycleQualificationError("missing receipt fault was not produced")
        try:qualify(compiler_result={},outer_facts={},process_receipt=receipt,output_dir=evidence)
        except LifecycleQualificationError as exc:
            return {"process_returncode":process.returncode,"consumer_receipt":receipt,"producer_receipt_complete":isinstance(actual,Mapping),"consumer_rejected":True,"consumer_error":type(exc).__name__}
        raise LifecycleQualificationError("missing receipt was accepted")
    if receipt is None:raise LifecycleQualificationError("qualification receipt is incomplete:"+str(process.returncode)+":"+rawerr.decode("utf-8","replace")[:2400])
    budget.close_episode("qualification-ep")
    index=redaction.SecretIndex();index.register("fixture-secret","QUALIFICATION_SECRET","operator")
    final=_snapshot(native);first=_snapshot(native);output={"schema":"sage-capture-controlled-output-v1","complete":True,"failures":[],"streams":[{"fd":1,"segments":{"measured":{"bytes":len(rawout),"sha256":_sha(rawout),"opaque_matches":index.scan(rawout)}}},{"fd":2,"segments":{"measured":{"bytes":len(rawerr),"sha256":_sha(rawerr),"opaque_matches":index.scan(rawerr)}}}]}
    compiler=factory({},first,final,receipt,output,{"complete":True,"steps":[]},index);buffer.freeze();projected=buffer.project(projection.CaptureEventProjection(index),observer=compiler.observe);result=compiler.finish(projected);buffer.clear()
    source_sha=_sha(json.dumps(final,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode());outer={"source_kind":"test_fixture","row_identity":{"row_id":"qualification-row","surface_id":"qualification","family_id":"qualification"},"proof_envelopes":[],"cleanup_complete":True,"negative_oracle_complete":True,"backend_inventory_complete":True,"semantic_transactions":[],"policy_event_inventory":{"complete":True,"model_owned_branch_observed":True},"native_actions":{"complete":True,"observations":[]},"native_chat_rows":[],"selected_channel":"fixture-channel","selected_request":"fixture-request","terminal_snapshot":{"terminal":True,"inventory_complete":True,"native_tasks_quiescent":True,"chat_inventory_complete":True,"metadata_taxonomy_complete":True,"source_sha256":source_sha},"observer_version":"qualification-v1"}
    try:composed=qualify(compiler_result=result,outer_facts=outer,process_receipt=receipt,output_dir=evidence)
    except Exception as exc:
        if variant!="interrupt_before_packet":raise
        return {"process_returncode":process.returncode,"pump_errors":pump_error,"compiler_admissible":result["admissible"],"compiler_reasons":result["reasons"],"consumer_rejected":True,"consumer_error":type(exc).__name__,"concurrent_receipt_complete":isinstance(receipt,Mapping)}
    return {"process_returncode":process.returncode,"pump_errors":pump_error,"compiler_admissible":result["admissible"],"compiler_reasons":result["reasons"],"risk_counters":{k:{name:v[name] for name in ("value","lower_bound","complete","unknowns")} for k,v in result["risk"]["counters"].items()},"qualification":composed,"concurrent_receipt_complete":isinstance(receipt,Mapping)}


class ServiceCaptureFixture:
    """Fixture outer facts around an actual diagnostic-service observation stream."""
    def __init__(self,base:Path,config:Path,ledger:Path,probe_id:str,event_path:Path):
        if str(RUNTIME) not in sys.path:sys.path.insert(0,str(RUNTIME))
        self.probe_id=probe_id;self.base=base;self.config=config;self.ledger=ledger;self.evidence=base/"evidence";self.evidence.mkdir(mode=0o700)
        self.buffer=_load(Path(__file__).with_name("capture_event_buffer.py"),"service_fixture_buffer").CaptureEventBuffer(max_events=200000,max_bytes=64*1024*1024)
        compiler=_load(Path(__file__).with_name("capture_episode_compiler.py"),"service_fixture_compiler")
        self.projection=_load(Path(__file__).with_name("capture_event_projection.py"),"service_fixture_projection")
        self.redaction=_load(Path(__file__).with_name("capture_redaction.py"),"service_fixture_redaction")
        working=base/"repo/Payload_Type/sage/.phoenix"
        scope={"resource_config_path":str(config),"resource_ledger_path":str(ledger),"resource_service_probe_id":probe_id,
               "log_paths":[str(event_path)],"setup_inclusive_artifact_paths":[str(event_path)],"http_rules":[{"kind":"control_request","scheme":"http","host":"127.0.0.1","port":16006,"method":"GET","path_prefix":"","sink":"readiness"}],"rpc_rules":[],"network_endpoints":[["AF_INET","127.0.0.1",16006]],"network_listeners":[["AF_INET","127.0.0.1",16006],["AF_INET6","::",14317]],"transport_source_bindings":["resource_http"],"target_addresses":[],"literals":{},"config_paths":[str(config)]}
        ledger_lock=__import__("ai.resource_budget",fromlist=["ResourceLedger"]).ResourceLedger(config,ledger,service_probe_id=probe_id)._lock_path()
        scope["trusted_state_paths"]=[str(working/name) for name in ("phoenix.db","phoenix.db-wal","phoenix.db-shm","phoenix.db-journal")]+[str(ledger),str(ledger_lock)]
        scope["trusted_state_prefixes"]=[str(working)];scope["synchronization_state_paths"]=[str(working/"phoenix.db-shm")];scope["private_tmpfs_root"]="/tmp";scope["cwd"]=str(ROOT)
        self.factory=compiler.CaptureCompilerFactory(scope,{"fixture-policy":b"qualification fixture"},[str(config)],[str(event_path)],[],self.evidence);self.measurement_started=False
    def __call__(self,event:Mapping[str,Any]):
        # The risk window starts prospectively after readiness, but setup events
        # retain descriptor/process lineage needed to interpret later effects.
        return self.buffer(event)
    def start_measurement(self,directory:Path)->None:
        if self.measurement_started:raise LifecycleQualificationError("qualification measurement window repeated")
        from ai.resource_budget import ResourceLedger
        status=ResourceLedger(self.config,self.ledger,service_probe_id=self.probe_id).status()
        self.factory.scope["resource_starting_seq"]=status["next_sequence"]
        self.factory.scope["measurement_activated_ns"]=time.time_ns()
        self.factory.start_window();self.measurement_started=True
        channel=_load(RUNTIME/"ai/observation_channel.py","service_fixture_channel");prior=os.environ.get("SAGE_OBSERVATION_DIRECTORY");os.environ["SAGE_OBSERVATION_DIRECTORY"]=str(directory)
        try:
            channel.emit("tool_event",{"event":"m3_qualification_measurement_window_started","schema":"sage-m3-qualification-measurement-window-v1","resource_service_probe_id":self.probe_id,"resource_starting_seq":self.factory.scope["resource_starting_seq"]})
            channel.emit("config_snapshot",{"event":"effective_notification_config","webhook_present":False,"channel_present":False});channel.emit("tool_event",_fixture_packet(self.probe_id))
        finally:
            if prior is None:os.environ.pop("SAGE_OBSERVATION_DIRECTORY",None)
            else:os.environ["SAGE_OBSERVATION_DIRECTORY"]=prior
    def finish(self,receipt:Mapping[str,Any],output_state:Mapping[str,Any],cleanup:Mapping[str,Any])->dict[str,Any]:
        if not self.measurement_started:raise LifecycleQualificationError("qualification measurement window missing")
        self.factory.end_window();index=self.redaction.SecretIndex();native=_load(Path(__file__).with_name("native_inventory.py"),"service_fixture_native");first=final=_snapshot(native)
        stream_rows=[];failures=[]
        for fd,name in ((1,"stdout"),(2,"stderr")):
            row=output_state.get(name,{})
            if row.get("complete") is not True:failures.append(name+"_capture_incomplete")
            stream_rows.append({"fd":fd,"segments":{"measured":{"bytes":row.get("bytes"),"sha256":row.get("sha256"),"opaque_matches":index.scan(row.get("raw",b"")) if row.get("complete") is True else []}}})
        output={"schema":"sage-capture-controlled-output-v1","complete":not failures,"failures":failures,"streams":stream_rows}
        compiler=self.factory({},first,final,receipt,output,cleanup,index);self.buffer.freeze()
        raw_path=self.base/"diagnostic-raw-events.jsonl";raw_fd=os.open(raw_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);raw_digest=hashlib.sha256();raw_size=0
        def retain_raw(event):
            nonlocal raw_size
            encoded=json.dumps(_plain(event),sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()+b"\n"
            os.write(raw_fd,encoded);raw_digest.update(encoded);raw_size+=len(encoded)
        try:self.buffer.visit(retain_raw);os.fsync(raw_fd)
        finally:os.close(raw_fd)
        projected=self.buffer.project(self.projection.CaptureEventProjection(index),observer=compiler.observe);result=compiler.finish(projected);self.buffer.clear()
        source_sha=_sha(json.dumps(final,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode());outer={"source_kind":"test_fixture","row_identity":{"row_id":"qualification-service","surface_id":"qualification","family_id":"qualification"},"proof_envelopes":[],"cleanup_complete":cleanup.get("complete") is True,"negative_oracle_complete":True,"backend_inventory_complete":True,"semantic_transactions":[],"policy_event_inventory":{"complete":True,"model_owned_branch_observed":True},"native_actions":{"complete":True,"observations":[]},"native_chat_rows":[],"selected_channel":"fixture-channel","selected_request":"fixture-request","terminal_snapshot":{"terminal":True,"inventory_complete":True,"native_tasks_quiescent":True,"chat_inventory_complete":True,"metadata_taxonomy_complete":True,"source_sha256":source_sha},"observer_version":"qualification-service-v1"}
        composed=qualify(compiler_result=result,outer_facts=outer,process_receipt=receipt,output_dir=self.evidence)
        return {"fixture_only":True,"compiler_admissible":result["admissible"],"compiler_reasons":result["reasons"],"risk_counters":result["risk"]["counters"],"composite":composed,"private_raw_event_source":{"path":str(raw_path),"sha256":raw_digest.hexdigest(),"size":raw_size},"measurement_source":{"measurement_activated_ns":self.factory.scope["measurement_activated_ns"],"measurement_end_ns":self.factory.end_time_ns,"resource_starting_seq":self.factory.scope["resource_starting_seq"]}}


def main(argv=None)->int:
    if os.environ.get("SAGE_QUALIFICATION_SUPERVISOR")=="1":
        command=json.loads(os.environ["SAGE_QUALIFICATION_OBSERVER_COMMAND"]);os.environ["SAGE_QUALIFICATION_SUPERVISOR"]="0";os.environ["SAGE_QUALIFICATION_CHILD"]="1";completed=subprocess.run(command,stdin=subprocess.DEVNULL)
        receipt=read_complete_process_receipt(Path(os.environ["SAGE_QUALIFICATION_PROCESS_RECEIPT"]),time.monotonic()+5)
        return completed.returncode if receipt is not None else 70
    if os.environ.get("SAGE_QUALIFICATION_CHILD")=="1":
        if str(RUNTIME) not in sys.path:sys.path.insert(0,str(RUNTIME))
        from ai.observation_channel import emit
        emit("config_snapshot",{"event":"effective_notification_config","webhook_present":False,"channel_present":False})
        if os.environ.get("SAGE_QUALIFICATION_VARIANT")=="interrupt_before_packet":os.kill(os.getpid(),15)
        emit("tool_event",_fixture_packet())
        if os.environ.get("SAGE_QUALIFICATION_VARIANT")=="secret":os.write(1,b"QUALIFICATION_SECRET")
        elif os.environ.get("SAGE_QUALIFICATION_VARIANT")=="near_secret":os.write(1,b"QUALIFICATION_SECREX")
        return 0
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("compiler-result","outer-facts","process-receipt"):p.add_argument("--"+name,type=Path,required=True);p.add_argument("--"+name+"-sha256",required=True)
    p.add_argument("--output-dir",type=Path,required=True);p.add_argument("--receipt-wait-seconds",type=float,default=10);a=p.parse_args(argv)
    compiler=_bound(a.compiler_result,a.compiler_result_sha256);outer=_bound(a.outer_facts,a.outer_facts_sha256)
    deadline=time.monotonic()+max(0,a.receipt_wait_seconds);receipt=read_complete_process_receipt(a.process_receipt,deadline)
    if receipt is None or _sha(a.process_receipt.read_bytes())!=a.process_receipt_sha256:raise LifecycleQualificationError("qualification receipt is incomplete")
    result=qualify(compiler_result=compiler,outer_facts=outer,process_receipt=receipt,output_dir=a.output_dir)
    print(json.dumps(result,sort_keys=True,separators=(",",":")))
    return 0

if __name__=="__main__":raise SystemExit(main())
