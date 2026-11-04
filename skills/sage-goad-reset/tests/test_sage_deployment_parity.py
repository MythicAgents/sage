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


# --- Published mode: the GHCR image named in config.json, running the image's own code -----------------

def test_published_image_is_the_tag_config_json_names():
    image = sage_deployment._published_image()
    config = json.loads((sage_deployment.REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    assert image == config["remote_images"]["sage"]
    assert image.startswith("ghcr.io/")


def test_published_mode_points_mythic_at_the_remote_image_and_keeps_the_bind_mount(tmp_path, monkeypatch):
    """D1: the bind mount stays (databases on the host, never in a volume `mythic-cli start` deletes)."""
    log = _fake_mythic(
        tmp_path, monkeypatch, image_present=True,
        env_lines='SAGE_USE_BUILD_CONTEXT="true"\nSAGE_USE_VOLUME="false"\nSAGE_REMOTE_IMAGE="ghcr.io/mythicagents/sage:0.2.0"\n',
    )
    result = sage_deployment._ensure_mythic_settings("published")

    assert result["returncode"] == 0, result
    image = sage_deployment._published_image()
    assert [call[1] for call in _calls(log) if call[0] == "cli"] == [
        "config set sage_use_build_context false",
        f"config set sage_remote_image {image}",
        "add sage",
    ]


def test_published_code_replaces_the_installed_code_but_keeps_operator_and_runtime_files(tmp_path, monkeypatch):
    """The image's /Mythic must become what runs, while the installed copy keeps what the operator and the
    runtime own there: .env, databases, the engagement ledger, the TLS bundle and the tool binaries."""
    installed = tmp_path / "mythic" / "InstalledServices" / "sage"
    (installed / "ai").mkdir(parents=True)
    (installed / "ai" / "model.py").write_text("working tree version\n")
    (installed / "ai" / "only_in_working_tree.py").write_text("x\n")
    (installed / ".env").write_text("BLOODHOUND_TOKEN_ID=keep\n")
    (installed / "sage.db").write_text("live db")
    (installed / ".sage_engagement").mkdir()
    (installed / ".sage_engagement" / "state.json").write_text("{}")
    (installed / "certs").mkdir()
    (installed / "certs" / "bundle.pem").write_text("operator CA")
    extracted = tmp_path / "extracted"
    (extracted / "ai").mkdir(parents=True)
    (extracted / "ai" / "model.py").write_text("published version\n")
    (extracted / "certs").mkdir()
    (extracted / "tools").mkdir()
    (extracted / "tools" / ".keep").write_text("")
    working_tools = tmp_path / "worktree-sage" / "tools"
    working_tools.mkdir(parents=True)
    (working_tools / "Rubeus.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sage_deployment, "_installed_service_dir", lambda: installed)
    monkeypatch.setattr(sage_deployment, "_payload_source_dir", lambda: working_tools.parent)

    result = sage_deployment._sync_published_code(extracted)

    assert result["returncode"] == 0, result
    assert (installed / "ai" / "model.py").read_text() == "published version\n"
    assert not (installed / "ai" / "only_in_working_tree.py").exists()
    assert (installed / ".env").read_text() == "BLOODHOUND_TOKEN_ID=keep\n"
    assert (installed / "sage.db").read_text() == "live db"
    assert (installed / ".sage_engagement" / "state.json").exists()
    assert (installed / "certs" / "bundle.pem").read_text() == "operator CA"
    assert (installed / "tools" / "Rubeus.exe").read_bytes() == b"MZ"


def _published_docker(tmp_path: Path, monkeypatch, *, running_image: str, image_id: str,
                      container_digest: str, image_digest: str, started: str = "2099-01-01T00:00:00Z") -> Path:
    log = tmp_path / "docker.log"
    bin_dir = tmp_path / "pbin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$PUB_LOG"\n'
        'case "$1" in\n'
        f'  image) echo "{image_id}" ;;\n'
        '  inspect) if [[ "$*" == *StartedAt* ]]; then echo "$PUB_STARTED"; else echo "$PUB_RUNNING_IMAGE"; fi ;;\n'
        f'  exec) echo "{container_digest} 3 1.0" ;;\n'
        f'  run) echo "{image_digest} 3 1.0" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("PUB_LOG", str(log))
    monkeypatch.setenv("PUB_RUNNING_IMAGE", running_image)
    monkeypatch.setenv("PUB_STARTED", started)
    return log


def test_published_identity_holds_when_the_running_image_and_code_are_the_tags(tmp_path, monkeypatch):
    log = _published_docker(tmp_path, monkeypatch, running_image="sha256:pub", image_id="sha256:pub",
                            container_digest="d1", image_digest="d1")
    result = sage_deployment._published_parity("ghcr.io/mythicagents/sage:0.3.0")

    assert result["ready"] is True, result
    # The image's own code is read from a throwaway container with no network and no bind mount.
    run = next(line for line in log.read_text().splitlines() if line.startswith("run "))
    assert "--network none" in run and "--rm" in run and "-v" not in run.split()


@pytest.mark.parametrize("running_image, container_digest, needle", [
    ("sha256:local-build", "d1", "not the published image"),
    ("sha256:pub", "d-working-tree", "differs from the published image's code"),
])
def test_published_identity_fails_on_another_image_or_shadowing_code(tmp_path, monkeypatch, running_image,
                                                                      container_digest, needle):
    """A local build still tagged in compose, or the working tree left in the bind mount, would otherwise
    pass as "published" — the exact confusion the 2026-11-03 investigation found."""
    _published_docker(tmp_path, monkeypatch, running_image=running_image, image_id="sha256:pub",
                      container_digest=container_digest, image_digest="d1")
    result = sage_deployment._published_parity("ghcr.io/mythicagents/sage:0.3.0")

    assert result["ready"] is False
    assert any(needle in blocker for blocker in result["blockers"]), result


def test_published_deploy_pulls_and_extracts_before_changing_anything(monkeypatch, capsys):
    calls: list[str] = []

    def record(name, result):
        def _fn(*a, **k):
            calls.append(name)
            return result
        return _fn

    monkeypatch.setattr(sage_deployment, "_mythic_dir", lambda: Path("/opt/mythic"))
    monkeypatch.setattr(sage_deployment, "_pull_image", record("pull", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_extract_image_code", record("extract", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_ensure_mythic_settings", record("settings", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_sync_published_code", record("sync", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_sync_into_mythic", record("working-tree-sync", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_stop_local", record("stop_local", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_start_container", record("start", {"returncode": 0}))
    monkeypatch.setattr(sage_deployment, "_published_parity", record("identity", {"ready": True, "blockers": []}))
    monkeypatch.setattr(sage_deployment.readiness_contract, "sage_deployment_status",
                        lambda **kw: {"local_process_running": True, "blockers": [], "ready": True})

    assert sage_deployment.main(["deploy", "--mode", "published"]) == 0
    assert calls == ["pull", "extract", "settings", "sync", "stop_local", "start", "identity"], calls


def test_enforce_published_stops_a_running_local_sage(monkeypatch, capsys):
    running = {"container": True, "local": True}
    stopped = _running_world(monkeypatch, running)

    assert sage_deployment.main(["enforce", "--mode", "published"]) == 0
    assert stopped == ["local"]


def test_published_identity_blocks_when_the_start_time_is_unreadable(tmp_path, monkeypatch):
    _published_docker(tmp_path, monkeypatch, running_image="sha256:pub", image_id="sha256:pub",
                      container_digest="d1", image_digest="d1", started="not-a-time")
    result = sage_deployment._published_parity("ghcr.io/mythicagents/sage:0.3.0")

    assert result["ready"] is False
    assert any("start time" in blocker for blocker in result["blockers"]), result


def test_extract_copies_from_a_created_container_and_always_removes_it(tmp_path, monkeypatch):
    log = tmp_path / "docker.log"
    bin_dir = tmp_path / "xbin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$X_LOG"\n'
        '[[ "$1" == "cp" && "${X_CP_FAIL-0}" == 1 ]] && exit 1\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("X_LOG", str(log))

    ok = sage_deployment._extract_image_code("ghcr.io/x/sage:1", tmp_path / "out")
    monkeypatch.setenv("X_CP_FAIL", "1")
    failed = sage_deployment._extract_image_code("ghcr.io/x/sage:1", tmp_path / "out")

    calls = log.read_text().splitlines()
    assert ok["returncode"] == 0 and failed["returncode"] != 0
    assert calls[0].startswith("create --name sage-published-extract-") and calls[0].endswith(" ghcr.io/x/sage:1")
    assert calls[1].endswith(f":/Mythic/. {tmp_path / 'out'}")
    assert sum(line.startswith("rm -f sage-published-extract-") for line in calls) == 2, calls
    assert not any(line.startswith(("start", "run")) for line in calls)


def test_a_failed_pull_changes_nothing(monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(sage_deployment, "_mythic_dir", lambda: Path("/opt/mythic"))
    monkeypatch.setattr(sage_deployment, "_pull_image", lambda image: calls.append("pull") or {"returncode": 1})
    for name in ("_extract_image_code", "_ensure_mythic_settings", "_sync_published_code", "_stop_local",
                 "_start_container"):
        monkeypatch.setattr(sage_deployment, name, lambda *a, _n=name, **k: calls.append(_n) or {"returncode": 0})

    assert sage_deployment.main(["deploy", "--mode", "published"]) == 1
    assert calls == ["pull"]


def test_published_sync_keeps_the_installed_directory_permissions(tmp_path, monkeypatch):
    """The scratch directory is created 0700; rsync -a would copy that mode onto the installed copy."""
    installed = tmp_path / "installed"
    installed.mkdir()
    installed.chmod(0o775)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    extracted.chmod(0o700)
    (extracted / "main.py").write_text("run()\n")
    monkeypatch.setattr(sage_deployment, "_installed_service_dir", lambda: installed)
    monkeypatch.setattr(sage_deployment, "_payload_source_dir", lambda: tmp_path / "no-worktree")

    assert sage_deployment._sync_published_code(extracted)["returncode"] == 0
    assert (installed.stat().st_mode & 0o777) == 0o775
