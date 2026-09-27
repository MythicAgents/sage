from __future__ import annotations
import hashlib, importlib.util, json, sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[3]
def load(path,name): s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);sys.modules[name]=m;s.loader.exec_module(m);return m
compiler=load(ROOT/"skills/sage-eval-gauge/scripts/capture_episode_compiler.py","capture_episode_compiler_test")
native=load(ROOT/"skills/sage-eval-gauge/scripts/native_inventory.py","native_episode_test")
redaction=load(ROOT/"skills/sage-eval-gauge/scripts/capture_redaction.py","redaction_episode_test")
buffering=load(ROOT/"skills/sage-eval-gauge/scripts/capture_event_buffer.py","buffer_episode_test")
projection=load(ROOT/"skills/sage-eval-gauge/scripts/capture_event_projection.py","projection_episode_test")
from ai import resource_budget

def resources(tmp_path,activate=True):
 tmp_path.mkdir(parents=True,exist_ok=True)
 c=tmp_path/"config.json";l=tmp_path/"ledger.jsonl";c.write_text(json.dumps({"schema":resource_budget.CONFIG_SCHEMA,"resources":{"episodes":{"limit":1,"cleanup_reserve":0},"control_requests":{"limit":2,"cleanup_reserve":0}},"calendar_seconds":1000,"cleanup_seconds":10}))
 r=resource_budget.ResourceLedger(c,l,now=lambda:100);r.register_episode("ep",action_seconds=10,settlement_seconds=10,provider_token_limit=10,per_call_token_reservation=10,payload_task_limit=1)
 if activate:r.activate_episode("ep")
 return c,l
def snapshot(): return {"schema":"sage-m3-native-inventory-v1","operation_id":1,"complete":True,"failures":[],"snapshot_consistency":"single_operation_root_query","query_hashes":{"operation_snapshot":native.SNAPSHOT_QUERY_SHA256},"rows":{x:[] for x in native.TABLE_FIELDS}}
def scope(c,l,log): return {"resource_config_path":str(c),"resource_ledger_path":str(l),"resource_episode_id":"ep","log_paths":[str(log)],"setup_inclusive_artifact_paths":[str(log)],"http_rules":[],"rpc_rules":[],"literals":{},"config_paths":[str(c)]}
def receipt(): return {"schema":"sage-process-observer-preexec-receipt-v1","tracer_attested":True,"processes_observed":1,"processes_ended":1,"observed_pids":[44],"ended_pids":[44],"event_count":0,"suppressed_observation_io_count":0,"failures":[],"tracee_pid":44,"stream_metadata":{"missing":True}}
def event(seq=1): return {"schema":"sage-observation-event-v2","event_id":f"{seq:032x}","process_uuid":"1"*32,"pid":44,"seq":seq,"time_ns":100000000001,"kind":"tool_event","payload":{"event":"measurement_capture_episode","episode_id":"ep","secret":"SYNTHETIC_SECRET"}}

def test_factory_actual_sources_packet_redaction_and_artifact_prefix(tmp_path):
 c,l=resources(tmp_path);log=tmp_path/"log";secrets=redaction.SecretIndex();secrets.register("h:operator","SYNTHETIC_SECRET","operator")
 evidence=tmp_path/"evidence";evidence.mkdir(mode=0o700)
 f=compiler.CaptureCompilerFactory(scope(c,l,log),{"policy":b"static"},[str(c)],[str(log)],[],evidence)
 f.start_window();log.write_bytes(b"SYNTHETIC_SECRET");f.end_window()
 ep=f({},snapshot(),snapshot(),receipt(),{}, {},secrets);ep.observe(event())
 result=ep.finish([])
 assert result["episode_packet_sha256"] and "SYNTHETIC_SECRET" not in json.dumps(result)
 assert result["projected_event_count"]==0 and result["admissible"] is False
 assert result["measurement_source"]=={
  "start_ns":ep.risk.scope["start_ns"],"end_ns":ep.risk.scope["end_ns"],"resource_episode_id":"ep"}
 assert set(result["retention"]["artifacts"])=={"native-start.json","native-final.json","projected-events.json","episode-packet.json"}
 assert all(Path(row["path"]).stat().st_mode&0o777==0o600 for row in result["retention"]["artifacts"].values())
 assert "SYNTHETIC_SECRET" not in "".join(Path(row["path"]).read_text() for row in result["retention"]["artifacts"].values())
 f3=compiler.CaptureCompilerFactory(scope(c,l,log),{},[str(c)],[str(log)],[]);f3.start_window();f3.end_window();ep3=f3({},snapshot(),snapshot(),receipt(),{}, {},secrets)
 buf=buffering.CaptureEventBuffer(max_events=2,max_bytes=10000);buf(event());buf.freeze();buf.visit(f3.secret_registrar(secrets))
 projected=buf.project(projection.CaptureEventProjection(secrets),observer=ep3.observe);assert ep3.finish(projected)["projected_event_count"]==1

