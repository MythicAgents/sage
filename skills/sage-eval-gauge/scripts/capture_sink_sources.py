#!/usr/bin/env python3
"""Pure transient parsers for native, OTLP and appended measurement sinks."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]


class SinkSourceError(ValueError):
    pass


def _native_module():
    path = Path(__file__).with_name("native_inventory.py")
    spec = importlib.util.spec_from_file_location("capture_sink_native_inventory", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def _canonical(value: Any) -> bytes:
    try: return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError): raise SinkSourceError("sink source is invalid") from None


def _match_groups(index: Any, value: Any) -> list[list[dict[str, str]]]:
    content = value if isinstance(value, bytes) else _canonical(value)
    groups = []
    for match in index.scan(content):
        identities = {(item["handle"], item["category"]) for item in match["identities"]}
        groups.append([{"handle": handle, "category": category} for handle, category in sorted(identities)])
    return groups


def _matches(index: Any, value: Any) -> list[dict[str, str]]:
    found = {(item["handle"], item["category"]) for group in _match_groups(index, value) for item in group}
    return [{"handle": handle, "category": category} for handle, category in sorted(found)]


def _snapshot(value: Any) -> tuple[int, dict[str, list[dict[str, Any]]], str]:
    native = _native_module()
    if not isinstance(value, Mapping) or value.get("schema") != "sage-m3-native-inventory-v1" \
            or value.get("snapshot_consistency") != "single_operation_root_query" \
            or value.get("complete") is not True or value.get("failures") != [] \
            or not isinstance(value.get("rows"), Mapping) or set(value["rows"]) != set(native.TABLE_FIELDS) \
            or value.get("query_hashes") != {"operation_snapshot": native.SNAPSHOT_QUERY_SHA256}:
        raise SinkSourceError("native snapshot is incomplete")
    operation = value.get("operation_id")
    if type(operation) is not int or operation <= 0: raise SinkSourceError("native snapshot is incomplete")
    rows = {}
    for table, fields in native.TABLE_FIELDS.items():
        values = value["rows"][table]
        if not isinstance(values, list): raise SinkSourceError("native snapshot is incomplete")
        seen = set()
        for row in values:
            if not isinstance(row, Mapping) or set(row) != set(fields) or row.get("operation_id") != operation \
                    or type(row.get("id")) is not int or row["id"] <= 0 or row["id"] in seen:
                raise SinkSourceError("native snapshot is incomplete")
            seen.add(row["id"])
        rows[table] = [dict(row) for row in values]
    raw = _canonical(value)
    return operation, rows, hashlib.sha256(raw).hexdigest()


def scan_native_delta(start_snapshot: Mapping[str, Any], end_snapshot: Mapping[str, Any], secret_index: Any) -> dict[str, Any]:
    operation, start, start_hash = _snapshot(start_snapshot)
    end_operation, end, end_hash = _snapshot(end_snapshot)
    if operation != end_operation: raise SinkSourceError("native snapshot scope changed")
    fields = {"chat_message": ("message", "tool_output", "metadata"),
              "task": ("original_params", "display_params"), "response": ("response_text",),
              "credential": ("type", "subtype", "account", "realm", "credential", "metadata", "credential_identity"),
              "chat_request": ("context_snapshot", "error")}
    changes = []; matches = []; unknowns = []
    for table, after in end.items():
        before = {row["id"]: row for row in start[table]}; after_ids = set()
        for row in after:
            row_id = row["id"]; after_ids.add(row_id); prior = before.get(row_id)
            if prior is not None and _canonical(prior) == _canonical(row): continue
            kind = "new" if prior is None else "changed"
            row_hash = hashlib.sha256(_canonical(row)).hexdigest()
            changes.append({"table": table, "id": row_id, "change": kind, "row_sha256": row_hash})
            if kind == "changed" and table != "callback":
                unknowns.append({"table": table, "id": row_id, "reason": "intermediate_versions_unobserved"})
            if table in fields:
                for field in fields[table]:
                    for group in _match_groups(secret_index, row[field]):
                        categories = {item["category"] for item in group}
                        if categories == {"operator", "runtime"}:
                            unknowns.append({"table": table, "id": row_id, "field": field,
                                "reason": "overlapping_secret_origin",
                                "handles": sorted(item["handle"] for item in group)})
                            continue
                        for identity in group:
                            matches.append({"table": table, "id": row_id, "field": field,
                                "handle": identity["handle"], "category": identity["category"],
                                "operator_forbidden": identity["category"] == "operator",
                                "runtime_context_allowed": identity["category"] == "runtime"})
        for row_id in sorted(set(before) - after_ids):
            unknowns.append({"table": table, "id": row_id, "reason": "row_removed"})
    counts = {table: {kind: sum(item["table"] == table and item["change"] == kind for item in changes)
                      for kind in ("new", "changed")} for table in start}
    return {"schema": "sage-native-delta-secret-scan-v1", "operation_id": operation,
            "start_source_sha256": start_hash, "end_source_sha256": end_hash,
            "row_changes": changes, "row_change_counts": counts, "secret_matches": matches,
            "unknowns": unknowns, "coverage": "two_complete_single_root_snapshots_only"}


def _proto_value(value: Any) -> Any:
    kind = value.WhichOneof("value")
    if kind in {"int_value", "double_value", "string_value"}: return getattr(value, kind)
    return object()


def validate_phoenix_export(raw_bytes: bytes) -> dict[str, Any]:
    if not isinstance(raw_bytes, bytes) or not raw_bytes: raise SinkSourceError("phoenix export is invalid")
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    request = ExportTraceServiceRequest()
    try: request.ParseFromString(raw_bytes)
    except Exception: return {"known": False, "reasons": ["protobuf_invalid"], "source_sha256": hashlib.sha256(raw_bytes).hexdigest()}
    allowed_tokens = {"llm.token_count.prompt", "llm.token_count.completion", "llm.token_count.total",
        "llm.token_count.prompt_details.cache_input", "llm.token_count.prompt_details.cache_read",
        "llm.token_count.prompt_details.cache_write", "llm.token_count.completion_details.cache_read",
        "llm.token_count.completion_details.cache_write"}
    allowed_kinds = {"TOOL", "CHAIN", "LLM", "RETRIEVER", "EMBEDDING", "AGENT", "RERANKER", "UNKNOWN", "GUARDRAIL", "EVALUATOR", "PROMPT"}
    spans = []; reasons = []
    clean = ExportTraceServiceRequest(); clean.CopyFrom(request); clean.DiscardUnknownFields()
    if clean.SerializeToString(deterministic=True) != request.SerializeToString(deterministic=True):
        reasons.append("protobuf_unknown_fields")
    for resource_spans in request.resource_spans:
        resource_attrs = resource_spans.resource.attributes
        if len(resource_attrs) != 1 or resource_attrs[0].key != "openinference.project.name" \
                or _proto_value(resource_attrs[0].value) != "Sage" \
                or resource_spans.resource.dropped_attributes_count:
            reasons.append("resource_attributes_forbidden")
        for scope_spans in resource_spans.scope_spans:
            if scope_spans.scope.name != "sage.measurement" or scope_spans.scope.version \
                    or scope_spans.scope.attributes or scope_spans.scope.dropped_attributes_count \
                    or scope_spans.schema_url or resource_spans.schema_url:
                reasons.append("instrumentation_scope_forbidden")
            for span in scope_spans.spans:
                spans.append(span)
                if span.name != "sage.measurement": reasons.append("span_name_forbidden")
                if span.events or span.links or span.status.message or span.trace_state or span.dropped_events_count \
                        or span.dropped_links_count or span.dropped_attributes_count:
                    reasons.append("dynamic_span_content_forbidden")
                for attribute in span.attributes:
                    value = _proto_value(attribute.value)
                    if attribute.key in allowed_tokens:
                        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 \
                                or isinstance(value, float) and not math.isfinite(value):
                            reasons.append("token_attribute_invalid")
                    elif attribute.key == "openinference.span.kind":
                        if value not in allowed_kinds: reasons.append("span_kind_invalid")
                    else: reasons.append("attribute_forbidden")
    if not spans: reasons.append("span_missing")
    return {"schema": "sage-phoenix-export-validation-v1", "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "request_count": 1, "resource_span_count": len(request.resource_spans), "span_count": len(spans),
            "known": not reasons, "reasons": sorted(set(reasons))}


def scan_artifact_append(path_handle: str, start_size: int, end_bytes: bytes,
                         expected_sha256: str, secret_index: Any) -> dict[str, Any]:
    if not isinstance(path_handle, str) or not path_handle or type(start_size) is not int or start_size < 0 \
            or not isinstance(end_bytes, bytes) or not isinstance(expected_sha256, str) \
            or hashlib.sha256(end_bytes).hexdigest() != expected_sha256:
        raise SinkSourceError("artifact append is invalid")
    identities = _matches(secret_index, end_bytes)
    return {"schema": "sage-artifact-append-scan-v1", "path_handle": path_handle,
            "start_offset": start_size, "append_length": len(end_bytes), "append_sha256": expected_sha256,
            "secret_matches": identities, "coverage": "caller_bound_append_extent_only"}
