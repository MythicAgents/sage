"""`sage_restart.sh` must refuse OUT LOUD when SAGE_BLOODHOUND_MCP_DIR is unresolvable.

The launcher carries a three-line error naming the files to set and the flag to pass. Under
`set -euo pipefail` two independent routes can make that message unreachable in the exact case it
is written for:

* a `grep | tail | cut` pipeline fails when `grep` matches nothing, so the assignment fails and
  the shell exits;
* a trailing `[[ -n "$configured" ]] && append_snapshot_override ...` returns 1 when the test is
  false, which `set -e` also treats as fatal.

Either way the operator sees `exit 1` and nothing else, on the one path where the script knows the
answer and has written it down.

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



#: Variables the fresh-start tests assert on. Removed from the inherited environment so a value the
#: person running pytest happens to export cannot decide the outcome.
_ISOLATED_KEYS = (
    "SAGE_BLOODHOUND_MCP_DIR", "DEBUG_LEVEL", "MYTHIC_SERVER_HOST", "RABBITMQ_HOST",
    "SAGE_FINDINGS_SLACK_WEBHOOK_URL", "SAGE_RESOURCE_EPISODE_ID", "SAGE_TEST_ONLY_SETTING", "model",
)


def _run_fresh_start(
    tmp_path: Path, *args: str, env_files: dict[str, str], caller_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, bytes]:
    """Run the REAL launcher down its fresh-start branch and return the snapshot it built.

    Only process control is faked (tmux, pgrep, the relaunch interpreter). Env-file reading, the
    defaults, the BloodHound guard and the KEY=VAL overrides run as written. `SAGE_ENV_SNAPSHOT` keeps
    the snapshot inside tmp_path instead of the shared /tmp path a live launcher uses.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pgrep_count = tmp_path / "pgrep-count"
    sage_dir = tmp_path / "sage"
    sage_dir.mkdir()
    for name, content in env_files.items():
        (sage_dir / name).write_text(content, encoding="utf-8")
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in\n"
        "  has-session|new-session|send-keys) exit 0 ;;\n"
        "  list-panes) printf '999999\\n'; exit 0 ;;\n"
        "esac\n"
        "exit 2\n",
        encoding="utf-8",
    )
    (fake_bin / "pgrep").write_text(
        "#!/usr/bin/env bash\n"
        "count=0\n"
        "[[ -f \"$FAKE_PGREP_COUNT\" ]] && count=$(cat \"$FAKE_PGREP_COUNT\")\n"
        "count=$((count + 1))\n"
        "printf '%s' \"$count\" > \"$FAKE_PGREP_COUNT\"\n"
        "if (( count == 1 )); then exit 1; fi\n"
        "printf '%s\\n' \"$FAKE_MAIN_PID\"\n",
        encoding="utf-8",
    )
    (fake_bin / "python").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for tool in ("tmux", "pgrep", "python"):
        (fake_bin / tool).chmod(0o755)

    snapshot = tmp_path / "snapshot"
    env = {key: value for key, value in os.environ.items() if key not in _ISOLATED_KEYS}
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "FAKE_PGREP_COUNT": str(pgrep_count),
        "FAKE_MAIN_PID": str(os.getpid()),
        "SAGE_CWD": str(sage_dir),
        "SAGE_VENV_PY": str(fake_bin / "python"),
        "SAGE_RUNTIME_ENV_PATH": str(sage_dir / ".env"),
        "SAGE_ENV_SNAPSHOT": str(snapshot),
        # Never source the operator's real Mythic install into a test snapshot.
        "MYTHIC_ENV_PATH": str(tmp_path / "no-mythic.env"),
        "RABBITMQ_PASSWORD": "test-only",
        "SAGE_STARTUP_DISCOVERY_SECONDS": "1",
        "SAGE_STARTUP_STABILITY_SECONDS": "1",
    })
    env.update(caller_env or {})
    result = subprocess.run(
        ["bash", str(RESTART_PATH), *args], capture_output=True, text=True, timeout=30, env=env,
    )
    return result, snapshot.read_bytes() if snapshot.exists() else b""


def _last_value(snapshot: bytes, key: str) -> str | None:
    value = None
    for entry in snapshot.split(b"\0"):
        name, sep, item = entry.partition(b"=")
        if sep and name.decode() == key:
            value = item.decode()
    return value


def test_empty_override_does_not_clear_the_required_bloodhound_dir(tmp_path: Path) -> None:
    """2026-10-31: `SAGE_BLOODHOUND_MCP_DIR="$SAGE_BLOODHOUND_MCP_DIR"` with the variable unexported
    appended an empty value that won over the configured one. The guard now runs after overrides and
    falls back to the env files."""
    result, snapshot = _run_fresh_start(
        tmp_path,
        "SAGE_BLOODHOUND_MCP_DIR=",
        env_files={".env.local": "SAGE_BLOODHOUND_MCP_DIR=/opt/from-local\n"},
    )
    assert result.returncode == 0, result.stderr
    assert _last_value(snapshot, "SAGE_BLOODHOUND_MCP_DIR") == "/opt/from-local"


