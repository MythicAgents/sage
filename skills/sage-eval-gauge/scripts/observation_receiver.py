"""Operator-owned durable receiver for Sage's local observation FIFO protocol.

The receiver validates delivery and producer sequencing only.  It does not
classify events or establish that an observation set is complete.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import select
import stat
import tempfile
import time
from typing import Any


EVENT_SCHEMA = "sage-observation-event-v2"
MAX_FRAME_BYTES = 16 * 1024 * 1024
FRAME_TRANSACTION_SECONDS = 4.0
_ALLOWED_KINDS = frozenset(
    {
        "process_start",
        "process_end",
        "model_request",
        "model_response",
        "control_request",
        "control_response",
        "log_record",
        "tool_event",
        "config_snapshot",
    }
)
_ENVELOPE_KEYS = frozenset(
    {"schema", "event_id", "process_uuid", "pid", "seq", "time_ns", "kind", "payload"}
)


class ObservationReceiverError(RuntimeError):
    """A generic receiver failure that does not include event contents."""


class ObservationReceiverTimeout(ObservationReceiverError):
    """No event arrived before a bounded serve operation expired."""


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _hex_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


class ObservationReceiver:
    """Receive events and persist only caller-redacted records.

    Producer identity and sequencing are telemetry supplied by the producer;
    these checks do not attest the producer or inventory completeness.
    """

    def __init__(
        self,
        record_path: str | os.PathLike[str],
        handler: Callable[[Mapping[str, Any]], Any],
        *,
        temporary_parent: str | os.PathLike[str] | None = None,
    ) -> None:
        self._record_path = Path(record_path)
        self._handler = handler
        self._temporary_parent = Path(temporary_parent) if temporary_parent is not None else None
        self.directory: Path | None = None
        self._record_fd = -1
        self._events_fd = -1
        self._acks_fd = -1
        self._lock_fd = -1
        self._seen_events: set[str] = set()
        self._processes: dict[str, tuple[int, int]] = {}
        self._failure: ObservationReceiverError | None = None

    def __enter__(self) -> "ObservationReceiver":
        if self.directory is not None or not self._record_path.is_absolute() or not callable(self._handler):
            raise ObservationReceiverError("observation receiver configuration is invalid")
        parent = self._temporary_parent
        if parent is not None:
            try:
                metadata = os.stat(parent, follow_symlinks=False)
            except OSError:
                raise ObservationReceiverError("observation receiver configuration is invalid") from None
            if not parent.is_absolute() or not stat.S_ISDIR(metadata.st_mode):
                raise ObservationReceiverError("observation receiver configuration is invalid")
        try:
            self._record_fd = os.open(
                self._record_path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            record_metadata = os.fstat(self._record_fd)
            if (
                not stat.S_ISREG(record_metadata.st_mode)
                or record_metadata.st_uid != os.getuid()
                or stat.S_IMODE(record_metadata.st_mode) != 0o600
            ):
                raise ObservationReceiverError("observation receiver configuration is invalid")
            directory = Path(tempfile.mkdtemp(prefix="sage-observation-", dir=parent))
            os.chmod(directory, 0o700)
            self.directory = directory
            os.mkfifo(directory / "events.fifo", 0o600)
            os.mkfifo(directory / "acks.fifo", 0o600)
            os.chmod(directory / "events.fifo", 0o600)
            os.chmod(directory / "acks.fifo", 0o600)
            self._lock_fd = os.open(
                directory / "lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            os.fchmod(self._lock_fd, 0o600)
            self._events_fd = os.open(directory / "events.fifo", os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
            self._acks_fd = os.open(directory / "acks.fifo", os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
            return self
        except ObservationReceiverError:
            self.close()
            raise
        except OSError:
            self.close()
            raise ObservationReceiverError("observation receiver configuration is invalid") from None

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _read_exact(self, size: int, deadline: float) -> bytes:
        value = bytearray()
        while len(value) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._events_fd], [], [], remaining)[0]:
                if not value:
                    raise ObservationReceiverTimeout("observation receive timed out")
                raise ObservationReceiverError("partial_frame")
            try:
                part = os.read(self._events_fd, size - len(value))
            except BlockingIOError:
                continue
            except OSError:
                raise ObservationReceiverError("read_failed") from None
            if not part:
                raise ObservationReceiverError("peer_closed" if not value else "partial_frame")
            value.extend(part)
        return bytes(value)

    def _write_ack(self, event_id: str, accepted: bool, deadline: float) -> None:
        value = (b"1" if accepted else b"0") + event_id.encode("ascii")
        offset = 0
        while offset < len(value):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self._acks_fd], [], remaining)[1]:
                raise ObservationReceiverError("ack_failed")
            try:
                count = os.write(self._acks_fd, value[offset:])
            except BlockingIOError:
                continue
            except OSError:
                raise ObservationReceiverError("ack_failed") from None
            if count <= 0:
                raise ObservationReceiverError("ack_failed")
            offset += count

    def _append(self, value: Mapping[str, Any]) -> None:
        try:
            encoded = (
                json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ObservationReceiverError("handler_failed") from None
        offset = 0
        while offset < len(encoded):
            try:
                count = os.write(self._record_fd, encoded[offset:])
            except OSError:
                raise ObservationReceiverError("durable_write_failed") from None
            if count <= 0:
                raise ObservationReceiverError("durable_write_failed")
            offset += count
        try:
            os.fsync(self._record_fd)
        except OSError:
            raise ObservationReceiverError("durable_write_failed") from None

    def _sticky_failure(self, reason: str, frame: bytes, event_id: str | None, deadline: float,
                        safe_metadata: Mapping[str, Any] | None = None) -> None:
        failure = ObservationReceiverError("observation stream failed")
        failure.reason = reason;failure.frame_bytes=len(frame);failure.frame_limit_bytes=MAX_FRAME_BYTES
        self._failure = failure
        try:
            record={"reason": reason, "frame_bytes":len(frame),"frame_limit_bytes":MAX_FRAME_BYTES,
                    "frame_sha256": hashlib.sha256(frame).hexdigest()}
            if safe_metadata is not None:record.update(safe_metadata)
            self._append(record)
        except ObservationReceiverError:
            raise failure from None
        if event_id is not None:
            try:
                self._write_ack(event_id, False, deadline)
            except ObservationReceiverError:
                pass
        raise failure

    def _validate(self, event: object) -> tuple[str, str, int, int, int, str, Mapping[str, Any]]:
        if not isinstance(event, dict) or frozenset(event) != _ENVELOPE_KEYS:
            raise ObservationReceiverError("invalid_envelope")
        event_id = event["event_id"]
        process_uuid = event["process_uuid"]
        if event["schema"] != EVENT_SCHEMA or not _hex_identifier(event_id) or not _hex_identifier(process_uuid):
            raise ObservationReceiverError("invalid_envelope")
        pid, seq, time_ns, kind, payload = (
            event["pid"], event["seq"], event["time_ns"], event["kind"], event["payload"]
        )
        if (
            not _positive_integer(pid)
            or not _positive_integer(seq)
            or not _positive_integer(time_ns)
            or not isinstance(kind, str)
            or kind not in _ALLOWED_KINDS
            or not isinstance(payload, dict)
        ):
            raise ObservationReceiverError("invalid_envelope")
        return event_id, process_uuid, pid, seq, time_ns, kind, payload

    def serve_one(self, timeout: float) -> Mapping[str, Any]:
        if self.directory is None:
            raise ObservationReceiverError("observation receiver is closed")
        if self._failure is not None:
            raise self._failure
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ObservationReceiverError("observation receive timeout is invalid")
        deadline = time.monotonic() + timeout
        frame = b""
        event_id: str | None = None
        sequence_metadata: dict[str, Any] | None = None
        try:
            prefix = self._read_exact(8, deadline)
            frame = prefix
            length = int.from_bytes(prefix, "big")
            if length <= 0 or length > MAX_FRAME_BYTES:
                raise ObservationReceiverError("invalid_frame_length")
            deadline=time.monotonic()+FRAME_TRANSACTION_SECONDS
            encoded = self._read_exact(length, deadline)
            frame += encoded
            try:
                event = json.loads(encoded.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                raise ObservationReceiverError("invalid_json") from None
            if isinstance(event, dict) and _hex_identifier(event.get("event_id")):
                event_id = event["event_id"]
            event_id, process_uuid, pid, seq, _time_ns, kind, _payload = self._validate(event)
            if event_id in self._seen_events:
                sequence_metadata={"pid":pid,"process_uuid":process_uuid,"seq":seq,"kind":kind,
                                   "event_id":event_id}
                raise ObservationReceiverError("duplicate_event")
            prior = self._processes.get(process_uuid)
            if prior is None:
                if seq != 1:
                    sequence_metadata={"pid":pid,"process_uuid":process_uuid,"seq":seq,"kind":kind,
                                       "event_id":event_id}
                    raise ObservationReceiverError("sequence_gap")
            elif pid != prior[0] or seq != prior[1] + 1:
                sequence_metadata={"pid":pid,"process_uuid":process_uuid,"seq":seq,"kind":kind,
                                   "event_id":event_id,"prior_pid":prior[0],"prior_seq":prior[1]}
                raise ObservationReceiverError("sequence_gap" if pid == prior[0] else "changed_pid")
            try:
                redacted = self._handler(event)
            except Exception:
                # Handler exceptions are external data even when their type is ours.
                self._sticky_failure("handler_failed", frame, event_id, deadline)
            record = {
                "event_id": event_id,
                "kind": kind,
                "process_uuid": process_uuid,
                "seq": seq,
                "pid": pid,
                "frame_bytes":len(frame),
                "frame_limit_bytes":MAX_FRAME_BYTES,
                "frame_sha256": hashlib.sha256(frame).hexdigest(),
                "record": redacted,
            }
            self._append(record)
            self._seen_events.add(event_id)
            self._processes[process_uuid] = (pid, seq)
            self._write_ack(event_id, True, deadline)
            return record
        except ObservationReceiverTimeout:
            if frame:
                self._sticky_failure("partial_frame", frame, event_id, deadline)
            raise
        except ObservationReceiverError as error:
            reason = str(error)
            if reason == "observation stream failed":
                raise
            self._sticky_failure(reason, frame, event_id, deadline, sequence_metadata)

    def close(self) -> None:
        for descriptor_name in ("_events_fd", "_acks_fd", "_lock_fd", "_record_fd"):
            descriptor = getattr(self, descriptor_name)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, descriptor_name, -1)
        if self.directory is not None:
            directory, self.directory = self.directory, None
            for name in ("events.fifo", "acks.fifo", "lock"):
                try:
                    os.unlink(directory / name)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            try:
                os.rmdir(directory)
            except OSError:
                pass
