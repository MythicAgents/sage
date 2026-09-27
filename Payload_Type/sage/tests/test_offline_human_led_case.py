"""Native chat, real graph checkpoint, and exact approval in one offline slice."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


SCRIPT = (Path(__file__).resolve().parents[3] / "skills" /
          "sage-focused-capability-tests" / "scripts" / "run_offline_human_led_case.py")


def run_cli(*args: str) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                            text=True, timeout=20, check=False)
    records = result.stdout.strip().splitlines()
    assert len(records) == 1, (result.stdout, result.stderr[-1000:])
    return result.returncode, json.loads(records[0])


def test_exact_accept_and_fresh_process_repeat():
    for _ in range(2):
        code, result = run_cli()
        assert code == 0
        assert result["status"] == "passed"
        assert result["card_count"] == 1
        assert result["card_released"] is True
        assert result["paused_on_checkpoint"] is True
        assert result["resumed_checkpoint"] is True
        assert result["first_terminal_count"] == 0
        assert result["terminal_count"] == 1
        assert result["effect_calls"] == [{"callback_display_id": 7,
                                           "command": "whoami", "parameters": ""}]


def test_reject_and_near_match_never_issue_effect():
    code, rejected = run_cli("--decision", "reject")
    assert code == 0
    assert rejected["status"] == "passed"
    assert rejected["effect_calls"] == []
    assert rejected["resumed_checkpoint"] is True

    code, unexpected = run_cli("--proposed-command", "hostname")
    assert code == 1
    assert unexpected["status"] == "unexpected_proposal"
    assert unexpected["applied_decision"] == "reject"
    assert unexpected["proposals"][0]["arguments"]["command"] == "hostname"
    assert unexpected["effect_calls"] == []


def test_shell_whoami_uses_the_same_native_approval_and_resume_path():
    code, result = run_cli("--proposed-command", "shell")
    assert code == 0
    assert result["status"] == "passed"
    assert result["applied_decision"] == "accept"
    assert result["card_count"] == 1
    assert result["paused_on_checkpoint"] is True
    assert result["response_matches_pending"] is True
    assert result["resumed_checkpoint"] is True
    assert result["first_terminal_count"] == 0
    assert result["terminal_count"] == 1
    assert result["effect_calls"] == [{"callback_display_id": 7,
                                       "command": "shell",
                                       "parameters": '{"command": "whoami"}'}]


def test_redirect_uses_native_respond_and_stops_without_mock_effect():
    code, result = run_cli("--decision", "redirect")
    assert code == 0
    assert result["status"] == "passed"
    assert result["requested_decision"] == "redirect"
    assert result["applied_decision"] == "redirect"
    assert result["requested_native_action"] == "respond"
    assert result["applied_native_action"] == "respond"
    assert result["operator_steer_text"].startswith("Change of plan.")
    assert result["response_matches_pending"] is True
    assert result["authenticated_steer_seen"] is True
    assert result["card_count"] == 1
    assert result["paused_on_checkpoint"] is True
    assert result["resumed_checkpoint"] is True
    assert result["first_terminal_count"] == 0
    assert result["terminal_count"] == 1
    assert result["pre_decision_effect_calls"] == []
    assert result["effect_calls"] == []
    assert result["terminal_answer_text"].startswith("This offline case has not verified")
    assert result["elapsed_seconds"] >= 0
    assert result["token_usage"] == {"status": "unavailable", "reason": "scripted fake model"}


def test_redirect_near_match_is_rejected_without_steering_or_effect():
    code, result = run_cli("--decision", "redirect", "--proposed-command", "hostname")
    assert code == 1
    assert result["status"] == "unexpected_proposal"
    assert result["requested_decision"] == "redirect"
    assert result["applied_decision"] == "reject"
    assert result["requested_native_action"] == "respond"
    assert result["applied_native_action"] == "reject"
    assert result["operator_steer_text"] is None
    assert result["authenticated_steer_seen"] is False
    assert result["pre_decision_effect_calls"] == result["effect_calls"] == []


def test_redirect_guard_fails_when_current_steer_is_not_seen():
    spec = importlib.util.spec_from_file_location("offline_human_led_case", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with patch.object(module, "_current_assisted_operator_input", return_value=module.PROMPT), patch(
        "socket.socket.connect", side_effect=RuntimeError("offline case blocked network connect")
    ), patch(
        "socket.socket.connect_ex", side_effect=RuntimeError("offline case blocked network connect")
    ):
        result = asyncio.run(module.run_case(decision="redirect", proposed_command="whoami"))
    assert result["status"] == "failed"
    assert result["authenticated_steer_seen"] is False
    # The old task may be blocked by turn authority before it becomes a second card.
    # Either way, it must not be reported as a successful redirect.
    assert result["terminal_answer_text"] != module.REDIRECT_ANSWER
    assert result["effect_calls"] == []
