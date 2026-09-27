from __future__ import annotations

import hashlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-eval-gauge/scripts/phoenix_startup_probe.py"


def _module():
    spec = importlib.util.spec_from_file_location("sage_m3_phoenix_startup_probe_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _args(module, tmp_path: Path) -> list[str]:
    manifest = {
        "schema": module.SOURCE_SCHEMA,
        "files": [
            {"role": role, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for role, path in module._ROLES.items()
        ],
    }
    raw = json.dumps(manifest, sort_keys=True).encode()
    source = tmp_path / "source.json"
    source.write_bytes(raw)
    config = tmp_path / "budget.json"; config.write_text('{"fixture":"v4"}')
    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("")
    approval_value = {
        "schema": "sage-resource-recovery-amendment-v3", "status": "approved",
        "amendment_ordinal": 3, "model": None,
        "base_config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "limit_changes": {"capture_correction_cycles": {"old": 5, "new": 6}},
        "diagnostic_session": {"stages": ["A", "B", "C"], "starts_per_stage": 2,
            "max_stage_starts": 6, "stage_active_seconds": 120, "stage_total_seconds": 180},
    }
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(approval_value))
    return [
        "--approval", str(approval),
        "--approval-sha256", hashlib.sha256(approval.read_bytes()).hexdigest(),
        "--config", str(config),
        "--ledger", str(ledger), "--probe-id", "m3-diagnostic-v4-session-01", "--stage", "A", "--output-dir", str(tmp_path),
        "--source-manifest", str(source), "--source-manifest-sha256", hashlib.sha256(raw).hexdigest(),
    ]


def test_no_go_validates_without_importing_or_launching_phoenix(tmp_path, capsys):
    module = _module()
    before = {name for name in sys.modules if name == "phoenix" or name.startswith("phoenix.")}
    assert module.main(_args(module, tmp_path)) == 0
    assert {name for name in sys.modules if name == "phoenix" or name.startswith("phoenix.")} == before
    assert '"action": "diagnostic"' in capsys.readouterr().out
    assert not (tmp_path / "result.json").exists()


def test_no_go_rejects_changed_source_manifest(tmp_path):
    module = _module()
    args = _args(module, tmp_path)
    args[args.index("--source-manifest-sha256") + 1] = "0" * 64
    with pytest.raises(module.ProbeError, match="binding differs"):
        module.main(args)


def test_living_inert_child_marker_survives_large_output_drain():
    module = _module()
    marker = {
        "ready": False,
        "diagnostic": {"status": "failure", "error_category": "alive_but_unready"},
    }
    code = (
        "import json,os,time;"
        "os.write(1,b'SYNTHETIC_SECRET'+b'x'*131072+b'\\n');"
        f"os.write(1,{module._CHILD_MARKER!r}+json.dumps({marker!r}).encode()+b'\\n');"
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-P", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        capture, projected = module._process_output(process, time.time() + 2, lambda: False)
        retained=bytes(capture.rows["stdout"]["raw"]);error=bytes(capture.rows["stderr"]["raw"])
        assert len(retained) == 65_536 and error == b""
        assert projected == marker and "SYNTHETIC_SECRET" not in repr(projected)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=2)
        state=capture.finish(time.time()+1)
        assert state["stdout"]["bytes"]>131072 and state["stdout"]["complete"] is False

def test_post_marker_tail_is_drained_and_cannot_claim_complete_secret_scan():
    module=_module();marker={"ready":True,"pid":12,"http":{"ready":True},"listeners":{"http":True,"grpc":True},"network":{"nonlocal":False}}
    code=(f"import json,os,time;os.write(1,{module._CHILD_MARKER!r}+json.dumps({marker!r}).encode()+b'\\n');"+"os.write(1,b'x'*70000+b'QUALIFICATION_SECRET');time.sleep(.1)")
    process=subprocess.Popen([sys.executable,"-P","-c",code],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    capture,projected=module._process_output(process,time.time()+1,lambda:False);process.wait(timeout=2);state=capture.finish(time.time()+1)
    assert projected==marker and state["stdout"]["bytes"]>70000
    assert state["stdout"]["complete"] is False and state["stdout"]["retained_bytes"]==65536

def test_measurement_handshake_waits_for_post_window_readiness_marker():
    module=_module();first={"awaiting_measurement":True,"pid":12,"listeners":{"http":True,"grpc":True},"network":{"nonlocal":False}}
    final={"ready":True,"pid":12,"http":{"ready":True},"listeners":{"http":True,"grpc":True},"network":{"nonlocal":False}}
    code=("import json,os,time;"
          f"os.write(1,{module._CHILD_MARKER!r}+json.dumps({first!r}).encode()+b'\\n');time.sleep(.15);"
          f"os.write(1,{module._CHILD_MARKER!r}+json.dumps({final!r}).encode()+b'\\n');time.sleep(.1)")
    process=subprocess.Popen([sys.executable,"-P","-c",code],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    capture,marker=module._process_output(process,time.time()+1,lambda:False)
    assert marker==first
    assert capture.wait_marker(time.time()+1,lambda:False,previous=marker)==final
    process.wait(timeout=2);capture.finish(time.time()+1)

def test_final_gate_reducer_uses_actual_marker_and_control_receipts():
    module=_module();handler=module._Handler();request="control-request-fixture"
    handler({"kind":"control_request","payload":{"request_id":request}})
    handler({"kind":"control_response","payload":{"request_id":request,"outcome":"returned","reservation_id":"r:fixture"}})
    marker={"ready":True,"pid":12,"http":{"ready":True},"listeners":{"http":True,"grpc":True},"network":{"nonlocal":False}}
    gates=module._reduce_final_gates(runtime_marker_ready=marker["ready"],http_ready=marker["http"]["ready"],
        listeners_ready=marker["listeners"]=={"http":True,"grpc":True},marker_no_nonlocal=marker["network"]["nonlocal"] is False,
        active_end_recorded=True,cleanup_no_nonlocal=True,processes_contained=True,archive_ok=True,cleanup_deadline_met=True,
        observer_receipt_valid=True,receiver_delivery_complete=True,control_correspondence=handler.control_receipt()["correspondence"],
        compiler_admissible=True,composite_admitted=True)
    assert gates["passed"] is True and gates["failed"]==[]
    handler({"kind":"control_response","payload":{"request_id":"other","outcome":"returned","reservation_id":"r:other"}})
    failed=module._reduce_final_gates(**{**gates["gates"],"control_correspondence":handler.control_receipt()["correspondence"]})
    assert failed["passed"] is False and failed["failed"]==["control_correspondence"]

def test_actual_receiver_pump_settlement_freezes_late_failure(tmp_path):
    module=_module();receiver_module=module._load(module._ROLES["observation_receiver"],"startup_probe_receiver_test")
    parent=tmp_path/"parent";parent.mkdir();receiver=receiver_module.ObservationReceiver((tmp_path/"events.jsonl").resolve(),lambda event:{},temporary_parent=parent.resolve());receiver.__enter__()
    ready=threading.Event();failures=[];stop=threading.Event()
    def partial_sender():
        lock_fd=os.open(receiver.directory/"lock",os.O_RDWR|os.O_NONBLOCK);events_fd=os.open(receiver.directory/"events.fifo",os.O_WRONLY|os.O_NONBLOCK);fcntl.flock(lock_fd,fcntl.LOCK_EX)
        try:os.write(events_fd,(1024).to_bytes(8,"big"));ready.set();time.sleep(2)
        finally:os.close(events_fd);os.close(lock_fd)
    def pump():
        try:receiver.serve_one(2)
        except Exception as exc:failures.append({"error_class":type(exc).__name__})
    sender=threading.Thread(target=partial_sender);sender.start();thread=threading.Thread(target=pump);thread.start();assert ready.wait(1)
    settled,projected=module._settle_pump(stop,thread,receiver,failures,time.time()+1)
    assert settled is False and projected==[{"error_class":"ObservationPumpUnsettled","protocol_stage":"ack","reason":"receiver pump did not settle","frame_bytes":None,"frame_limit_bytes":None}]
    sender.join(3);thread.join(2);assert projected==[{"error_class":"ObservationPumpUnsettled","protocol_stage":"ack","reason":"receiver pump did not settle","frame_bytes":None,"frame_limit_bytes":None}]
