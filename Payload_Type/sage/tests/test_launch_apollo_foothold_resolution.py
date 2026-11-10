"""The interactive Apollo wrapper must find Sage's existing skill-local credential."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = (Path(__file__).resolve().parents[3] / "skills" /
          "sage-mythic-payload-deploy" / "scripts" / "launch_apollo_foothold.sh")


def test_interactive_launcher_uses_existing_skill_local_password(tmp_path):
    repo = tmp_path / "sage"
    launcher = repo / "skills/sage-mythic-payload-deploy/scripts/launch_apollo_foothold.sh"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, launcher)

    skill_env = repo / "skills/sage-foothold-launch/.env"
    skill_env.parent.mkdir(parents=True)
    skill_env.write_text("SAGE_RUN_AS_PASSWORD=fixture-only-secret\n")
    skill_env.chmod(0o600)
    mythic_env = tmp_path / "mythic.env"
    mythic_env.write_text("MYTHIC_ADMIN_PASSWORD=mythic-fixture-secret\n")
    mythic_env.chmod(0o600)

    calls = tmp_path / "calls.txt"
    fake_python = repo / ".venv/bin/python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text(
        "#!/bin/sh\n"
        '[ "$SAGE_RUN_AS_PASSWORD" = "$EXPECTED_SECRET" ] || exit 78\n'
        'printf "%s\\n" "$*" >> "$CALLS"\n'
    )
    fake_python.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python3 = fake_bin / "python3"
    fake_python3.write_text("#!/bin/sh\nexit 0\n")
    fake_python3.chmod(0o755)

    env = dict(os.environ)
    for name in ("SAGE_RUN_AS_PASSWORD", "SAGE_RUNAS_FILE", "MYTHIC_ENV_PATH"):
        env.pop(name, None)
    env.update(HOME=str(tmp_path / "home"), PATH=f"{fake_bin}:/usr/bin:/bin",
               SAGE_RDP_DISPLAY=":42", EXPECTED_SECRET="fixture-only-secret",
               MYTHIC_ENV_PATH=str(mythic_env), CALLS=str(calls))
    result = subprocess.run(["/bin/bash", str(launcher), "10.4.10.22",
                             r"NORTH\samwell.tarly"], cwd=repo, env=env,
                            capture_output=True, text=True, timeout=10, check=False)

    assert result.returncode == 0, result.stderr
    invoked = calls.read_text().splitlines()
    assert "deploy_payload_via_ludus.py logoff-user " in invoked[0]
    assert "--allow-already-absent" not in invoked[0]
    assert "deploy_payload_via_ludus.py launch-existing " in invoked[1]
    assert "fixture-only-secret" not in result.stdout + result.stderr
    assert "mythic-fixture-secret" not in result.stdout + result.stderr


def test_castelblack_launcher_waits_for_real_localuser_logoff_before_rdp(tmp_path):
    repo = tmp_path / "sage"
    launcher = repo / "skills/sage-mythic-payload-deploy/scripts/launch_apollo_foothold.sh"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, launcher)
    fake_python = repo / ".venv/bin/python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$CALLS"\n'
        'case "$*" in *--allow-already-absent*) exit 0;; esac\n'
        'exit 1\n'
    )
    fake_python.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("python3", "sleep"):
        stub = fake_bin / name
        stub.write_text(f"#!/bin/sh\nprintf '{name}\\n' >> \"$CALLS\"\n")
        stub.chmod(0o755)
    calls = tmp_path / "calls.txt"
    mythic_env = tmp_path / "mythic.env"
    mythic_env.write_text("MYTHIC_ADMIN_PASSWORD=fixture-only-secret\n")
    env = dict(os.environ)
    env.update(
        PATH=f"{fake_bin}:/usr/bin:/bin", SAGE_RDP_DISPLAY=":42",
        SAGE_RUN_AS_PASSWORD="fixture-only-password", MYTHIC_ENV_PATH=str(mythic_env),
        CALLS=str(calls),
    )
    result = subprocess.run(
        ["/bin/bash", str(launcher), "10.4.10.22", r"NORTH\samwell.tarly"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    invoked = calls.read_text().splitlines()
    assert result.returncode == 2
    assert len([line for line in invoked if " logoff-user " in line]) == 30
    assert all("--allow-already-absent" not in line for line in invoked)
    assert "python3" not in invoked
    assert "launch-existing" not in " ".join(invoked)


def test_non_castelblack_launcher_accepts_verified_absence(tmp_path):
    repo = tmp_path / "sage"
    launcher = repo / "skills/sage-mythic-payload-deploy/scripts/launch_apollo_foothold.sh"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, launcher)
    fake_python = repo / ".venv/bin/python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$CALLS"\n'
    )
    fake_python.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python3 = fake_bin / "python3"
    fake_python3.write_text("#!/bin/sh\nexit 0\n")
    fake_python3.chmod(0o755)
    calls = tmp_path / "calls.txt"
    mythic_env = tmp_path / "mythic.env"
    mythic_env.write_text("MYTHIC_ADMIN_PASSWORD=fixture-only-secret\n")
    env = dict(os.environ)
    env.update(
        PATH=f"{fake_bin}:/usr/bin:/bin", SAGE_RDP_DISPLAY=":42",
        SAGE_RUN_AS_PASSWORD="fixture-only-password", MYTHIC_ENV_PATH=str(mythic_env),
        CALLS=str(calls),
    )
    result = subprocess.run(
        ["/bin/bash", str(launcher), "10.4.10.12", r"ESSOS\jorah.mormont",
         "--", "--target-host", "MEEREEN"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    invoked = calls.read_text().splitlines()
    assert "--allow-already-absent" in invoked[0]


def test_interactive_launcher_rejects_unusable_mythic_env_before_helpers(tmp_path):
    repo = tmp_path / "sage"
    launcher = repo / "skills/sage-mythic-payload-deploy/scripts/launch_apollo_foothold.sh"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, launcher)

    calls = tmp_path / "calls.txt"
    fake_python = repo / ".venv/bin/python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text(f"#!/bin/sh\nprintf 'called\\n' >> '{calls}'\n")
    fake_python.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python3 = fake_bin / "python3"
    fake_python3.write_text(f"#!/bin/sh\nprintf 'called\\n' >> '{calls}'\n")
    fake_python3.chmod(0o755)

    unreadable_env = tmp_path / "unreadable.env"
    unreadable_env.write_text("MYTHIC_ADMIN_PASSWORD=hidden-fixture-secret\n")
    unreadable_env.chmod(0)
    invalid_paths = [None, repo / "missing.env", repo, unreadable_env]
    for env_path in invalid_paths:
        env = dict(os.environ)
        env.update(
            HOME=str(tmp_path / "home"), PATH=f"{fake_bin}:/usr/bin:/bin",
            SAGE_RDP_DISPLAY=":42", SAGE_RUN_AS_PASSWORD="skill-fixture-secret",
        )
        if env_path is None:
            env.pop("MYTHIC_ENV_PATH", None)
        else:
            env["MYTHIC_ENV_PATH"] = str(env_path)
        result = subprocess.run(
            ["/bin/bash", str(launcher)], cwd=repo, env=env,
            capture_output=True, text=True, timeout=10, check=False,
        )
        assert result.returncode == 2
        assert "MYTHIC_ENV_PATH must name a readable regular file" in result.stderr
        assert "skill-fixture-secret" not in result.stdout + result.stderr
        assert not calls.exists()


def _orphan_probe_launcher(tmp_path, launch_exit: int, opener_ignores_term: bool = False):
    """Run the launcher with a long-lived stand-in RDP opener and a launch-existing that exits `launch_exit`.

    Returns (completed process, opener pid). The opener records its pid and stays up until signalled, as the real
    opener holds the RDP session open."""
    repo = tmp_path / "sage"
    launcher = repo / "skills/sage-mythic-payload-deploy/scripts/launch_apollo_foothold.sh"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, launcher)
    pid_file = tmp_path / "opener.pid"
    fake_python = repo / ".venv/bin/python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text(
        "#!/bin/sh\n"
        'case "$*" in *logoff-user*) exit 0;; esac\n'
        # launch-existing starts once the RDP session (the opener) is up, as the real one polls for it.
        'i=0; while [ ! -s "$PID_FILE" ] && [ $i -lt 100 ]; do sleep 0.05; i=$((i+1)); done\n'
        f"exit {launch_exit}\n"
    )
    fake_python.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    opener = fake_bin / "python3"
    term = "trap '' TERM\n" if opener_ignores_term else "trap 'exit 0' TERM\n"
    opener.write_text("#!/bin/sh\n" + term + 'echo $$ > "$PID_FILE"\nwhile :; do sleep 0.05; done\n')
    opener.chmod(0o755)
    mythic_env = tmp_path / "mythic.env"
    mythic_env.write_text("MYTHIC_ADMIN_PASSWORD=fixture-only-secret\n")
    env = dict(os.environ)
    env.update(
        PATH=f"{fake_bin}:/usr/bin:/bin", SAGE_RDP_DISPLAY=":42", SAGE_RUN_AS_PASSWORD="fixture-only-password",
        MYTHIC_ENV_PATH=str(mythic_env), PID_FILE=str(pid_file), SAGE_RDP_CLEANUP_SECONDS="1",
    )
    result = subprocess.run(["/bin/bash", str(launcher), "10.4.10.22", r"NORTH\samwell.tarly"],
                            cwd=repo, env=env, capture_output=True, text=True, timeout=20, check=False)
    return result, int(pid_file.read_text().strip())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("launch_exit", [0, 2])
def test_the_rdp_opener_never_outlives_the_launcher(tmp_path, launch_exit):
    """2026-11-09: launch-existing exited 2 and the EXIT trap killed only the retry subshell, leaving the opener and
    its RDP client running. Every exit path must end the opener itself, and the launch result is kept."""
    result, pid = _orphan_probe_launcher(tmp_path, launch_exit)
    try:
        assert not _alive(pid), "the RDP opener survived the launcher"
        assert result.returncode == launch_exit, result.stderr
        assert "local RDP client cleanup: ok" in result.stderr
    finally:
        if _alive(pid):
            os.kill(pid, 9)


def test_a_launch_success_with_a_failed_local_cleanup_exits_non_zero_and_says_so(tmp_path):
    result, pid = _orphan_probe_launcher(tmp_path, 0, opener_ignores_term=True)
    try:
        assert result.returncode != 0
        assert "local RDP client cleanup: failed" in result.stderr
        assert "launch succeeded" in result.stderr
        assert not _alive(pid)  # forced down, but reported as a failed cleanup
    finally:
        if _alive(pid):
            os.kill(pid, 9)
