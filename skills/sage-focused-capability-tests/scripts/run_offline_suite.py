#!/usr/bin/env python3
"""Run the Sage offline test suite.

There is exactly one tier: no exclusions, no tier argument to get wrong, and a green run means the
tree is green.

Rejected successor-portfolio suites freeze hash contracts that describe older product bytes, so they
can never go green against current source and would make the unfiltered command permanently red.
They are rejected *evaluation evidence*, and `AGENTS.md` § Repository map and artifacts names
`.sage_history/` as the home for exactly that, so they live under
`.sage_history/evaluation/architecture-policy/rejected-successor-portfolios/`, preserved
append-only, out of the product tree.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_ROOT = REPO_ROOT / "Payload_Type" / "sage" / "tests"
# Repository hygiene (portability, privacy, build context) lives outside the Sage suite because it
# tests the repo rather than the product. It still runs here — a guard nobody executes is not a guard.
HYGIENE_ROOT = REPO_ROOT / "tests" / "repo_hygiene"
# The operator skills carry their own tests (reset, bootstrap, live-runner and governor tooling).
# Outside this tier they gate nothing, and the skills drift out from under the workflow while the
# product suite stays green. "One tier, no exclusions" is only true with this root included.
SKILLS_ROOT = REPO_ROOT / "skills"
DEV_REQUIREMENTS = REPO_ROOT / "Payload_Type" / "sage" / "requirements-dev.txt"


# Each xdist worker grows to about 1 GiB over the run. With Mythic and Sage up on a 15 GiB host, `-n auto` (one
# worker per core) swapped the machine into a lockup, so the default is the core count capped by free memory.
WORKER_MEMORY_BYTES = 3 * 2**29          # 1.5 GiB per worker
RESERVED_MEMORY_BYTES = 2 * 2**30        # left for everything else running on the host
MEMINFO = Path("/proc/meminfo")


def available_memory_bytes(meminfo: Path = MEMINFO) -> int | None:
    """The kernel's MemAvailable estimate, or None where it cannot be read (non-Linux hosts)."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def default_workers(available: int | None, cores: int | None) -> int:
    """Parallel workers: one per core, but no more than free memory can hold after the reserve."""
    cores = max(1, cores or 1)
    if available is None:
        return cores
    return max(1, min(cores, (available - RESERVED_MEMORY_BYTES) // WORKER_MEMORY_BYTES))


def _chooses_workers(pytest_args: list[str]) -> bool:
    return any(arg in ("-n", "--numprocesses") or arg.startswith(("-n", "--numprocesses=")) for arg in pytest_args)


def command_for(pytest_args: list[str], *, parallel: bool = False, workers: int | str = "auto") -> list[str]:
    """The pytest command; with `parallel` it runs on `workers` processes unless the caller picks `-n`."""
    args = list(pytest_args) or ["-q"]
    command = [sys.executable, "-m", "pytest", str(TEST_ROOT), str(HYGIENE_ROOT), str(SKILLS_ROOT)]
    if parallel and not _chooses_workers(args):
        command.extend(["-n", str(workers)])
    command.extend(args)
    return command


def pass_commands(pytest_args: list[str], *, parallel: bool = False, workers: int | str = "auto") -> list[list[str]]:
    """The run as one or two pytest commands.

    Parallel (the default when pytest-xdist is installed) is a parallel pass over everything not marked `serial`,
    then a serial pass over the timing-sensitive rest; both cover every test. Without `parallel` it is one
    single-process run of everything (`--serial`, or no xdist). A caller who picks `-n` or `-m` gets exactly one
    command with those choices."""
    args = list(pytest_args) or ["-q"]
    if not parallel or _chooses_workers(args) or "-m" in args:
        return [command_for(args, parallel=parallel, workers=workers)]
    return [command_for([*args, "-m", "not serial"], parallel=True, workers=workers),
            command_for([*args, "-m", "serial"], parallel=False)]


def pytest_missing_message() -> str | None:
    """Return actionable guidance if this interpreter has no pytest, else None.

    Without this the subprocess emits a bare `No module named pytest`, which gives no hint that
    test dependencies live in a manifest separate from the runtime one — a fresh venv built from
    requirements.txt alone reaches exactly that dead end.
    """
    if importlib.util.find_spec("pytest") is not None:
        return None
    return (
        f"pytest is not installed in {sys.executable}\n\n"
        "Test dependencies are declared separately from runtime ones, because the container image\n"
        "installs only requirements.txt. Install both with:\n\n"
        f"  {sys.executable} -m pip install -r {DEV_REQUIREMENTS}\n"
    )


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
    parser.add_argument("--serial", action="store_true",
                        help="Run everything in one process instead of the default parallel pass then serial pass.")
    parser.add_argument("--parallel", action="store_true",
                        help="Require the parallel run (the default); fail rather than fall back without pytest-xdist.")
    return parser


WRAPPER_OPTIONS = ("supported", "--print-command", "--parallel", "--serial")


def split_args(argv: list[str]) -> tuple[set[str], list[str]]:
    """(wrapper options, pytest_args). Wrapper options come first; everything after them goes to pytest
    unparsed, so pytest options such as `-q`, `-k` or `-n 0` pass through without a separator."""
    rest = list(argv)
    options: set[str] = set()
    while rest and rest[0] in WRAPPER_OPTIONS:
        options.add(rest.pop(0))
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return options, rest


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv in (["-h"], ["--help"]):
        build_parser().print_help()
        return 0
    options, pytest_args = split_args(argv)
    if {"--parallel", "--serial"} <= options:
        print("choose --parallel or --serial, not both", file=sys.stderr)
        return 2
    have_xdist = importlib.util.find_spec("xdist") is not None
    if "--parallel" in options and not have_xdist:
        print(f"--parallel needs pytest-xdist; install the dev requirements:\n"
              f"  {sys.executable} -m pip install -r {DEV_REQUIREMENTS}", file=sys.stderr)
        return 1
    parallel = "--serial" not in options and have_xdist
    if "--serial" not in options and not have_xdist:
        print("pytest-xdist is not installed; running every test in one process", file=sys.stderr)
    workers = default_workers(available_memory_bytes(), os.cpu_count())
    if parallel and not _chooses_workers(pytest_args):
        if workers < 2 and "--parallel" not in options:
            parallel = False
            print("not enough free memory for parallel workers; running every test in one process", file=sys.stderr)
        elif workers < (os.cpu_count() or 1):
            print(f"free memory caps the parallel pass at {workers} workers (pass -n to choose)", file=sys.stderr)
    commands = pass_commands(pytest_args, parallel=parallel, workers=workers)
    for command in commands:
        print(" ".join(command), flush=True)
    if "--print-command" in options:
        return 0
    missing = pytest_missing_message()
    if missing is not None:
        print(missing, file=sys.stderr)
        return 1
    worst = 0
    for index, command in enumerate(commands):
        code = subprocess.run(command, cwd=REPO_ROOT, check=False).returncode
        # Exit 5 means "no tests collected": expected for the serial pass when a filter selects none of them.
        if code == 5 and index > 0:
            code = 0
        worst = worst or code
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
