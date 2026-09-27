from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import base64
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import pytest


ROOT = Path(__file__).resolve().parents[3]
OBSERVER = ROOT / "skills/sage-eval-gauge/scripts/process_observer.py"
RELAUNCH = ROOT / "skills/sage-goad-reset/scripts/_sage_relaunch.py"
CHANNEL = ROOT / "Payload_Type/sage/ai/observation_channel.py"
RECEIVER = ROOT / "skills/sage-eval-gauge/scripts/observation_receiver.py"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


receiver_module = _module(RECEIVER, "preexec_receiver")
observer_module = _module(OBSERVER, "preexec_observer")


def _prepare(tmp_path: Path, observation_directory: Path, *, timeout: float = 5, absolute_entry: bool = False) -> tuple[Path, Path, Path, Path, Path]:
    os.chmod(tmp_path, 0o700)
    source_binding = tmp_path / "source-binding.json"
    source_binding.write_text('{"approved":true}')
    os.chmod(source_binding, 0o600)
    entry = tmp_path / ("diagnostic_entry.py" if absolute_entry else "main.py")
    entry.write_text(
        "import importlib.util,os,signal,subprocess,sys,time\n"
        "open(os.environ['ROOT_PID_FILE'],'w').write(str(os.getpid()))\n"
        "child=subprocess.Popen(['/bin/sleep','10'],start_new_session=True)\n"
        "open(os.environ['CHILD_PID_FILE'],'w').write(str(child.pid))\n"
        "def stop(*_):\n"
        " child.terminate();child.wait(timeout=2);raise SystemExit(0)\n"
        "signal.signal(signal.SIGINT,stop)\n"
        "if os.environ.get('EMIT_LARGE'):\n"
        " os.write(1,(os.environ['SAGE_OBSERVATION_DIRECTORY']+'/events.fifo:SYNTHETIC_STDOUT_SECRET').encode())\n"
        " open(os.environ['SAGE_OBSERVATION_DIRECTORY']+'-near','w').write('SYNTHETIC_NEAR_SECRET')\n"
        " s=importlib.util.spec_from_file_location('test_channel',os.environ['CHANNEL_PATH']);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.emit('log_record',{'message':'SYNTHETIC_SECRET_'+'x'*524288})\n"
        "ready=os.environ['READY_FILE'];tmp=ready+'.pending'\n"
        "with open(tmp,'w') as marker: marker.write('ready');marker.flush();os.fsync(marker.fileno())\n"
        "os.replace(tmp,ready)\n"
        "fd=os.open('/dev/null',os.O_WRONLY)\n"
        "while True: os.write(fd,b'x');time.sleep(.05)\n"
    )
    receipt = tmp_path / "os-receipt.json"
    config = {
        "schema": "sage-process-observer-exec-config-v1",
        "observer_path": str(OBSERVER), "observer_sha256": hashlib.sha256(OBSERVER.read_bytes()).hexdigest(),
        "entry_path": str(entry), "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
        "source_binding_path": str(source_binding), "source_binding_sha256": hashlib.sha256(source_binding.read_bytes()).hexdigest(),
        "observation_channel_path": str(CHANNEL), "observation_channel_sha256": hashlib.sha256(CHANNEL.read_bytes()).hexdigest(),
        "strace_path": "/usr/bin/strace", "strace_sha256": hashlib.sha256(Path('/usr/bin/strace').read_bytes()).hexdigest(),
        "python": sys.executable, "cwd": str(tmp_path), "argv": [sys.executable, "-P", "-u", str(entry)] if absolute_entry else [sys.executable, "-u", "main.py"],
        "limits": {"timeout_seconds": timeout, "max_events": 20_000, "max_line_bytes": 65_536, "max_trace_bytes": 32 * 1024 * 1024},
        "observation_directory": str(observation_directory), "receipt_path": str(receipt),
    }
    config_path = tmp_path / "exec-config.json"
    config_path.write_text(json.dumps(config)); os.chmod(config_path, 0o600)
    snapshot = tmp_path / "env.snapshot"
    environment = {
        "PATH": os.environ["PATH"], "SAGE_OBSERVATION_DIRECTORY": str(observation_directory),
        "SAGE_PROCESS_OBSERVER_CONFIG": str(config_path),
        "SAGE_PROCESS_OBSERVER_CONFIG_SHA256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "ROOT_PID_FILE": str(tmp_path / "root.pid"), "CHILD_PID_FILE": str(tmp_path / "child.pid"),
        "READY_FILE": str(tmp_path / "ready"), "CHANNEL_PATH": str(CHANNEL), "EMIT_LARGE": "1",
    }
    snapshot.write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environment.items()) + b"\0")
    return config_path, receipt, tmp_path / "root.pid", tmp_path / "child.pid", tmp_path / "ready"