def test_bloodhound_dir_override_satisfies_the_guard(tmp_path: Path) -> None:
    """The guard's own advice ("or pass SAGE_BLOODHOUND_MCP_DIR=<dir> to this script") must work."""
    result, snapshot = _run_fresh_start(
        tmp_path, "SAGE_BLOODHOUND_MCP_DIR=/opt/from-override", env_files={".env": "# nothing\n"},
    )
    assert result.returncode == 0, result.stderr
    assert _last_value(snapshot, "SAGE_BLOODHOUND_MCP_DIR") == "/opt/from-override"


def test_empty_override_still_clears_an_optional_setting(tmp_path: Path) -> None:
    """The eval gauge's service probe passes `KEY=` to switch off the Slack hook and drop a stale
    episode id (capture_service_probe.py); an empty override must keep meaning "clear"."""
    result, snapshot = _run_fresh_start(
        tmp_path,
        "SAGE_FINDINGS_SLACK_WEBHOOK_URL=",
        "SAGE_RESOURCE_EPISODE_ID=",
        env_files={
            ".env.local": "SAGE_BLOODHOUND_MCP_DIR=/opt/from-local\n"
                          "SAGE_FINDINGS_SLACK_WEBHOOK_URL=https://hooks.example.invalid/x\n",
        },
        caller_env={"SAGE_RESOURCE_EPISODE_ID": "stale-episode"},
    )
    assert result.returncode == 0, result.stderr
    assert _last_value(snapshot, "SAGE_FINDINGS_SLACK_WEBHOOK_URL") == ""
    assert _last_value(snapshot, "SAGE_RESOURCE_EPISODE_ID") == ""
    assert "SAGE_RESOURCE_EPISODE_ID=<empty>" in result.stderr


def test_fresh_start_prefers_env_local_over_the_tracked_env(tmp_path: Path) -> None:
    """The runtime's order is `.env.local` then `.env`. The old `source "$SAGE_RUNTIME_ENV_PATH"`
    made `.env` the "existing" value, which `.env.local` can never override."""
    result, snapshot = _run_fresh_start(
        tmp_path,
        env_files={
            ".env": "SAGE_BLOODHOUND_MCP_DIR=/opt/from-env\n",
            ".env.local": "SAGE_BLOODHOUND_MCP_DIR=/opt/from-local\n",
        },
    )
    assert result.returncode == 0, result.stderr
    assert _last_value(snapshot, "SAGE_BLOODHOUND_MCP_DIR") == "/opt/from-local"


def test_fresh_start_does_not_freeze_runtime_env_files_into_the_process(tmp_path: Path) -> None:
    """Sage loads its env files itself. Anything the launcher exported would become process
    environment, which outranks the files on every later restart of the running Sage, so an edit to
    `.env.local` (a model change, say) would silently stop applying."""
    result, snapshot = _run_fresh_start(
        tmp_path,
        env_files={
            ".env": "SAGE_TEST_ONLY_SETTING=from-env\n",
            ".env.local": "SAGE_BLOODHOUND_MCP_DIR=/opt/from-local\nmodel=old-model\n",
        },
    )
    assert result.returncode == 0, result.stderr
    assert _last_value(snapshot, "model") is None
    assert _last_value(snapshot, "SAGE_TEST_ONLY_SETTING") is None


def test_fresh_start_keeps_the_callers_value(tmp_path: Path) -> None:
    result, snapshot = _run_fresh_start(
        tmp_path,
        env_files={
            ".env": "SAGE_BLOODHOUND_MCP_DIR=/opt/from-env\n",
            ".env.local": "SAGE_BLOODHOUND_MCP_DIR=/opt/from-local\n",
        },
        caller_env={"SAGE_BLOODHOUND_MCP_DIR": "/opt/from-caller"},
    )
    assert result.returncode == 0, result.stderr
    assert _last_value(snapshot, "SAGE_BLOODHOUND_MCP_DIR") == "/opt/from-caller"


def test_launcher_defaults_apply_only_when_no_source_sets_them(tmp_path: Path) -> None:
    unset_dir, configured_dir = tmp_path / "unset", tmp_path / "configured"
    unset_dir.mkdir()
    configured_dir.mkdir()

    unset, unset_snapshot = _run_fresh_start(
        unset_dir, env_files={".env": "DEBUG_LEVEL=\n", ".env.local": "SAGE_BLOODHOUND_MCP_DIR=/opt/x\n"},
    )
    assert unset.returncode == 0, unset.stderr
    assert _last_value(unset_snapshot, "DEBUG_LEVEL") == "debug", "an empty file value leaves the default in place"

    configured, configured_snapshot = _run_fresh_start(
        configured_dir, env_files={".env.local": "SAGE_BLOODHOUND_MCP_DIR=/opt/x\nDEBUG_LEVEL=info\n"},
    )
    assert configured.returncode == 0, configured.stderr
    assert _last_value(configured_snapshot, "DEBUG_LEVEL") is None, "Sage applies `.env.local`'s info itself"


def test_override_value_ending_in_equals_is_kept_intact(tmp_path: Path) -> None:
    """Base64 tokens end in `=`; only a truly empty value is "empty"."""
    result, snapshot = _run_fresh_start(
        tmp_path, "SAGE_ENGAGEMENT_ID=dG9rZW4=", env_files={".env.local": "SAGE_BLOODHOUND_MCP_DIR=/opt/x\n"},
    )
    assert result.returncode == 0, result.stderr
    assert _last_value(snapshot, "SAGE_ENGAGEMENT_ID") == "dG9rZW4="
