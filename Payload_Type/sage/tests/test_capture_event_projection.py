from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[3]
PROJECTION = ROOT / "skills/sage-eval-gauge/scripts/capture_event_projection.py"
REDACTION = ROOT / "skills/sage-eval-gauge/scripts/capture_redaction.py"
RECEIVER = ROOT / "skills/sage-eval-gauge/scripts/observation_receiver.py"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


projection_module = _module(PROJECTION, "m3_capture_event_projection")
redaction_module = _module(REDACTION, "m3_capture_event_projection_redaction")
receiver_module = _module(RECEIVER, "m3_capture_event_projection_receiver")


def _index():
    value = redaction_module.SecretIndex()
    value.register("synthetic-secret", b"ALPHA_SECRET_123", "operator")
    return value


def _identities(value):
    if isinstance(value, dict):
        if set(value) >= {"handle", "category", "encoding"}:
            yield value
        for item in value.values(): yield from _identities(item)
    elif isinstance(value, list):
        for item in value: yield from _identities(item)


def test_actual_fifo_decodes_bodies_and_split_iovec_before_redaction(tmp_path):
    os.chmod(tmp_path, 0o700)
    records = tmp_path / "projected.jsonl"; projected = []
    handler = projection_module.CaptureEventProjection(_index())
    with receiver_module.ObservationReceiver(records, handler, temporary_parent=tmp_path) as receiver:
        def serve():
            for _ in range(3): projected.append(receiver.serve_one(5)["record"])
        thread = threading.Thread(target=serve); thread.start()
        line = (
            b'501 1770000000.2 writev(7</tmp/capture>, '
            b'[{iov_base="ALPHA_", iov_len=6}, {iov_base="SECR\\105T_123", iov_len=10}], 2) = 16'
        )
        http_body = b'xxxxx{"token":"ALPHA_SECRET_123"}'
        rpc_body = b'\x00\xffRPCALPHA_SECRET_123\x01'
        program = (
            "import base64,sys;sys.path.insert(0,sys.argv[1]);from ai.observation_channel import emit;"
            "emit('model_request',{'request_id':'a'*32,'method':'POST','url':'http://local/',"
            "'body_base64':sys.argv[2]});"
            "emit('control_request',{'request_id':'b'*32,'transport':'grpc','rpc_method':'/local/method',"
            "'body_base64':sys.argv[3]});"
            "emit('tool_event',{'event':'os_syscall','kind':'write_attempt',"
            "'line_base64':sys.argv[4]})"
        )
        environment = dict(os.environ, SAGE_OBSERVATION_DIRECTORY=str(receiver.directory))
        result = subprocess.run([sys.executable, "-c", program, str(ROOT / "Payload_Type/sage"),
                                 base64.b64encode(http_body).decode(), base64.b64encode(rpc_body).decode(),
                                 base64.b64encode(line).decode()],
                                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=60, check=False)
        thread.join(timeout=10)
    assert result.returncode == 0 and len(projected) == 3
    durable = records.read_bytes()
    forbidden = [b"ALPHA_SECRET_123", base64.b64encode(b"ALPHA_SECRET_123"),
                 base64.b64encode(http_body), base64.b64encode(rpc_body), base64.b64encode(line)]
    assert all(value not in durable for value in forbidden)
    assert {item["handle"] for item in _identities(projected)} == {"synthetic-secret"}
    assert projected[0]["payload"]["decoded_body"]["metadata"]["original_length"] == len(http_body)
    assert projected[0]["payload"]["decoded_body"]["metadata"]["original_sha256"] == hashlib.sha256(http_body).hexdigest()
    assert projected[1]["payload"]["decoded_body"]["metadata"]["original_length"] == len(rpc_body)
    assert projected[1]["payload"]["decoded_body"]["metadata"]["original_sha256"] == hashlib.sha256(rpc_body).hexdigest()
    syscall = projected[2]["payload"]["decoded_os_syscall"]
    assert syscall["semantic_known"] is True
    assert syscall["write_metadata"]["attempted_bytes"] == syscall["write_metadata"]["requested_bytes"] == 16
    assert any(item["encoding"] == "raw_utf8" for item in _identities(syscall["redacted_content"]))


def test_malformed_whole_body_base64_fails_receiver_without_persisting_raw(tmp_path):
    os.chmod(tmp_path, 0o700); records = tmp_path / "failed.jsonl"
    with receiver_module.ObservationReceiver(
        records, projection_module.CaptureEventProjection(_index()), temporary_parent=tmp_path,
    ) as receiver:
        errors = []
        def serve():
            try: receiver.serve_one(5)
            except Exception as error: errors.append(str(error))
        thread = threading.Thread(target=serve); thread.start()
        program = (
            "import sys;sys.path.insert(0,sys.argv[1]);from ai.observation_channel import emit;"
            "emit('control_request',{'request_id':'c'*32,'body_base64':'!!!!ALPHA_SECRET_123'})"
        )
        result = subprocess.run(
            [sys.executable, "-c", program, str(ROOT / "Payload_Type/sage")],
            env=dict(os.environ, SAGE_OBSERVATION_DIRECTORY=str(receiver.directory)),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=60, check=False,
        )
        thread.join(timeout=10)
    assert result.returncode != 0 and errors == ["observation stream failed"]
    durable = records.read_bytes()
    assert b"ALPHA_SECRET_123" not in durable and b"body_base64" not in durable
    assert b"handler_failed" in durable


