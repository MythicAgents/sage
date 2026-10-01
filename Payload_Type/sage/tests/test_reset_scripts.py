import os
import json
import socket
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MYTHIC_RESET = ROOT / "skills" / "sage-goad-reset" / "scripts" / "mythic_reset.sh"
SAGE_STOP = ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_stop.sh"


def _mythic_reset_env(
    tmp_path,
    *,
    initial_rows,
    recovered_rows=None,
    services="mythic_postgres\napollo\nhttp\nsage",
    docker_failure="",
    timeout_failure="",
    recover_on_targeted_start=False,
    cli_start_message="",
    cli_start_failure=False,
    sage_running_on_start=False,
    enforce_deployment=False,
    simulate_postgres_init=False,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    recovered = tmp_path / "recovered"
    sage_running = tmp_path / "sage-running"
    cli = tmp_path / "mythic-cli"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'printf "cli|%s|%s|%s|%s|%s|%s\\n" "$*" "${RABBITMQ_HOST-unset}" '
        '"${MYTHIC_SERVER_HOST-unset}" "${NGINX_HOST-unset}" '
        '"${COMPOSE_FILE-unset}" "${COMPOSE_PROJECT_NAME-unset}" >> "$CALL_LOG"\n'
        'if [[ "$1" == "start" ]]; then printf "%s\\n" "${CLI_START_MESSAGE-}"; fi\n'
        'if [[ "$1" == "start" && "${SAGE_RUNNING_ON_START-0}" == "1" ]]; then : > "$SAGE_RUNNING_FILE"; fi\n'
        'if [[ "$1" == "stop" ]]; then rm -f "$SAGE_RUNNING_FILE"; fi\n'
        'if [[ "$1" == "start" && "${CLI_START_FAILURE-0}" == "1" ]]; then exit 7; fi\n'
        'if [[ "$1" == "start" && "${SIMULATE_POSTGRES_INIT-0}" == "1" ]]; then\n'
        '  [[ -f "$POSTGRES_INIT_SOURCE" ]] || { echo "missing pg_hba.conf" >&2; exit 8; }\n'
        '  rm "$POSTGRES_INIT_SOURCE"\n'
        'fi\n'
        'if [[ "$1" == "start" && "$#" -gt 1 '
        '&& "${RECOVER_ON_TARGETED_START-0}" == "1" ]]; then\n'
        '  : > "$RECOVERED_FILE"\n'
        "fi\n"
    )
    cli.chmod(0o755)
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'printf "docker|%s|%s|%s|%s\\n" "$*" "${COMPOSE_FILE-unset}" '
        '"${COMPOSE_PROJECT_NAME-unset}" "$PWD" >> "$CALL_LOG"\n'
        'if [[ "$1 ${2-} ${3-}" == "compose config --services" ]]; then\n'
        '  [[ "${DOCKER_FAILURE-}" == "config" ]] && exit 9\n'
        '  printf "%s\\n" "$SERVICES"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1 ${2-} ${3-} ${4-}" == "compose rm --force mythic_postgres" ]]; then\n'
        '  [[ "${DOCKER_FAILURE-}" == "rm" ]] && exit 9\n'
        '  [[ "${SIMULATE_POSTGRES_INIT-0}" == "1" ]] && : > "$POSTGRES_INIT_SOURCE"\n'
        '  exit 0\n'
        'fi\n'
        'if [[ "$1" == "run" ]]; then exit 0; fi\n'
        'if [[ "$1 ${2-}" == "ps --filter" ]]; then\n'
        '  [[ -f "$SAGE_RUNNING_FILE" ]] && printf "sage\\n"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1 ${2-} ${3-} ${4-}" == "compose ps --all --quiet" ]]; then\n'
        '  [[ "${DOCKER_FAILURE-}" == "ps" ]] && exit 9\n'
        '  printf "%s\\n" "$CONTAINER_IDS"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "inspect" ]]; then\n'
        '  [[ "${DOCKER_FAILURE-}" == "inspect" ]] && exit 9\n'
        '  if [[ -f "$RECOVERED_FILE" ]]; then\n'
        '    printf "%s\\n" "$RECOVERED_ROWS"\n'
        "  else\n"
        '    printf "%s\\n" "$INITIAL_ROWS"\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 98\n"
    )
    docker.chmod(0o755)
    timeout = fake_bin / "timeout"
    timeout.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'printf "timeout|%s\\n" "$*" >> "$CALL_LOG"\n'
        '[[ "${TIMEOUT_FAILURE-}" == "${3-}" ]] && exit 124\n'
        "shift 3\n"
        'exec "$@"\n'
    )
    timeout.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "MYTHIC_CLI": str(cli),
        "CALL_LOG": str(log),
        "RECOVERED_FILE": str(recovered),
        "SAGE_RUNNING_FILE": str(sage_running),
        "INITIAL_ROWS": initial_rows,
        "RECOVERED_ROWS": recovered_rows if recovered_rows is not None else initial_rows,
        "SERVICES": services,
        "CONTAINER_IDS": "id-postgres\nid-apollo\nid-http\nid-sage",
        "DOCKER_FAILURE": docker_failure,
        "TIMEOUT_FAILURE": timeout_failure,
        "RECOVER_ON_TARGETED_START": "1" if recover_on_targeted_start else "0",
        "CLI_START_MESSAGE": cli_start_message,
        "CLI_START_FAILURE": "1" if cli_start_failure else "0",
        "SIMULATE_POSTGRES_INIT": "1" if simulate_postgres_init else "0",
        "POSTGRES_INIT_SOURCE": str(tmp_path / "container-pg_hba.conf"),
        "SAGE_RUNNING_ON_START": "1" if sage_running_on_start else "0",
        "RABBITMQ_HOST": "127.0.0.1",
        "MYTHIC_SERVER_HOST": "127.0.0.1",
        "NGINX_HOST": "127.0.0.1",
        "COMPOSE_FILE": "/tmp/not-the-mythic-compose.yml",
        "COMPOSE_PROJECT_NAME": "not-mythic",
        "SAGE_RESET_SKIP_DEPLOYMENT_ENFORCE": "0" if enforce_deployment else "1",
    }
    return env, log


