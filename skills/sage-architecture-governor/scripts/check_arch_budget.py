#!/usr/bin/env python3
"""Budget checks for high-risk Sage architecture changes."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from arch_governor_common import (
    GOAD_LITERALS,
    added_lines_from_diff,
    approval_status,
    high_risk_paths,
    is_goad_allowed,
    repo_root,
)


def _git(root: Path, args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=str(root), text=True, stderr=subprocess.DEVNULL)


def changed_paths(root: Path) -> list[str]:
    try:
        out = _git(root, ["diff", "--name-only"])
    except Exception:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def diff_text(root: Path, paths: list[str]) -> str:
    if not paths:
        return ""
    try:
        return _git(root, ["diff", "--unified=0", "--", *paths])
    except Exception:
        return ""


def count_prompt_tools(prompt_path: Path) -> int:
    try:
        lines = prompt_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    if not lines or lines[0].strip() != "---":
        return 0
    in_tools = False
    count = 0
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.strip() == "tools:":
            in_tools = True
            continue
        if in_tools:
            if re.match(r"^\s+-\s+\S+", line):
                count += 1
            elif line and not line.startswith(" "):
                in_tools = False
    return count


def run_checks(args) -> tuple[bool, list[str]]:
    root = repo_root()
    paths = args.paths or (changed_paths(root) if args.changed else [])
    high = high_risk_paths(paths, root)
    errors: list[str] = []

    if high and args.require_token:
        ok, reason = approval_status(root, high)
        if not ok:
            errors.append(reason)

    prompt = root / "Payload_Type/sage/prompts/mythic_operator.md"
    if prompt.exists():
        line_count = len(prompt.read_text(encoding="utf-8").splitlines())
        if line_count > args.max_mythic_operator_lines:
            errors.append(
                f"mythic_operator.md has {line_count} lines; budget is {args.max_mythic_operator_lines}"
            )
        tool_count = count_prompt_tools(prompt)
        if tool_count > args.max_mythic_operator_tools:
            errors.append(
                f"mythic_operator.md exposes {tool_count} tools; budget is {args.max_mythic_operator_tools}"
            )

    additions = added_lines_from_diff(diff_text(root, high))
    errors.extend(addition_budget_errors(additions))

    return not errors, errors


def addition_budget_errors(additions: list[tuple[str, str]]) -> list[str]:
    errors: list[str] = []
    for path, line in additions:
        if not path or is_goad_allowed(path):
            continue
        lowered = line.casefold()
        for literal in GOAD_LITERALS:
            if literal.casefold() in lowered:
                errors.append(f"new GOAD literal in live high-risk file {path}: {literal}")
                break
        if re.search(r"\b20\d\d-\d\d-\d\d\b", line):
            errors.append(f"new dated run-specific comment in high-risk file {path}")
    return errors


def self_test() -> int:
    sample = """diff --git a/Payload_Type/sage/ai/langgraph/model.py b/Payload_Type/sage/ai/langgraph/model.py
+++ b/Payload_Type/sage/ai/langgraph/model.py
+# 2026-06-17 CASTELBLACK special case
"""
    additions = added_lines_from_diff(sample)
    if additions != [("Payload_Type/sage/ai/langgraph/model.py", "# 2026-06-17 CASTELBLACK special case")]:
        print("self-test failed: added line parsing", file=sys.stderr)
        return 1
    errors = addition_budget_errors(additions)
    if not any("GOAD literal" in error for error in errors):
        print("self-test failed: GOAD literal not detected", file=sys.stderr)
        return 1
    if not any("dated run-specific" in error for error in errors):
        print("self-test failed: dated comment not detected", file=sys.stderr)
        return 1
    print("check_arch_budget self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--changed", action="store_true", help="Check tracked git diff paths.")
    parser.add_argument("--paths", action="append", default=[], help="Specific repo-relative path to check.")
    parser.add_argument("--require-token", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="Report architecture budget violations without returning a failing exit code.",
    )
    parser.add_argument("--max-mythic-operator-lines", type=int, default=430)
    parser.add_argument("--max-mythic-operator-tools", type=int, default=24)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    ok, errors = run_checks(args)
    if not ok:
        prefix = "ARCHITECTURE BUDGET WARNING" if args.warn_only else "ARCHITECTURE BUDGET"
        for error in errors:
            print(f"{prefix}: {error}", file=sys.stderr)
        return 0 if args.warn_only else 1
    if not args.quiet:
        print("architecture budget checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
