"""The interactive Apollo wrapper must find Sage's existing skill-local credential."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


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
    assert "--allow-already-absent" in invoked[0]
    assert "deploy_payload_via_ludus.py launch-existing " in invoked[1]
    assert "fixture-only-secret" not in result.stdout + result.stderr
    assert "mythic-fixture-secret" not in result.stdout + result.stderr


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
