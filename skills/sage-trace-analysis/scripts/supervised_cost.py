"""Read-only, metadata-only cost timeline for retained supervised Sage runs.

Input bytes below describe retained OpenInference objects, not provider wire bytes or
token allocations. Decisions are never treated as task issuance without task evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


TASK_ID = re.compile(r"\[Mythic task issuance identity\]\s+task_display_id=(\d+)\b")


def instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def seconds(value: Any) -> float | None:
    parsed = instant(value)
    return parsed.timestamp() if parsed else None


def retained_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("JSONL row must be an object")
                rows.append(row)
    return rows


def _attributes(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (TypeError, ValueError):
        value = None
    return value if isinstance(value, dict) else {}


def _content_bytes(message: dict[str, Any]) -> int:
    value = message.get("message", message)
    return retained_bytes(value)


def _output_tools(output_messages: Any) -> tuple[list[str], list[str]]:
    names = []
    fingerprints = []
    for outer in output_messages if isinstance(output_messages, list) else []:
        message = outer.get("message") if isinstance(outer, dict) else None
        if not isinstance(message, dict):
            continue
        for item in message.get("tool_calls") or []:
            function = (item.get("tool_call") or {}).get("function") if isinstance(item, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and name:
                names.append(name)
                fingerprints.append(digest({"name": name, "arguments": function.get("arguments")}))
    return names, fingerprints


def read_phoenix(path: Path, start: str, end: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """SQLite read-only URI includes a live WAL; never copies or checkpoints the DB."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        cols = {row[1] for row in connection.execute("PRAGMA table_info(spans)")}
        attr_col = "attributesJSONB" if "attributesJSONB" in cols else "attributes"
        if attr_col not in cols:
            raise ValueError("Phoenix span attributes unavailable")
        start_sql = instant(start).strftime("%Y-%m-%d %H:%M:%S.%f")
        end_sql = instant(end).strftime("%Y-%m-%d %H:%M:%S.%f")
        rows = connection.execute(
            f"SELECT t.trace_id,s.span_id,s.parent_id,s.name,s.span_kind,s.start_time,s.end_time,"
            f"s.llm_token_count_prompt,s.llm_token_count_completion,s.{attr_col} "
            "FROM spans s JOIN traces t ON t.id=s.trace_rowid "
            "WHERE s.start_time>=? AND s.start_time<? ORDER BY s.start_time,s.id",
            (start_sql, end_sql),
        ).fetchall()
        root_names: dict[str, str] = {}
        for trace_id, span_id, parent_id, name, kind, *_ in rows:
            if not parent_id and trace_id not in root_names:
                root_names[trace_id] = str(name or "")
        calls = []
        for trace_id, span_id, parent_id, name, kind, began, finished, prompt, completion, raw in rows:
            if kind != "LLM":
                continue
            attrs = _attributes(raw)
            llm = attrs.get("llm") if isinstance(attrs.get("llm"), dict) else {}
            metadata = attrs.get("metadata") if isinstance(attrs.get("metadata"), dict) else {}
            thread_id = metadata.get("thread_id")
            channel_match = re.match(r"^(\d+):generation:", thread_id) if isinstance(thread_id, str) else None
            messages = llm.get("input_messages")
            schemas = llm.get("tools")
            roles: dict[str, int] = defaultdict(int)
            if isinstance(messages, list):
                for outer in messages:
                    message = outer.get("message") if isinstance(outer, dict) else None
                    if isinstance(message, dict):
                        roles[str(message.get("role") or "unknown")] += _content_bytes(message)
            cache = (llm.get("token_count") or {}).get("prompt_details") if isinstance(llm.get("token_count"), dict) else None
            cache = cache if isinstance(cache, dict) else {}
            output_names, output_hashes = _output_tools(llm.get("output_messages"))
            calls.append({
                "trace_id": trace_id, "span_id": span_id, "parent_span_id": parent_id,
                "root_agent": metadata.get("lc_agent_name") or root_names.get(trace_id),
                "thread_channel_id": int(channel_match.group(1)) if channel_match else None,
                "model": llm.get("model_name"),
                "provider": llm.get("provider"), "start_at": began, "end_at": finished,
                "prompt_tokens": prompt, "completion_tokens": completion,
                "cache_read_tokens": cache.get("cache_read"), "cache_write_tokens": cache.get("cache_write"),
                "retained_input_message_bytes_by_role": dict(sorted(roles.items())) if isinstance(messages, list) else None,
                "retained_tool_schema_bytes": sum(retained_bytes(x) for x in schemas) if isinstance(schemas, list) else None,
                "exact_token_component_split": None,
                "output_tool_names": output_names, "output_tool_call_hashes": output_hashes,
                "input_message_count": len(messages) if isinstance(messages, list) else None,
                "tool_schema_count": len(schemas) if isinstance(schemas, list) else None,
                "input_fingerprint": digest({"messages": messages, "tools": schemas}) if messages is not None and schemas is not None else None,
            })
        tool_spans = []
        for trace_id, span_id, parent_id, name, kind, began, finished, *_ in rows:
            if kind == "TOOL" and name == "issue_task_and_waitfor_task_output":
                tool_spans.append({"trace_id": trace_id, "span_id": span_id, "start_at": began,
                                   "end_at": finished, "tool_name": name,
                                   "duration_seconds": round(max(0.0, seconds(finished) - seconds(began)), 6)
                                   if seconds(began) is not None and seconds(finished) is not None else None,
                                   "duration_label": "combined_tool_adapter_control_and_target_wait"})
        return calls, tool_spans
    finally:
        connection.close()


