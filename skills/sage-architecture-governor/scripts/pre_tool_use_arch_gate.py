#!/usr/bin/env python3
"""PreToolUse hook for Sage architecture gate enforcement (Codex and Claude Code).

Two event dialects reach this hook and they carry paths differently. Codex `apply_patch` puts a patch
blob in `tool_input["command"]`; Claude Code `Edit`/`Write`/`MultiEdit` put the target in
`tool_input["file_path"]` and set no command at all. Reading only `command` made this hook inspect
ZERO paths under Claude Code while still exiting 0 — a guard that reports success having checked
nothing. Both dialects are covered by `_event_paths` and both are exercised by `--self-test`.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from arch_governor_common import (
    approval_status,
    frozen_path_conflicts,
    gate_deny_paths,
    high_risk_paths,
    parse_apply_patch_paths,
    repo_root,
    shell_write_paths,
)

# Keys through which a harness names an edit target directly, rather than inside a patch or a shell
# command. Claude Code's Edit/Write/MultiEdit use `file_path`; NotebookEdit uses `notebook_path`.
DIRECT_PATH_KEYS = ("file_path", "notebook_path", "path")


def _event_paths(event: dict) -> list[str]:
    tool_name = str(event.get("tool_name") or event.get("name") or "").strip()
    tool_input = event.get("tool_input")
    if tool_input is None:
        tool_input = event.get("input")
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        direct = [
            str(tool_input[key]).strip()
            for key in DIRECT_PATH_KEYS
            if isinstance(tool_input.get(key), str) and str(tool_input[key]).strip()
        ]
    else:
        command = str(tool_input or "")
        direct = []

    if direct:
        return direct
    if tool_name == "apply_patch" or command.startswith("*** Begin Patch"):
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
    conflicts, lease = frozen_path_conflicts(root, paths)
    if conflicts:
        return 0, _deny(
            "active Sage review lease "
            f"{lease.get('lease_id')} freezes candidate "
            f"{lease.get('candidate_id')}; write conflicts: {', '.join(conflicts)}. "
            "Close the exact lease before changing reviewed bytes."
        )
    high = high_risk_paths(paths, root)
    if not high:
        return 0, {}
    ok, reason = approval_status(root, high)
    if ok:
        return 0, _allow_context(reason)

    deny_paths = gate_deny_paths(high, root)
    if deny_paths:
        return 0, _deny(
            f"architecture gate: no approval token covers {', '.join(deny_paths)}"
            ". Prepare the Sage Architecture Gate brief, get explicit user approval, then run "
            "`python3 skills/sage-architecture-governor/scripts/open_gate.py open --files <scope> ...`."
        )

    # Advisory lane: high-risk but not deny-listed. Name the surface and ask the doctrine's real
    # question instead of charging a toll for every touch.
    return 0, _allow_context(
        f"architecture-sensitive edit: {', '.join(high)}. No approval token is open, and this "
        "surface is advisory rather than blocked. Before growing prompts, tool surfaces, "
        "GOAD-specific live code, or symbolic planning/gating logic here, compare a thinner "
        "verifier/retrieval/data-backed alternative. If this is the third tactical patch to this "
        "subsystem, do the RCA before writing more code."
    )


def _patch_event(path: str) -> dict:
    return {
        "tool_name": "apply_patch",
        "tool_input": {
            "command": f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+new\n*** End Patch\n"
        },
    }


def _edit_event(path: str) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"},
    }


def _verdict(payload: dict) -> str:
    """Collapse a hook payload to deny / advise / silent."""

    specific = payload.get("hookSpecificOutput") or {}
    if specific.get("permissionDecision") == "deny":
        return "deny"
    if specific.get("additionalContext"):
        return "advise"
    return "silent"


DENY_TARGET = "Payload_Type/sage/prompts/supervisor.md"
ADVISE_TARGET = "Payload_Type/sage/ai/langgraph/model.py"
SAFE_TARGET = "README.md"


def self_test() -> int:
    """Green -> red -> green control across BOTH event dialects.

    Runs against a temp token/lease directory so a live approval token on this machine cannot
    silently turn a red case green. A gate whose own test can pass while a violation sails through
    is worse than no gate.
    """

    import tempfile

    import arch_governor_common as common

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        common.TOKEN_DIR = Path(tmp) / "token"
        os.environ["SAGE_ARCH_REVIEW_DIR"] = str(Path(tmp) / "review")

        # Path extraction must work in both dialects before any verdict is meaningful.
        if ADVISE_TARGET not in _event_paths(_patch_event(ADVISE_TARGET)):
            failures.append("apply_patch dialect: patch path not extracted")
        if _event_paths(_edit_event(ADVISE_TARGET)) != [ADVISE_TARGET]:
            failures.append("Claude dialect: file_path not extracted")

        cases = [
            ("apply_patch", _patch_event, DENY_TARGET, "deny"),
            ("apply_patch", _patch_event, ADVISE_TARGET, "advise"),
            ("apply_patch", _patch_event, SAFE_TARGET, "silent"),
            ("Edit", _edit_event, DENY_TARGET, "deny"),
            ("Edit", _edit_event, ADVISE_TARGET, "advise"),
            ("Edit", _edit_event, SAFE_TARGET, "silent"),
        ]
        for dialect, build, target, expected in cases:
            _code, payload = run_hook(build(target))
            got = _verdict(payload)
            if got != expected:
                failures.append(f"{dialect} on {target}: expected {expected}, got {got}")

        # The shell lane must still see a redirect into a deny-listed path.
        _code, payload = run_hook(
            {"tool_name": "Bash", "tool_input": {"command": f"echo x > {DENY_TARGET}"}}
        )
        if _verdict(payload) != "deny":
            failures.append("Bash redirect into a deny-listed path was not denied")

    if failures:
        for line in failures:
            print(f"self-test failed: {line}", file=sys.stderr)
        return 1
    print(f"pre_tool_use_arch_gate self-test passed ({len(cases)} cases, both dialects)")
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