def _metered_reset_env(
    tmp_path, *, initial_rows, recovered_rows=None, recover=False, missing_image="",
    simulate_postgres_init=False, remove_failure=False,
):
    fake_bin = tmp_path / "metered-bin"; fake_bin.mkdir()
    log, recovered, up_count = tmp_path / "metered.log", tmp_path / "recovered", tmp_path / "up-count"
    cli = tmp_path / "mythic-cli"; cli.write_text("#!/usr/bin/env bash\nprintf 'cli|%s\\n' \"$*\" >> \"$CALL_LOG\"\n"); cli.chmod(0o755)
    (tmp_path / "postgres-docker/database").mkdir(parents=True)
    config = {
        "services": {
            "mythic_postgres": {"image": "mythic/postgres:test"},
            "apollo": {"image": "mythic/apollo:test"}, "http": {"image": "mythic/http:test"},
            "sage": {"image": "mythic/sage:test"},
        }
    }
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\nset -u\n"
        'printf "docker|%s|%s|%s|%s\\n" "$*" "${DOCKER_HOST-unset}" "${COMPOSE_FILE-unset}" "${COMPOSE_PROJECT_NAME-unset}" >> "$CALL_LOG"\n'
        'if [[ "$1 ${2-} ${3-}" == "compose config --format" && "${4-}" == "json" ]]; then printf "%s\\n" "$COMPOSE_JSON"; exit 0; fi\n'
        'if [[ "$1" == "image" && "$2" == "inspect" ]]; then [[ "${5-}" == "$MISSING_IMAGE" ]] && exit 9; printf "sha256:installed\\n"; exit 0; fi\n'
        'if [[ "$1 ${2-} ${3-} ${4-}" == "compose rm --force mythic_postgres" ]]; then\n'
        '  [[ "$REMOVE_FAILURE" == "1" ]] && exit 9\n'
        '  [[ "$SIMULATE_POSTGRES_INIT" == "1" ]] && : > "$POSTGRES_INIT_SOURCE"\n'
        '  exit 0\n'
        'fi\n'
        'if [[ "$1 ${2-} ${3-} ${4-}" == "compose ps --all --quiet" ]]; then printf "id-postgres\\nid-apollo\\nid-http\\nid-sage\\n"; exit 0; fi\n'
        'if [[ "$1" == "inspect" ]]; then if [[ -f "$RECOVERED_FILE" ]]; then printf "%s\\n" "$RECOVERED_ROWS"; else printf "%s\\n" "$INITIAL_ROWS"; fi; exit 0; fi\n'
        'if [[ "$1 ${2-} ${3-}" == "compose up -d" ]]; then\n'
        '  if [[ "$SIMULATE_POSTGRES_INIT" == "1" ]]; then\n'
        '    [[ -f "$POSTGRES_INIT_SOURCE" ]] || { echo "missing pg_hba.conf" >&2; exit 8; }\n'
        '    rm "$POSTGRES_INIT_SOURCE"\n'
        '  fi\n'
        '  count=0; [[ -f "$UP_COUNT" ]] && count=$(<"$UP_COUNT"); count=$((count+1)); printf "%s" "$count" > "$UP_COUNT"; [[ "$count" == 2 && "$RECOVER" == 1 ]] && : > "$RECOVERED_FILE"; exit 0; fi\n'
        'if [[ "$1 ${2-}" == "compose stop" || "$1" == "run" ]]; then exit 0; fi\nexit 98\n'
    ); docker.chmod(0o755)
    timeout = fake_bin / "timeout"; timeout.write_text("#!/usr/bin/env bash\nshift 3\nexec \"$@\"\n"); timeout.chmod(0o755)
    budget_config, budget_ledger = tmp_path / "resource-config.json", tmp_path / "resource-ledger.jsonl"
    budget_config.write_text("{}")
    budget_ledger.write_text("started\n")
    proxy_path = tmp_path / "docker-meter.sock"; proxy = socket.socket(socket.AF_UNIX); proxy.bind(str(proxy_path)); proxy.listen(); os.chmod(proxy_path, 0o600)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "MYTHIC_CLI": str(cli),
           "CALL_LOG": str(log), "COMPOSE_JSON": json.dumps(config), "INITIAL_ROWS": initial_rows,
           "RECOVERED_ROWS": recovered_rows or initial_rows, "RECOVERED_FILE": str(recovered),
           "UP_COUNT": str(up_count), "RECOVER": "1" if recover else "0", "MISSING_IMAGE": missing_image,
           "SIMULATE_POSTGRES_INIT": "1" if simulate_postgres_init else "0",
           "POSTGRES_INIT_SOURCE": str(tmp_path / "container-pg_hba.conf"),
           "REMOVE_FAILURE": "1" if remove_failure else "0",
           "SAGE_RESET_METERED_DOCKER": "1", "SAGE_RESOURCE_BUDGET_CONFIG": str(budget_config),
           "SAGE_RESOURCE_BUDGET_LEDGER": str(budget_ledger), "DOCKER_HOST": f"unix://{proxy_path}",
           "SAGE_RESET_SKIP_DEPLOYMENT_ENFORCE": "1", "RABBITMQ_HOST": "container-only",
           "MYTHIC_SERVER_HOST": "container-only", "NGINX_HOST": "container-only",
           "COMPOSE_FILE": "/tmp/wrong.yml", "COMPOSE_PROJECT_NAME": "wrong-project"}
    return env, log, proxy


