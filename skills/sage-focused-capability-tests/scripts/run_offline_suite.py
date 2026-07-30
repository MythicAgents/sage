#!/usr/bin/env python3
"""Run the Sage offline test suite.

There is exactly one tier. There used to be two: `supported` ran the tree minus four rejected
successor-portfolio suites, and `retired` ran just those four. That split existed because the
rejected portfolios' frozen hash contracts describe older product bytes, so they can never go green
against current source and would have made the unfiltered command permanently red.

Those portfolios are rejected *evaluation evidence*, and `AGENTS.md` § Durable Artifact Retention
names `.sage_history/` as the home for exactly that. They now live under
`.sage_history/evaluation/architecture-policy/rejected-successor-portfolios/`, preserved append-only
as the doctrine requires, but out of the product tree — where 28k lines of rejected candidates were
five times the weight of the working instruments they were candidates for.

With them relocated, the plain command is honest again: no exclusions, no tier argument to get
wrong, and a green run means the tree is green.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_ROOT = REPO_ROOT / "Payload_Type" / "sage" / "tests"
# Repository hygiene (portability, privacy, build context) lives outside the Sage suite because it
# tests the repo rather than the product. It still runs here — a guard nobody executes is not a guard.
HYGIENE_ROOT = REPO_ROOT / "tests" / "repo_hygiene"


def command_for(pytest_args: list[str]) -> list[str]:
    command = [sys.executable, "-m", "pytest", str(TEST_ROOT), str(HYGIENE_ROOT)]
    command.extend(pytest_args or ["-q"])
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # Accepted and ignored so muscle memory and older handoff docs that say `… supported` keep
    # working rather than dying on an argparse error the reader has to go decode.
    parser.add_argument(
        "tier",
        nargs="?",
        choices=("supported",),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    parser.add_argument("--print-command", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = command_for(args.pytest_args)
    print(" ".join(command), flush=True)
    if args.print_command:
        return 0
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
