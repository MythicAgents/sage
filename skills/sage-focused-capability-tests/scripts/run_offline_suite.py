#!/usr/bin/env python3
"""Run explicit Sage offline test lifecycles without hiding historical exclusions."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_ROOT = REPO_ROOT / "Payload_Type" / "sage" / "tests"

# Append-only rejected successor portfolios. Their frozen hashes intentionally describe an older product
# surface, so they are reviewed as historical lifecycle artifacts instead of being rewritten for green CI.
RETIRED_SUITES = (
    "test_phase16r_phase17r1_successor_portfolio.py",
    "test_phase16r_phase17r1_successor_r2_portfolio.py",
    "test_phase16r_phase17r1_successor_r3_portfolio.py",
    "test_phase16r_phase17r1_successor_r4_portfolio.py",
)


def command_for(tier: str, pytest_args: list[str]) -> list[str]:
    command = [sys.executable, "-m", "pytest"]
    if tier == "supported":
        command.append(str(TEST_ROOT))
        command.extend(f"--ignore={TEST_ROOT / name}" for name in RETIRED_SUITES)
    elif tier == "retired":
        missing = [name for name in RETIRED_SUITES if not (TEST_ROOT / name).exists()]
        if missing:
            raise FileNotFoundError("retired suite files are absent: " + ", ".join(missing))
        command.extend(str(TEST_ROOT / name) for name in RETIRED_SUITES)
    else:  # argparse prevents this; keep the pure helper fail-closed.
        raise ValueError(f"unknown test tier: {tier}")
    command.extend(pytest_args or ["-q"])
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", choices=("supported", "retired"))
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    parser.add_argument("--print-command", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        command = command_for(args.tier, args.pytest_args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(" ".join(command), flush=True)
    if args.print_command:
        return 0
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
