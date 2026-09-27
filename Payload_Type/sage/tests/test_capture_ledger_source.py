from __future__ import annotations
import hashlib, importlib.util, json, sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[3]
SCRIPT=ROOT/"skills/sage-eval-gauge/scripts/capture_ledger_source.py"
s=importlib.util.spec_from_file_location("capture_ledger_source_test",SCRIPT); source=importlib.util.module_from_spec(s); sys.modules[s.name]=source; s.loader.exec_module(source)
from ai import resource_budget

def setup(tmp_path):
 tmp_path.mkdir(parents=True,exist_ok=True)
 config=tmp_path/"config.json"; ledger=tmp_path/"ledger.jsonl"
 config.write_text(json.dumps({"schema":resource_budget.CONFIG_SCHEMA,"resources":{
  "episodes":{"limit":3,"cleanup_reserve":0},"provider_requests":{"limit":5,"cleanup_reserve":0},
  "provider_tokens":{"limit":100000,"cleanup_reserve":0},"control_requests":{"limit":10,"cleanup_reserve":2},
  "payload_task_attempts":{"limit":5,"cleanup_reserve":0}},"calendar_seconds":1000,"cleanup_seconds":100}))
 instance=resource_budget.ResourceLedger(config,ledger,now=lambda:100)
 instance.register_episode("episode",action_seconds=100,settlement_seconds=20,provider_token_limit=65536,
                           per_call_token_reservation=32768,payload_task_limit=2)
 instance.activate_episode("episode")
 return config,ledger,instance

def test_real_episode_records_join_settlements_and_global_slice(tmp_path):
 config,path,_=setup(tmp_path); scoped=resource_budget.ResourceLedger(config,path,now=lambda:101,episode_id="episode")
 provider=scoped.reserve({"provider_requests":1,"provider_tokens":32768},"provider")
 control=scoped.reserve({"control_requests":1},"control")
 scoped.settle_provider(provider,321)
 unscoped=resource_budget.ResourceLedger(config,path,now=lambda:102)
 global_id=unscoped.reserve({"control_requests":1},"observer")
 result=source.read_ledger_source(config,path,episode_id="episode",starting_seq=4)
 assert [row["reservation_id"] for row in result["reservations"]]==[provider,control]
 assert result["reservations"][0]["actual_tokens"]==321 and result["reservations"][0]["settlement_present"]
 assert result["reservations"][1]["settlement_present"] is False
 assert [row["reservation_id"] for row in result["global_reservations"]]==[control,global_id]
 assert result["episode_action_deadline_epoch"]==200 and result["episode_settlement_deadline_epoch"]==220
 assert result["source_sha256"]==hashlib.sha256(path.read_bytes()).hexdigest()

def test_measurement_projection_retains_setup_and_post_window_records(tmp_path):
 config,path,instance=setup(tmp_path);clock=[101]
 # Rebuild the episode with a controllable clock and a pre-activation control.
 path.unlink();instance=resource_budget.ResourceLedger(config,path,now=lambda:clock[0]);instance.register_episode("episode",action_seconds=100,settlement_seconds=20,provider_token_limit=65536,per_call_token_reservation=32768,payload_task_limit=2)
 scoped=resource_budget.ResourceLedger(config,path,now=lambda:clock[0],episode_id="episode");setup_id=scoped.reserve({"control_requests":1},"setup")
 start_seq=2;clock[0]=102;instance.activate_episode("episode");clock[0]=103
 measured_id=scoped.reserve({"provider_requests":1,"provider_tokens":32768},"measured");scoped.settle_provider(measured_id,123)
 clock[0]=104;post_id=scoped.reserve({"control_requests":1},"post")
 result=source.read_ledger_source(config,path,episode_id="episode",starting_seq=start_seq,measurement_end_ns=103_500_000_000)
 assert [row["reservation_id"] for row in result["reservations"]]==[setup_id,measured_id,post_id]
 assert [row["reservation_id"] for row in result["measured_reservations"]]==[measured_id]
 assert [row["reservation_id"] for row in result["global_reservations"]]==[setup_id,measured_id,post_id]