def test_sage_stop_accepts_absent_tmux_session_as_already_stopped(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux = fake_bin / "tmux"
    tmux.write_text(
        "#!/usr/bin/env bash\n"
        "[[ \"$1\" == \"has-session\" ]] && exit 1\n"
        "exit 99\n"
    )
    tmux.chmod(0o755)

    stopped = subprocess.run(
        ["/bin/bash", str(SAGE_STOP)],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert stopped.returncode == 0, stopped.stderr
    assert "already stopped" in stopped.stdout
    assert "is absent" in stopped.stdout


def test_sage_stop_retries_interrupt_during_stuck_python_finalization(tmp_path):
    ready = tmp_path / "ready"
    target = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, signal, time\n"
                "count = 0\n"
                "def interrupt(_signum, _frame):\n"
                "    global count\n"
                "    count += 1\n"
                "    if count >= 3:\n"
                "        raise SystemExit(0)\n"
                "signal.signal(signal.SIGINT, interrupt)\n"
                f"open({str(ready)!r}, 'w').close()\n"
                "while True:\n"
                "    time.sleep(0.1)\n"
            ),
        ]
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        call_count = tmp_path / "interrupt-count"
        tmux = fake_bin / "tmux"
        tmux.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "case \"$1\" in\n"
            "  has-session) exit 0 ;;\n"
            "  list-panes) echo 12345; exit 0 ;;\n"
            "  send-keys)\n"
            "    count=0\n"
            "    [[ -f \"$CALL_COUNT\" ]] && count=$(<\"$CALL_COUNT\")\n"
            "    count=$((count + 1))\n"
            "    printf '%s' \"$count\" > \"$CALL_COUNT\"\n"
            "    kill -INT \"$TARGET_PID\"\n"
            "    exit 0 ;;\n"
            "esac\n"
            "exit 1\n"
        )
        tmux.chmod(0o755)
        pgrep = fake_bin / "pgrep"
        pgrep.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$TARGET_PID\"\n")
        pgrep.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CALL_COUNT": str(call_count),
            "TARGET_PID": str(target.pid),
            "SAGE_STOP_INITIAL_WAIT_SECONDS": "1",
            "SAGE_STOP_RETRY_WAIT_SECONDS": "1",
            "SAGE_STOP_FINAL_WAIT_SECONDS": "1",
        }

        stopped = subprocess.run(
            ["/bin/bash", str(SAGE_STOP)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )

        assert stopped.returncode == 0, stopped.stderr
        assert call_count.read_text() == "3"
        assert "Stopped local Sage pid" in stopped.stdout
        assert target.wait(timeout=2) == 0
    finally:
        if target.poll() is None:
            target.terminate()
            target.wait(timeout=2)


def test_mythic_reset_drops_container_only_host_overrides(tmp_path):
    env, log = _mythic_reset_env(
        tmp_path,
        initial_rows=(
            "mythic_postgres|true|healthy\n"
            "apollo|true|none\nhttp|true|none\nsage|true|none"
        ),
        cli_start_message="dependency failed to start: container near_match is unhealthy",
    )

    subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"],
        cwd=ROOT,
        env=env,
        check=True,
    )

    cli_calls = [line for line in log.read_text().splitlines() if line.startswith("cli|")]
    assert cli_calls == [
        "cli|stop|unset|unset|unset|unset|unset",
        "cli|database reset -f|unset|unset|unset|unset|unset",
        "cli|start mythic_postgres apollo http|unset|unset|unset|unset|unset",
    ]
    docker_calls = [line for line in log.read_text().splitlines() if line.startswith("docker|")]
    assert len(docker_calls) == 3
    assert all(line.endswith(f"|unset|unset|{tmp_path}") for line in docker_calls)


