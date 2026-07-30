from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "trace_audit.py"
SPEC = importlib.util.spec_from_file_location("sage_trace_audit", SCRIPT)
trace_audit = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(trace_audit)


def _transcript() -> dict:
    return {
        "schema": "sage-native-chat-transcript-v1",
        "chat_channel_id": 4,
        "chat_request_id": 9,
        "status": "complete",
        "error": "",
        "request": {
            "id": 9,
            "channel_id": 4,
            "request_message_id": 1,
            "status": "complete",
            "error": "",
        },
        "messages": [
            {
                "id": 1,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "operator",
                "message": "What callbacks are available?",
                "metadata": {},
                "created_at": "2026-07-23T01:00:00Z",
                "updated_at": "2026-07-23T01:00:00Z",
            },
            {
                "id": 2,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "container",
                "message": "",
                "metadata": {
                    "special_type": "subagent",
                    "subagent": {
                        "name": "Mythic Operator",
                        "status": "finished",
                        "summary": "Two active callbacks.",
                    },
                },
                "created_at": "2026-07-23T01:00:01Z",
                "updated_at": "2026-07-23T01:00:04Z",
            },
            {
                "id": 3,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "container",
                "message": "| ID | Host |\n|---|---|\n| 1 | CASTELBLACK |",
                "metadata": {"runtime_telemetry": {"transactions": []}},
                "created_at": "2026-07-23T01:00:05Z",
                "updated_at": "2026-07-23T01:00:05Z",
            },
        ],
        "runtime_telemetry": {"transactions": []},
    }


def _set_path(value: dict, path: tuple[object, ...], replacement) -> None:
    current = value
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = replacement


def test_audit_accepts_structured_single_final_transcript():
    result = trace_audit.audit_transcript(
        _transcript(), require_zero_payload_tasks=True
    )

    assert result["ok"] is True
    assert result["transcript"]["finished_subagents"] == 1
    assert result["transcript"]["payload_task_count"] == 0
    assert result["transcript"]["max_post_subagent_gap_seconds"] == 1.0


def test_audit_flags_missing_summary_duplicate_final_and_payload_task():
    transcript = _transcript()
    transcript["messages"][1]["metadata"]["subagent"]["summary"] = ""
    transcript["messages"].append(
        {
            "id": 4,
            "channel_id": 4,
            "chat_request_id": 9,
            "author_type": "container",
            "message": transcript["messages"][2]["message"],
            "metadata": {},
        }
    )
    transcript["runtime_telemetry"]["transactions"] = [
        {"child_tasks": [{"task_id": 16, "command": "dcsync"}]}
    ]

    result = trace_audit.audit_transcript(
        transcript, require_zero_payload_tasks=True
    )

    assert result["ok"] is False
    assert set(result["violations"]) >= {
        "finished_subagent_missing_summary",
        "duplicate_assistant_terminal_text",
        "payload_task_delta_nonzero",
    }


def test_load_transcript_fails_closed_on_wrong_schema(tmp_path):
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps({"schema": "unknown"}), encoding="utf-8")

    with pytest.raises(ValueError, match="schema"):
        trace_audit.load_transcript(path)


def test_load_transcript_fails_closed_on_redundant_identity_or_status_drift(
    tmp_path,
):
    transcript = _transcript()
    transcript["chat_request_id"] = 10
    path = tmp_path / "request-drift.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    with pytest.raises(ValueError, match="chat_request_id"):
        trace_audit.load_transcript(path)

    transcript = _transcript()
    transcript["chat_channel_id"] = 10
    path = tmp_path / "channel-drift.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    with pytest.raises(ValueError, match="chat_channel_id"):
        trace_audit.load_transcript(path)

    transcript = _transcript()
    transcript["status"] = "processing"
    path = tmp_path / "status-drift.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    with pytest.raises(ValueError, match="status"):
        trace_audit.load_transcript(path)

    transcript = _transcript()
    transcript["error"] = "different"
    path = tmp_path / "error-drift.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    with pytest.raises(ValueError, match="error"):
        trace_audit.load_transcript(path)

    transcript = _transcript()
    transcript["messages"][0]["channel_id"] = 99
    path = tmp_path / "message-drift.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    with pytest.raises(ValueError, match="messages\\[0\\].channel_id"):
        trace_audit.load_transcript(path)

    transcript = _transcript()
    transcript["messages"][0]["chat_request_id"] = 99
    path = tmp_path / "message-request-drift.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    with pytest.raises(
        ValueError, match="messages\\[0\\].chat_request_id"
    ):
        trace_audit.load_transcript(path)