def _serve(receiver, stop: threading.Event, errors: list[str]) -> None:
    while not stop.is_set():
        try:
            receiver.serve_one(.1)
        except receiver_module.ObservationReceiverTimeout:
            continue
        except Exception:
            errors.append("receiver_error"); return


def _wait_file(path: Path, timeout: float = 4) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not path.exists(): time.sleep(.02)
    assert path.exists()


def _wait_receipt(path: Path, timeout: float = 4) -> tuple[str, dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try: raw = path.read_bytes()
        except OSError: raw = b""
        if raw.endswith(b"\n"):
            try: value = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError): value = None
            if isinstance(value, dict):
                return raw.decode("utf-8"), value
        time.sleep(.02)
    raise AssertionError("complete observer receipt unavailable")


def test_receipt_wait_ignores_created_empty_file(tmp_path):
    path = tmp_path / "receipt.json"; path.write_bytes(b"")
    writer = threading.Thread(target=lambda: (time.sleep(.05), path.write_text('{"complete":true}\n')))
    writer.start(); raw, value = _wait_receipt(path, timeout=1); writer.join()
    assert raw.endswith("\n") and value == {"complete": True}


def test_trace_spool_preserves_exact_order_hash_and_releases_consumed_bytes(monkeypatch,tmp_path):
    read,write=os.pipe();os.set_blocking(read,False);spool=observer_module._TraceSpool(read,tmp_path,1024*1024);spool.start()
    expected=b"a"*32768+b"b"*16384;os.write(write,expected[:32768]);os.write(write,expected[32768:]);os.close(write);assert spool.finish(time.monotonic()+2)
    observed=b""
    while chunk:=spool.read(time.monotonic()+1):observed+=chunk
    assert observed==expected and spool.digest.hexdigest()==hashlib.sha256(expected).hexdigest()
    assert spool.buffered==0 and spool.error is False;spool.close()

    read,write=os.pipe();os.set_blocking(read,False);spool=observer_module._TraceSpool(read,tmp_path,1024*1024)
    original=observer_module.os.read
    monkeypatch.setattr(observer_module.os,"read",lambda fd,size:(_ for _ in ()).throw(OSError("drain")) if fd==read else original(fd,size))
    spool.start();os.write(write,b"failure");os.close(write);assert spool.finish(time.monotonic()+2)
    assert spool.error is True and spool.eof is True and not spool.worker.is_alive();spool.close()