def test_mythic_reset_recreates_postgres_image_files_on_repeated_wipes(tmp_path):
    rows = "mythic_postgres|true|healthy\napollo|true|none\nhttp|true|none\nsage|true|none"
    env, log = _mythic_reset_env(tmp_path, initial_rows=rows, simulate_postgres_init=True)
    (tmp_path / "postgres-docker/database").mkdir(parents=True)

    for _ in range(2):
        result = subprocess.run(
            ["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
            text=True, capture_output=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "container-pg_hba.conf").exists()

    calls = log.read_text().splitlines()
    removes = [i for i, call in enumerate(calls) if "docker|compose rm --force mythic_postgres|" in call]
    starts = [i for i, call in enumerate(calls) if call.startswith("cli|start ")]
    assert len(removes) == len(starts) == 2
    assert all(remove < start for remove, start in zip(removes, starts))
    assert not any("compose rm" in call and "--volumes" in call for call in calls)


def test_mythic_reset_stops_before_wipe_when_postgres_removal_fails(tmp_path):
    rows = "mythic_postgres|true|healthy\napollo|true|none\nhttp|true|none\nsage|true|none"
    env, log = _mythic_reset_env(tmp_path, initial_rows=rows, docker_failure="rm")
    (tmp_path / "postgres-docker/database").mkdir(parents=True)

    result = subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=10,
    )

    assert result.returncode == 1
    assert "unable to remove stopped Mythic postgres container" in result.stderr
    calls = log.read_text().splitlines()
    assert any(call.startswith("cli|stop|") for call in calls)
    assert not any(call.startswith("docker|run ") or call.startswith("cli|start ") for call in calls)


def test_local_reset_stops_unexpected_sage_container_after_start_failure(tmp_path):
    env, log = _mythic_reset_env(
        tmp_path,
        initial_rows="mythic_postgres|true|healthy\napollo|true|none\nhttp|true|none\nsage|true|none",
        cli_start_failure=True,
        sage_running_on_start=True,
        enforce_deployment=True,
    )

    result = subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=10,
    )

    assert result.returncode == 7, result.stderr
    calls = log.read_text().splitlines()
    assert any(line.startswith("cli|start mythic_postgres apollo http|") for line in calls)
    assert any(line.startswith("cli|stop sage|") for line in calls)
    assert not (tmp_path / "sage-running").exists()


