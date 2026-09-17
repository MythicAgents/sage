"""`sage_restart.sh` must refuse OUT LOUD when SAGE_BLOODHOUND_MCP_DIR is unresolvable.

The launcher carries a three-line error naming the files to set and the flag to pass. Under
`set -euo pipefail` that message was unreachable in the exact case it was written for, by two
independent routes:

* the `grep | tail | cut` pipeline fails when `grep` matches nothing, so the assignment fails and
  the shell exits;
* a trailing `[[ -n "$configured" ]] && append_snapshot_override ...` returns 1 when the test is
  false, which `set -e` also treats as fatal.

Either way the operator saw `exit 1` and nothing else, on the one path where the script knew the
answer and had written it down. Found 2026-08-11 while bringing local Sage up; the same
diagnostic-never-reaches-the-human shape the BloodHound connect work is about.

The guard runs the REAL function text extracted from the real script, so it fails when someone
reintroduces a fragile pipeline or drops a `|| true` — not merely when someone deletes a line. It
cannot be satisfied by source-string matching, because what is asserted is the exit status and the
stderr of an actual bash run.

Also pins the search order to Sage's own loader: `dotenv_bootstrap.DOTENV_FILENAMES` is
`(".env.local", ".env")`, and a launcher that reads only `.env` refuses to start a Sage whose
variable lives in the file the runtime prefers, then tells the operator to edit the other one.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RESTART_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_restart.sh"

#: Functions the guard needs. Extracted rather than reimplemented so drift cannot hide here.
REQUIRED_FUNCTIONS = ("snapshot_last_value", "append_snapshot_override", "require_bloodhound_dir")


def _extract_functions(source: str) -> str:
    """Pull the named top-level functions out of the launcher, verbatim.

    Each is matched from `name() {` to the first column-zero `}`, which is how this file writes
    them. A function that stops matching that shape fails the floor assertion below rather than
    silently contributing nothing.
    """
    chunks = []
    for name in REQUIRED_FUNCTIONS:
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", source, re.MULTILINE | re.DOTALL)
        assert match, f"{name}() not found in {RESTART_PATH}; the guard is inspecting nothing"
        chunks.append(match.group(0))
    return "\n\n".join(chunks)


@pytest.fixture(scope="module")
def functions_under_test() -> str:
    source = RESTART_PATH.read_text(encoding="utf-8")
    extracted = _extract_functions(source)
    # Floor assertion: a guard that reads an empty or near-empty body and reports success is worse
    # than no guard, because it converts silence into confidence.
    assert len(extracted.splitlines()) > 20, "extracted far too little of the launcher to be testing it"
    assert "set -euo pipefail" in source, "the launcher no longer runs under the mode this guard exists for"
    return extracted


def _run_guard(tmp_path: Path, functions: str, *, snapshot: bytes, env_files: dict[str, str]):
    """Execute `require_bloodhound_dir` under the launcher's own shell mode.

    `set -euo pipefail` is re-declared here deliberately: without it this test would pass against
    the very code it exists to reject.
    """
    sage_dir = tmp_path / "sage"
    sage_dir.mkdir(exist_ok=True)
    for name, content in env_files.items():
        (sage_dir / name).write_text(content, encoding="utf-8")

    snapshot_path = tmp_path / "snapshot"
    snapshot_path.write_bytes(snapshot)

    script = tmp_path / "guard.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'SNAP="{snapshot_path}"\n'
        f'SAGE_RUNTIME_ENV_PATH="{sage_dir / ".env"}"\n'
        f"{functions}\n"
        "require_bloodhound_dir\n"
        'echo "GUARD_PASSED"\n',
        encoding="utf-8",
    )
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30)


def test_missing_everywhere_fails_loudly(tmp_path: Path, functions_under_test: str) -> None:
    """The regression itself: refusing is correct, refusing silently is the defect."""
    result = _run_guard(
        tmp_path,
        functions_under_test,
        snapshot=b"PATH=/usr/bin\0",
        env_files={".env": "# nothing set here\n"},
    )
    assert result.returncode == 1
    assert "SAGE_BLOODHOUND_MCP_DIR is not set" in result.stderr
    assert ".env.local" in result.stderr, "the message must name both files it searched"
    assert "GUARD_PASSED" not in result.stdout


def test_env_local_is_searched(tmp_path: Path, functions_under_test: str) -> None:
    """`.env.local` is where the runtime looks first, so the launcher must look there too."""
    result = _run_guard(
        tmp_path,
        functions_under_test,
        snapshot=b"PATH=/usr/bin\0",
        env_files={".env": "# empty\n", ".env.local": "SAGE_BLOODHOUND_MCP_DIR=/opt/from-local\n"},
    )
    assert result.returncode == 0, result.stderr
    assert "GUARD_PASSED" in result.stdout
    assert "SAGE_BLOODHOUND_MCP_DIR=<set>" in result.stderr


def test_runtime_env_is_searched(tmp_path: Path, functions_under_test: str) -> None:
    """The tracked `.env` still works, quoted values included.

    Each case gets a FRESH snapshot on purpose: `append_snapshot_override` writes into it, so a
    shared one lets an earlier case satisfy a later one and the later case proves nothing.
    """
    result = _run_guard(
        tmp_path,
        functions_under_test,
        snapshot=b"PATH=/usr/bin\0",
        env_files={".env": 'SAGE_BLOODHOUND_MCP_DIR="/opt/from-env"\n'},
    )
    assert result.returncode == 0, result.stderr
    assert "GUARD_PASSED" in result.stdout
    snapshot = (tmp_path / "snapshot").read_bytes()
    assert b"SAGE_BLOODHOUND_MCP_DIR=/opt/from-env\0" in snapshot, "quotes must be stripped"


def test_existing_snapshot_wins_without_touching_files(tmp_path: Path, functions_under_test: str) -> None:
    """A running Sage's own environment is authoritative; no file needs to exist."""
    result = _run_guard(
        tmp_path,
        functions_under_test,
        snapshot=b"PATH=/usr/bin\0SAGE_BLOODHOUND_MCP_DIR=/opt/from-snapshot\0",
        env_files={},
    )
    assert result.returncode == 0, result.stderr
    assert "GUARD_PASSED" in result.stdout
    assert "SAGE_BLOODHOUND_MCP_DIR=<set>" not in result.stderr, "nothing to override when already set"


