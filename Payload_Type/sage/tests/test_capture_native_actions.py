import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "skills/sage-eval-gauge/scripts/capture_native_actions.py"
SPEC = importlib.util.spec_from_file_location("capture_native_actions", SCRIPT)
capture = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(capture)


def _callback(callback_id=7):
    return {
        "id": callback_id, "operation_id": 42, "display_id": 3,
        "host": "host.test", "pid": 812, "user": "user", "process_name": "agent.exe",
    }


def _context(callback, first_task_id=10):
    return {
        "basis": "verified_start", "observed_before_task_id": first_task_id,
        "operation_id": callback["operation_id"], "callback_id": callback["id"],
        "callback_display_id": callback["display_id"], "host": callback["host"],
        "pid": callback["pid"], "source_record_sha256": capture._canonical_hash(callback),
        "source_ref": "fixture:verified-start", "source_sha256": "a" * 64,
        "effective_context_fingerprint": "b" * 64,
    }


def _task(task_id, command, when, *, params="", response_count=1, status="completed",
          parent_task_id=None):
    return {
        "id": task_id, "operation_id": 42, "callback_id": 7,
        "parent_task_id": parent_task_id, "command_name": command,
        "original_params": params, "display_params": params, "completed": True,
        "status": status, "status_timestamp_preprocessing": when,
        "opsec_pre_blocked": False, "opsec_pre_bypassed": False,
        "response_count": response_count,
    }


def _response(response_id, task_id, text, sequence_number=0):
    return {
        "id": response_id, "operation_id": 42, "task_id": task_id,
        "response_text": text, "timestamp": "2026-09-21T00:01:00Z",
        "sequence_number": sequence_number,
    }


def test_transparent_native_contracts_are_attempted_and_repeats_share_handle():
    callback = _callback()
    result = capture.extract_native_actions(
        [
            _task(12, "whoami", "2026-09-21T00:00:02Z"),
            _task(10, "ps", "2026-09-21T00:00:00Z"),
            _task(11, "whoami", "2026-09-21T00:00:01Z"),
            _task(13, "getuid", "2026-09-21T00:00:03Z"),
        ],
        [
            _response(22, 12, r"NT AUTHORITY\SYSTEM"),
            _response(20, 10, "process rows"),
            _response(21, 11, r"NORTH\user"),
            _response(23, 13, r"NT AUTHORITY\SYSTEM"),
        ],
        [callback], expected_operation_id=42, episode_task_ids=[10, 11, 12, 13],
        runtime_context_by_callback={7: _context(callback)},
    )
    assert result["complete"] is True
    assert [row["task_id"] for row in result["observations"]] == [10, 11, 12, 13]
    assert all(row["attempted"] is True for row in result["observations"])
    assert all(row["mutation_relevance"] == "transparent_nonmutating"
               for row in result["observations"])
    assert result["observations"][1]["action_handle"] == result["observations"][2]["action_handle"]
    assert result["observations"][1]["task_id"] != result["observations"][2]["task_id"]
    assert result["observations"][2]["terminal_outcome"] == "system_identity_observed"
    assert result["observations"][0]["terminal_outcome"] == "unknown"


def test_opaque_and_near_match_commands_stay_semantically_unknown_even_when_completed():
    callback = _callback()
    result = capture.extract_native_actions(
        [
            _task(10, "shell", "2026-09-21T00:00:00Z", params="whoami", status="error"),
            _task(11, "whoami", "2026-09-21T00:00:01Z", params="{}", status="completed"),
        ],
        [_response(20, 10, "synthetic-secret"), _response(21, 11, r"NT AUTHORITY\SYSTEM")],
        [callback], expected_operation_id=42, episode_task_ids=[10, 11],
        runtime_context_by_callback={7: _context(callback)},
    )
    assert result["complete"] is True
    assert [row["mutation_relevance"] for row in result["observations"]] == ["unknown", "unknown"]
    assert all(row["normalization"] == "unknown" for row in result["observations"])
    assert all(row["terminal_outcome"] == "unknown" for row in result["observations"])
    assert all(row["attempted"] is True for row in result["observations"])
    serialized = json.dumps(result)
    assert "synthetic-secret" not in serialized
    assert "whoami\"" not in serialized


