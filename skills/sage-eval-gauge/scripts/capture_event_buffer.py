#!/usr/bin/env python3
"""Bounded in-memory staging before final evaluator secret registration."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any


_ID = re.compile(r"^[0-9a-f]{32}$")
_KINDS = frozenset({"process_start", "process_end", "model_request", "model_response",
                    "control_request", "control_response", "log_record", "tool_event", "config_snapshot"})
_REGISTRATION_SCHEMA = "sage-chat-registration-diagnostic-v1"
_REGISTRATION_EVENT = "sage_chat_registration_diagnostic"
_REGISTRATION_STAGE = "sage_chat_container_registration"
_REGISTRATION_ERRORS = frozenset({"timeout", "connection", "permission", "value", "runtime", "other"})


def _registration_diagnostic(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("event") != _REGISTRATION_EVENT:
        return None
    status = payload.get("status")
    if not isinstance(status, str):
        return None
    expected = {"event", "schema", "stage", "status", "attempt_count", "containers"}
    if status in {"timeout", "error"}: expected.add("error_category")
    if (set(payload) != expected or payload.get("schema") != _REGISTRATION_SCHEMA
            or payload.get("stage") != _REGISTRATION_STAGE or status not in {"success", "timeout", "error"}
            or type(payload.get("attempt_count")) is not int or payload["attempt_count"] <= 0
            or status in {"timeout", "error"} and (
                not isinstance(payload.get("error_category"), str)
                or payload.get("error_category") not in _REGISTRATION_ERRORS)
            or not isinstance(payload.get("containers"), list) or len(payload["containers"]) > 32):
        return None
    rows = payload["containers"]
    if any(not isinstance(row, dict) or set(row) != {"id", "running", "deleted"}
           or type(row["id"]) is not int or type(row["running"]) is not bool
           or type(row["deleted"]) is not bool for row in rows):
        return None
    return {key: copy.deepcopy(payload[key]) for key in sorted(expected)}


class EventBufferError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise EventBufferError("event buffer failed") from None


def _immutable(value: Any) -> Any:
    if isinstance(value, dict): return MappingProxyType({key: _immutable(item) for key, item in value.items()})
    if isinstance(value, list): return tuple(_immutable(item) for item in value)
    return value


class CaptureEventBuffer:
    """Thread-safe ObservationReceiver handler with no raw public accessor."""

    def __init__(self, *, max_events: int, max_bytes: int) -> None:
        if type(max_events) is not int or type(max_bytes) is not int or max_events <= 0 or max_bytes <= 0:
            raise EventBufferError("event buffer limits are invalid")
        self._max_events = max_events; self._max_bytes = max_bytes
        self._events: list[tuple[dict[str, Any], bytes]] = []
        self._bytes = 0; self._lock = threading.Lock()
        self._frozen = False; self._visited = False; self._projected = False; self._closed = False; self._failure = False

    def _fail(self) -> None:
        self._failure = True
        raise EventBufferError("event buffer failed")

    def __call__(self, event: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closed or self._failure or self._frozen: self._fail()
            if not isinstance(event, Mapping): self._fail()
            try:
                encoded = _canonical(event)
                independent = json.loads(encoded.decode("utf-8"))
                expected = {"schema", "event_id", "process_uuid", "pid", "seq", "time_ns", "kind", "payload"}
                invalid = set(independent) != expected or independent["schema"] not in {
                    "sage-observation-event-v1", "sage-observation-event-v2"
                } or not _ID.fullmatch(independent["event_id"]) or not _ID.fullmatch(independent["process_uuid"]) \
                        or type(independent["pid"]) is not int or independent["pid"] <= 0 \
                        or type(independent["seq"]) is not int or independent["seq"] <= 0 \
                        or type(independent["time_ns"]) is not int or independent["time_ns"] <= 0 \
                        or independent["kind"] not in _KINDS or not isinstance(independent["payload"], dict)
            except Exception:
                invalid = True
            if invalid:
                self._fail()
            if len(self._events) >= self._max_events or self._bytes + len(encoded) > self._max_bytes:
                self._fail()
            self._events.append((independent, encoded)); self._bytes += len(encoded)
            receipt = {"schema": "sage-event-buffer-receipt-v1", "event_id": independent["event_id"],
                       "order": len(self._events) - 1,
                       "raw_event_sha256": hashlib.sha256(encoded).hexdigest()}
            diagnostic = (_registration_diagnostic(independent["payload"])
                          if independent["kind"] == "tool_event" else None)
            if diagnostic is not None:
                receipt.update({"registration_diagnostic": diagnostic, "non_admission_diagnostic": True})
            return receipt

    def freeze(self) -> dict[str, Any]:
        with self._lock:
            if self._closed or self._failure: self._fail()
            self._frozen = True
            stream = hashlib.sha256()
            for _event, encoded in self._events:
                stream.update(len(encoded).to_bytes(8, "big")); stream.update(encoded)
            return {"schema": "sage-event-buffer-freeze-v1", "event_count": len(self._events),
                    "raw_event_bytes": self._bytes, "raw_event_stream_sha256": stream.hexdigest()}

    def project(self, projector: Callable[[Mapping[str, Any]], Mapping[str, Any]], *,
                observer: Callable[[Mapping[str, Any]], Any] | None = None) -> list[Mapping[str, Any]]:
        with self._lock:
            if not callable(projector) or observer is not None and not callable(observer): self._fail()
            if self._closed or self._failure or not self._frozen or self._projected: self._fail()
            projected: list[Mapping[str, Any]] = []
            try:
                for event, _encoded in self._events:
                    if observer is not None: observer(_immutable(copy.deepcopy(event)))
                    value = projector(copy.deepcopy(event))
                    # Require a fresh, JSON-safe projection before exposing any results.
                    projected.append(json.loads(_canonical(value).decode("utf-8")))
            except Exception:
                self._failure = True
                raise EventBufferError("event buffer failed") from None
            self._projected = True
            return projected

    def visit(self, visitor: Callable[[Mapping[str, Any]], Any]) -> None:
        """Forward immutable transient copies once after freeze, retaining no result."""
        with self._lock:
            if not callable(visitor) or self._closed or self._failure or not self._frozen \
                    or self._visited or self._projected:
                self._fail()
            try:
                for event, _encoded in self._events:
                    visitor(_immutable(copy.deepcopy(event)))
            except Exception:
                self._failure = True
                raise EventBufferError("event buffer failed") from None
            self._visited = True

    def clear(self) -> None:
        with self._lock:
            self._events.clear(); self._bytes = 0; self._closed = True
