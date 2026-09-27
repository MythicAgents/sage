import json
import multiprocessing
import os
from pathlib import Path
import select
import signal
import threading

import pytest

from ai import observation_channel as channel

def _directory(path: Path):
    path.mkdir()
    os.chmod(path, 0o700)
    for name in ("events.fifo", "acks.fifo"):
        os.mkfifo(path / name, 0o600)
    (path / "lock").write_bytes(b"")
    os.chmod(path / "lock", 0o600)
    return path

def _observer(path, frames, count, ack=lambda event: b"1" + event["event_id"].encode()):
    ready = threading.Event()
    def read_exact(fd, size):
        value = b""
        while len(value) < size:
            assert select.select([fd], [], [], 5)[0]
            value += os.read(fd, size - len(value))
        return value
    def run():
        events = os.open(path / "events.fifo", os.O_RDWR | os.O_NONBLOCK)
        acks = os.open(path / "acks.fifo", os.O_RDWR | os.O_NONBLOCK)
        ready.set()
        try:
            for _ in range(count):
                event = json.loads(read_exact(events, int.from_bytes(read_exact(events, 8), "big")))
                frames.append(event)
                response = ack(event)
                if response is not None:
                    os.write(acks, response)
        finally:
            os.close(acks); os.close(events)
    thread = threading.Thread(target=run, daemon=True); thread.start()
    assert ready.wait(2)
    return thread

def _child_emit(path, gate):
    os.environ[channel.ENV_NAME] = str(path)
    gate.wait()
    channel.emit("process_start", {"synthetic": os.getpid()})

def test_fifo_frame_payload_and_nonce_ack(tmp_path, monkeypatch):
    path, frames = _directory(tmp_path / "observer"), []
    thread = _observer(path, frames, 1)
    monkeypatch.setenv(channel.ENV_NAME, str(path))
    payload = {"synthetic": [1, "two"]}
    assert channel.emit("tool_event", payload) is True
    thread.join(2)
    assert frames[0]["schema"] == "sage-observation-event-v2"
    assert len(frames[0]["event_id"]) == 32
    assert frames[0]["payload"] == payload

def test_main_sigint_is_replayed_after_delivered_frame_without_sequence_gap(tmp_path, monkeypatch):
    path, frames = _directory(tmp_path / "observer"), []
    thread = _observer(path, frames, 2)
    monkeypatch.setenv(channel.ENV_NAME, str(path))
    interrupted = []
    previous = signal.getsignal(signal.SIGINT)
    def handler(signum, frame):
        interrupted.append(signum)
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, handler)
    write = channel._write_all
    once = [False]
    def interrupt_then_write(descriptor, frame, deadline):
        if not once[0]:
            once[0] = True
            os.kill(os.getpid(), signal.SIGINT)
        write(descriptor, frame, deadline)
    monkeypatch.setattr(channel, "_write_all", interrupt_then_write)
    try:
        with pytest.raises(KeyboardInterrupt):
            channel.emit("control_request", {"synthetic": 1})
        assert signal.getsignal(signal.SIGINT) is handler
        assert channel.emit("process_end", {"synthetic": 2}) is True
    finally:
        signal.signal(signal.SIGINT, previous)
    thread.join(2)
    assert interrupted == [signal.SIGINT]
    assert len(frames) == 2 and frames[1]["seq"] == frames[0]["seq"] + 1

def test_deferred_sigint_does_not_hide_transport_failure(tmp_path, monkeypatch):
    path = _directory(tmp_path / "observer")
    peers = [os.open(path / name, os.O_RDWR | os.O_NONBLOCK) for name in ("events.fifo", "acks.fifo")]
    monkeypatch.setenv(channel.ENV_NAME, str(path))
    previous = signal.getsignal(signal.SIGINT); interrupted = []
    def handler(signum, frame):
        interrupted.append(signum)
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, handler)
    def fail(*_args):
        os.kill(os.getpid(), signal.SIGINT)
        raise channel.ObservationChannelError("observation delivery failed", stage="write")
    monkeypatch.setattr(channel, "_write_all", fail)
    try:
        with pytest.raises(channel.ObservationChannelError) as caught:
            channel.emit("process_start", {})
        assert caught.value.stage == "write"
        assert signal.getsignal(signal.SIGINT) is handler
    finally:
        signal.signal(signal.SIGINT, previous)
        for descriptor in peers: os.close(descriptor)
    assert interrupted == [signal.SIGINT]

def test_unconfigured_wrong_paths_types_permissions_and_symlinks_fail(tmp_path, monkeypatch):
    monkeypatch.delenv(channel.ENV_NAME, raising=False)
    monkeypatch.setattr(channel, "_paths", lambda: pytest.fail("files inspected"))
    assert channel.emit("process_start", {}) is False
    monkeypatch.undo()
    ordinary = tmp_path / "ordinary"; ordinary.write_text("x")
    symlink = tmp_path / "link"; symlink.symlink_to(ordinary)
    for value in ("relative", tmp_path / "missing", ordinary, symlink):
        monkeypatch.setenv(channel.ENV_NAME, str(value))
        with pytest.raises(channel.ObservationChannelError, match="configuration"):
            channel.emit("process_start", {})

    path = _directory(tmp_path / "observer")
    monkeypatch.setenv(channel.ENV_NAME, str(path))
    os.chmod(path, 0o750)
    with pytest.raises(channel.ObservationChannelError, match="configuration"):
        channel.emit("process_start", {})
    os.chmod(path, 0o700); os.chmod(path / "events.fifo", 0o640)
    with pytest.raises(channel.ObservationChannelError, match="configuration"):
        channel.emit("process_start", {})

def test_missing_peer_ack_mismatch_overlength_and_bad_payload_fail(tmp_path, monkeypatch):
    path = _directory(tmp_path / "no-peer")
    monkeypatch.setenv(channel.ENV_NAME, str(path))
    with pytest.raises(channel.ObservationChannelError, match="delivery"):
        channel.emit("process_start", {})
    path2, frames = _directory(tmp_path / "bad-ack"), []
    monkeypatch.setenv(channel.ENV_NAME, str(path2))
    thread = _observer(path2, frames, 1, ack=lambda event: b"1" + b"0" * 32)
    with pytest.raises(channel.ObservationChannelError, match="acknowledgement"):
        channel.emit("process_start", {})
    thread.join(2)
    with pytest.raises(channel.ObservationChannelError, match="frame"):
        channel.emit("tool_event", {"synthetic": "x" * channel.MAX_FRAME_BYTES})
    with pytest.raises(channel.ObservationChannelError, match="serializable"):
        channel.emit("tool_event", {"synthetic": {1}})

def test_threads_and_forked_processes_serialize_without_identity_collision(tmp_path, monkeypatch):
    path, frames = _directory(tmp_path / "observer"), []
    context = multiprocessing.get_context("fork")
    gate = context.Event()
    processes = [context.Process(target=_child_emit, args=(path, gate)) for _ in range(2)]
    for process in processes: process.start()
    thread = _observer(path, frames, 8)
    monkeypatch.setenv(channel.ENV_NAME, str(path)); gate.set()
    calls = [threading.Thread(target=channel.emit, args=("control_request", {"synthetic": i})) for i in range(6)]
    for call in calls: call.start()
    for call in calls: call.join(5)
    for process in processes:
        process.join(5); assert process.exitcode == 0
    thread.join(5)
    assert len(frames) == 8
    by_process = {}
    for event in frames:
        by_process.setdefault(event["process_uuid"], []).append(event["seq"])
    assert len(by_process) == 3
    assert all(values == sorted(values) for values in by_process.values())