def native_records(paths: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requests: dict[tuple[Any, Any], dict[str, Any]] = {}
    messages: dict[tuple[Any, Any], dict[str, Any]] = {}
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        request = data.get("request") or {}
        key = (request.get("channel_id"), request.get("id"))
        if not all(type(x) is int for x in key):
            raise ValueError("native request identity unavailable")
        old = requests.get(key)
        if old is None or (seconds(request.get("updated_at")) or 0) >= (seconds(old.get("updated_at")) or 0):
            requests[key] = {**request, "source": str(path), "observed_at": None}
        for message in data.get("messages") or []:
            mid = message.get("id")
            if type(mid) is not int:
                continue
            mkey = (key[0], mid)
            old = messages.get(mkey)
            if old is None or (seconds(message.get("updated_at")) or 0) >= (seconds(old.get("updated_at")) or 0):
                messages[mkey] = {**message, "source": str(path)}
    return list(requests.values()), list(messages.values())


def _interval_union(intervals: list[tuple[float, float]], lo: float, hi: float) -> float:
    spans = sorted((max(lo, a), min(hi, b)) for a, b in intervals if a is not None and b is not None and b > a and b > lo and a < hi)
    length = 0.0
    end = lo
    for a, b in spans:
        if b > end:
            length += b - max(a, end)
            end = b
    return length


def _partition(lo: float, hi: float, model: list[tuple[float, float]], decision: list[tuple[float, float]]) -> dict[str, float]:
    boundaries = {lo, hi}
    for a, b in model + decision:
        boundaries.update((max(lo, min(hi, a)), max(lo, min(hi, b))))
    points = sorted(boundaries)
    totals = {"model_active_seconds": 0.0, "awaiting_native_decision_seconds": 0.0, "remainder_seconds": 0.0}
    for a, b in zip(points, points[1:]):
        middle = (a + b) / 2
        label = ("model_active_seconds" if any(x <= middle < y for x, y in model) else
                 "awaiting_native_decision_seconds" if any(x <= middle < y for x, y in decision) else
                 "remainder_seconds")
        totals[label] += b - a
    return {key: round(value, 6) for key, value in totals.items()}


def _card(message: dict[str, Any]) -> dict[str, Any] | None:
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return None
    card = metadata.get("input_requested")
    return card if isinstance(card, dict) else None


def _task_ids_from_card(message: dict[str, Any]) -> list[int]:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    tool = metadata.get("tool_use")
    if not isinstance(tool, dict) or tool.get("tool_name") != "issue_task_and_waitfor_task_output":
        return []
    preview = tool.get("result_preview")
    match = TASK_ID.search(preview) if isinstance(preview, str) else None
    return [int(match.group(1))] if match else []


def _meter(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reservations = {r.get("reservation_id"): r for r in rows if r.get("type") == "reservation" and r.get("reservation_id")}
    settlements = {r.get("reservation_id"): r for r in rows if r.get("type") == "provider_settlement" and r.get("reservation_id")}
    return {"provider_reservations": sum(1 for r in reservations.values() if "provider_tokens" in (r.get("amounts") or {})),
            "provider_settlements": len(settlements),
            "settled_tokens": sum(r.get("actual_tokens") for r in settlements.values() if type(r.get("actual_tokens")) is int),
            "unsettled_provider_reservations": sum(1 for k, r in reservations.items() if "provider_tokens" in (r.get("amounts") or {}) and k not in settlements)}


def build_report(db: Path, start: str, end: str, native: list[Path], events: list[Path], tasks_path: Path, meter: list[Path]) -> dict[str, Any]:
    lo, hi = seconds(start), seconds(end)
    if (lo is None or hi is None or lo >= hi or
            not (start.endswith("Z") or start.endswith("+00:00")) or
            not (end.endswith("Z") or end.endswith("+00:00"))):
        raise ValueError("explicit ordered UTC start/end required")
    calls, tool_spans = read_phoenix(db, start, end)
    requests, messages = native_records(native)
    known_requests = {r["id"] for r in requests}
    decisions = [r for r in jsonl(events) if r.get("event") == "native-card-decision"
                 and r.get("request_id") in known_requests]
    tasks_data = json.loads(tasks_path.read_text(encoding="utf-8"))
    task_rows = tasks_data.get("tasks") or []
    tasks = {r.get("display_id"): r for r in task_rows if type(r.get("display_id")) is int}
    observed = tasks_data.get("observed_at")
    by_message = {(m.get("chat_request_id"), m.get("id")): m for m in messages}
    approval_rows = []
    card_intervals: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for d in decisions:
        rid, mid = d.get("request_id"), d.get("message_id")
        m = by_message.get((rid, mid))
        card = _card(m) if m else None
        at, created = seconds(d.get("at")), seconds(m.get("created_at")) if m else None
        if at is not None and created is not None and at >= created:
            card_intervals[rid].append((created, at))
        data = card.get("data") if card else None
        if not isinstance(data, dict):
            data = {}
        approval_rows.append({"request_id": rid, "message_id": mid, "decision": d.get("decision"),
                              "decision_at": d.get("at"), "card_created_at": m.get("created_at") if m else None,
                              "card_updated_at": m.get("updated_at") if m else None,
                              "card_present": card is not None, "card_digest_match": digest(data) == d.get("card_digest") if data else None,
                              "tool_name": data.get("tool_name"), "task_ids": [], "execution_state": "unmatched"})
    # Only exact task identity receipts in an issue-tool card can link a native task.
    issue_cards = []
    for m in messages:
        for task_id in _task_ids_from_card(m):
            issue_cards.append({"request_id": m.get("chat_request_id"), "message_id": m.get("id"),
                                "created_at": m.get("created_at"), "updated_at": m.get("updated_at"),
                                "task_id": task_id, "task_record_present": task_id in tasks,
                                "task_created_at": tasks.get(task_id, {}).get("timestamp")})
    for approval in approval_rows:
        if approval["decision"] != "accept":
            approval["execution_state"] = "denied"
            continue
        if approval["card_digest_match"] is not True:
            approval["execution_state"] = "card_missing_or_digest_mismatch"
            continue
        if approval["tool_name"] != "issue_task_and_waitfor_task_output":
            approval["execution_state"] = "accepted_non_task_action"
            continue
        at = seconds(approval["decision_at"])
        following = [x for x in approval_rows if x["request_id"] == approval["request_id"] and seconds(x["decision_at"]) is not None and seconds(x["decision_at"]) > at] if at is not None else []
        cutoff = min((seconds(x["decision_at"]) for x in following), default=hi)
        candidates = [x for x in issue_cards if x["request_id"] == approval["request_id"] and at is not None
                      and seconds(x["task_created_at"]) is not None and at <= seconds(x["task_created_at"]) < cutoff]
        if len(candidates) == 1 and candidates[0]["task_record_present"]:
            approval["task_ids"] = [candidates[0]["task_id"]]
            approval["execution_state"] = "issued_temporal_match"
        elif candidates:
            approval["execution_state"] = "ambiguous_or_task_record_missing"
        else:
            approval["execution_state"] = "accepted_no_issue_evidence"
    request_rows = []
    for req in requests:
        rid = req["id"]
        user = next((m for m in messages if m.get("id") == req.get("request_message_id") and m.get("channel_id") == req.get("channel_id")), None)
        start_at, end_at = user.get("created_at") if user else None, req.get("updated_at")
        a, b = seconds(start_at), seconds(end_at)
        model = [(seconds(c["start_at"]), seconds(c["end_at"])) for c in calls
                 if c["thread_channel_id"] == req["channel_id"]
                 and seconds(c["start_at"]) is not None and seconds(c["end_at"]) is not None
                 and a is not None and b is not None and a <= seconds(c["start_at"]) < b]
        partition = _partition(a, b, model, card_intervals.get(rid, [])) if a is not None and b is not None and b >= a else None
        request_rows.append({"channel_id": req["channel_id"], "request_id": rid, "status": req.get("status"),
                             "start_at": start_at, "start_source": "native_user_message.created_at" if user else None,
                             "end_at": end_at, "end_source": "native_request.updated_at", "wall_partition": partition})
    for call in calls:
        when = seconds(call["start_at"])
        candidates = [r for r in request_rows if r["channel_id"] == call["thread_channel_id"]
                      and when is not None and seconds(r["start_at"]) is not None and seconds(r["end_at"]) is not None
                      and seconds(r["start_at"]) <= when <= seconds(r["end_at"])]
        call["request_id"] = candidates[0]["request_id"] if len(candidates) == 1 else None
        call["request_join"] = "temporal_thread_channel_inference" if len(candidates) == 1 else "unmatched_or_ambiguous"
    for span in tool_spans:
        when = seconds(span["start_at"])
        candidates = [r for r in request_rows if when is not None and seconds(r["start_at"]) is not None
                      and seconds(r["end_at"]) is not None and seconds(r["start_at"]) <= when <= seconds(r["end_at"])]
        span["request_id"] = candidates[0]["request_id"] if len(candidates) == 1 else None
        span["request_join"] = "temporal_inference" if len(candidates) == 1 else "unmatched_or_ambiguous"
    for request in request_rows:
        a, b = seconds(request["start_at"]), seconds(request["end_at"])
        matching = [x for x in tool_spans if x["request_id"] == request["request_id"]]
        request["combined_task_adapter_seconds"] = (
            round(_interval_union([(seconds(x["start_at"]), seconds(x["end_at"])) for x in matching], a, b), 6)
            if a is not None and b is not None and b >= a else None)
        request["combined_task_adapter_label"] = "tool_adapter_control_and_target_wait_not_pure_target"
    by_agent: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    by_request: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    tool_counts: Counter[str] = Counter()
    repeats: Counter[str] = Counter()
    repeated_tools: Counter[str] = Counter()
    for call in calls:
        row = by_agent[call["root_agent"] or "unknown"]
        row["calls"] += 1
        row["prompt_tokens"] += call["prompt_tokens"] or 0
        row["completion_tokens"] += call["completion_tokens"] or 0
        request_row = by_request[str(call["request_id"]) if call["request_id"] is not None else "unmatched"]
        request_row["calls"] += 1
        request_row["prompt_tokens"] += call["prompt_tokens"] or 0
        request_row["completion_tokens"] += call["completion_tokens"] or 0
        tool_counts.update(call["output_tool_names"])
        repeated_tools.update(call["output_tool_call_hashes"])
        if call["input_fingerprint"]:
            repeats[call["input_fingerprint"]] += 1
    scoped_tasks = {k for k, v in tasks.items() if seconds(v.get("timestamp")) is not None
                    and lo <= seconds(v.get("timestamp")) < hi}
    card_task_ids = {x["task_id"] for x in issue_cards}
    linked_task_ids = {task_id for row in approval_rows for task_id in row["task_ids"]}
    all_model = [(seconds(c["start_at"]), seconds(c["end_at"])) for c in calls
                 if seconds(c["start_at"]) is not None and seconds(c["end_at"]) is not None]
    all_decision = [interval for intervals in card_intervals.values() for interval in intervals]
    model_union = _interval_union(all_model, lo, hi)
    decision_union = _interval_union(all_decision, lo, hi)
    overlap = _interval_union([(max(a, c), min(b, d)) for a, b in all_model
                               for c, d in all_decision if max(a, c) < min(b, d)], lo, hi)
    meter_summary = _meter(jsonl(meter))
    usage_bearing = sum(1 for c in calls if (c["prompt_tokens"] or 0) + (c["completion_tokens"] or 0) > 0)
    return {"schema": "sage-supervised-cost-v1", "scope": {"start_utc": start, "end_utc": end},
            "calls": calls, "requests": sorted(request_rows, key=lambda x: (x["channel_id"], x["request_id"])),
            "approvals": approval_rows, "issue_cards": issue_cards, "task_adapter_spans": tool_spans,
            "tasks": [{"task_id": k, "created_at": v.get("timestamp"), "created_source": "mythic_task.timestamp",
                       "completed": v.get("completed"), "status": v.get("status"), "response_timestamp": None,
                       "observed_at": observed, "observed_source": "native_task_final.observed_at"}
                      for k, v in sorted(tasks.items()) if seconds(v.get("timestamp")) is not None
                      and lo <= seconds(v.get("timestamp")) < hi],
            "meter": meter_summary,
            "usage_reconciliation": {"traced_llm_spans": len(calls), "usage_bearing_llm_spans": usage_bearing,
                                     "meter_provider_settlements": meter_summary["provider_settlements"],
                                     "usage_bearing_count_equals_settlement_count": usage_bearing == meter_summary["provider_settlements"],
                                     "count_equality_is_not_per_call_settlement_proof": True},
            "global_wall": {"window_seconds": round(hi - lo, 6),
                            "raw_model_union_seconds": round(model_union, 6),
                            "raw_decision_union_seconds": round(decision_union, 6),
                            "model_decision_overlap_seconds": round(overlap, 6),
                            "partition": _partition(lo, hi, all_model, all_decision)},
            "aggregates": {"workers": by_agent, "requests": by_request,
            "output_tool_calls": dict(tool_counts),
            "exact_repeated_input_fingerprints": sum(n - 1 for n in repeats.values() if n > 1),
            "exact_repeated_tool_call_fingerprints": sum(n - 1 for n in repeated_tools.values() if n > 1)},
            "join_gaps": {"task_ids_without_issue_card": sorted(scoped_tasks - card_task_ids),
                          "issue_card_task_ids_without_final_record": sorted(card_task_ids - set(tasks)),
                          "issue_card_task_ids_without_unique_accept_match": sorted((card_task_ids & scoped_tasks) - linked_task_ids)},
            "limits": ["exact_token_component_split_unavailable", "retained_byte_counts_are_not_provider_wire_bytes",
                       "temporal_approval_task_match_is_not_argument_identity", "task_wait_spans_not_separated_from_control_or_target",
                       "native_request_updated_at_is_not_response_time", "task_response_timestamps_unavailable_when_absent_from_final_record",
                       "unassigned_global_wall_includes_request_gaps_and_unobserved_work"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phoenix-db", type=Path, required=True)
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--native", type=Path, action="append", required=True)
    parser.add_argument("--operator-events", type=Path, action="append", required=True)
    parser.add_argument("--native-task-final", type=Path, required=True)
    parser.add_argument("--meter", type=Path, action="append", required=True)
    args = parser.parse_args()
    print(json.dumps(build_report(args.phoenix_db, args.start_utc, args.end_utc, args.native,
                                  args.operator_events, args.native_task_final, args.meter),
                     sort_keys=True, ensure_ascii=True))


if __name__ == "__main__":
    main()
