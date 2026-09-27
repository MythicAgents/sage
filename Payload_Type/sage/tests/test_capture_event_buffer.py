from __future__ import annotations

import base64
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[3]
BUFFER = ROOT / "skills/sage-eval-gauge/scripts/capture_event_buffer.py"
PROJECTION = ROOT / "skills/sage-eval-gauge/scripts/capture_event_projection.py"
REDACTION = ROOT / "skills/sage-eval-gauge/scripts/capture_redaction.py"
RECEIVER = ROOT / "skills/sage-eval-gauge/scripts/observation_receiver.py"
BOOTSTRAP = ROOT / "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path); assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value


buffer_module = _module(BUFFER, "m3_capture_event_buffer")
projection_module = _module(PROJECTION, "m3_capture_event_buffer_projection")
redaction_module = _module(REDACTION, "m3_capture_event_buffer_redaction")
receiver_module = _module(RECEIVER, "m3_capture_event_buffer_receiver")


def _event(number=1, payload=None):
    return {"schema": "sage-observation-event-v2", "event_id": f"{number:032x}",
            "process_uuid": "a" * 32, "pid": 4, "seq": number, "time_ns": number,
            "kind": "model_request", "payload": payload or {"request_id": "b" * 32}}


def test_fifo_receipt_precedes_runtime_secret_registration(tmp_path):
    os.chmod(tmp_path, 0o700); durable = tmp_path / "receipts.jsonl"
    buffer = buffer_module.CaptureEventBuffer(max_events=2, max_bytes=100_000)
    secret = b"SYNTHETIC_RUNTIME_LATE_SECRET"; body = b'{"token":"' + secret + b'"}'
    with receiver_module.ObservationReceiver(durable, buffer, temporary_parent=tmp_path) as receiver:
        records = []
        thread = threading.Thread(target=lambda: records.append(receiver.serve_one(5))); thread.start()
        code = ("import sys;sys.path.insert(0,sys.argv[1]);from ai.observation_channel import emit;"
                "emit('model_request',{'request_id':'c'*32,'body_base64':sys.argv[2]})")
        result = subprocess.run([sys.executable, "-c", code, str(ROOT / "Payload_Type/sage"),
                                 base64.b64encode(body).decode()],
                                env=dict(os.environ, SAGE_OBSERVATION_DIRECTORY=str(receiver.directory)),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=60, check=False)
        thread.join(timeout=10)
    assert result.returncode == 0 and len(records) == 1
    frozen = buffer.freeze(); persisted = durable.read_bytes()
    assert frozen["event_count"] == 1 and secret not in persisted
    assert base64.b64encode(secret) not in persisted and base64.b64encode(body) not in persisted
    index = redaction_module.SecretIndex(); index.register("late-runtime", secret, "runtime")
    projected = buffer.project(projection_module.CaptureEventProjection(index))
    rendered = json.dumps(projected, sort_keys=True)
    assert secret.decode() not in rendered and "late-runtime" in rendered


def test_registration_wait_diagnostic_survives_failed_setup_without_raw_rows(monkeypatch, tmp_path):
    bootstrap = _module(BOOTSTRAP, "m3_capture_event_buffer_bootstrap")
    os.chmod(tmp_path, 0o700); durable = tmp_path / "receipts.jsonl"
    buffer = buffer_module.CaptureEventBuffer(max_events=4, max_bytes=100_000)
    with receiver_module.ObservationReceiver(durable, buffer, temporary_parent=tmp_path) as receiver:
        async def absent(*_args):
            return {"consuming_container": [{"id": 9, "container_running": False,
                "deleted": True, "name": "SYNTHETIC_SECRET"}]}
        monkeypatch.setattr(bootstrap.mythic, "execute_custom_query", absent)
        monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", str(receiver.directory))
        serving = threading.Thread(target=lambda: receiver.serve_one(5)); serving.start()
        with pytest.raises(RuntimeError, match="Timed out"):
            asyncio.run(bootstrap.wait_for_running_sage_chat_container(
                object(), timeout_seconds=0, poll_seconds=0))
        serving.join(timeout=10)
    buffer.freeze(); buffer.clear(); persisted = durable.read_text()
    assert "SYNTHETIC_SECRET" not in persisted
    record = json.loads(persisted)
    assert record["record"]["registration_diagnostic"] == {
        "attempt_count": 1, "containers": [{"deleted": True, "id": 9, "running": False}],
        "error_category": "timeout", "event": "sage_chat_registration_diagnostic",
        "schema": "sage-chat-registration-diagnostic-v1", "stage": "sage_chat_container_registration",
        "status": "timeout",
    }
    assert record["record"]["non_admission_diagnostic"] is True


