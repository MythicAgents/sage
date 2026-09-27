#!/usr/bin/env python3
"""Normalize a small source-bound subset of native Mythic action evidence."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any


SCHEMA = "sage-m3-native-action-observations-v1"
TRANSPARENT_CONTRACTS = {
    "ps": "process-listing",
    "whoami": "process-identity-query",
    "getuid": "process-identity-query",
}
_TASK_FIELDS = {
    "id", "operation_id", "callback_id", "parent_task_id", "command_name",
    "original_params", "display_params", "completed", "status",
    "status_timestamp_preprocessing", "opsec_pre_blocked", "opsec_pre_bypassed",
    "response_count",
}
_RESPONSE_FIELDS = {
    "id", "operation_id", "task_id", "response_text", "timestamp", "sequence_number",
}
_CALLBACK_FIELDS = {
    "id", "operation_id", "display_id", "host", "pid", "user", "process_name",
}
_SOURCE_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class NativeActionCaptureError(ValueError):
    pass


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _opaque(namespace: str, value: Any) -> str:
    return _canonical_hash({"namespace": namespace, "value": value})


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    result = parsed.astimezone(UTC).timestamp()
    return result if math.isfinite(result) else None


def _index_rows(rows: Sequence[Mapping[str, Any]], *, table: str, required: set[str],
                operation_id: int, failures: list[str]) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    seen: set[int] = set()
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        failures.append(f"{table}:rows_not_sequence")
        return indexed
    for row in rows:
        if not isinstance(row, Mapping):
            failures.append(f"{table}:row_not_mapping")
            continue
        if not required <= set(row):
            failures.append(f"{table}:missing_required_fields")
            continue
        row_id = row.get("id")
        if not _positive_int(row_id):
            failures.append(f"{table}:invalid_id")
            continue
        if row_id in seen:
            failures.append(f"{table}:duplicate_id")
            indexed.pop(row_id, None)
            continue
        seen.add(row_id)
        if row.get("operation_id") != operation_id:
            failures.append(f"{table}:wrong_operation")
            continue
        indexed[row_id] = dict(row)
    return indexed


def _runtime_handles(
    callback: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
    *,
    expected_before_task_id: int,
) -> tuple[str | None, str | None, str | None]:
    """Accept a source-bound effective context observed before this task window."""
    if not isinstance(evidence, Mapping):
        return None, None, "runtime_context_missing"
    expected = {
        "basis": "verified_start",
        "observed_before_task_id": expected_before_task_id,
        "operation_id": callback.get("operation_id"),
        "callback_id": callback.get("id"),
        "callback_display_id": callback.get("display_id"),
        "host": callback.get("host"),
        "pid": callback.get("pid"),
        "source_record_sha256": _canonical_hash(callback),
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        return None, None, "runtime_context_source_mismatch"
    if not isinstance(evidence.get("source_ref"), str) or not _SOURCE_REF.fullmatch(evidence["source_ref"]) \
            or not isinstance(evidence.get("source_sha256"), str) \
            or len(evidence["source_sha256"].removeprefix("sha256:")) != 64 \
            or any(c not in "0123456789abcdef" for c in evidence["source_sha256"].removeprefix("sha256:").casefold()) \
            or not isinstance(evidence.get("effective_context_fingerprint"), str) \
            or len(evidence["effective_context_fingerprint"].removeprefix("sha256:")) != 64 \
            or any(c not in "0123456789abcdef" for c in evidence["effective_context_fingerprint"].removeprefix("sha256:").casefold()):
        return None, None, "runtime_context_unbound"
    target = _opaque("native-target", {
        "operation_id": expected["operation_id"], "host": expected["host"],
    })
    context = _opaque("native-process-context", {
        "operation_id": expected["operation_id"],
        "callback_id": expected["callback_id"],
        "callback_display_id": expected["callback_display_id"],
        "host": expected["host"], "pid": expected["pid"],
        "effective_context_fingerprint": evidence["effective_context_fingerprint"],
    })
    return target, context, None


def _transparent_contract(task: Mapping[str, Any]) -> tuple[str | None, str | None]:
    command = task.get("command_name")
    if task.get("original_params") != "" or task.get("display_params") != "":
        return None, "unsupported_argument_shape" if command in TRANSPARENT_CONTRACTS else "unsupported_command_contract"
    if command in {"whoami", "getuid"}:
        from ai.hillclimb.live_seams import _task_is_identity_query
        return (TRANSPARENT_CONTRACTS[command], None) if _task_is_identity_query(dict(task)) else (None, "unsupported_command_contract")
    if command == "ps":
        from ai.hillclimb.trust_walker_outcome import OBJECTIVE_INERT_UNFINISHED_COMMANDS
        return (TRANSPARENT_CONTRACTS[command], None) if command in OBJECTIVE_INERT_UNFINISHED_COMMANDS else (None, "unsupported_command_contract")
    return None, "unsupported_command_contract"


def extract_native_actions(
    tasks: Sequence[Mapping[str, Any]],
    responses: Sequence[Mapping[str, Any]],
    callbacks: Sequence[Mapping[str, Any]],
    *,
    expected_operation_id: int,
    episode_task_ids: Sequence[int],
    runtime_context_by_callback: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return in-memory observations; this function makes no scorer/admission decision.

    The hashes cover authoritative source records while the returned object excludes
    raw task arguments, response bodies, and callback identity values.
    """
    if not _positive_int(expected_operation_id):
        raise NativeActionCaptureError("expected_operation_id must be a positive integer")
    if isinstance(episode_task_ids, (str, bytes)) or not isinstance(episode_task_ids, Sequence) \
            or any(not _positive_int(item) for item in episode_task_ids):
        raise NativeActionCaptureError("episode_task_ids must be positive integers")
    if len(set(episode_task_ids)) != len(episode_task_ids):
        raise NativeActionCaptureError("episode_task_ids must be unique")
    if runtime_context_by_callback is not None and not isinstance(runtime_context_by_callback, Mapping):
        raise NativeActionCaptureError("runtime_context_by_callback must be a mapping")

    failures: list[str] = []
    unknowns: list[dict[str, Any]] = []
    task_by_id = _index_rows(tasks, table="task", required=_TASK_FIELDS,
                             operation_id=expected_operation_id, failures=failures)
    response_by_id = _index_rows(responses, table="response", required=_RESPONSE_FIELDS,
                                 operation_id=expected_operation_id, failures=failures)
    callback_by_id = _index_rows(callbacks, table="callback", required=_CALLBACK_FIELDS,
                                 operation_id=expected_operation_id, failures=failures)

    all_task_ids = set(task_by_id)
    response_by_task: dict[int, list[dict[str, Any]]] = {}
    for response in response_by_id.values():
        task_id = response.get("task_id")
        if task_id not in all_task_ids:
            failures.append("response:orphan_task")
            continue
        response_by_task.setdefault(task_id, []).append(response)
    for task in task_by_id.values():
        callback_id = task.get("callback_id")
        if callback_id not in callback_by_id:
            failures.append("task:orphan_callback")
        parent_id = task.get("parent_task_id")
        if parent_id is not None and parent_id not in all_task_ids:
            failures.append("task:orphan_parent")

    selected: list[dict[str, Any]] = []
    for task_id in episode_task_ids:
        task = task_by_id.get(task_id)
        if task is None:
            failures.append("episode:task_missing_or_ambiguous")
        else:
            selected.append(task)

    task_times: dict[int, float] = {}
    time_to_ids: dict[float, list[int]] = {}
    for task in selected:
        parsed = _timestamp(task.get("status_timestamp_preprocessing"))
        if parsed is None:
            failures.append("task:missing_or_invalid_preprocessing_timestamp")
            continue
        task_times[task["id"]] = parsed
        time_to_ids.setdefault(parsed, []).append(task["id"])
    if any(len(ids) != 1 for ids in time_to_ids.values()):
        failures.append("task:ambiguous_preprocessing_timestamp")
    ordering_reliable = len(task_times) == len(selected) and not any(
        len(ids) != 1 for ids in time_to_ids.values()
    )
    selected.sort(key=lambda row: (task_times.get(row["id"], float("inf")), row["id"]))
    order_by_task = {row["id"]: index for index, row in enumerate(selected)} if ordering_reliable else {}
    if ordering_reliable:
        for task in selected:
            parent_id = task.get("parent_task_id")
            if parent_id in order_by_task and order_by_task[parent_id] >= order_by_task[task["id"]]:
                failures.append("task:parent_order_invalid")

    # A task omitted from the caller's episode IDs but placed by Mythic between
    # selected tasks on the same callback is an explicit episode-boundary gap.
    selected_ids = set(episode_task_ids)
    barriers: dict[int, list[float]] = {}
    if ordering_reliable:
        selected_times_by_callback: dict[int, list[float]] = {}
        for task in selected:
            selected_times_by_callback.setdefault(task["callback_id"], []).append(task_times[task["id"]])
        for task in task_by_id.values():
            if task["id"] in selected_ids or task.get("callback_id") not in selected_times_by_callback:
                continue
            parsed = _timestamp(task.get("status_timestamp_preprocessing"))
            window = selected_times_by_callback[task["callback_id"]]
            if parsed is not None and min(window) <= parsed <= max(window):
                barriers.setdefault(task["callback_id"], []).append(parsed)
                failures.append("episode:unselected_interleaving_task")

    first_task_by_callback: dict[int, int] = {}
    for task in selected:
        first_task_by_callback.setdefault(task.get("callback_id"), task["id"])
    live_context: dict[int, tuple[str | None, str | None, str | None]] = {}
    for callback_id, first_task_id in first_task_by_callback.items():
        callback = callback_by_id.get(callback_id)
        if callback is not None:
            live_context[callback_id] = _runtime_handles(
                callback, (runtime_context_by_callback or {}).get(callback_id),
                expected_before_task_id=first_task_id,
            )

    observations = []
    for task in selected:
        task_id = task["id"]
        if task.get("completed") is not True:
            failures.append("task:unfinished")
        callback = callback_by_id.get(task.get("callback_id"))
        task_responses = sorted(response_by_task.get(task_id, []), key=lambda row: row["id"])
        sequences = [row.get("sequence_number") for row in task_responses]
        if any(type(item) is not int or item < 0 for item in sequences) \
                or len(set(sequences)) != len(sequences):
            failures.append("response:invalid_or_duplicate_sequence")
        if type(task.get("response_count")) is not int or task.get("response_count") < 0 \
                or task.get("response_count") != len(task_responses):
            failures.append("response:count_mismatch")

        command = task.get("command_name")
        action_class, unsupported_reason = _transparent_contract(task)
        transparent = action_class is not None
        target_handle = context_handle = None
        context_gap = None
        if callback is not None and any(
            timestamp <= task_times.get(task_id, float("-inf"))
            for timestamp in barriers.get(callback["id"], [])
        ):
            live_context[callback["id"]] = (
                None, None, "runtime_context_invalidated_by_unselected_task"
            )
        if transparent and callback is not None:
            target_handle, context_handle, context_gap = live_context.get(
                callback["id"], (None, None, "runtime_context_missing")
            )
        if not transparent:
            unknowns.append({"task_id": task_id, "field": "mutation_relevance",
                             "reason": unsupported_reason})
        if transparent and context_gap:
            unknowns.append({"task_id": task_id, "field": "normalization", "reason": context_gap})

        action_handle = None
        if action_class is not None and target_handle is not None and context_handle is not None:
            action_handle = _opaque("native-action", {
                "action_class": action_class,
                "target_handle": target_handle,
                "context_handle": context_handle,
            })
        terminal = "unknown"
        parser = None
        if transparent and task.get("completed") is True \
                and command in {"whoami", "getuid"} and task_responses:
            # Existing live identity verification treats this exact output line as definitive.
            from ai.hillclimb.live_seams import _output_is_system_identity
            if any(_output_is_system_identity(str(row["response_text"])) for row in task_responses):
                terminal = "system_identity_observed"
                parser = "ai.hillclimb.live_seams._output_is_system_identity-v1"
        if terminal == "unknown":
            unknowns.append({"task_id": task_id, "field": "terminal_outcome",
                             "reason": "no_definitive_supported_result_parser"})

        observations.append({
            "task_id": task_id,
            "order_index": order_by_task.get(task_id),
            "ordering_reliable": ordering_reliable,
            "attempted": True,
            "mutation_relevance": "transparent_nonmutating" if transparent else "unknown",
            "action_class": action_class,
            "normalization": "known" if action_handle is not None else "unknown",
            "action_handle": action_handle,
            "target_handle": target_handle,
            "context_handle": context_handle,
            "terminal_outcome": terminal,
            "terminal_parser": parser,
            "task_complete_is_success_evidence": False,
            "context_evidence": (
                {
                    "basis": (runtime_context_by_callback or {})[callback["id"]]["basis"],
                    "source_ref": (runtime_context_by_callback or {})[callback["id"]]["source_ref"],
                    "source_sha256": (runtime_context_by_callback or {})[callback["id"]]["source_sha256"],
                    "effective_context_fingerprint": (
                        runtime_context_by_callback or {}
                    )[callback["id"]]["effective_context_fingerprint"],
                }
                if action_handle is not None and callback is not None else None
            ),
            "source_records": [
                {"table": "task", "id": task_id, "record_sha256": _canonical_hash(task)},
                *({"table": "callback", "id": callback["id"],
                   "record_sha256": _canonical_hash(callback)} for _ in [0] if callback is not None),
                *({"table": "response", "id": row["id"],
                   "record_sha256": _canonical_hash(row)} for row in task_responses),
            ],
            "source_fields": {
                "task": sorted(_TASK_FIELDS),
                "callback": sorted(_CALLBACK_FIELDS),
                "response": sorted(_RESPONSE_FIELDS) if task_responses else [],
            },
        })
        # An unsupported action can change the effective token, ticket or process
        # context. Callback user/host metadata is not a fresh proof of that state.
        if not transparent and callback is not None:
            live_context[callback["id"]] = (None, None, "runtime_context_invalidated_by_unknown_action")

    return {
        "schema": SCHEMA,
        "operation_id": expected_operation_id,
        "complete": not failures,
        "failures": list(dict.fromkeys(failures)),
        "observations": observations,
        "unknowns": unknowns,
        "semantics_source": {
            "transparent_commands": "trust_walker_outcome.OBJECTIVE_INERT_UNFINISHED_COMMANDS and live_seams._task_is_identity_query",
            "argument_contract": "mythic_tools issue_task_and_waitfor_task_output no-argument normalization",
            "terminal_parser": "live_seams._output_is_system_identity",
        },
    }


def main() -> int:
    print("DRY RUN: import extract_native_actions with an authoritative in-memory inventory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
