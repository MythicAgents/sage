"""`sage_deployment.py` container deploy + parity (build-layer tests: no Docker, no Mythic).

Parity answers one question that is invisible to the eye and has failed twice: **is the container
executing the code you just wrote?** The Docker image cannot answer it, because Mythic's generated
compose file bind-mounts the installed service directory over `/Mythic/` and shadows the application
code the image baked in. So the check reads from the container and compares against the working tree.

These tests cover the parts that can be exercised without a running container: the digest that
decides "same code or not", the excludes that keep runtime databases out of the comparison, and the
two fail-closed guards. The Docker-facing paths are deliberately not mocked into a fake pass — a
mock of `docker exec` would assert only that the mock was called.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sage_deployment  # noqa: E402


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A miniature service tree carrying one file of every kind parity cares about."""
    root = tmp_path / "sage"
    (root / "ai" / "langgraph").mkdir(parents=True)
    (root / "sage_chat").mkdir()
    (root / "prompts").mkdir()
    (root / "ai" / "langgraph" / "model.py").write_text("x = 1\n")
    (root / "sage_chat" / "service.py").write_text("y = 2\n")
    (root / "prompts" / "supervisor.md").write_text("be helpful\n")
    (root / "main.py").write_text("run()\n")
    (root / "mcp_tool_policy.json").write_text("{}\n")
    return root


def test_the_digest_is_deterministic(tree: Path):
    """Two reads of one unchanged tree must agree, or every comparison is noise."""
    assert sage_deployment._digest_local(tree)[0] == sage_deployment._digest_local(tree)[0]


def test_the_digest_is_independent_of_location(tree: Path, tmp_path: Path):
    """The repo and the container hold the same code at different paths.

    If the digest folded in absolute paths it would ALWAYS differ and parity would be permanently
    red — which reads as "deploy again" forever rather than as a broken check.
    """
    elsewhere = tmp_path / "another" / "place" / "sage"
    elsewhere.parent.mkdir(parents=True)
    shutil.copytree(tree, elsewhere)
    assert sage_deployment._digest_local(tree)[0] == sage_deployment._digest_local(elsewhere)[0]


def test_the_digest_notices_a_one_line_change(tree: Path):
    """The falsifier. A check that cannot see an edit is worse than no check."""
    before = sage_deployment._digest_local(tree)[0]
    target = tree / "ai" / "langgraph" / "model.py"
    target.write_text(target.read_text() + "# drift\n")
    assert sage_deployment._digest_local(tree)[0] != before


def test_the_digest_ignores_runtime_databases(tree: Path):
    """The container writes its own sage.db into the mounted directory as it runs.

    Counting that would make parity go red on its own accord seconds after a successful deploy.
    """
    before = sage_deployment._digest_local(tree)[0]
    (tree / "sage.db").write_bytes(b"\x00" * 64)
    (tree / "sage_20260101-0000.db.zst").write_bytes(b"\x01" * 64)
    assert sage_deployment._digest_local(tree)[0] == before


def test_the_digest_ignores_tests_and_evals(tree: Path):
    """Only runtime code is compared; a test edit must not read as a stale container."""
    before = sage_deployment._digest_local(tree)[0]
    (tree / "tests").mkdir()
    (tree / "tests" / "test_thing.py").write_text("assert True\n")
    assert sage_deployment._digest_local(tree)[0] == before


def test_the_installed_directory_is_never_guessed(monkeypatch):
    """Publishable-by-default: external locations come from an env var and fail closed.

    Guessing a checkout by name silently deploys into the wrong Mythic on a machine with two.
    """
    monkeypatch.delenv("MYTHIC_ENV_PATH", raising=False)
    with pytest.raises(SystemExit) as caught:
        sage_deployment._installed_service_dir()
    assert "MYTHIC_ENV_PATH" in str(caught.value), caught.value