def test_unknown_and_overrun_are_retained(tmp_path):
 config,path,_=setup(tmp_path); scoped=resource_budget.ResourceLedger(config,path,now=lambda:101,episode_id="episode")
 first=scoped.reserve({"provider_requests":1,"provider_tokens":32768},"a"); scoped.settle_provider(first,None)
 result=source.read_ledger_source(config,path,episode_id="episode")
 assert result["unknown_provider_usage"] is True and result["reservations"][0]["actual_tokens"] is None
 config2,path2,_=setup(tmp_path/"two"); scoped2=resource_budget.ResourceLedger(config2,path2,now=lambda:101,episode_id="episode")
 rid=scoped2.reserve({"provider_requests":1,"provider_tokens":32768},"b")
 with pytest.raises(resource_budget.ResourceBudgetExceeded): scoped2.settle_provider(rid,32769)
 assert source.read_ledger_source(config2,path2,episode_id="episode")["provider_overrun"] is True

def test_wrong_episode_and_corrupt_duplicate_fail(tmp_path):
 config,path,_=setup(tmp_path)
 with pytest.raises(source.LedgerSourceError): source.read_ledger_source(config,path,episode_id="missing")
 events=path.read_text().splitlines(); duplicate=json.loads(events[-1]); duplicate["seq"]+=1
 with path.open("a") as handle: handle.write(json.dumps(duplicate)+"\n")
 with pytest.raises(source.LedgerSourceError): source.read_ledger_source(config,path,episode_id="episode")

def test_service_source_uses_only_current_stage_and_preserves_reserved_caps(tmp_path,monkeypatch):
 config=tmp_path/"config.json";ledger=tmp_path/"ledger.jsonl";config.write_text("{}")
 events=[
  {"seq":0,"type":"service_probe_start"},
  {"seq":1,"type":"diagnostic_stage_start","probe_id":"probe","stage":"A","started_epoch":10},
  {"seq":2,"type":"reservation","service_probe_id":"probe","reservation_id":"old","operation":"ready-a","amounts":{"control_requests":1},"cleanup":False,"reserved_epoch":11},
  {"seq":3,"type":"diagnostic_stage_close"},
  {"seq":4,"type":"diagnostic_stage_start","probe_id":"probe","stage":"B","started_epoch":20},
  {"seq":5,"type":"reservation","service_probe_id":"probe","reservation_id":"current","operation":"ready-b","amounts":{"control_requests":1},"cleanup":False,"reserved_epoch":21},
  {"seq":6,"type":"diagnostic_stage_active_end"},
  {"seq":7,"type":"diagnostic_stage_close"},
 ]
 ledger.write_text("".join(json.dumps(row,separators=(",",":"))+"\n" for row in events))
 class FakeLedger:
  def __init__(self,*_a,**_kw):pass
  def status(self):
   return {"next_sequence":len(events),"frozen_config_sha256":"c"*64,"started_epoch":0,
    "active_deadline_epoch":1000,"total_deadline_epoch":1200,
    "service_probes":{"probe":{"allocation":{"stage_active_seconds":600,"stage_cleanup_seconds":120},"stages":[
     {"stage":"A","started_epoch":10,"active_deadline_epoch":610,"active_ended_epoch":15,"total_deadline_epoch":135,"closed_epoch":16},
     {"stage":"B","started_epoch":20,"active_deadline_epoch":620,"active_ended_epoch":27,"total_deadline_epoch":147,"closed_epoch":30}]}},
    "outstanding_reservations":[],"unknown_provider_usage":False,"provider_overrun":False}
 monkeypatch.setattr(source.resource_budget,"ResourceLedger",FakeLedger)
 result=source.read_ledger_source(config,ledger,service_probe_id="probe")
 assert [row["reservation_id"] for row in result["reservations"]]==["current"]
 assert result["episode_registered"]["action_seconds"]==600
 assert result["episode_registered"]["settlement_seconds"]==120
 assert result["episode_action_deadline_epoch"]==27
 assert result["episode_reserved_action_deadline_epoch"]==620
 assert result["episode_actual_active_ended_epoch"]==27
 assert result["episode_settlement_deadline_epoch"]==147
 measured=source.read_ledger_source(config,ledger,service_probe_id="probe",starting_seq=6,
                                    measurement_start_ns=21_500_000_000,measurement_end_ns=22_500_000_000)
 assert [row["reservation_id"] for row in measured["reservations"]]==["current"]
 assert measured["measured_reservations"]==[] and measured["global_reservations"]==[]