@pytest.mark.parametrize("invalid_id", [True, 9.0, "9"])
@pytest.mark.parametrize(
    "path",
    [
        ("chat_request_id",),
        ("request", "id"),
        ("request", "request_message_id"),
        ("messages", 0, "id"),
        ("messages", 0, "chat_request_id"),
    ],
)
def test_request_and_message_ids_require_exact_integers(
    tmp_path, path, invalid_id
):
    transcript = _transcript()
    _set_path(transcript, path, invalid_id)
    transcript_path = tmp_path / "invalid-identity.json"
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")

    with pytest.raises(ValueError, match="exact integer"):
        trace_audit.load_transcript(transcript_path)


@pytest.mark.parametrize(
    ("field", "invalid_value", "error_match"),
    [
        ("status", True, "status"),
        ("status", 1.0, "status"),
        ("error", True, "error"),
        ("error", 1.0, "error"),
    ],
)
def test_status_and_error_identity_require_canonical_types(
    tmp_path, field, invalid_value, error_match
):
    transcript = _transcript()
    transcript[field] = invalid_value
    transcript["request"][field] = invalid_value
    transcript_path = tmp_path / "invalid-status-or-error.json"
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")

    with pytest.raises(ValueError, match=error_match):
        trace_audit.load_transcript(transcript_path)


@pytest.mark.parametrize("field", ["status", "error"])
def test_status_and_error_redundancy_uses_type_exact_equality(
    tmp_path, field
):
    transcript = _transcript()
    transcript[field] = True
    transcript["request"][field] = 1
    transcript_path = tmp_path / "loosely-equal-redundancy.json"
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        trace_audit.load_transcript(transcript_path)


def test_status_and_error_identity_accepts_exact_string_and_null():
    transcript = _transcript()
    transcript["error"] = None
    transcript["request"]["error"] = None

    result = trace_audit.audit_transcript(transcript)

    assert result["ok"] is True
    assert result["request"]["status"] == "complete"


def test_optional_phoenix_reader_loads_with_dataclass_metadata():
    module = trace_audit._load_module(
        "sage_trace_audit_test_phoenix_reader",
        trace_audit.PHOENIX_READER,
    )

    assert hasattr(module, "aggregate_metrics")


def test_input_requested_and_expected_halt_are_audited():
    transcript = _transcript()
    transcript["status"] = "processing"
    transcript["request"]["status"] = "processing"
    transcript["halt_reason"] = "operator_input_requested"
    transcript["messages"] = [
        transcript["messages"][0],
        {
            "id": 2,
            "channel_id": 4,
            "chat_request_id": 9,
            "author_type": "container",
            "message": "",
            "metadata": {
                "container_metadata": {
                    "special_type": "input_requested",
                    "input_requested": {"title": "Approve"},
                }
            },
        },
    ]

    result = trace_audit.audit_transcript(
        transcript,
        expect_halt_reason="operator_input_requested",
        require_zero_payload_tasks=True,
    )

    assert result["ok"] is True
    assert result["transcript"]["input_requested_count"] == 1


@pytest.mark.parametrize("input_card_state", ["missing", "deleted"])
def test_operator_input_halt_requires_a_visible_input_card(input_card_state):
    transcript = _transcript()
    transcript["status"] = "processing"
    transcript["request"]["status"] = "processing"
    transcript["halt_reason"] = "operator_input_requested"
    if input_card_state == "deleted":
        transcript["messages"].append(
            {
                "id": 4,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "container",
                "message": "",
                "deleted": True,
                "metadata": {
                    "special_type": "input_requested",
                    "input_requested": {"title": "Approve"},
                },
            }
        )

    result = trace_audit.audit_transcript(transcript)

    assert result["ok"] is False
    assert result["transcript"]["input_requested_count"] == 0
    assert (
        "operator_input_requested_without_visible_input_card"
        in result["violations"]
    )