def test_container_mode_retains_sage_in_full_start(tmp_path):
    env, log = _mythic_reset_env(
        tmp_path,
        initial_rows="mythic_postgres|true|healthy\napollo|true|none\nhttp|true|none\nsage|true|none",
    )
    env["SAGE_DEPLOYMENT_MODE"] = "container"

    result = subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert any(line.startswith("cli|start|") for line in log.read_text().splitlines())


def test_local_reset_rejects_sage_only_compose_before_mutation(tmp_path):
    env, log = _mythic_reset_env(tmp_path, initial_rows="sage|true|none", services="sage")

    result = subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=10,
    )

    assert result.returncode == 1
    assert "no services for the selected deployment mode" in result.stderr
    assert not any(line.startswith("cli|") for line in log.read_text().splitlines())


def test_mythic_reset_fails_closed_when_cli_start_hides_failed_services(tmp_path):
    failed_rows = (
        "mythic_postgres|true|unhealthy\n"
        "apollo|false|none\nhttp|false|none\nsage|true|none"
    )
    env, log = _mythic_reset_env(tmp_path, initial_rows=failed_rows)

    result = subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert log.read_text().count(
        "cli|start mythic_postgres apollo http|unset|unset|unset|unset|unset"
    ) == 2
    assert "not ready after targeted recovery" in result.stderr


def test_mythic_reset_recovers_only_failed_configured_services_once(tmp_path):
    failed_rows = (
        "mythic_postgres|true|unhealthy\n"
        "apollo|false|none\nhttp|false|none\nsage|true|none"
    )
    recovered_rows = (
        "mythic_postgres|true|healthy\n"
        "apollo|true|none\nhttp|true|none\nsage|true|none"
    )
    env, log = _mythic_reset_env(
        tmp_path,
        initial_rows=failed_rows,
        recovered_rows=recovered_rows,
        recover_on_targeted_start=True,
    )

    result = subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text().count(
        "cli|start mythic_postgres apollo http|unset|unset|unset|unset|unset"
    ) == 1


def test_mythic_reset_does_not_accept_healthy_near_match_service(tmp_path):
    env, _ = _mythic_reset_env(
        tmp_path,
        initial_rows=(
            "mythic_postgres|true|healthy\n"
            "apollo-old|true|none\nhttp|true|none\nsage|true|none"
        ),
    )

    result = subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 1


def test_mythic_reset_fails_closed_when_container_inspection_fails(tmp_path):
    env, log = _mythic_reset_env(
        tmp_path,
        initial_rows="ignored",
        docker_failure="inspect",
    )

    result = subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "unable to inspect" in result.stderr
    assert sum(line.startswith("cli|start ") for line in log.read_text().splitlines()) == 1


def test_mythic_reset_fails_closed_when_bounded_docker_call_times_out(tmp_path):
    env, log = _mythic_reset_env(
        tmp_path,
        initial_rows="ignored",
        timeout_failure="30s",
    )

    result = subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "unable to enumerate configured" in result.stderr
    assert not any(line.startswith("cli|start ") for line in log.read_text().splitlines())


def test_mythic_reset_fails_closed_when_targeted_start_times_out(tmp_path):
    env, log = _mythic_reset_env(
        tmp_path,
        initial_rows=(
            "mythic_postgres|true|unhealthy\n"
            "apollo|false|none\nhttp|false|none\nsage|true|none"
        ),
        timeout_failure="180s",
    )

    result = subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 124
    assert log.read_text().count("timeout|-k 5s 180s ") == 1
    assert sum(line.startswith("cli|start ") for line in log.read_text().splitlines()) == 1