def test_malformed_registration_diagnostic_does_not_persist_free_fields():
    buffer = buffer_module.CaptureEventBuffer(max_events=2, max_bytes=100_000)
    malformed = _event(payload={"event": "sage_chat_registration_diagnostic",
        "schema": "sage-chat-registration-diagnostic-v1", "stage": "sage_chat_container_registration",
        "status": "timeout", "attempt_count": 1, "containers": [], "error_category": "arbitrary",
        "free_field": "SYNTHETIC_SECRET"})
    malformed["kind"] = "tool_event"
    receipt = buffer(malformed)
    assert set(receipt) == {"schema", "event_id", "order", "raw_event_sha256"}
    second = _event(2, payload={**malformed["payload"], "status": {}, "error_category": []})
    second["kind"] = "tool_event"
    receipt = buffer(second)
    assert set(receipt) == {"schema", "event_id", "order", "raw_event_sha256"}


def test_limits_malformed_and_postfreeze_are_sticky():
    buffer = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=10_000)
    buffer(_event())
    with pytest.raises(buffer_module.EventBufferError, match="event buffer failed"):
        buffer(_event(2))
    with pytest.raises(buffer_module.EventBufferError, match="event buffer failed"): buffer.freeze()
    with pytest.raises(buffer_module.EventBufferError): buffer.project(lambda item: item)
    encoded_size = len(json.dumps(_event(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode())
    bytes_limited = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=encoded_size - 1)
    with pytest.raises(buffer_module.EventBufferError): bytes_limited(_event())
    with pytest.raises(buffer_module.EventBufferError): bytes_limited.freeze()
    malformed = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=10)
    with pytest.raises(buffer_module.EventBufferError): malformed({"raw": "secret"})
    with pytest.raises(buffer_module.EventBufferError): malformed(_event())
    with pytest.raises(buffer_module.EventBufferError): malformed.freeze()
    wrong_type = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=10_000)
    typed = _event(); typed["event_id"] = None
    with pytest.raises(buffer_module.EventBufferError): wrong_type(typed)
    with pytest.raises(buffer_module.EventBufferError): wrong_type(_event())
    frozen = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=10_000); frozen.freeze()
    with pytest.raises(buffer_module.EventBufferError): frozen(_event())
    with pytest.raises(buffer_module.EventBufferError): frozen.project(lambda item: item)
    invalid_projector = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=10_000)
    invalid_projector(_event()); invalid_projector.freeze()
    with pytest.raises(buffer_module.EventBufferError): invalid_projector.project(None)
    with pytest.raises(buffer_module.EventBufferError): invalid_projector.project(lambda item: item)


def test_caller_mutation_does_not_change_stored_copy_and_observer_is_immutable():
    buffer = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=10_000)
    event = _event(payload={"nested": {"value": "original"}}); buffer(event)
    event["payload"]["nested"]["value"] = "changed"; buffer.freeze(); observed = []
    def observer(value):
        observed.append(value["payload"]["nested"]["value"])
        with pytest.raises(TypeError): value["payload"]["nested"]["value"] = "mutation"
    projected = buffer.project(lambda value: value, observer=observer)
    assert observed == ["original"] and projected[0]["payload"]["nested"]["value"] == "original"


def test_projector_failure_returns_no_partial_and_clear_is_irreversible():
    buffer = buffer_module.CaptureEventBuffer(max_events=2, max_bytes=10_000)
    buffer(_event()); buffer(_event(2)); buffer.freeze(); calls = []
    def projector(event):
        calls.append(event["event_id"])
        if len(calls) == 2: raise ValueError("SYNTHETIC_PRIVATE_VALUE")
        return {"ok": True}
    with pytest.raises(buffer_module.EventBufferError, match="event buffer failed") as error:
        buffer.project(projector)
    assert "SYNTHETIC_PRIVATE_VALUE" not in str(error.value)
    with pytest.raises(buffer_module.EventBufferError): buffer.project(lambda item: item)
    buffer.clear()
    with pytest.raises(buffer_module.EventBufferError): buffer.freeze()


def test_freeze_race_never_accepts_late_event_as_usable():
    buffer = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=10_000); buffer.freeze()
    errors = []
    thread = threading.Thread(target=lambda: (buffer(_event()) if False else None))
    def late():
        try: buffer(_event())
        except Exception as error: errors.append(str(error))
    thread = threading.Thread(target=late); thread.start(); thread.join(timeout=2)
    assert errors == ["event buffer failed"]
    with pytest.raises(buffer_module.EventBufferError): buffer.project(lambda item: item)


def test_optional_visit_is_freeze_gated_one_time_and_before_project():
    early = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=10_000); early(_event())
    with pytest.raises(buffer_module.EventBufferError): early.visit(lambda _event: None)
    once = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=10_000); once(_event()); once.freeze()
    once.visit(lambda _event: None)
    with pytest.raises(buffer_module.EventBufferError): once.visit(lambda _event: None)
    after = buffer_module.CaptureEventBuffer(max_events=1, max_bytes=10_000); after(_event()); after.freeze(); after.project(lambda item: item)
    with pytest.raises(buffer_module.EventBufferError): after.visit(lambda _event: None)
