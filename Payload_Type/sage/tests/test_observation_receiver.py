import fcntl
import hashlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import select
import threading
import time
import uuid

import pytest

from ai import observation_channel as channel


SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-eval-gauge/scripts/observation_receiver.py"
SPEC = importlib.util.spec_from_file_location("sage_observation_receiver", SCRIPT)
receiver_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(receiver_module)


def _event(*, event_id=None, process_uuid=None, pid=123, seq=1, payload=None):
    return {
        "schema": receiver_module.EVENT_SCHEMA,
        "event_id": event_id or uuid.uuid4().hex,
        "process_uuid": process_uuid or uuid.uuid4().hex,
        "pid": pid,
        "seq": seq,
        "time_ns": time.time_ns(),
        "kind": "tool_event",
        "payload": payload or {"synthetic": True},
    }


def _raw_send(directory, chunks, responses, hold=False):
    lock_fd = os.open(directory / "lock", os.O_RDWR | os.O_NONBLOCK)
    events_fd = os.open(directory / "events.fifo", os.O_WRONLY | os.O_NONBLOCK)
    acks_fd = os.open(directory / "acks.fifo", os.O_RDONLY | os.O_NONBLOCK)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        for chunk in chunks:
            os.write(events_fd, chunk)
        if hold:
            time.sleep(0.2)
        elif select.select([acks_fd], [], [], 1)[0]:
            responses.append(os.read(acks_fd, 33))
    finally:
        os.close(acks_fd); os.close(events_fd); os.close(lock_fd)


def _child_emit(directory, result):
    os.environ[channel.ENV_NAME] = str(directory)
    try:
        channel.emit("process_start", {"synthetic": "process"})
        result.put("accepted")
    except Exception as error:
        result.put(type(error).__name__)


def test_real_emit_durable_redaction_and_cleanup(tmp_path, monkeypatch):
    record_path = (tmp_path / "events.jsonl").resolve()
    parent = (tmp_path / "temporary").resolve(); parent.mkdir()
    with receiver_module.ObservationReceiver(
        record_path,
        lambda event: {"classification": "synthetic", "count": len(event["payload"])},
        temporary_parent=parent,
    ) as receiver:
        directory = receiver.directory
        assert directory and directory.is_absolute()
        assert oct(directory.stat().st_mode & 0o777) == "0o700"
        monkeypatch.setenv(channel.ENV_NAME, str(directory))
        channel._reset_after_fork()
        outcome = []
        call = threading.Thread(target=lambda: outcome.append(channel.emit("tool_event", {"secret": "SENTINEL"})))
        call.start()
        accepted = receiver.serve_one(2)
        call.join(2)
        assert outcome == [True]
        assert accepted["seq"] == 1
        line = record_path.read_text()
        assert "SENTINEL" not in line and accepted == json.loads(line)
        assert oct(record_path.stat().st_mode & 0o777) == "0o600"
        assert set(accepted) == {"event_id", "kind", "process_uuid", "seq", "pid", "frame_bytes","frame_limit_bytes","frame_sha256", "record"}
    assert not directory.exists()
    assert record_path.exists()


