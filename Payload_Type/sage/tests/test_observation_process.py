import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest

from ai import observation_channel


ROOT = Path(__file__).resolve().parents[3]
RECEIVER_SPEC = importlib.util.spec_from_file_location(
    "process_test_receiver", ROOT / "skills/sage-eval-gauge/scripts/observation_receiver.py"
)
receiver_module = importlib.util.module_from_spec(RECEIVER_SPEC)
assert RECEIVER_SPEC and RECEIVER_SPEC.loader
RECEIVER_SPEC.loader.exec_module(receiver_module)
from ai import observation_process as process


def _reset(monkeypatch):
    process._started_pids.clear(); process._ended_pids.clear()
    monkeypatch.setattr(process, "_hook_registered", False)
    monkeypatch.setattr(process, "_atexit_registered", False)
    monkeypatch.setattr(process, "_local", threading.local())


def test_install_idempotence_process_lifecycle_and_recursion(monkeypatch, tmp_path):
    _reset(monkeypatch); hooks=[]; emitted=[]
    monkeypatch.setenv(observation_channel.ENV_NAME, str(tmp_path))
    monkeypatch.setattr(process.sys, "addaudithook", hooks.append)
    def emit(kind,payload):
        emitted.append((kind,payload))
        if len(emitted)==2:
            hooks[0]("open", ("own-fifo", "w", os.O_WRONLY))
    monkeypatch.setattr(observation_channel, "emit", emit)
    assert process.install() is True and process.install() is True
    hooks[0]("os.mkdir", ("synthetic", 0o700, -1)); process._process_end(); process._process_end()
    assert [value[0] for value in emitted] == ["process_start", "tool_event", "process_end"]
    assert all(value[1]["observation_class"] == "ATTEMPT" for value in emitted)


def test_unconfigured_getenv_and_read_open_emit_nothing(monkeypatch):
    _reset(monkeypatch); emitted=[]
    monkeypatch.delenv(observation_channel.ENV_NAME,raising=False)
    monkeypatch.setattr(process.sys,"addaudithook",lambda *_args:pytest.fail("hook installed"))
    assert process.install() is False
    monkeypatch.setattr(observation_channel,"emit",lambda *args:emitted.append(args))
    process._audit_hook("os.getenv",("SYNTHETIC",)); process._audit_hook("open",("synthetic","r",os.O_RDONLY))
    assert emitted == []


def test_attempt_payloads_strip_environment_values_and_report_failed_write(monkeypatch):
    emitted=[]; monkeypatch.setenv(observation_channel.ENV_NAME,"/synthetic")
    monkeypatch.setattr(observation_channel,"emit",lambda *args:emitted.append(args))
    process._audit_hook("subprocess.Popen",("python",["python","-c","pass"],None,{"PRIVATE_NAME":"SENTINEL"}))
    process._audit_hook("os.putenv",("PRIVATE_NAME","SENTINEL"))
    process._audit_hook("open",("/does-not-exist/file","w",os.O_WRONLY|os.O_CREAT))
    process._audit_hook("socket.getaddrinfo",("synthetic.invalid",443,0,0,0))
    assert [kind for kind,_payload in emitted] == ["tool_event","config_snapshot","tool_event","control_request"]
    assert all(payload["attempted"] is True for _kind,payload in emitted)
    assert "SENTINEL" not in json.dumps(emitted)
    assert emitted[0][1]["environment_names"] == ["PRIVATE_NAME"]


def test_only_channel_internal_activity_suppresses_nearby_write(monkeypatch):
    emitted=[]; monkeypatch.setenv(observation_channel.ENV_NAME,"/synthetic")
    monkeypatch.setattr(observation_channel,"emit",lambda *args:emitted.append(args))
    monkeypatch.setattr(observation_channel,"internal_activity",lambda:True)
    process._audit_hook("open",("/synthetic/lock","w",os.O_WRONLY))
    monkeypatch.setattr(observation_channel,"internal_activity",lambda:False)
    process._audit_hook("open",("/synthetic/nearby","w",os.O_WRONLY))
    assert [payload["path"] for _kind,payload in emitted] == ["/synthetic/nearby"]


def test_actual_child_interpreter_emits_start_popen_and_end_to_fifo(tmp_path):
    durable=(tmp_path/"redacted.jsonl").resolve(); parent=tmp_path.resolve()
    with receiver_module.ObservationReceiver(
        durable,
        lambda event:{"audit_event":event["payload"].get("audit_event"),
                      "environment_names":event["payload"].get("environment_names",[])},
        temporary_parent=parent,
    ) as receiver:
        code=("import os,subprocess,sys\nfrom ai import observation_process\n"
              "observation_process.install()\n"
              "subprocess.run([sys.executable,'-c','pass'],env={'PRIVATE_NAME':'SENTINEL'},check=True)\n")
        environment={**os.environ,observation_channel.ENV_NAME:str(receiver.directory),
                     "PYTHONPATH":str(ROOT/"Payload_Type/sage")}
        child=subprocess.Popen([sys.executable,"-c",code],env=environment,text=True,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        receipts=[receiver.serve_one(5) for _ in range(4)]
        stdout,stderr=child.communicate(timeout=5)
    assert child.returncode==0 and stdout==stderr==""
    assert [value["kind"] for value in receipts]==["process_start","tool_event","tool_event","process_end"]
    assert receipts[1]["record"]["audit_event"]=="subprocess.Popen"
    assert receipts[2]["record"]["audit_event"]=="os.posix_spawn"
    assert receipts[1]["record"]["environment_names"]==["PRIVATE_NAME"]
    assert "SENTINEL" not in durable.read_text()


def test_observer_failure_denies_socket_connect_before_local_dispatch(tmp_path):
    target_path=tmp_path/"target.sock"; target=socket.socket(socket.AF_UNIX); target.bind(str(target_path)); target.listen()
    accepted=threading.Event(); target.settimeout(0.4)
    def accept():
        try: client,_=target.accept(); accepted.set(); client.close()
        except OSError: pass
    thread=threading.Thread(target=accept,daemon=True); thread.start()
    durable=(tmp_path/"redacted.jsonl").resolve(); gate=tmp_path/"go"
    with receiver_module.ObservationReceiver(durable,lambda event:{},temporary_parent=tmp_path.resolve()) as receiver:
        code=("import os,socket,time\nfrom ai import observation_process\n"
              "observation_process.install()\n"
              f"\nwhile not os.path.exists({str(gate)!r}):time.sleep(.01)\n"
              "try:\n s=socket.socket(socket.AF_UNIX);s.connect("+repr(str(target_path))+")\n"
              "except BaseException as error:\n observation_process._started_pids.clear();raise SystemExit(7 if type(error).__name__=='ObservationChannelError' else 8)\n")
        environment={**os.environ,observation_channel.ENV_NAME:str(receiver.directory),
                     "PYTHONPATH":str(ROOT/"Payload_Type/sage")}
        child=subprocess.Popen([sys.executable,"-c",code],env=environment)
        receiver.serve_one(5)
    gate.write_text("go"); child.wait(timeout=3); thread.join(1); target.close()
    assert child.returncode==7 and not accepted.is_set()
