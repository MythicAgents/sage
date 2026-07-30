#!/usr/bin/env python3
"""Audit a Sage native-chat transcript without mutating runtime evidence."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
PHOENIX_READER = (
    REPO_ROOT / "Payload_Type" / "sage" / "evals" / "phoenix_reader.py"
)
TERMINAL_STATUSES = {
    "completed",
    "complete",
    "error",
    "failed",
    "cancelled",
    "canceled",
}
ASSISTANT_AUTHOR_TYPES = {"container", "assistant", "ai"}
TASK_CARD_STATUSES = {"started", "completed", "error", "failed"}
ISSUE_TASK_TOOL_NAME = "issue_task_and_waitfor_task_output"
READ_ONLY_TASK_OUTPUT_TOOL_NAME = "get_all_task_output_by_task_id"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load helper module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def load_transcript(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to load transcript {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Transcript root must be a JSON object.")
    if value.get("schema") != "sage-native-chat-transcript-v1":
        raise ValueError(
            "Transcript schema must be sage-native-chat-transcript-v1."
        )
    if not isinstance(value.get("request"), dict):
        raise ValueError("Transcript request must be a JSON object.")
    if not isinstance(value.get("messages"), list):
        raise ValueError("Transcript messages must be a JSON array.")
    if not all(isinstance(row, dict) for row in value["messages"]):
        raise ValueError("Every transcript message must be a JSON object.")
    _validate_transcript_identity(value)
    return value


def _validate_transcript_identity(transcript: dict[str, Any]) -> None:
    request = transcript.get("request")
    if not isinstance(request, dict):
        raise ValueError("Transcript request must be a JSON object.")

    exact_int_fields = (
        ("chat_channel_id", transcript.get("chat_channel_id")),
        ("chat_request_id", transcript.get("chat_request_id")),
        ("request.id", request.get("id")),
        ("request.channel_id", request.get("channel_id")),
    )
    if "request_message_id" in request:
        exact_int_fields += (
            ("request.request_message_id", request.get("request_message_id")),
        )
    for name, value in exact_int_fields:
        if type(value) is not int:
            raise ValueError(f"Transcript {name} must be an exact integer.")

    exact_pairs = (
        ("chat_channel_id", "channel_id"),
        ("chat_request_id", "id"),
        ("status", "status"),
        ("error", "error"),
    )
    for top_level_key, request_key in exact_pairs:
        top_level_value = transcript.get(top_level_key)
        request_value = request.get(request_key)
        if (
            type(top_level_value) is not type(request_value)
            or top_level_value != request_value
        ):
            raise ValueError(
                f"Transcript {top_level_key} does not match request.{request_key}."
            )

    status = request.get("status")
    if type(status) is not str or not status.strip():
        raise ValueError("Transcript request.status must be a nonempty string.")
    error = request.get("error")
    if error is not None and type(error) is not str:
        raise ValueError("Transcript request.error must be a string or null.")

    request_id = request.get("id")
    channel_id = request.get("channel_id")
    for index, message in enumerate(transcript.get("messages") or []):
        for field in ("id", "chat_request_id", "channel_id"):
            if type(message.get(field)) is not int:
                raise ValueError(
                    f"Transcript messages[{index}].{field} must be an exact integer."
                )
        if (
            type(message.get("chat_request_id")) is not type(request_id)
            or message.get("chat_request_id") != request_id
        ):
            raise ValueError(
                f"Transcript messages[{index}].chat_request_id does not match request.id."
            )
        if (
            type(message.get("channel_id")) is not type(channel_id)
            or message.get("channel_id") != channel_id
        ):
            raise ValueError(
                f"Transcript messages[{index}].channel_id does not match request.channel_id."
            )


def _metadata_containers(message: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return []
    containers = [metadata]
    nested = metadata.get("container_metadata")
    if isinstance(nested, dict):
        containers.append(nested)
    return containers


def _special_types(message: dict[str, Any]) -> set[str]:
    return {
        str(metadata.get("special_type") or "").strip().casefold()
        for metadata in _metadata_containers(message)
        if str(metadata.get("special_type") or "").strip()
    }


def _parse_time(value: Any) -> tuple[datetime | None, str]:
    if value is None or value == "":
        return None, "missing"
    if type(value) is not str or not value.strip():
        return None, "malformed"
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, "malformed"
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC), "valid"
    return parsed.astimezone(UTC), "valid"


def _message_is_deleted(message: dict[str, Any]) -> bool:
    deleted = message.get("deleted")
    return deleted is not None and deleted is not False


def _finished_subagent_message(message: dict[str, Any]) -> bool:
    if "subagent" not in _special_types(message):
        return False
    for metadata in _metadata_containers(message):
        subagent = metadata.get("subagent")
        if (
            isinstance(subagent, dict)
            and str(subagent.get("status") or "").strip().casefold()
            in {"finished", "completed"}
        ):
            return True
    return False


def _message_gaps(messages: list[dict[str, Any]]) -> dict[str, Any]:
    visible_messages = [
        message for message in messages if not _message_is_deleted(message)
    ]
    gaps: list[float] = []
    post_subagent_gaps: list[float] = []
    timing_issues: list[dict[str, Any]] = []
    parsed_times: list[dict[str, datetime | None]] = []

    for message in visible_messages:
        parsed: dict[str, datetime | None] = {}
        for field in ("created_at", "updated_at"):
            value, state = _parse_time(message.get(field))
            parsed[field] = value
            if state == "malformed":
                timing_issues.append(
                    {
                        "kind": "malformed_message_timestamp",
                        "message_id": message.get("id"),
                        "field": field,
                        "value": message.get(field),
                    }
                )
        created_at = parsed["created_at"]
        updated_at = parsed["updated_at"]
        if (
            created_at is not None
            and updated_at is not None
            and updated_at < created_at
        ):
            timing_issues.append(
                {
                    "kind": "negative_message_duration",
                    "message_id": message.get("id"),
                    "created_at": message.get("created_at"),
                    "updated_at": message.get("updated_at"),
                }
            )
        parsed_times.append(parsed)

    post_subagent_expected = 0
    post_subagent_measured = 0
    for index, previous in enumerate(visible_messages):
        if _finished_subagent_message(previous):
            post_subagent_expected += 1
        if index + 1 >= len(visible_messages):
            continue
        current = visible_messages[index + 1]
        previous_at = (
            parsed_times[index]["updated_at"]
            or parsed_times[index]["created_at"]
        )
        current_at = parsed_times[index + 1]["created_at"]
        if previous_at is None or current_at is None:
            continue
        gap = (current_at - previous_at).total_seconds()
        if gap < 0:
            timing_issues.append(
                {
                    "kind": "negative_message_gap",
                    "previous_message_id": previous.get("id"),
                    "current_message_id": current.get("id"),
                    "gap_seconds": gap,
                }
            )
            continue
        gaps.append(gap)
        if _finished_subagent_message(previous):
            post_subagent_gaps.append(gap)
            post_subagent_measured += 1

    return {
        "max_message_gap_seconds": max(gaps) if gaps else None,
        "max_post_subagent_gap_seconds": (
            max(post_subagent_gaps) if post_subagent_gaps else None
        ),
        "post_subagent_gap_expected_count": post_subagent_expected,
        "post_subagent_gap_measured_count": post_subagent_measured,
        "post_subagent_gap_unmeasurable_count": (
            post_subagent_expected - post_subagent_measured
        ),
        "timing_issues": timing_issues,
    }


def _runtime_tasks(runtime_telemetry: dict[str, Any]) -> list[dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    unbound_index = 0
    for transaction in runtime_telemetry.get("transactions") or []:
        if not isinstance(transaction, dict):
            continue
        for task in transaction.get("child_tasks") or []:
            if not isinstance(task, dict):
                continue
            task_id = task.get("task_id")
            normalized = dict(task)
            if type(task_id) is int:
                normalized["task_identity_valid"] = True
                key = f"id:{task_id}"
            else:
                normalized["task_id"] = None
                normalized["task_identity_valid"] = False
                normalized["invalid_task_id"] = task_id
                key = f"unbound:{unbound_index}"
                unbound_index += 1
            tasks.setdefault(key, normalized)
    return list(tasks.values())


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _task_ids(value: Any) -> set[int]:
    ids: set[int] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"task_id", "mythic_task_id"}:
                if type(item) is int:
                    ids.add(item)
            else:
                ids.update(_task_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.update(_task_ids(item))
    return ids


def _invalid_task_ids(value: Any) -> list[Any]:
    invalid: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"task_id", "mythic_task_id"}:
                if item is not None and type(item) is not int:
                    invalid.append(item)
            else:
                invalid.extend(_invalid_task_ids(item))
    elif isinstance(value, list):
        for item in value:
            invalid.extend(_invalid_task_ids(item))
    return invalid


def _mythic_task_card_evidence(
    tool_cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for index, card in enumerate(tool_cards):
        if str(card.get("tool_source") or "").strip().casefold() != "mythic":
            continue
        status = str(card.get("status") or "").strip().casefold()
        if status not in TASK_CARD_STATUSES:
            continue
        tool_name = str(card.get("tool_name") or "").strip()
        if tool_name == READ_ONLY_TASK_OUTPUT_TOOL_NAME:
            continue
        parsed_arguments = _json_value(card.get("arguments"))
        is_issue_wrapper = tool_name == ISSUE_TASK_TOOL_NAME
        is_execution_event = (
            isinstance(parsed_arguments, dict)
            and {"callback_id", "parameters"}.issubset(parsed_arguments)
        )
        if not is_issue_wrapper and not is_execution_event:
            continue
        task_values = (
            card,
            parsed_arguments,
            _json_value(card.get("result_preview")),
            _json_value(card.get("output")),
        )
        task_ids: set[int] = set()
        invalid_task_ids: list[Any] = []
        for value in task_values:
            task_ids.update(_task_ids(value))
            invalid_task_ids.extend(_invalid_task_ids(value))
        tool_call_id = str(card.get("tool_call_id") or "").strip()
        group_key = (
            f"call:{tool_call_id}"
            if tool_call_id
            else f"card:{index}"
        )
        group = grouped.setdefault(
            group_key,
            {
                "task_ids": set(),
                "invalid_task_ids": [],
                "tool_call_id": tool_call_id or None,
                "tool_names": [],
                "statuses": [],
            },
        )
        group["task_ids"].update(task_ids)
        group["invalid_task_ids"].extend(invalid_task_ids)
        if tool_name not in group["tool_names"]:
            group["tool_names"].append(tool_name)
        if status not in group["statuses"]:
            group["statuses"].append(status)

    evidence: list[dict[str, Any]] = []
    for group in grouped.values():
        task_ids = group.pop("task_ids")
        tool_names = group.pop("tool_names")
        base = {
            **group,
            "tool_name": tool_names[-1],
            "tool_names": tool_names,
        }
        if task_ids:
            for task_id in sorted(task_ids):
                evidence.append(
                    {
                        **base,
                        "task_id": task_id,
                        "task_identity_valid": not base[
                            "invalid_task_ids"
                        ],
                        "evidence_source": "mythic_task_tool_card",
                    }
                )
            continue
        evidence.append(
            {
                **base,
                "task_id": None,
                "task_identity_valid": (
                    False if base["invalid_task_ids"] else None
                ),
                "evidence_source": "unbound_mythic_task_tool_card",
            }
        )
    return evidence


def _merge_task_evidence(
    runtime_tasks: list[dict[str, Any]],
    card_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for index, task in enumerate(runtime_tasks):
        task_id = task.get("task_id")
        key = (
            f"id:{task_id}"
            if type(task_id) is int
            else f"runtime:{index}:{json.dumps(task, sort_keys=True, default=str)}"
        )
        merged[key] = {
            **task,
            "evidence_sources": ["runtime_telemetry"],
        }
    for index, card in enumerate(card_evidence):
        task_id = card.get("task_id")
        key = (
            f"id:{task_id}"
            if type(task_id) is int
            else f"card:{index}:{card.get('tool_call_id') or ''}"
        )
        if key in merged:
            sources = list(merged[key].get("evidence_sources") or [])
            if card["evidence_source"] not in sources:
                sources.append(card["evidence_source"])
            merged[key]["evidence_sources"] = sources
            if card.get("task_identity_valid") is False:
                merged[key]["task_identity_valid"] = False
                merged[key]["invalid_task_ids"] = list(
                    card.get("invalid_task_ids") or []
                )
            merged[key]["tool_call_id"] = card.get("tool_call_id")
            merged[key]["tool_name"] = card.get("tool_name")
            merged[key]["statuses"] = list(card.get("statuses") or [])
            continue
        merged[key] = {
            **card,
            "evidence_sources": [card["evidence_source"]],
        }
    return list(merged.values())


def _phoenix_evidence(
    db_path: str | Path | None, trace_rowids: list[int]
) -> dict[str, Any]:
    if db_path is None:
        return {"present": False, "trace_rowids": []}
    if not trace_rowids:
        raise ValueError("--phoenix-db requires at least one --trace-rowid.")
    reader = _load_module("sage_trace_audit_phoenix_reader", PHOENIX_READER)
    metrics = reader.aggregate_metrics(db_path, trace_rowids)
    tokens = reader.token_breakdown(db_path, trace_rowids)
    return {
        "present": True,
        "trace_rowids": trace_rowids,
        "metrics": asdict(metrics),
        "token_breakdown": asdict(tokens),
        "command_histogram": reader.command_histogram(
            db_path, trace_rowids
        ),
        "final_answer": reader.extract_answer_with_fallback(
            db_path, trace_rowids
        ),
    }


def audit_transcript(
    transcript: dict[str, Any],
    *,
    phoenix_db: str | Path | None = None,
    trace_rowids: list[int] | None = None,
    require_zero_payload_tasks: bool = False,
    max_payload_tasks: int | None = None,
    expect_halt_reason: str | None = None,
    max_post_subagent_gap_seconds: float | None = None,
) -> dict[str, Any]:
    _validate_transcript_identity(transcript)
    request = transcript.get("request") or {}
    messages = sorted(
        [dict(row) for row in transcript.get("messages") or []],
        key=lambda row: row["id"],
    )
    assistant_texts: list[str] = []
    tool_cards: list[dict[str, Any]] = []
    subagent_cards: list[dict[str, Any]] = []
    input_requested_count = 0
    for message in messages:
        if _message_is_deleted(message):
            continue
        special_types = _special_types(message)
        if "input_requested" in special_types:
            input_requested_count += 1
        for metadata in _metadata_containers(message):
            tool_use = metadata.get("tool_use")
            if isinstance(tool_use, dict):
                tool_cards.append(tool_use)
            subagent = metadata.get("subagent")
            if isinstance(subagent, dict):
                subagent_cards.append(subagent)
        text = str(message.get("message") or "").strip()
        author = str(message.get("author_type") or "").strip().casefold()
        if (
            text
            and author in ASSISTANT_AUTHOR_TYPES
            and not special_types.intersection(
                {"tool_use", "subagent", "input_requested"}
            )
        ):
            assistant_texts.append(text)

    finished_subagents = [
        card
        for card in subagent_cards
        if str(card.get("status") or "").casefold()
        in {"finished", "completed"}
    ]
    missing_summaries = [
        str(card.get("name") or card.get("agent_name") or "unknown")
        for card in finished_subagents
        if not str(card.get("summary") or "").strip()
    ]
    duplicate_assistant_texts = sorted(
        {
            text
            for text in assistant_texts
            if assistant_texts.count(text) > 1
        }
    )
    runtime_telemetry = transcript.get("runtime_telemetry")
    if not isinstance(runtime_telemetry, dict):
        runtime_telemetry = {}
    runtime_payload_tasks = _runtime_tasks(runtime_telemetry)
    task_card_evidence = _mythic_task_card_evidence(tool_cards)
    payload_tasks = _merge_task_evidence(
        runtime_payload_tasks, task_card_evidence
    )
    payload_task_count = len(payload_tasks)
    gaps = _message_gaps(messages)
    status = request.get("status")
    status_casefold = status.strip().casefold()
    terminal = status_casefold in TERMINAL_STATUSES
    error = request.get("error")
    has_error = error is not None and bool(error.strip())
    terminal_status_consistent = (
        not terminal
        or bool(assistant_texts)
        or has_error
        or status_casefold in {"cancelled", "canceled"}
    )
    violations: list[str] = []
    if not terminal_status_consistent:
        violations.append("terminal_without_visible_answer_or_error")
    if missing_summaries:
        violations.append("finished_subagent_missing_summary")
    if duplicate_assistant_texts:
        violations.append("duplicate_assistant_terminal_text")
    if require_zero_payload_tasks and payload_task_count:
        violations.append("payload_task_delta_nonzero")
    if (
        max_payload_tasks is not None
        and payload_task_count > max_payload_tasks
    ):
        violations.append("payload_task_budget_exceeded")
    if (
        expect_halt_reason is not None
        and (
            type(transcript.get("halt_reason"))
            is not type(expect_halt_reason)
            or transcript.get("halt_reason") != expect_halt_reason
        )
    ):
        violations.append("unexpected_halt_reason")
    if (
        transcript.get("halt_reason") == "operator_input_requested"
        and input_requested_count == 0
    ):
        violations.append(
            "operator_input_requested_without_visible_input_card"
        )
    timing_violation_kinds = {
        issue["kind"] for issue in gaps["timing_issues"]
    }
    violations.extend(sorted(timing_violation_kinds))
    if (
        max_post_subagent_gap_seconds is not None
        and gaps["post_subagent_gap_unmeasurable_count"] > 0
    ):
        violations.append("post_subagent_activity_gap_unmeasurable")
    elif max_post_subagent_gap_seconds is not None:
        measured_gap = gaps["max_post_subagent_gap_seconds"]
        if measured_gap is None:
            violations.append("post_subagent_activity_gap_unmeasurable")
        elif measured_gap > max_post_subagent_gap_seconds:
            violations.append("post_subagent_activity_gap_exceeded")
    if any(
        task.get("task_identity_valid") is False
        for task in payload_tasks
    ):
        violations.append("invalid_task_identity")

    violations = list(dict.fromkeys(violations))

    return {
        "schema": "sage-trace-audit-v1",
        "request": {
            "chat_channel_id": request.get("channel_id"),
            "chat_request_id": request.get("id"),
            "status": status,
            "message_count": len(messages),
            "halt_reason": transcript.get("halt_reason"),
        },
        "transcript": {
            "assistant_text_messages": len(assistant_texts),
            "tool_cards": len(tool_cards),
            "subagent_cards": len(subagent_cards),
            "finished_subagents": len(finished_subagents),
            "finished_subagents_missing_summary": missing_summaries,
            "input_requested_count": input_requested_count,
            "duplicate_assistant_texts": duplicate_assistant_texts,
            "runtime_telemetry_present": bool(runtime_telemetry),
            "payload_task_count": payload_task_count,
            "payload_tasks": payload_tasks,
            "runtime_payload_task_count": len(runtime_payload_tasks),
            "mythic_task_card_evidence_count": len(task_card_evidence),
            "mythic_task_card_evidence": task_card_evidence,
            **gaps,
        },
        "phoenix": _phoenix_evidence(
            phoenix_db, list(trace_rowids or [])
        ),
        "checks": {
            "request_identity_present": (
                type(request.get("id")) is int
                and type(request.get("channel_id")) is int
            ),
            "terminal_status_consistent": terminal_status_consistent,
            "finished_subagents_have_summaries": not missing_summaries,
            "assistant_text_is_not_duplicated": not duplicate_assistant_texts,
            "payload_task_expectation_met": not any(
                item
                in {
                    "payload_task_delta_nonzero",
                    "payload_task_budget_exceeded",
                }
                for item in violations
            ),
        },
        "violations": violations,
        "ok": not violations,
    }


def _write_json(path: str | Path, value: dict[str, Any]) -> Path:
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(resolved)
    return resolved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--phoenix-db")
    parser.add_argument("--trace-rowid", type=int, action="append", default=[])
    parser.add_argument("--require-zero-payload-tasks", action="store_true")
    parser.add_argument("--max-payload-tasks", type=int)
    parser.add_argument("--expect-halt-reason")
    parser.add_argument("--max-post-subagent-gap-seconds", type=float)
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        transcript = load_transcript(args.transcript)
        result = audit_transcript(
            transcript,
            phoenix_db=args.phoenix_db,
            trace_rowids=args.trace_rowid,
            require_zero_payload_tasks=args.require_zero_payload_tasks,
            max_payload_tasks=args.max_payload_tasks,
            expect_halt_reason=args.expect_halt_reason,
            max_post_subagent_gap_seconds=args.max_post_subagent_gap_seconds,
        )
    except (RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "status": "audit_input_error", "error": str(exc)},
                indent=2,
            )
        )
        return 2
    if args.output:
        result["output_path"] = str(_write_json(args.output, result))
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