def test_unknown_os_record_is_metadata_only_with_matches():
    handler = projection_module.CaptureEventProjection(_index())
    line = b'501 1770000000.2 sendfile(7</tmp/out>, 8</tmp/in>, NULL, ALPHA_SECRET_123) = -1'
    event = {"schema": "sage-observation-event-v2", "event_id": "d" * 32,
             "process_uuid": "e" * 32, "pid": 5, "seq": 1, "time_ns": 1,
             "kind": "tool_event", "payload": {"event": "os_syscall", "kind": "write_attempt",
                                                   "line_base64": base64.b64encode(line).decode()}}
    result = handler(event); syscall = result["payload"]["decoded_os_syscall"]
    assert syscall["semantic_known"] is False and syscall["reason"] == "unsupported_transfer"
    assert "redacted_base64" not in syscall and "line_base64" not in json.dumps(result)
    assert {item["handle"] for item in _identities(syscall)} == {"synthetic-secret"}

def test_network_projection_retains_connected_peer_semantics_without_address():
    handler=projection_module.CaptureEventProjection(_index())
    lines=[
     b'501 1770000000.1 socket(AF_INET, SOCK_STREAM|SOCK_CLOEXEC, IPPROTO_IP) = 7<socket:[9]>',
     b'501 1770000000.2 connect(7<socket:[9]>, {sa_family=AF_INET, sin_port=htons(16006), sin_addr=inet_addr("127.0.0.1")}, 16) = 0',
     b'501 1770000000.3 sendto(7<socket:[9]>, "abc", 3, 0, NULL, 0) = 3',
    ]
    results=[]
    for seq,line in enumerate(lines,1):
     event={"schema":"sage-observation-event-v2","event_id":f"{seq:032x}","process_uuid":"a"*32,
      "pid":5,"seq":seq,"time_ns":seq,"kind":"tool_event","payload":{"event":"os_syscall",
      "kind":"network_attempt","line_base64":base64.b64encode(line).decode()}}
     results.append(handler(event)["payload"]["decoded_os_syscall"])
    fact=results[-1];assert fact["semantic_known"] is True
    assert fact["network_metadata"]["connected_socket"] is True
    assert fact["network_metadata"]["port"]==16006 and "address" not in fact["network_metadata"]


def test_reserved_duplicate_rejects_but_metadata_near_match_is_redacted():
    handler = projection_module.CaptureEventProjection(_index())
    envelope = {"schema": "sage-observation-event-v2", "event_id": "f" * 32,
                "process_uuid": "1" * 32, "pid": 5, "seq": 1, "time_ns": 1,
                "kind": "model_response"}
    encoded = base64.b64encode(b"body ALPHA_SECRET_123").decode()
    with pytest.raises(projection_module.CaptureProjectionError, match="capture projection failed"):
        handler({**envelope, "payload": {"body_base64": encoded, "decoded_body": {"near": "x"}}})
    result = handler({**envelope, "event_id": "2" * 32,
                      "payload": {"body_base64": encoded,
                                  "decoded_body_near": {"original_sha256": "ALPHA_SECRET_123"}}})
    encoded_result = json.dumps(result, sort_keys=True)
    assert "ALPHA_SECRET_123" not in encoded_result and "decoded_body_near" in encoded_result


def test_short_secret_does_not_corrupt_generated_provenance():
    index = redaction_module.SecretIndex(); index.register("opaque-a", b"a", "runtime")
    handler = projection_module.CaptureEventProjection(index)
    body = b"data"; expected_hash = hashlib.sha256(body).hexdigest()
    event = {"schema": "sage-observation-event-v2", "event_id": "3" * 32,
             "process_uuid": "4" * 32, "pid": 5, "seq": 1, "time_ns": 1,
             "kind": "model_request", "payload": {
                 "request_id": "5" * 32, "method": "POST", "body_base64": base64.b64encode(body).decode()
             }}
    result = handler(event); decoded = result["payload"]["decoded_body"]
    assert decoded["metadata"]["original_sha256"] == expected_hash
    assert any(identity["handle"] == "opaque-a" for identity in _identities(decoded))
    assert base64.b64decode(decoded["redacted_base64"]) == b"d<SAGE_REDACTED>t<SAGE_REDACTED>"


def test_descriptor_rendering_is_metadata_only_and_escaped_paths_are_unknown():
    handler = projection_module.CaptureEventProjection(_index())
    path_rendering = b"/tmp/\\x41LPHA_SECRET_123"
    line = b'501 1770000000.2 write(7<' + path_rendering + b'>, "x", 1) = 1'
    event = {"schema": "sage-observation-event-v2", "event_id": "6" * 32,
             "process_uuid": "7" * 32, "pid": 5, "seq": 1, "time_ns": 1,
             "kind": "tool_event", "payload": {"event": "os_syscall", "kind": "write_attempt",
                                                   "line_base64": base64.b64encode(line).decode()}}
    syscall = handler(event)["payload"]["decoded_os_syscall"]
    encoded = json.dumps(syscall, sort_keys=True).encode()
    assert syscall["semantic_known"] is True and syscall["path_interpretation"] == "unknown"
    assert "path_metadata" in syscall and "redacted_path" not in syscall
    assert path_rendering not in encoded and base64.b64encode(path_rendering) not in encoded


def test_denied_without_body_preserves_outcome_without_empty_observation():
    handler = projection_module.CaptureEventProjection(_index())
    event = {"schema": "sage-observation-event-v2", "event_id": "8" * 32,
             "process_uuid": "9" * 32, "pid": 5, "seq": 1, "time_ns": 1,
             "kind": "control_response", "payload": {
                 "request_id": "0" * 32, "outcome": "denied_before_dispatch",
                 "exception_type": "ResourceBudgetExceeded",
             }}
    payload = handler(event)["payload"]
    assert payload["outcome"] == "denied_before_dispatch"
    assert "decoded_body" not in payload and "body_unavailable" not in payload