def test_source_and_order_ambiguity_is_incomplete_without_suppressing_attempts():
    callback = _callback()
    tasks = [
        _task(10, "ps", "2026-09-21T00:00:00Z", response_count=2),
        _task(11, "ps", "2026-09-21T00:00:00Z", parent_task_id=99),
    ]
    result = capture.extract_native_actions(
        tasks, [_response(20, 10, "one")], [callback], expected_operation_id=42,
        episode_task_ids=[10, 11], runtime_context_by_callback={7: _context(callback)},
    )
    assert result["complete"] is False
    assert "task:ambiguous_preprocessing_timestamp" in result["failures"]
    assert "task:orphan_parent" in result["failures"]
    assert "response:count_mismatch" in result["failures"]
    assert all(row["attempted"] is True for row in result["observations"])
    assert all(row["order_index"] is None for row in result["observations"])


def test_runtime_context_needs_exact_callback_source_evidence():
    callback = _callback()
    bad = _context(callback); bad["pid"] += 1
    result = capture.extract_native_actions(
        [_task(10, "ps", "2026-09-21T00:00:00Z")], [_response(20, 10, "rows")],
        [callback], expected_operation_id=42, episode_task_ids=[10],
        runtime_context_by_callback={7: bad},
    )
    row = result["observations"][0]
    assert row["mutation_relevance"] == "transparent_nonmutating"
    assert row["normalization"] == "unknown"
    assert row["action_handle"] is None
    assert any(item["reason"] == "runtime_context_source_mismatch" for item in result["unknowns"])


def test_unknown_action_invalidates_same_callback_context_for_later_transparent_action():
    callback = _callback()
    result = capture.extract_native_actions(
        [
            _task(10, "ps", "2026-09-21T00:00:00Z"),
            _task(11, "steal_token", "2026-09-21T00:00:01Z"),
            _task(12, "ps", "2026-09-21T00:00:02Z"),
        ],
        [_response(20, 10, "rows"), _response(21, 11, "unknown"), _response(22, 12, "rows")],
        [callback], expected_operation_id=42, episode_task_ids=[10, 11, 12],
        runtime_context_by_callback={7: _context(callback)},
    )
    assert result["observations"][0]["normalization"] == "known"
    assert result["observations"][1]["normalization"] == "unknown"
    assert result["observations"][2]["normalization"] == "unknown"
    assert result["observations"][0]["action_handle"] is not None
    assert result["observations"][2]["action_handle"] is None
    assert any(item["task_id"] == 12 and item["reason"] == "runtime_context_invalidated_by_unknown_action"
               for item in result["unknowns"])


def test_unselected_interleaving_task_is_explicit_boundary_gap_and_invalidates_context():
    callback = _callback()
    result = capture.extract_native_actions(
        [
            _task(10, "ps", "2026-09-21T00:00:00Z"),
            _task(11, "make_token", "2026-09-21T00:00:01Z"),
            _task(12, "ps", "2026-09-21T00:00:02Z"),
        ],
        [_response(20, 10, "rows"), _response(21, 11, "changed"), _response(22, 12, "rows")],
        [callback], expected_operation_id=42, episode_task_ids=[10, 12],
        runtime_context_by_callback={7: _context(callback)},
    )
    assert result["complete"] is False
    assert "episode:unselected_interleaving_task" in result["failures"]
    assert result["observations"][0]["normalization"] == "known"
    assert result["observations"][1]["normalization"] == "unknown"
    assert any(item["reason"] == "runtime_context_invalidated_by_unselected_task"
               for item in result["unknowns"])


def test_creation_error_and_unfinished_rows_remain_attempts_without_success_inference():
    callback = _callback()
    task = _task(10, "ps", "2026-09-21T00:00:00Z", response_count=0, status="error")
    task["completed"] = False
    result = capture.extract_native_actions(
        [task], [], [callback], expected_operation_id=42, episode_task_ids=[10],
        runtime_context_by_callback={7: _context(callback)},
    )
    assert result["complete"] is False
    assert "task:unfinished" in result["failures"]
    assert result["observations"][0]["attempted"] is True
    assert result["observations"][0]["terminal_outcome"] == "unknown"
    assert result["observations"][0]["task_complete_is_success_evidence"] is False