def test_fresh_start_creates_missing_tmux_session(tmp_path: Path) -> None:
    """The real launcher must make its documented fresh-start branch reachable.

    Fake only the process-control boundary.  The real shell script still performs its session
    detection, snapshot construction, launch dispatch, PID discovery, and stability check.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "tmux-calls"
    pgrep_count = tmp_path / "pgrep-count"
    sage_cwd = tmp_path / "sage-cwd"
    sage_cwd.mkdir()

    tmux = fake_bin / "tmux"
    tmux.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_TMUX_CALLS\"\n"
        "case \"${1:-}\" in\n"
        "  has-session) exit 1 ;;\n"
        "  new-session|send-keys) exit 0 ;;\n"
        "  list-panes) printf '999999\\n'; exit 0 ;;\n"
        "esac\n"
        "exit 2\n",
        encoding="utf-8",
    )
    tmux.chmod(0o755)

    pgrep = fake_bin / "pgrep"
    pgrep.write_text(
        "#!/usr/bin/env bash\n"
        "count=0\n"
        "[[ -f \"$FAKE_PGREP_COUNT\" ]] && count=$(cat \"$FAKE_PGREP_COUNT\")\n"
        "count=$((count + 1))\n"
        "printf '%s' \"$count\" > \"$FAKE_PGREP_COUNT\"\n"
        "if (( count == 1 )); then exit 1; fi\n"
        "printf '%s\\n' \"$FAKE_MAIN_PID\"\n",
        encoding="utf-8",
    )
    pgrep.chmod(0o755)

    python = fake_bin / "python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "FAKE_TMUX_CALLS": str(calls),
        "FAKE_PGREP_COUNT": str(pgrep_count),
        "FAKE_MAIN_PID": str(os.getpid()),
        "SAGE_CWD": str(sage_cwd),
        "SAGE_VENV_PY": str(python),
        "SAGE_BLOODHOUND_MCP_DIR": str(tmp_path / "bloodhound-mcp"),
        "RABBITMQ_PASSWORD": "test-only",
        "SAGE_STARTUP_DISCOVERY_SECONDS": "1",
        "SAGE_STARTUP_STABILITY_SECONDS": "1",
    })
    result = subprocess.run(
        ["bash", str(RESTART_PATH)],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "created tmux session 'sage'" in result.stderr
    assert "new-session -d -s sage -c" in calls.read_text(encoding="utf-8")
    assert "Sage relaunch verified" in result.stderr