@pytest.mark.parametrize("status", ["started", "completed", "error", "failed"])
@pytest.mark.parametrize("card_kind", ["issue-wrapper", "execution"])
def test_any_task_card_lifecycle_state_blocks_zero_task_certification(
    status, card_kind
):
    transcript = _transcript()
    tool_use = {
        "tool_call_id": f"{card_kind}-{status}",
        "tool_name": (
            "issue_task_and_waitfor_task_output"
            if card_kind == "issue-wrapper"
            else "whoami"
        ),
        "tool_source": "mythic",
        "status": status,
    }
    if card_kind == "execution":
        tool_use["arguments"] = json.dumps(
            {"callback_id": 1, "parameters": ""}
        )
    transcript["messages"].append(
        {
            "id": 4,
            "channel_id": 4,
            "chat_request_id": 9,
            "author_type": "container",
            "message": "",
            "metadata": {
                "special_type": "tool_use",
                "tool_use": tool_use,
            },
            "created_at": "2026-07-23T01:00:06Z",
            "updated_at": "2026-07-23T01:00:06Z",
        }
    )

    result = trace_audit.audit_transcript(
        transcript, require_zero_payload_tasks=True
    )

    assert result["ok"] is False
    assert "payload_task_delta_nonzero" in result["violations"]
    assert result["transcript"]["mythic_task_card_evidence_count"] == 1
    assert result["transcript"]["payload_task_count"] == 1
    assert result["transcript"]["mythic_task_card_evidence"][0][
        "statuses"
    ] == [status]


def test_completed_mythic_issue_task_card_counts_as_payload_task_evidence():
    transcript = _transcript()
    transcript["messages"].append(
        {
            "id": 4,
            "channel_id": 4,
            "chat_request_id": 9,
            "author_type": "container",
            "message": "",
            "metadata": {
                "special_type": "tool_use",
                "tool_use": {
                    "tool_call_id": "tool-1",
                    "tool_name": "issue_task_and_waitfor_task_output",
                    "tool_source": "mythic",
                    "status": "completed",
                    "result_preview": json.dumps({"task_id": 123}),
                },
            },
            "created_at": "2026-07-23T01:00:06Z",
            "updated_at": "2026-07-23T01:00:06Z",
        },
    )

    result = trace_audit.audit_transcript(
        transcript, require_zero_payload_tasks=True
    )

    assert result["ok"] is False
    assert "payload_task_delta_nonzero" in result["violations"]
    assert result["transcript"]["runtime_payload_task_count"] == 0
    assert (
        result["transcript"]["mythic_task_card_evidence_count"]
        == 1
    )
    assert result["transcript"]["payload_task_count"] == 1


def test_task_evidence_is_an_exact_id_union_with_unbound_cards():
    transcript = _transcript()
    transcript["runtime_telemetry"]["transactions"] = [
        {"child_tasks": [{"task_id": 16, "command": "whoami"}]}
    ]
    transcript["messages"].extend(
        [
            {
                "id": 4,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "container",
                "message": "",
                "metadata": {
                    "special_type": "tool_use",
                    "tool_use": {
                        "tool_call_id": "execution-16",
                        "tool_name": "whoami",
                        "tool_source": "mythic",
                        "status": "completed",
                        "arguments": json.dumps(
                            {
                                "callback_id": 1,
                                "parameters": "",
                                "task_id": 16,
                            }
                        ),
                    },
                },
            },
            {
                "id": 5,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "container",
                "message": "",
                "metadata": {
                    "special_type": "tool_use",
                    "tool_use": {
                        "tool_call_id": "execution-17",
                        "tool_name": "hostname",
                        "tool_source": "mythic",
                        "status": "completed",
                        "arguments": json.dumps(
                            {
                                "callback_id": 1,
                                "parameters": "",
                                "task_id": 17,
                            }
                        ),
                    },
                },
            },
            {
                "id": 6,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "container",
                "message": "",
                "metadata": {
                    "special_type": "tool_use",
                    "tool_use": {
                        "tool_call_id": "unbound-attempt",
                        "tool_name": "issue_task_and_waitfor_task_output",
                        "tool_source": "mythic",
                        "status": "completed",
                    },
                },
            },
        ]
    )

    result = trace_audit.audit_transcript(transcript)

    assert result["transcript"]["runtime_payload_task_count"] == 1
    assert (
        result["transcript"]["mythic_task_card_evidence_count"]
        == 3
    )
    assert result["transcript"]["payload_task_count"] == 3
    assert {
        str(task.get("task_id"))
        for task in result["transcript"]["payload_tasks"]
    } == {"16", "17", "None"}


