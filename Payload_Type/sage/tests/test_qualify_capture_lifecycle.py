from __future__ import annotations
import importlib.util,json,sys,threading,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
def _load():
 s=importlib.util.spec_from_file_location("qualify_capture_lifecycle_test",ROOT/"skills/sage-eval-gauge/scripts/qualify_capture_lifecycle.py");m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m);return m

def test_actual_receipt_reader_waits_for_completed_json(tmp_path):
 m=_load();path=tmp_path/"receipt.json";path.write_bytes(b"")
 value={"schema":"sage-process-observer-preexec-receipt-v1","tracer_attested":True,"failures":[]}
 thread=threading.Thread(target=lambda:(time.sleep(.1),path.write_text(json.dumps(value))));thread.start()
 started=time.monotonic();assert m.read_complete_process_receipt(path,time.monotonic()+1)==value
 thread.join(timeout=1);assert not thread.is_alive() and time.monotonic()-started>=.08

def test_actual_receipt_reader_fails_closed_for_missing_or_truncated(tmp_path):
 m=_load();path=tmp_path/"receipt.json";path.write_text('{"schema":')
 assert m.read_complete_process_receipt(path,time.monotonic()+.1) is None

def test_isolated_raw_events_drive_compiler_and_exact_secret_near_match(tmp_path):
 m=_load();safe=m.run_isolated_fixture(tmp_path/"safe","safe");harm=m.run_isolated_fixture(tmp_path/"harm","secret");near=m.run_isolated_fixture(tmp_path/"near","near_secret")
 assert safe["compiler_admissible"] and not any(x["unknowns"] for x in safe["risk_counters"].values())
 assert safe["qualification"]["composite"]["admission"]["status"]=="admitted"
 assert harm["risk_counters"]["secret_exposure_count"]["value"]==2
 assert near["risk_counters"]["secret_exposure_count"]["value"]==0

def test_full_path_delayed_receipt_completes_from_actual_observer(tmp_path):
 m=_load();result=m.run_isolated_fixture(tmp_path/"delayed","delayed_receipt")
 assert result["concurrent_receipt_complete"] is True and result["compiler_admissible"] is True
 assert result["qualification"]["composite"]["admission"]["status"]=="admitted"

def test_full_path_missing_receipt_join_stays_rejected(tmp_path):
 m=_load();result=m.run_isolated_fixture(tmp_path/"missing","missing_receipt_join")
 assert result=={"process_returncode":70,"consumer_receipt":None,"producer_receipt_complete":True,"consumer_rejected":True,"consumer_error":"LifecycleQualificationError"}

def test_full_path_interrupted_child_reaches_consumer_rejection(tmp_path):
 m=_load();result=m.run_isolated_fixture(tmp_path/"interrupted","interrupt_before_packet")
 assert result["process_returncode"]!=0 and result["concurrent_receipt_complete"] is True
 assert result["compiler_admissible"] is False and result["consumer_rejected"] is True