def test_raw_frame_hash_and_nack_for_duplicate_and_changed_pid(tmp_path):
    parent = (tmp_path / "temporary").resolve(); parent.mkdir()
    process_uuid = uuid.uuid4().hex
    with receiver_module.ObservationReceiver((tmp_path / "events.jsonl").resolve(), lambda event: {}, temporary_parent=parent) as receiver:
        one = _event(process_uuid=process_uuid,payload={"secret":"DUPLICATE_PAYLOAD_SENTINEL"})
        body = json.dumps(one, sort_keys=True, separators=(",", ":")).encode(); frame = len(body).to_bytes(8, "big") + body
        responses = []; sender = threading.Thread(target=_raw_send, args=(receiver.directory, [frame], responses)); sender.start()
        result = receiver.serve_one(2); sender.join(2)
        assert responses == [b"1" + one["event_id"].encode()]
        assert result["frame_sha256"] == hashlib.sha256(frame).hexdigest()

        duplicate_body = json.dumps(one, sort_keys=True, separators=(",", ":")).encode()
        responses = []; sender = threading.Thread(target=_raw_send, args=(receiver.directory, [len(duplicate_body).to_bytes(8, "big") + duplicate_body], responses)); sender.start()
        with pytest.raises(receiver_module.ObservationReceiverError):
            receiver.serve_one(2)
        sender.join(2)
        assert responses == [b"0" + one["event_id"].encode()]
        with pytest.raises(receiver_module.ObservationReceiverError):
            receiver.serve_one(1)
    lines = [json.loads(value) for value in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert lines[-1]["reason"] == "duplicate_event"
    assert set(lines[-1]) == {"reason","frame_bytes","frame_limit_bytes","frame_sha256",
                              "pid","process_uuid","seq","kind","event_id"}
    assert {key:lines[-1][key] for key in ("pid","process_uuid","seq","kind","event_id")} == {
        "pid":one["pid"],"process_uuid":one["process_uuid"],"seq":one["seq"],
        "kind":one["kind"],"event_id":one["event_id"]}
    assert "DUPLICATE_PAYLOAD_SENTINEL" not in (tmp_path / "events.jsonl").read_text()

    with receiver_module.ObservationReceiver((tmp_path / "pid.jsonl").resolve(), lambda event: {}, temporary_parent=parent) as receiver:
        first, second = _event(process_uuid=process_uuid), _event(process_uuid=process_uuid, pid=124, seq=2)
        for event in (first, second):
            encoded = json.dumps(event).encode(); responses = []
            sender = threading.Thread(target=_raw_send, args=(receiver.directory, [len(encoded).to_bytes(8, "big") + encoded], responses)); sender.start()
            if event is first:
                receiver.serve_one(2)
            else:
                with pytest.raises(receiver_module.ObservationReceiverError): receiver.serve_one(2)
            sender.join(2)
    changed=json.loads((tmp_path / "pid.jsonl").read_text().splitlines()[-1])
    assert changed["reason"] == "changed_pid" and changed["prior_pid"]==first["pid"]
    assert changed["prior_seq"]==first["seq"] and changed["pid"]==second["pid"] and changed["seq"]==second["seq"]


def test_sequence_gap_retains_only_validated_headers_and_rejects_payload(tmp_path):
    parent=(tmp_path/"temporary").resolve();parent.mkdir();record=(tmp_path/"gap.jsonl").resolve()
    event=_event(seq=2,payload={"secret":"ARBITRARY_PAYLOAD_SENTINEL"})
    with receiver_module.ObservationReceiver(record,lambda event:pytest.fail("handler called"),temporary_parent=parent) as receiver:
        body=json.dumps(event).encode();responses=[]
        sender=threading.Thread(target=_raw_send,args=(receiver.directory,[len(body).to_bytes(8,"big")+body],responses));sender.start()
        with pytest.raises(receiver_module.ObservationReceiverError):receiver.serve_one(2)
        sender.join(2);assert responses==[b"0"+event["event_id"].encode()]
    failure=json.loads(record.read_text());assert failure["reason"]=="sequence_gap"
    assert {k:failure[k] for k in ("pid","process_uuid","seq","kind","event_id")} == {
        k:event[k] for k in ("pid","process_uuid","seq","kind","event_id")}
    assert "ARBITRARY_PAYLOAD_SENTINEL" not in record.read_text() and "time_ns" not in failure


def test_process_emit_sequence_and_no_data_timeout(tmp_path):
    parent = (tmp_path / "temporary").resolve(); parent.mkdir()
    context = multiprocessing.get_context("fork")
    with receiver_module.ObservationReceiver((tmp_path / "events.jsonl").resolve(), lambda event: {"ok": True}, temporary_parent=parent) as receiver:
        with pytest.raises(receiver_module.ObservationReceiverTimeout):
            receiver.serve_one(0.02)
        result = context.Queue(); process = context.Process(target=_child_emit, args=(receiver.directory, result)); process.start()
        receiver.serve_one(2); process.join(2)
        assert process.exitcode == 0 and result.get(timeout=1) == "accepted"

def test_accept_poll_does_not_bound_an_accepted_frame_transaction(tmp_path,monkeypatch):
    parent=(tmp_path/"temporary").resolve();parent.mkdir();record=(tmp_path/"events.jsonl").resolve()
    original=receiver_module.os.fsync
    with receiver_module.ObservationReceiver(record,lambda event:{"ok":True},temporary_parent=parent) as receiver:
        monkeypatch.setenv(channel.ENV_NAME,str(receiver.directory));channel._reset_after_fork()
        monkeypatch.setattr(receiver_module.os,"fsync",lambda fd:(time.sleep(.25),original(fd))[1])
        result=[];thread=threading.Thread(target=lambda:result.append(channel.emit("tool_event",{"synthetic":True})));thread.start()
        row=receiver.serve_one(.2);thread.join(2)
        assert result==[True] and row["frame_bytes"]<=row["frame_limit_bytes"]


@pytest.mark.parametrize("content,reason", [
    (b"bad", "invalid_json"),
    (json.dumps({"schema": "wrong"}).encode(), "invalid_envelope"),
    (json.dumps({**_event(), "kind": []}).encode(), "invalid_envelope"),
])
def test_malformed_frames_fail_sticky_with_redacted_reason(tmp_path, content, reason):
    parent = (tmp_path / "temporary").resolve(); parent.mkdir()
    record = (tmp_path / f"{reason}.jsonl").resolve()
    with receiver_module.ObservationReceiver(record, lambda event: pytest.fail("handler called"), temporary_parent=parent) as receiver:
        frame = len(content).to_bytes(8, "big") + content
        sender = threading.Thread(target=_raw_send, args=(receiver.directory, [frame], [])); sender.start()
        with pytest.raises(receiver_module.ObservationReceiverError): receiver.serve_one(2)
        sender.join(2)
        with pytest.raises(receiver_module.ObservationReceiverError): receiver.serve_one(1)
    failure=json.loads(record.read_text());assert failure["reason"] == reason
    assert set(failure)=={"reason","frame_bytes","frame_limit_bytes","frame_sha256"}


@pytest.mark.parametrize("handler_error", [RuntimeError, receiver_module.ObservationReceiverError])
def test_oversize_partial_and_handler_failure_do_not_leak(tmp_path, handler_error):
    parent = (tmp_path / "temporary").resolve(); parent.mkdir()
    cases = [
        (receiver_module.MAX_FRAME_BYTES.to_bytes(8, "big")[:-1] + b"\x01", "invalid_frame_length"),
        ((5).to_bytes(8, "big"), "partial_frame"),
        ((5).to_bytes(8, "big") + b"x", "partial_frame"),
    ]
    for index, (frame, reason) in enumerate(cases):
        record = (tmp_path / f"failure-{index}.jsonl").resolve()
        with receiver_module.ObservationReceiver(record, lambda event: {}, temporary_parent=parent) as receiver:
            sender = threading.Thread(target=_raw_send, args=(receiver.directory, [frame], [], True)); sender.start()
            with pytest.raises(receiver_module.ObservationReceiverError): receiver.serve_one(0.1)
            sender.join(1)
            with pytest.raises(receiver_module.ObservationReceiverError, match="stream failed"):
                receiver.serve_one(0.1)
        assert json.loads(record.read_text())["reason"] == reason

    record = (tmp_path / "handler.jsonl").resolve()
    def failing_handler(event):
        raise handler_error(event["payload"]["secret"])
    with receiver_module.ObservationReceiver(record, failing_handler, temporary_parent=parent) as receiver:
        event = _event(payload={"secret": "SENTINEL"}); body = json.dumps(event).encode(); responses = []
        sender = threading.Thread(target=_raw_send, args=(receiver.directory, [len(body).to_bytes(8, "big") + body], responses)); sender.start()
        with pytest.raises(receiver_module.ObservationReceiverError): receiver.serve_one(2)
        sender.join(2)
        assert responses == [b"0" + event["event_id"].encode()]
    assert "SENTINEL" not in record.read_text()
    assert json.loads(record.read_text())["reason"] == "handler_failed"

def test_oversize_length_is_rejected_before_payload_read(tmp_path):
    parent=(tmp_path/"temporary").resolve();parent.mkdir();record=(tmp_path/"oversize.jsonl").resolve()
    prefix=(receiver_module.MAX_FRAME_BYTES+1).to_bytes(8,"big")
    with receiver_module.ObservationReceiver(record,lambda event:pytest.fail("handler called"),temporary_parent=parent) as receiver:
        sender=threading.Thread(target=_raw_send,args=(receiver.directory,[prefix],[],True));sender.start()
        with pytest.raises(receiver_module.ObservationReceiverError):receiver.serve_one(.1)
        sender.join(1)
    failure=json.loads(record.read_text());assert failure["reason"]=="invalid_frame_length"
    assert failure["frame_bytes"]==8 and failure["frame_limit_bytes"]==receiver_module.MAX_FRAME_BYTES


def test_preexisting_or_symlink_paths_refuse_without_overwrite(tmp_path):
    parent = (tmp_path / "temporary").resolve(); parent.mkdir()
    record = (tmp_path / "events.jsonl").resolve(); record.write_text("keep")
    with pytest.raises(receiver_module.ObservationReceiverError):
        receiver_module.ObservationReceiver(record, lambda event: {}, temporary_parent=parent).__enter__()
    assert record.read_text() == "keep"
    symlink = tmp_path / "parent-link"; symlink.symlink_to(parent, target_is_directory=True)
    with pytest.raises(receiver_module.ObservationReceiverError):
        receiver_module.ObservationReceiver((tmp_path / "other.jsonl").resolve(), lambda event: {}, temporary_parent=symlink).__enter__()