@pytest.mark.parametrize("invalid_task_id", [True, 16.0, "16"])
def test_runtime_task_ids_are_exact_integers(invalid_task_id):
    transcript = _transcript()
    transcript["runtime_telemetry"]["transactions"] = [
        {
            "child_tasks": [
                {"task_id": 16, "command": "whoami"},
                {"task_id": invalid_task_id, "command": "hostname"},
            ]
        }
    ]

    result = trace_audit.audit_transcript(transcript)

    assert result["ok"] is False
    assert "invalid_task_identity" in result["violations"]
    assert result["transcript"]["payload_task_count"] == 2
    assert sorted(
        task["task_id"]
        for task in result["transcript"]["payload_tasks"]
        if task["task_id"] is not None
    ) == [16]
    assert sum(
        task["task_id"] is None
        for task in result["transcript"]["payload_tasks"]
    ) == 1


@pytest.mark.parametrize("invalid_task_id", [True, 16.0, "16"])
def test_tool_card_task_ids_are_not_coerced_or_merged(invalid_task_id):
    transcript = _transcript()
    transcript["runtime_telemetry"]["transactions"] = [
        {"child_tasks": [{"task_id": 16, "command": "whoami"}]}
    ]
    transcript["messages"].append(
        {
            "id": 4,
            "channel_id": 4,
            "chat_request_id": 9,
            "author_type": "container",
            "message": "",
            "metadata": {
                "special_type": "tool_use",
                "tool_use": {
                    "tool_call_id": "invalid-task-id",
                    "tool_name": "issue_task_and_waitfor_task_output",
                    "tool_source": "mythic",
                    "status": "completed",
                    "result_preview": json.dumps(
                        {"task_id": invalid_task_id}
                    ),
                },
            },
            "created_at": "2026-07-23T01:00:06Z",
            "updated_at": "2026-07-23T01:00:06Z",
        }
    )

    result = trace_audit.audit_transcript(transcript)

    assert result["ok"] is False
    assert "invalid_task_identity" in result["violations"]
    assert result["transcript"]["payload_task_count"] == 2
    assert {
        task["task_id"] for task in result["transcript"]["payload_tasks"]
    } == {16, None}


def test_mcp_and_read_only_task_card_near_matches_do_not_count():
    transcript = _transcript()
    transcript["messages"].extend(
        [
            {
                "id": 4,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "container",
                "message": "",
                "metadata": {
                    "special_type": "tool_use",
                    "tool_use": {
                        "tool_call_id": "tool-1",
                        "tool_name": "issue_task_and_waitfor_task_output",
                        "tool_source": "mcp",
                        "status": "completed",
                    },
                },
                "created_at": "2026-07-23T01:00:06Z",
                "updated_at": "2026-07-23T01:00:06Z",
            },
            {
                "id": 5,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "container",
                "message": "",
                "metadata": {
                    "special_type": "tool_use",
                    "tool_use": {
                        "tool_call_id": "tool-2",
                        "tool_name": "get_all_task_output_by_task_id",
                        "tool_source": "mythic",
                        "status": "completed",
                        "arguments": json.dumps({"task_id": 44}),
                    },
                },
                "created_at": "2026-07-23T01:00:07Z",
                "updated_at": "2026-07-23T01:00:07Z",
            },
            {
                "id": 6,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "container",
                "message": "",
                "metadata": {
                    "special_type": "tool_use",
                    "tool_use": {
                        "tool_call_id": "tool-3",
                        "tool_name": "get_all_task_output_by_task_id_extra",
                        "tool_source": "mythic",
                        "status": "completed",
                        "arguments": json.dumps({"task_id": 44}),
                    },
                },
                "created_at": "2026-07-23T01:00:08Z",
                "updated_at": "2026-07-23T01:00:08Z",
            },
            {
                "id": 7,
                "channel_id": 4,
                "chat_request_id": 9,
                "author_type": "container",
                "message": "",
                "metadata": {
                    "special_type": "tool_use",
                    "tool_use": {
                        "tool_call_id": "tool-4",
                        "tool_name": (
                            "issue_task_and_waitfor_task_output_extra"
                        ),
                        "tool_source": "mythic",
                        "status": "completed",
                    },
                },
                "created_at": "2026-07-23T01:00:09Z",
                "updated_at": "2026-07-23T01:00:09Z",
            },
        ]
    )

    result = trace_audit.audit_transcript(
        transcript, require_zero_payload_tasks=True
    )

    assert result["ok"] is True
    assert (
        result["transcript"]["mythic_task_card_evidence_count"]
        == 0
    )
    assert result["transcript"]["payload_task_count"] == 0


@pytest.mark.parametrize("author_type", ["container", "assistant", "ai"])
def test_known_assistant_authors_can_supply_terminal_answer(author_type):
    transcript = _transcript()
    transcript["messages"][2]["author_type"] = author_type

    result = trace_audit.audit_transcript(transcript)

    assert result["ok"] is True
    assert result["transcript"]["assistant_text_messages"] == 1


