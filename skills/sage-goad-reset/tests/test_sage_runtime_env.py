"""`sage_runtime_env.py` resolves a Sage runtime setting the way the running Sage does.

Order: the process environment, then `Payload_Type/sage/.env.local`, then `Payload_Type/sage/.env`.
An empty value sets nothing, wherever it appears. Other operators keep the setting in the tracked
`.env`; a developer keeps it in the gitignored `.env.local`; both must resolve without the tool
knowing which file holds it. An unresolved setting is a refusal naming the variable, never a guess
at a sibling checkout (AGENTS.md: external dependency paths are configured, not guessed).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_runtime_env.py"
SPEC = importlib.util.spec_from_file_location("sage_runtime_env_under_test", SCRIPT)
runtime_env = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runtime_env)

NAME = "SAGE_BLOODHOUND_MCP_DIR"


def _runtime_dir(tmp_path: Path, **files: str) -> Path:
    directory = tmp_path / "sage"
    directory.mkdir(exist_ok=True)
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")
    return directory


def test_process_environment_wins_over_both_files(tmp_path):
    directory = _runtime_dir(
        tmp_path, **{".env.local": f"{NAME}=/from-local\n", ".env": f"{NAME}=/from-env\n"}
    )
    assert runtime_env.resolve(NAME, environ={NAME: "/from-process"}, runtime_dir=directory) == "/from-process"


def test_env_local_wins_over_the_tracked_env(tmp_path):
    directory = _runtime_dir(
        tmp_path, **{".env.local": f"{NAME}=/from-local\n", ".env": f"{NAME}=/from-env\n"}
    )
    assert runtime_env.resolve(NAME, environ={}, runtime_dir=directory) == "/from-local"


def test_tracked_env_serves_an_operator_without_env_local(tmp_path):
    directory = _runtime_dir(tmp_path, **{".env": f'{NAME}="/from-env"\n'})
    assert runtime_env.resolve(NAME, environ={}, runtime_dir=directory) == "/from-env"


def test_empty_values_set_nothing_anywhere(tmp_path):
    # The 2026-10-31 failure: `SAGE_BLOODHOUND_MCP_DIR="$SAGE_BLOODHOUND_MCP_DIR"` passed an empty
    # string that replaced the configured directory.
    directory = _runtime_dir(
        tmp_path, **{".env.local": f"{NAME}=\n", ".env": f"{NAME}=/from-env\n"}
    )
    assert runtime_env.resolve(NAME, environ={NAME: ""}, runtime_dir=directory) == "/from-env"


def test_unresolved_is_none_even_when_a_sibling_checkout_exists(tmp_path):
    (tmp_path / "bloodhound_mcp").mkdir()
    directory = _runtime_dir(tmp_path, **{".env": f"# {NAME}=\n"})
    assert runtime_env.resolve(NAME, environ={}, runtime_dir=directory) is None


def test_resolver_does_not_change_the_callers_environment(tmp_path):
    directory = _runtime_dir(tmp_path, **{".env.local": f"{NAME}=/from-local\n"})
    environ = {"UNRELATED": "1"}
    runtime_env.resolve(NAME, environ=environ, runtime_dir=directory)
    assert environ == {"UNRELATED": "1"}


def _cli(tmp_path: Path, *args: str, env_extra: dict[str, str] | None = None):
    env = {k: v for k, v in os.environ.items() if k != NAME}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(tmp_path),
    )


def test_get_prints_the_resolved_value(tmp_path):
    directory = _runtime_dir(tmp_path, **{".env.local": f"{NAME}=/from-local\n"})
    result = _cli(tmp_path, "--runtime-dir", str(directory), "get", NAME)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/from-local"


def test_get_refuses_naming_the_variable_and_the_files(tmp_path):
    directory = _runtime_dir(tmp_path)
    result = _cli(tmp_path, "--runtime-dir", str(directory), "get", NAME)
    assert result.returncode == 2
    assert result.stdout == ""
    assert NAME in result.stderr
    assert str(directory / ".env.local") in result.stderr
    assert str(directory / ".env") in result.stderr


def _fake_uv(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@"\nprintf "ENV=%s\\n" "$SAGE_BLOODHOUND_MCP_DIR"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return bin_dir


def test_mcp_run_executes_the_script_inside_the_resolved_mcp_checkout(tmp_path):
    mcp_dir = tmp_path / "mcp"
    mcp_dir.mkdir()
    directory = _runtime_dir(tmp_path, **{".env.local": f"{NAME}={mcp_dir}\n"})
    bin_dir = _fake_uv(tmp_path)
    result = _cli(
        tmp_path, "--runtime-dir", str(directory), "mcp-run", "scripts/bh_reset.py", "wipe", "--yes",
        env_extra={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    # `uv --directory` changes the working directory, so a relative script must arrive absolute.
    assert result.stdout.splitlines() == [
        "--directory", str(mcp_dir), "run", "python", str(tmp_path / "scripts" / "bh_reset.py"), "wipe", "--yes",
        # The child sees the directory it runs in, so its own lookups agree with this one.
        f"ENV={mcp_dir}",
    ]


def test_mcp_run_refuses_when_unresolved_or_missing(tmp_path):
    bin_dir = _fake_uv(tmp_path)
    path_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    empty = _runtime_dir(tmp_path)
    unresolved = _cli(tmp_path, "--runtime-dir", str(empty), "mcp-run", "x.py", env_extra=path_env)
    assert unresolved.returncode == 2 and NAME in unresolved.stderr and unresolved.stdout == ""

    missing = _cli(
        tmp_path, "--runtime-dir", str(empty), "mcp-run", "x.py",
        env_extra={**path_env, NAME: str(tmp_path / "absent")},
    )
    assert missing.returncode == 2 and "does not exist" in missing.stderr and missing.stdout == ""