def test_trace_drain_failure_retains_receipt_and_contains_inert_tracee(monkeypatch,tmp_path):
    tracee=subprocess.Popen(["/bin/sleep","20"],start_new_session=True)
    tracee_pidfd=os.pidfd_open(tracee.pid);read,write=os.pipe();receipt=tmp_path/"setup-receipt.json"
    receipt_fd=os.open(receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    original=observer_module.os.read
    monkeypatch.setattr(observer_module.os,"read",lambda fd,size:(_ for _ in ()).throw(OSError("drain")) if fd==read else original(fd,size))
    config={"limits":{"timeout_seconds":5,"max_trace_bytes":1024},"receipt_path":str(receipt),
        "observation_directory":str(tmp_path),
        "observation_channel_path":str(CHANNEL),"argv":[sys.executable],"entry_sha256":"a"*64,
        "source_binding_sha256":"b"*64,"observation_channel_sha256":"c"*64}
    collector=os.fork()
    if collector==0:
        os.close(write);observer_module._collect_preexec(read,receipt_fd,tracee.pid,tracee_pidfd,config,"d"*64);os._exit(0)
    os.close(read);os.close(write);os.close(receipt_fd);os.close(tracee_pidfd);os.waitpid(collector,0);tracee.wait(7)
    value=json.loads(receipt.read_text())
    assert value["complete"] is False and "collector_error" in value["failures"]
    assert value["containment"]["requested"] is True and value["containment"]["tracee_exited"] is True


def test_absolute_entry_argv_mismatch_fails_closed(tmp_path):
    with receiver_module.ObservationReceiver(tmp_path / "events.jsonl", lambda event: {}, temporary_parent=tmp_path) as receiver:
        config_path, *_ = _prepare(tmp_path, receiver.directory, absolute_entry=True)
        config = json.loads(config_path.read_text())
        config["argv"][-1] = str(tmp_path / "diagnostic_entry-near.py")
        config_path.write_text(json.dumps(config))
        with pytest.raises(observer_module.ProcessObserverError, match="configuration is invalid"):
            observer_module.load_exec_config(
                config_path, hashlib.sha256(config_path.read_bytes()).hexdigest(),
                cwd=tmp_path, python=sys.executable,
            )


@pytest.mark.parametrize("absolute_entry", [False, True])
def test_preexec_preserves_main_pid_clean_signal_and_avoids_recursive_fifo_deadlock(tmp_path, absolute_entry):
    records = tmp_path / "redacted-records.jsonl"
    unrelated_sink_events: list[bytes] = []
    stream_events = []
    def redact(event):
        if event["payload"].get("event") == "observed_process_streams":
            stream_events.append(event["payload"])
            return {"event": "observed_process_streams"}
        raw = base64.b64decode(event["payload"].get("line_base64", ""))
        if b"SYNTHETIC_STDOUT_SECRET" in raw or b"SYNTHETIC_NEAR_SECRET" in raw:
            unrelated_sink_events.append(raw)
        return {"event": event["payload"].get("event"), "kind": event["payload"].get("kind")}
    with receiver_module.ObservationReceiver(
        records,
        redact,
        temporary_parent=tmp_path,
    ) as receiver:
        _prepare(tmp_path, receiver.directory, absolute_entry=absolute_entry)
        copied_relaunch = tmp_path / "_sage_relaunch.py"; copied_relaunch.write_bytes(RELAUNCH.read_bytes())
        (tmp_path / "readiness_contract.py").write_text(
            f"from pathlib import Path\nDEFAULT_STARTUP_IDENTITY_PATH=Path({str(tmp_path / 'startup.json')!r})\n"
            "def write_startup_identity(path,env,**values): path.write_text(__import__('json').dumps(values))\n"
        )
        stop = threading.Event(); errors: list[str] = []
        thread = threading.Thread(target=_serve, args=(receiver, stop, errors), daemon=True); thread.start()
        process = subprocess.Popen([sys.executable, str(copied_relaunch), str(tmp_path), sys.executable, str(tmp_path / "env.snapshot")], start_new_session=True)
        _wait_file(tmp_path / "ready")
        assert int((tmp_path / "root.pid").read_text()) == process.pid
        os.killpg(process.pid, signal.SIGINT)
        assert process.wait(timeout=8) == 0
        receipt, parsed = _wait_receipt(tmp_path / "os-receipt.json")
        stop.set(); thread.join(timeout=2)
    child_pid = int((tmp_path / "child.pid").read_text())
    assert not Path(f"/proc/{child_pid}").exists()
    assert "SYNTHETIC_SECRET" not in receipt and "SYNTHETIC_SECRET" not in records.read_text()
    assert not errors and parsed["tracer_attested"] is True
    assert parsed["complete"] is True and "truncated_syscall_data" not in parsed["failures"]
    assert parsed["suppressed_observation_io_count"] > 0
    assert parsed["tracee_pid"] == process.pid and process.pid in parsed["observed_pids"]
    assert parsed["observed_pids"] == parsed["ended_pids"] and parsed["stream_metadata_count"] == 2
    assert stream_events[0]["tracee_pid"] == process.pid
    assert {row["fd"] for row in stream_events[0]["streams"]} == {1, 2}
    for raw_stream, retained in zip(stream_events[0]["streams"], parsed["stream_metadata"]["descriptors"]):
        assert retained["target_sha256"] == hashlib.sha256(os.fsencode(raw_stream["target"])).hexdigest()
    assert any(b"SYNTHETIC_STDOUT_SECRET" in raw for raw in unrelated_sink_events)
    assert any(b"SYNTHETIC_NEAR_SECRET" in raw for raw in unrelated_sink_events)


def test_protocol_suppression_is_anchored_to_exact_operands(tmp_path):
    directory = os.fsencode(str(tmp_path / "channel"))
    members = tuple(directory + b"/" + name for name in (b"events.fifo", b"acks.fifo", b"lock"))
    observer = _module(OBSERVER, "preexec_operand_parser")
    assert observer._observation_protocol_operation(
        b'123 1.0 write(9<' + members[0] + b'>, "data", 4) = 4', "write", members
    )
    assert observer._observation_protocol_operation(
        b'123 1.0 openat(AT_FDCWD</cwd>, "' + members[2] + b'", O_RDWR) = 9', "openat", members
    )
    assert not observer._observation_protocol_operation(
        b'123 1.0 write(1</dev/null>, "log ' + members[0] + b'", 4) = 4', "write", members
    )
    assert not observer._observation_protocol_operation(
        b'123 1.0 openat(AT_FDCWD</cwd>, "' + members[0] + b'-near", O_RDWR) = 9', "openat", members
    )
    assert observer._strace_string_truncated(b'123 1.0 write(3</dev/null>, "data"..., 70000) = 70000')
    assert not observer._strace_string_truncated(b'123 1.0 write(3</dev/null>, "quote\\"...literal", 16) = 16')


@pytest.mark.parametrize("size,string_limit,complete",[
    (17_000,65_536,True),(70_000,65_536,False),(1_048_577,1_048_576,False)])
def test_preexec_string_cap_distinguishes_escaped_rendering_from_real_truncation(
        tmp_path,size,string_limit,complete):
    case=tmp_path/str(size);case.mkdir();os.chmod(case,0o700);entry=case/"main.py"
    entry.write_text(f"import os\nfd=os.open('/dev/null',os.O_WRONLY)\nos.write(fd,b'\\xff'*{size})\nos.close(fd)\n")
    binding=case/"binding";binding.write_text("bound");os.chmod(binding,0o600);rendered=[]
    def handler(event):
        raw=base64.b64decode(event.get("payload",{}).get("line_base64",""))
        if event.get("payload",{}).get("kind")=="write_attempt" and raw.rstrip().endswith(f"= {size}".encode()):rendered.append(raw)
        return {}
    with receiver_module.ObservationReceiver(case/"events",handler,temporary_parent=case) as receiver:
        receipt=case/"receipt";config={"schema":"sage-process-observer-exec-config-v1","observer_path":str(OBSERVER),"observer_sha256":hashlib.sha256(OBSERVER.read_bytes()).hexdigest(),
            "entry_path":str(entry),"entry_sha256":hashlib.sha256(entry.read_bytes()).hexdigest(),"source_binding_path":str(binding),"source_binding_sha256":hashlib.sha256(binding.read_bytes()).hexdigest(),
            "observation_channel_path":str(CHANNEL),"observation_channel_sha256":hashlib.sha256(CHANNEL.read_bytes()).hexdigest(),"strace_path":"/usr/bin/strace","strace_sha256":hashlib.sha256(Path('/usr/bin/strace').read_bytes()).hexdigest(),
            "python":sys.executable,"cwd":str(case),"argv":[sys.executable,"-P","-u","main.py"],"limits":{"timeout_seconds":10,"max_events":1000,"max_line_bytes":string_limit,"max_trace_bytes":5*1024*1024},
            "observation_directory":str(receiver.directory),"receipt_path":str(receipt)}
        config_path=case/"config";config_path.write_text(json.dumps(config));os.chmod(config_path,0o600)
        stop=threading.Event();errors=[];server=threading.Thread(target=_serve,args=(receiver,stop,errors),daemon=True);server.start()
        environment={"PATH":os.environ["PATH"],"SAGE_OBSERVATION_DIRECTORY":str(receiver.directory),"PYTHONDONTWRITEBYTECODE":"1"}
        result=subprocess.run([sys.executable,str(OBSERVER),"--exec-config",str(config_path),"--exec-config-sha256",hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "--cwd",str(case),"--python",sys.executable],cwd=case,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        _raw,parsed=_wait_receipt(receipt);stop.set();server.join(2)
    assert result.returncode==0 and errors==[] and len(rendered)==1 and len(rendered[0])>65536
    assert parsed["complete"] is complete
    assert ("truncated_syscall_data" in parsed["failures"]) is (not complete)
    assert parsed["truncated_syscall_count"]==(0 if complete else 1)
    assert len(parsed["truncated_syscall_samples"])==(0 if complete else 1)
    if not complete:
        sample=parsed["truncated_syscall_samples"][0]
        assert set(sample)=={"pid","call","rendered_line_bytes","source_string_limit_bytes","raw_line_sha256","syscall_epoch_ns","return_count"}
        assert sample["call"]=="write" and sample["rendered_line_bytes"]==len(rendered[0])
        assert sample["source_string_limit_bytes"]==string_limit and sample["return_count"]==size
        assert isinstance(sample["pid"],int) and isinstance(sample["syscall_epoch_ns"],int) and len(sample["raw_line_sha256"])==64
        assert "\\377" not in json.dumps(sample) and "dev/null" not in json.dumps(sample)


def test_preexec_megabyte_source_cap_covers_root_write_and_sendto_shapes(tmp_path):
    case=tmp_path/"megabyte-source-cap";case.mkdir();os.chmod(case,0o700);entry=case/"main.py"
    entry.write_text("""import os,socket,threading
a,b=socket.socketpair()
def drain():
 while b.recv(65536): pass
t=threading.Thread(target=drain);t.start()
fd=os.open('/dev/null',os.O_WRONLY);os.write(fd,b'\\xff'*190695);os.close(fd)
a.send(b'x'*1011676);a.shutdown(socket.SHUT_WR);a.close();t.join();b.close()
""")
    binding=case/"binding";binding.write_text("bound");os.chmod(binding,0o600)
    calls={};max_frame=[0]
    def handler(event):
        max_frame[0]=max(max_frame[0],len(json.dumps(event).encode()))
        raw=base64.b64decode(event.get("payload",{}).get("line_base64",""))
        for call,count in (("write",190695),("sendto",1011676)):
            if (f"{call}(".encode() in raw and raw.rstrip().endswith(f"= {count}".encode())):
                calls[call]=len(raw)
        return {}
    with receiver_module.ObservationReceiver(case/"events",handler,temporary_parent=case) as receiver:
        receipt=case/"receipt";config={"schema":"sage-process-observer-exec-config-v1","observer_path":str(OBSERVER),"observer_sha256":hashlib.sha256(OBSERVER.read_bytes()).hexdigest(),
            "entry_path":str(entry),"entry_sha256":hashlib.sha256(entry.read_bytes()).hexdigest(),"source_binding_path":str(binding),"source_binding_sha256":hashlib.sha256(binding.read_bytes()).hexdigest(),
            "observation_channel_path":str(CHANNEL),"observation_channel_sha256":hashlib.sha256(CHANNEL.read_bytes()).hexdigest(),"strace_path":"/usr/bin/strace","strace_sha256":hashlib.sha256(Path('/usr/bin/strace').read_bytes()).hexdigest(),
            "python":sys.executable,"cwd":str(case),"argv":[sys.executable,"-P","-u","main.py"],"limits":{"timeout_seconds":12,"max_events":2000,"max_line_bytes":1_048_576,"max_trace_bytes":8*1024*1024},
            "observation_directory":str(receiver.directory),"receipt_path":str(receipt)}
        config_path=case/"config";config_path.write_text(json.dumps(config));os.chmod(config_path,0o600)
        stop=threading.Event();errors=[];server=threading.Thread(target=_serve,args=(receiver,stop,errors),daemon=True);server.start()
        environment={"PATH":os.environ["PATH"],"SAGE_OBSERVATION_DIRECTORY":str(receiver.directory),"PYTHONDONTWRITEBYTECODE":"1"}
        result=subprocess.run([sys.executable,str(OBSERVER),"--exec-config",str(config_path),"--exec-config-sha256",hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "--cwd",str(case),"--python",sys.executable],cwd=case,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=18)
        _raw,parsed=_wait_receipt(receipt);stop.set();server.join(2)
    assert result.returncode==0 and errors==[] and parsed["complete"] is True
    assert parsed["truncated_syscall_count"]==0 and set(calls)=={"write","sendto"}
    assert calls["write"]>190695 and calls["sendto"]>1011676 and max_frame[0]<16*1024*1024


def test_raw_trace_drain_does_not_block_behind_concurrent_native_frames(tmp_path):
    counts={"native":0,"os":0,"streams":0};errors=[]
    def record(event):
        payload=event["payload"]
        if payload.get("event")=="os_syscall":counts["os"]+=1
        elif payload.get("event")=="observed_process_streams":counts["streams"]+=1
        elif event["kind"]=="control_request":counts["native"]+=1
        return {}
    with receiver_module.ObservationReceiver(tmp_path/"cycle-events.jsonl",record,temporary_parent=tmp_path) as receiver:
        config_path,receipt,*_=_prepare(tmp_path,receiver.directory,timeout=35)
        entry=tmp_path/"main.py";entry.write_text(
            "import importlib.util,os,threading\n"
            "s=importlib.util.spec_from_file_location('fixture_channel',os.environ['CHANNEL_PATH']);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
            "def burst():\n fd=os.open('/dev/null',os.O_WRONLY)\n for _ in range(3000):os.write(fd,b'x'*8192)\n os.close(fd)\n"
            "t=threading.Thread(target=burst);t.start()\n"
            "for i in range(800):m.emit('control_request',{'request_id':str(i),'transport':'fixture','application_request':False})\n"
            "t.join()\n")
        config=json.loads(config_path.read_text());config["entry_sha256"]=hashlib.sha256(entry.read_bytes()).hexdigest()
        config["limits"]["max_events"]=100_000;config["limits"]["max_trace_bytes"]=128*1024*1024
        config_path.write_text(json.dumps(config));os.chmod(config_path,0o600)
        stop=threading.Event();server=threading.Thread(target=_serve,args=(receiver,stop,errors),daemon=True);server.start()
        environment={"PATH":os.environ["PATH"],"SAGE_OBSERVATION_DIRECTORY":str(receiver.directory),
                     "CHANNEL_PATH":str(CHANNEL),"PYTHONDONTWRITEBYTECODE":"1"}
        result=subprocess.run([sys.executable,str(OBSERVER),"--exec-config",str(config_path),
            "--exec-config-sha256",hashlib.sha256(config_path.read_bytes()).hexdigest(),"--cwd",str(tmp_path),
            "--python",sys.executable],cwd=tmp_path,env=environment,stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=42)
        _raw,parsed=_wait_receipt(receipt);stop.set();server.join(2)
    assert result.returncode==0 and parsed["complete"] is True and errors==[]
    assert counts["native"]==800 and counts["streams"]==1
    assert counts["os"]==parsed["event_count"]-parsed["suppressed_observation_io_count"]
    assert parsed["trace_bytes"]>20*1024*1024 and parsed["processes_observed"]==parsed["processes_ended"]


def test_dead_receiver_contains_setsid_child(tmp_path):
    records = tmp_path / "redacted-dead-records.jsonl"
    receiver = receiver_module.ObservationReceiver(
        records, lambda event: {"kind": event["payload"].get("kind")}, temporary_parent=tmp_path
    ).__enter__()
    directory = receiver.directory
    _prepare(tmp_path, directory, timeout=3)
    errors: list[str] = []; stopped = threading.Event()
    thread = threading.Thread(target=_serve, args=(receiver, stopped, errors), daemon=True); thread.start()
    environment = dict(os.environ, SAGE_OBSERVATION_DIRECTORY=str(directory),
                       ROOT_PID_FILE=str(tmp_path / "root.pid"), CHILD_PID_FILE=str(tmp_path / "child.pid"),
                       READY_FILE=str(tmp_path / "ready"), CHANNEL_PATH=str(CHANNEL), EMIT_LARGE="1")
    process = subprocess.Popen([sys.executable, str(OBSERVER), "--exec-config", str(tmp_path / "exec-config.json"),
                                "--exec-config-sha256", hashlib.sha256((tmp_path / "exec-config.json").read_bytes()).hexdigest(),
                                "--cwd", str(tmp_path), "--python", sys.executable], env=environment, start_new_session=True)
    _wait_file(tmp_path / "ready", timeout=15)
    stopped.set(); thread.join(timeout=2); receiver.close()
    process.wait(timeout=8); _receipt, result = _wait_receipt(tmp_path / "os-receipt.json")
    child_pid = int((tmp_path / "child.pid").read_text())
    assert not Path(f"/proc/{child_pid}").exists()
    assert "receiver_error" in result["failures"] and result["containment"]["tracee_exited"] is True


def test_deadline_contains_setsid_child(tmp_path):
    records = tmp_path / "redacted-timeout-records.jsonl"
    with receiver_module.ObservationReceiver(
        records, lambda event: {"kind": event["payload"].get("kind")}, temporary_parent=tmp_path
    ) as receiver:
        _prepare(tmp_path, receiver.directory, timeout=.4)
        environment = dict(os.environ, SAGE_OBSERVATION_DIRECTORY=str(receiver.directory),
                           ROOT_PID_FILE=str(tmp_path / "root.pid"), CHILD_PID_FILE=str(tmp_path / "child.pid"),
                           READY_FILE=str(tmp_path / "ready"), CHANNEL_PATH=str(CHANNEL), EMIT_LARGE="")
        stopped = threading.Event(); errors: list[str] = []
        thread = threading.Thread(target=_serve, args=(receiver, stopped, errors), daemon=True); thread.start()
        process = subprocess.Popen([sys.executable, str(OBSERVER), "--exec-config", str(tmp_path / "exec-config.json"),
                                    "--exec-config-sha256", hashlib.sha256((tmp_path / "exec-config.json").read_bytes()).hexdigest(),
                                    "--cwd", str(tmp_path), "--python", sys.executable], env=environment, start_new_session=True)
        _wait_file(tmp_path / "ready"); process.wait(timeout=8)
        _receipt, result = _wait_receipt(tmp_path / "os-receipt.json")
        stopped.set(); thread.join(timeout=2)
    child_pid = int((tmp_path / "child.pid").read_text())
    assert not Path(f"/proc/{child_pid}").exists()
    assert "deadline_exceeded" in result["failures"] and result["containment"]["tracer_killed"] is True


def test_trace_spool_bound_failure_contains_all_owned_processes(tmp_path):
    with receiver_module.ObservationReceiver(tmp_path/"bounded-events.jsonl",lambda _event:{},temporary_parent=tmp_path) as receiver:
        config_path,receipt,_root,child,_ready=_prepare(tmp_path,receiver.directory,timeout=5)
        config=json.loads(config_path.read_text());config["limits"]["max_trace_bytes"]=1024
        config_path.write_text(json.dumps(config));os.chmod(config_path,0o600)
        stop=threading.Event();errors=[];server=threading.Thread(target=_serve,args=(receiver,stop,errors),daemon=True);server.start()
        environment=dict(os.environ,SAGE_OBSERVATION_DIRECTORY=str(receiver.directory),ROOT_PID_FILE=str(tmp_path/"root.pid"),
            CHILD_PID_FILE=str(child),READY_FILE=str(tmp_path/"ready"),CHANNEL_PATH=str(CHANNEL),EMIT_LARGE="")
        process=subprocess.Popen([sys.executable,str(OBSERVER),"--exec-config",str(config_path),"--exec-config-sha256",
            hashlib.sha256(config_path.read_bytes()).hexdigest(),"--cwd",str(tmp_path),"--python",sys.executable],
            env=environment,start_new_session=True);process.wait(10);_raw,parsed=_wait_receipt(receipt);stop.set();server.join(2)
    assert parsed["complete"] is False and "trace_byte_limit_exceeded" in parsed["failures"]
    assert parsed["containment"]["tracee_exited"] is True and not Path(f"/proc/{process.pid}").exists()
    assert not child.exists() or not Path(f"/proc/{child.read_text()}").exists()


def test_relaunch_default_exec_vector_is_unchanged(monkeypatch):
    module = _module(RELAUNCH, "relaunch_default")
    assert module.optional_observer_command({}, "/tmp/example", sys.executable) is None