@pytest.mark.parametrize(
    ("author_type", "deleted"),
    [("unknown-runtime", False), ("container", True)],
)
def test_deleted_or_unknown_author_text_cannot_supply_terminal_answer(
    author_type, deleted
):
    transcript = _transcript()
    transcript["messages"][2]["author_type"] = author_type
    transcript["messages"][2]["deleted"] = deleted

    result = trace_audit.audit_transcript(transcript)

    assert result["ok"] is False
    assert result["transcript"]["assistant_text_messages"] == 0
    assert (
        "terminal_without_visible_answer_or_error" in result["violations"]
    )


@pytest.mark.parametrize(
    ("message_index", "field", "value"),
    [
        (0, "created_at", "not-a-timestamp"),
        (1, "updated_at", " "),
        (2, "created_at", 123),
    ],
)
def test_malformed_nonempty_timestamps_are_violations(
    message_index, field, value
):
    transcript = _transcript()
    transcript["messages"][message_index][field] = value

    result = trace_audit.audit_transcript(transcript)

    assert result["ok"] is False
    assert "malformed_message_timestamp" in result["violations"]
    assert any(
        issue["kind"] == "malformed_message_timestamp"
        and issue["message_id"]
        == transcript["messages"][message_index]["id"]
        and issue["field"] == field
        for issue in result["transcript"]["timing_issues"]
    )


def test_reversed_message_duration_and_negative_gap_are_violations():
    transcript = _transcript()
    transcript["messages"][1]["created_at"] = "2026-07-23T01:00:04Z"
    transcript["messages"][1]["updated_at"] = "2026-07-23T01:00:03Z"
    transcript["messages"][2]["created_at"] = "2026-07-23T01:00:02Z"
    transcript["messages"][2]["updated_at"] = "2026-07-23T01:00:02Z"

    result = trace_audit.audit_transcript(transcript)

    assert result["ok"] is False
    assert set(result["violations"]) >= {
        "negative_message_duration",
        "negative_message_gap",
    }
    assert result["transcript"]["max_post_subagent_gap_seconds"] is None


@pytest.mark.parametrize("unmeasurable_state", ["missing-time", "no-next"])
def test_requested_post_subagent_gap_fails_closed_when_unmeasurable(
    unmeasurable_state,
):
    transcript = _transcript()
    if unmeasurable_state == "missing-time":
        transcript["messages"][2]["created_at"] = None
    else:
        transcript["messages"] = transcript["messages"][:2]

    result = trace_audit.audit_transcript(
        transcript, max_post_subagent_gap_seconds=2.0
    )

    assert result["ok"] is False
    assert (
        "post_subagent_activity_gap_unmeasurable"
        in result["violations"]
    )
    assert (
        result["transcript"]["post_subagent_gap_unmeasurable_count"]
        == 1
    )


def test_requested_post_subagent_gap_accepts_measurable_gap_in_budget():
    result = trace_audit.audit_transcript(
        _transcript(), max_post_subagent_gap_seconds=1.0
    )

    assert result["ok"] is True
    assert result["transcript"]["post_subagent_gap_measured_count"] == 1
    assert result["transcript"]["max_post_subagent_gap_seconds"] == 1.0


def test_mixed_naive_and_aware_timestamps_are_normalized_to_utc():
    transcript = _transcript()
    transcript["messages"][0]["created_at"] = "2026-07-23T01:00:00"
    transcript["messages"][0]["updated_at"] = "2026-07-23T01:00:00"
    transcript["messages"][1]["created_at"] = "2026-07-23T01:00:01Z"
    transcript["messages"][1]["updated_at"] = "2026-07-23T01:00:04Z"

    result = trace_audit.audit_transcript(transcript)

    assert result["ok"] is True
    assert result["transcript"]["max_message_gap_seconds"] == 1.0


def test_cli_writes_only_the_requested_audit_artifact(tmp_path):
    transcript_path = tmp_path / "transcript.json"
    output_path = tmp_path / "audit.json"
    transcript_path.write_text(
        json.dumps(_transcript()), encoding="utf-8"
    )

    rc = trace_audit.main(
        [
            "--transcript",
            str(transcript_path),
            "--require-zero-payload-tasks",
            "--output",
            str(output_path),
        ]
    )

    assert rc == 0
    assert json.loads(output_path.read_text())["ok"] is True