def test_metered_docker_reset_preflights_images_before_stop_and_never_calls_cli(tmp_path):
    rows = "mythic_postgres|true|healthy\napollo|true|none\nhttp|true|none\nsage|true|none"
    env, log, proxy = _metered_reset_env(tmp_path, initial_rows=rows)
    try:
        result = subprocess.run(["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=10)
    finally:
        proxy.close()
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert not any(value.startswith("cli|") for value in calls)
    docker = [value for value in calls if value.startswith("docker|")]
    assert sum("image inspect" in value for value in docker) == 4
    assert any("compose stop" in value for value in docker)
    assert any("run --rm --pull never" in value for value in docker)
    assert sum("compose up -d --no-build --pull never mythic_postgres apollo http" in value for value in docker) == 1
    assert all(f"unix://{tmp_path / 'docker-meter.sock'}|unset|unset" in value for value in docker)


def test_metered_reset_recreates_postgres_without_rebuilding_or_removing_volumes(tmp_path):
    rows = "mythic_postgres|true|healthy\napollo|true|none\nhttp|true|none\nsage|true|none"
    env, log, proxy = _metered_reset_env(tmp_path, initial_rows=rows, simulate_postgres_init=True)
    try:
        for _ in range(2):
            result = subprocess.run(
                ["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=10,
            )
            assert result.returncode == 0, result.stderr
            assert not (tmp_path / "container-pg_hba.conf").exists()
    finally:
        proxy.close()

    calls = log.read_text().splitlines()
    removes = [i for i, call in enumerate(calls) if "docker|compose rm --force mythic_postgres|" in call]
    starts = [i for i, call in enumerate(calls) if "docker|compose up -d --no-build --pull never" in call]
    assert len(removes) == len(starts) == 2
    assert all(remove < start for remove, start in zip(removes, starts))
    assert not any("compose rm" in call and "--volumes" in call for call in calls)


def test_metered_reset_stops_before_wipe_when_postgres_removal_fails(tmp_path):
    rows = "mythic_postgres|true|healthy\napollo|true|none\nhttp|true|none\nsage|true|none"
    env, log, proxy = _metered_reset_env(tmp_path, initial_rows=rows, remove_failure=True)
    try:
        result = subprocess.run(
            ["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
            text=True, capture_output=True, timeout=10,
        )
    finally:
        proxy.close()

    assert result.returncode == 1
    assert "unable to remove stopped Mythic postgres container" in result.stderr
    calls = log.read_text().splitlines()
    assert any("compose stop" in call for call in calls)
    assert not any("docker|run " in call or "compose up" in call for call in calls)


def test_metered_container_mode_keeps_sage_service(tmp_path):
    rows = "mythic_postgres|true|healthy\napollo|true|none\nhttp|true|none\nsage|true|none"
    env, log, proxy = _metered_reset_env(tmp_path, initial_rows=rows)
    env["SAGE_DEPLOYMENT_MODE"] = "container"
    try:
        result = subprocess.run(["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=10)
    finally:
        proxy.close()

    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert sum("image inspect" in value for value in calls) == 5
    assert any("compose up -d --no-build --pull never mythic_postgres apollo http sage" in value
               for value in calls)


def test_metered_docker_reset_missing_image_refuses_before_stop(tmp_path):
    rows = "mythic_postgres|true|healthy\napollo|true|none\nhttp|true|none\nsage|true|none"
    env, log, proxy = _metered_reset_env(tmp_path, initial_rows=rows, missing_image="mythic/http:test")
    try:
        result = subprocess.run(["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=10)
    finally:
        proxy.close()
    assert result.returncode == 1
    calls = log.read_text().splitlines()
    assert not any("compose stop" in value or "compose up" in value or "run --rm" in value for value in calls)


def test_metered_docker_reset_allows_one_targeted_recovery_only(tmp_path):
    failed = "mythic_postgres|true|unhealthy\napollo|false|none\nhttp|false|none\nsage|true|none"
    healthy = "mythic_postgres|true|healthy\napollo|true|none\nhttp|true|none\nsage|true|none"
    env, log, proxy = _metered_reset_env(tmp_path, initial_rows=failed, recovered_rows=healthy, recover=True)
    try:
        result = subprocess.run(["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=10)
    finally:
        proxy.close()
    assert result.returncode == 0, result.stderr
    assert log.read_text().count("compose up -d --no-build --pull never") == 2

    other = tmp_path / "failed"; other.mkdir()
    env, log, proxy = _metered_reset_env(other, initial_rows=failed, recover=False)
    try:
        result = subprocess.run(["/bin/bash", str(MYTHIC_RESET), "--yes"], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=10)
    finally:
        proxy.close()
    assert result.returncode == 1 and log.read_text().count("compose up -d --no-build --pull never") == 2
