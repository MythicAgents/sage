#!/usr/bin/env python3
"""Codex PreToolUse hook for Sage architecture gate enforcement."""

from __future__ import annotations

import argparse
import json
import sys

from arch_governor_common import (
    approval_status,
    high_risk_paths,
    parse_apply_patch_paths,
    repo_root,
    shell_write_paths,
)


def _event_paths(event: dict) -> list[str]:
    tool_name = str(event.get("tool_name") or event.get("name") or "").strip()
    tool_input = event.get("tool_input")
    if tool_input is None:
        tool_input = event.get("input")
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
    else:
        command = str(tool_input or "")

    if tool_name in {"apply_patch", "Edit", "Write"} or command.startswith("*** Begin Patch"):
        return parse_apply_patch_paths(command)
    if tool_name == "Bash" or command:
        return shell_write_paths(command)
    return []


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allow_context(message: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": message,
        }
    }


def run_hook(event: dict) -> tuple[int, dict]:
    root = repo_root()
    paths = _event_paths(event)
    high = high_risk_paths(paths, root)
    if not high:
        return 0, {}
    ok, reason = approval_status(root, high)
    if ok:
        return 0, _allow_context(reason)
    return 0, _deny(
        reason
        + ". Prepare the Sage Architecture Gate brief, get explicit user approval, then run "
        "`python3 skills/sage-architecture-governor/scripts/open_gate.py open --files <scope> ...`."
    )


def self_test() -> int:
    patch = """*** Begin Patch
*** Update File: Payload_Type/sage/ai/langgraph/model.py
@@
-old
+new
*** End Patch
"""
    event = {"tool_name": "apply_patch", "tool_input": {"command": patch}}
    paths = _event_paths(event)
    if "Payload_Type/sage/ai/langgraph/model.py" not in paths:
        print("self-test failed: patch path not extracted", file=sys.stderr)
        return 1
    code, payload = run_hook(event)
    if code != 0:
        print("self-test failed: hook returned non-zero", file=sys.stderr)
        return 1
    if "permissionDecision" not in json.dumps(payload):
        print("self-test failed: missing denial for unapproved high-risk edit", file=sys.stderr)
        return 1
    safe = {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Update File: README.md\n*** End Patch\n"}}
    _code, safe_payload = run_hook(safe)
    if safe_payload:
        print("self-test failed: safe edit should not emit hook payload", file=sys.stderr)
        return 1
    print("pre_tool_use_arch_gate self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    _code, payload = run_hook(event if isinstance(event, dict) else {})
    if payload:
        print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
