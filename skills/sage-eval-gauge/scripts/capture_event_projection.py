#!/usr/bin/env python3
"""Redacted evaluator projection for observation-channel events."""
from __future__ import annotations

import base64
import importlib.util
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class CaptureProjectionError(ValueError):
    pass


def _sibling(name: str):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("sage_capture_projection_" + name, path)
    if spec is None or spec.loader is None:
        raise CaptureProjectionError("capture projection failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_writes = _sibling("capture_os_writes")
_network = _sibling("capture_os_network")
_BODY_KINDS = frozenset({"model_request", "model_response", "control_request", "control_response"})
_ENVELOPE = ("schema", "event_id", "process_uuid", "pid", "seq", "time_ns", "kind")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _decode(value: Any) -> bytes:
    if not isinstance(value, str):
        raise CaptureProjectionError("capture projection failed")
    try:
        raw = value.encode("ascii")
        decoded = base64.b64decode(raw, validate=True)
    except (UnicodeError, ValueError):
        raise CaptureProjectionError("capture projection failed") from None
    if base64.b64encode(decoded) != raw:
        raise CaptureProjectionError("capture projection failed")
    return decoded


def _binary_projection(index: Any, content: bytes, *, schema: str) -> dict[str, Any]:
    result = index.redact_bytes(content)
    redacted = result.get("redacted")
    metadata = result.get("metadata")
    if not isinstance(redacted, bytes) or not isinstance(metadata, dict):
        raise CaptureProjectionError("capture projection failed")
    return {"schema": schema, "redacted_base64": base64.b64encode(redacted).decode("ascii"),
            "metadata": metadata}


class CaptureEventProjection:
    """Callable ObservationReceiver handler; it owns no source or secret inventory."""

    def __init__(self, secret_index: Any) -> None:
        if not callable(getattr(secret_index, "redact_bytes", None)) \
                or not callable(getattr(secret_index, "redact_json", None)):
            raise CaptureProjectionError("capture projection failed")
        self._index = secret_index
        self._network = _network.NetworkTraceParser()

    def __call__(self, event: Mapping[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(event, Mapping) or not isinstance(event.get("payload"), Mapping):
                raise CaptureProjectionError("capture projection failed")
            retained = {key: event[key] for key in _ENVELOPE}
            if retained["schema"] not in {"sage-observation-event-v1", "sage-observation-event-v2"} \
                    or not _HEX32.fullmatch(retained["event_id"]):
                raise CaptureProjectionError("capture projection failed")
            payload = dict(event["payload"]); kind = retained["kind"]
            if "decoded_body" in payload or "decoded_os_syscall" in payload:
                raise CaptureProjectionError("capture projection failed")
            decoded_body = None
            decoded_syscall = None
            if (kind in _BODY_KINDS or kind == "tool_event" and payload.get("event") == "amqp_rpc_result") \
                    and "body_base64" in payload:
                content = _decode(payload.pop("body_base64"))
                decoded_body = _binary_projection(
                    self._index, content, schema="sage-capture-decoded-body-v1"
                )
            if kind == "tool_event" and payload.get("event") == "os_syscall" \
                    and "line_base64" in payload:
                line = _decode(payload.pop("line_base64"))
                source_kind=payload.get("kind")
                if source_kind=="process_lifecycle":self._network.observe_process_line(line)
                fact = (self._network.parse_line(line) if source_kind=="network_attempt" else
                        _writes.parse_mutation_line(line) if source_kind=="file_mutation_attempt" else _writes.parse_line(line))
                if source_kind=="network_attempt" and fact.get("known") is True:
                    decoded_syscall={"schema":"sage-capture-os-network-v1","semantic_known":True,
                        "network_metadata":_network.metadata_projection(fact)}
                elif source_kind=="network_attempt":
                    decoded_syscall={"schema":"sage-capture-os-network-v1","semantic_known":False,
                        "call":fact.get("call"),"reason":str(fact.get("reason") or "unknown"),
                        "line_metadata":self._index.redact_bytes(line)["metadata"]}
                elif source_kind=="file_mutation_attempt" and fact.get("known") is True:
                    decoded_syscall={"schema":"sage-capture-os-mutation-v1","semantic_known":True,
                        "mutation_metadata":{key:fact.get(key) for key in ("call","pid","timestamp","mutated","errno","remove_directory")}}
                    if fact.get("path"):
                        path_result=self._index.redact_bytes(fact["path"].encode())
                        decoded_syscall["mutation_metadata"].update(path_sha256=path_result["metadata"]["original_sha256"],
                            path_length=path_result["metadata"]["original_length"])
                elif source_kind=="file_mutation_attempt":
                    decoded_syscall={"schema":"sage-capture-os-mutation-v1","semantic_known":False,
                        "call":fact.get("call"),"reason":str(fact.get("reason") or "unknown"),
                        "line_metadata":self._index.redact_bytes(line)["metadata"]}
                elif source_kind=="write_attempt" and fact.get("known") is not True \
                        and _writes.parse_mmap_line(line).get("known") is True:
                    mapped=_writes.parse_mmap_line(line);path_result=self._index.redact_bytes(mapped["path"].encode())
                    decoded_syscall={"schema":"sage-capture-os-mmap-v1","semantic_known":True,
                        "mmap_metadata":{"call":"mmap","pid":mapped["pid"],"timestamp":mapped["timestamp"],
                            "fd":mapped["fd"],"path_sha256":path_result["metadata"]["original_sha256"],
                            "path_length":path_result["metadata"]["original_length"],"length":mapped["length"],
                            "offset":mapped["offset"],"mutated":mapped["mutated"],"errno":mapped["errno"]}}
                elif fact.get("known") is True:
                    content = fact.pop("attempted_content")
                    path = fact.pop("fd_path")
                    metadata = _writes.metadata_projection({**fact, "attempted_content": content, "fd_path": path})
                    path_metadata = self._index.redact_bytes(path)["metadata"]
                    decoded_syscall = {
                        "schema": "sage-capture-os-write-v1", "semantic_known": True,
                        "write_metadata": metadata,
                        "path_interpretation": (
                            "literal" if b"\\" not in path and all(0x20 <= value <= 0x7e for value in path)
                            else "unknown"
                        ),
                        "path_metadata": path_metadata,
                        "redacted_content": _binary_projection(
                            self._index, content, schema="sage-capture-redacted-content-v1"
                        ),
                    }
                else:
                    line_result = self._index.redact_bytes(line)
                    decoded_syscall = {
                        "schema": "sage-capture-os-write-v1", "semantic_known": False,
                        "call":fact.get("call"),"reason": str(fact.get("reason") or "unknown"),
                        "line_metadata": line_result["metadata"],
                    }
            retained["payload"] = self._index.redact_json(payload)
            if decoded_body is not None:
                if "decoded_body" in retained["payload"]:
                    raise CaptureProjectionError("capture projection failed")
                retained["payload"]["decoded_body"] = decoded_body
            if decoded_syscall is not None:
                if "decoded_os_syscall" in retained["payload"]:
                    raise CaptureProjectionError("capture projection failed")
                retained["payload"]["decoded_os_syscall"] = decoded_syscall
            return retained
        except CaptureProjectionError:
            raise
        except Exception:
            raise CaptureProjectionError("capture projection failed") from None