def test_duplicate_packet_and_missing_activation_fail_closed(tmp_path):
 c,l=resources(tmp_path);log=tmp_path/"optional";idx=redaction.SecretIndex()
 f=compiler.CaptureCompilerFactory(scope(c,l,log),{},[str(c)],[str(log)],[]);f.start_window();f.end_window();ep=f({},snapshot(),snapshot(),receipt(),{}, {},idx)
 ep.observe(event(1));ep.observe(event(2)); assert "duplicate_episode_packet" in ep.finish([])["reasons"]
 c2,l2=resources(tmp_path/"pending",activate=False);f2=compiler.CaptureCompilerFactory(scope(c2,l2,tmp_path/"pending"/"log"),{},[str(c2)],[str(tmp_path/"pending"/"log")],[]);f2.start_window();f2.end_window()
 with pytest.raises(compiler.CaptureCompilerError): f2({},snapshot(),snapshot(),receipt(),{}, {},idx)

def test_actual_ledger_compiler_join_uses_activation_window_reservations(tmp_path):
 c,l=resources(tmp_path);l.unlink();cv=json.loads(c.read_text());cv["resources"]["control_requests"]["limit"]=3;c.write_text(json.dumps(cv));clock=[100];budget=resource_budget.ResourceLedger(c,l,now=lambda:clock[0]);budget.register_episode("ep",action_seconds=100,settlement_seconds=20,provider_token_limit=65536,per_call_token_reservation=32768,payload_task_limit=2)
 scoped=resource_budget.ResourceLedger(c,l,now=lambda:clock[0],episode_id="ep");setup=scoped.reserve({"control_requests":1},"setup");start=budget.status()["next_sequence"]
 evidence=tmp_path/"evidence";evidence.mkdir(mode=0o700);scope_value={**scope(c,l,tmp_path/"unused"),"resource_starting_seq":start,"log_paths":[],"setup_inclusive_artifact_paths":[],"http_rules":[{"kind":"control_request","scheme":"http","host":"127.0.0.1","port":6006,"method":"GET","path_prefix":"/","sink":"readiness"}]}
 f=compiler.CaptureCompilerFactory(scope_value,{"policy":b"ordinary generic strategy"},[str(c)],[],[],evidence);f.start_window();clock[0]=102;budget.activate_episode("ep");clock[0]=103;measured=scoped.reserve({"control_requests":1},"measured");clock[0]=104;post=scoped.reserve({"control_requests":1},"post");f.end_window();f.end_time_ns=103_500_000_000
 streams=[{"fd":fd,"target":f"pipe:[{fd}]","device":7,"inode":fd,"type":"fifo","missing":False} for fd in (1,2)];stream_event={**event(1),"time_ns":101_000_000_000,"payload":{"event":"observed_process_streams","tracee_pid":44,"streams":streams}}
 config_event={**event(2),"time_ns":101_500_000_000,"kind":"config_snapshot","payload":{"event":"effective_notification_config","webhook_present":False,"channel_present":False}};packet={**event(3),"time_ns":103_100_000_000};packet["payload"]["secret"]=""
 request={**event(4),"time_ns":103_200_000_000,"kind":"control_request","payload":{"request_id":"request","reservation_id":measured,"url":"http://127.0.0.1:6006/","method":"GET"}};response={**event(5),"time_ns":103_300_000_000,"kind":"control_response","payload":{"request_id":"request","reservation_id":measured,"outcome":"returned"}}
 observer_receipt={**receipt(),"stream_metadata":{"descriptors":[{"fd":fd,"target_sha256":hashlib.sha256(f"pipe:[{fd}]".encode()).hexdigest(),"device":7,"inode":fd,"type":"fifo"} for fd in (1,2)]}}
 empty=hashlib.sha256(b"").hexdigest();output={"schema":"sage-capture-controlled-output-v1","complete":True,"failures":[],"streams":[{"fd":fd,"segments":{"measured":{"bytes":0,"sha256":empty,"opaque_matches":[]}}} for fd in (1,2)]}
 ep=f({},snapshot(),snapshot(),observer_receipt,output,{},redaction.SecretIndex())
 for row in (stream_event,config_event,packet,request,response):ep.observe(row)
 result=ep.finish([]);transport=result["risk"]["sink_proofs"]["transport"]
 assert [row["reservation_id"] for row in ep.ledger["reservations"]]==[setup,measured,post]
 assert [row["reservation_id"] for row in ep.ledger["measured_reservations"]]==[measured]
 assert transport["reservation_count"]==1 and "request_reservation_cardinality_mismatch" not in result["risk"]["counters"]["unauthorized_live_request_count"]["unknowns"]