def test_deploy_refuses_unless_container_mode_is_explicit(monkeypatch, capsys):
    """The safety gate. Container mode is the exception; local tmux is the default workflow.

    A deploy that ran just because the mode variable was unset would overwrite the installed tree
    while the operator believed they were working locally.
    """
    monkeypatch.delenv("SAGE_DEPLOYMENT_MODE", raising=False)
    with pytest.raises(SystemExit) as caught:
        sage_deployment.main(["deploy"])
    message = str(caught.value)
    assert "container" in message and "local" in message, message


def _stub_deploy(monkeypatch, *, local_running: bool) -> list[str]:
    """Run `deploy` with its own side-effecting helpers replaced, recording the order they fire.

    The substitutions are at this module's seams, not at Docker's — the test drives the real
    `main`, real argument parsing and real control flow, and only the four functions that touch the
    world are stubbed. That keeps it a behavioural test rather than an assertion about source text.
    """
    calls: list[str] = []

    def record(name: str, result: dict):
        def _fn(*a, **k):
            calls.append(name)
            return result
        return _fn

    monkeypatch.setattr(sage_deployment, "_mythic_dir", lambda: Path("/opt/mythic"))
    monkeypatch.setattr(sage_deployment, "_sync_into_mythic", record("sync", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_ensure_mythic_settings", record("settings", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_stop_local", record("stop_local", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_start_container", record("restart", {"returncode": 0}))
    monkeypatch.setattr(
        sage_deployment, "_parity",
        record("parity", {"ready": True, "blockers": [], "repo_digest": "x"}),
    )
    monkeypatch.setattr(
        sage_deployment.readiness_contract, "sage_deployment_status",
        lambda **kw: {"local_process_running": local_running, "blockers": [], "ready": True},
    )
    sage_deployment.main(["deploy", "--mode", "container"])
    return calls


def test_deploy_stops_a_running_local_sage_before_restarting(monkeypatch, capsys):
    """A deploy must not create the split brain the rest of this module exists to prevent.

    Both processes register as the `sage` service and one wins the RabbitMQ queue, so deploying
    while a tmux Sage is up would leave parity measuring a container that is not the process
    answering Mythic. Order matters: the local Sage must stop BEFORE the container returns.
    """
    calls = _stub_deploy(monkeypatch, local_running=True)
    assert "stop_local" in calls, f"deploy did not stop the running local Sage: {calls}"
    assert calls.index("stop_local") < calls.index("restart"), (
        f"local Sage was stopped after the container came back; both were briefly live: {calls}"
    )


def test_deploy_leaves_a_stopped_local_sage_alone(monkeypatch, capsys):
    """The control. If stop fired unconditionally the test above would pass while proving nothing."""
    calls = _stub_deploy(monkeypatch, local_running=False)
    assert "stop_local" not in calls, f"deploy stopped a local Sage that was not running: {calls}"
    assert calls == ["sync", "settings", "restart", "parity"], calls


def test_sync_excludes_cover_every_database_spelling():
    """The live database must survive `rsync --delete`.

    rsync protects excluded files on the receiver from deletion, so this list is what stands between
    a deploy and destroying the running container's state. The three spellings all occur in practice.
    """
    for name in ("sage.db", "sage.db-wal", "sage_20260101-0000.db.zst"):
        assert any(
            _matches(name, pattern) for pattern in sage_deployment._SYNC_EXCLUDES
        ), f"{name} is not excluded from the sync"


def _matches(name: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(name, pattern)



# --- Starting the container without a surprise rebuild (fake docker / mythic-cli on PATH) -------------

def _fake_mythic(tmp_path: Path, monkeypatch, *, image_present: bool, env_lines: str) -> Path:
    """A Mythic install whose `docker` and `mythic-cli` only record what they were asked to do."""
    mythic = tmp_path / "mythic"
    (mythic / "InstalledServices" / "sage").mkdir(parents=True)
    (mythic / ".env").write_text(env_lines, encoding="utf-8")
    log = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'printf "docker|%s|%s|%s\\n" "$*" "$PWD" "${RABBITMQ_HOST-unset}" >> "$CALL_LOG"\n'
        f'if [[ "$1 $2" == "image inspect" ]]; then {"exit 0" if image_present else "exit 1"}; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    cli = mythic / "mythic-cli"
    # Like the real one, `config set` rewrites Mythic's .env (uppercased key) and exits 0 even when it
    # changes nothing; FAKE_CLI_IGNORE_SET simulates a set that silently did not apply.
    cli.write_text(
        "#!/usr/bin/env bash\n"
        'printf "cli|%s|%s|%s\\n" "$*" "$PWD" "${RABBITMQ_HOST-unset}" >> "$CALL_LOG"\n'
        'if [[ "$1 $2" == "config set" && "${FAKE_CLI_IGNORE_SET-0}" != 1 ]]; then\n'
        '  key="$(printf %s "$3" | tr a-z A-Z)"; env_file="$(dirname "$0")/.env"\n'
        '  grep -v "^$key=" "$env_file" > "$env_file.new" || true\n'
        '  printf \'%s="%s"\\n\' "$key" "$4" >> "$env_file.new"; mv "$env_file.new" "$env_file"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for tool in (bin_dir / "docker", cli):
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("CALL_LOG", str(log))
    monkeypatch.setenv("MYTHIC_CLI", str(cli))
    monkeypatch.setenv("MYTHIC_ENV_PATH", str(mythic / ".env"))
    # Sage's .env carries Docker hostnames that poison host-side mythic-cli; they must be stripped.
    monkeypatch.setenv("RABBITMQ_HOST", "mythic_rabbitmq")
    return log


def _calls(log: Path) -> list[list[str]]:
    return [line.split("|") for line in log.read_text().splitlines()] if log.exists() else []


def test_start_recreates_the_container_from_the_existing_image_without_building(tmp_path, monkeypatch):
    """2026-10-30 and 2026-11-03: `mythic-cli stop sage` removes the container, so `docker restart`
    failed; and `mythic-cli start` rebuilt the image under REBUILD_ON_START. Compose recreates the
    container from the image that is already there."""
    log = _fake_mythic(tmp_path, monkeypatch, image_present=True, env_lines="")
    result = sage_deployment._start_container()

    assert result["returncode"] == 0, result
    calls = _calls(log)
    commands = [call[1] for call in calls]
    assert "compose up -d --no-build --pull never --force-recreate sage" in commands
    assert not any(c.startswith("restart") for c in commands)
    assert not any(call[0] == "cli" and call[1].startswith("build") for call in calls)
    up = next(call for call in calls if call[1].startswith("compose up"))
    assert up[2] == str(tmp_path / "mythic") and up[3] == "unset"


@pytest.mark.parametrize("image_present, rebuild", [(False, False), (True, True)])
def test_start_builds_only_when_the_image_is_missing_or_a_rebuild_is_asked(tmp_path, monkeypatch, image_present, rebuild):
    log = _fake_mythic(tmp_path, monkeypatch, image_present=image_present, env_lines="")
    result = sage_deployment._start_container(rebuild=rebuild)

    assert result["returncode"] == 0, result
    calls = _calls(log)
    build = [i for i, call in enumerate(calls) if call[0] == "cli" and call[1] == "build sage"]
    up = [i for i, call in enumerate(calls) if call[1].startswith("compose up")]
    assert build and up and build[0] < up[0], calls
    assert calls[build[0]][3] == "unset", "mythic-cli must not inherit Sage's container hostnames"


def test_container_mode_switches_mythic_back_to_the_local_build_and_bind_mount(tmp_path, monkeypatch):
    log = _fake_mythic(
        tmp_path, monkeypatch, image_present=True,
        env_lines='SAGE_USE_BUILD_CONTEXT="false"\nSAGE_USE_VOLUME="true"\n',
    )
    result = sage_deployment._ensure_mythic_settings("container")

    assert result["returncode"] == 0, result
    assert [call[1] for call in _calls(log) if call[0] == "cli"] == [
        "config set sage_use_build_context true",
        "config set sage_use_volume false",
        "add sage",
    ]


def test_matching_settings_are_not_rewritten_but_the_compose_entry_is_regenerated(tmp_path, monkeypatch):
    """Mythic's .env is not the compose file: a setting changed without a regenerate leaves the entry
    naming another image, and parity (which hashes code only) would not notice."""
    log = _fake_mythic(
        tmp_path, monkeypatch, image_present=True,
        env_lines='SAGE_USE_BUILD_CONTEXT="true"\nSAGE_USE_VOLUME="false"\n',
    )
    result = sage_deployment._ensure_mythic_settings("container")

    assert result["returncode"] == 0 and result.get("changed") == []
    assert [call[1] for call in _calls(log)] == ["add sage"]


def test_a_mythic_setting_that_did_not_apply_fails_the_deploy(tmp_path, monkeypatch):
    """`mythic-cli config set` exits 0 even when it reports it found no matching key."""
    log = _fake_mythic(tmp_path, monkeypatch, image_present=True, env_lines='SAGE_USE_BUILD_CONTEXT="false"\n')
    monkeypatch.setenv("FAKE_CLI_IGNORE_SET", "1")
    result = sage_deployment._ensure_mythic_settings("container")

    assert result["returncode"] != 0
    assert "sage_use_build_context" in json.dumps(result)
    assert not any(call[1] == "add sage" for call in _calls(log))


def test_mythic_cli_and_env_path_must_name_the_same_install(tmp_path, monkeypatch):
    """Otherwise deploy syncs into one install's InstalledServices and starts the other's container."""
    _fake_mythic(tmp_path, monkeypatch, image_present=True, env_lines="")
    other = tmp_path / "other-mythic"
    other.mkdir()
    (other / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("MYTHIC_ENV_PATH", str(other / ".env"))

    with pytest.raises(SystemExit) as caught:
        sage_deployment._start_container()
    assert "MYTHIC_CLI" in str(caught.value) and "MYTHIC_ENV_PATH" in str(caught.value)


# --- Stopping requires knowing which Sage is intended --------------------------------------------------

def test_enforce_without_an_explicit_mode_stops_nothing(monkeypatch, capsys):
    """2026-11-04: container mode lived only in one shell, so a later `enforce` defaulted to local and
    stopped the container the operator meant to run."""
    stopped: list[str] = []
    monkeypatch.delenv("SAGE_DEPLOYMENT_MODE", raising=False)
    monkeypatch.setattr(sage_deployment, "_stop_container", lambda: stopped.append("container") or {})
    monkeypatch.setattr(sage_deployment, "_stop_local", lambda: stopped.append("local") or {})
    monkeypatch.setattr(
        sage_deployment.readiness_contract, "sage_deployment_status",
        lambda **kw: {"ready": False, "mode": "local", "container_running": True,
                      "local_process_running": True, "blockers": ["two Sages"]},
    )
    assert sage_deployment.main(["enforce"]) == 1
    assert stopped == []
    assert "SAGE_DEPLOYMENT_MODE" in capsys.readouterr().out


def _running_world(monkeypatch, running: dict) -> list[str]:
    stopped: list[str] = []

    def stopper(kind):
        def _stop():
            stopped.append(kind)
            running[kind] = False
            return {"returncode": 0}
        return _stop

    monkeypatch.setattr(sage_deployment, "_stop_container", stopper("container"))
    monkeypatch.setattr(sage_deployment, "_stop_local", stopper("local"))
    monkeypatch.setattr(sage_deployment.readiness_contract, "probe_sage_container_running",
                        lambda *a, **k: running["container"])
    monkeypatch.setattr(sage_deployment.readiness_contract, "probe_local_sage_running",
                        lambda *a, **k: running["local"])
    return stopped


def test_stop_all_stops_whichever_sages_are_running(monkeypatch, capsys):
    running = {"container": True, "local": True}
    stopped = _running_world(monkeypatch, running)

    assert sage_deployment.main(["stop-all"]) == 0
    assert sorted(stopped) == ["container", "local"]
    assert running == {"container": False, "local": False}


def test_stop_all_with_nothing_running_stops_nothing(monkeypatch, capsys):
    stopped = _running_world(monkeypatch, {"container": False, "local": False})

    assert sage_deployment.main(["stop-all"]) == 0
    assert stopped == []
