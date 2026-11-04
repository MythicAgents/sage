#!/usr/bin/env python3
"""Enforce that exactly one Sage serves Mythic: the local process or the Docker container.

`mythic-cli start` starts **every** registered service, so resetting Mythic brings the Sage
container up whether or not it is wanted. If a tmux Sage is also running, both register as the
`sage` service, one wins the RabbitMQ queue, and Mythic requests are answered by whichever won.

If the container wins, it answers chat requests from its baked image rather than the working tree,
and fails with `BloodHound MCP is not connected` when it has no `SAGE_BLOODHOUND_MCP_DIR`. The
losing tmux process logs `Another instance of this service, sage, is running` on a loop, and a
readiness check that does not look for a second instance still reports `ready: true`.

Mode comes from `--mode`, else `SAGE_DEPLOYMENT_MODE`, else `local` — the mode this repo's
workflow uses (see `AGENTS.md`: Sage runs locally in the `sage` tmux session).

  check     report status; exit 0 when exactly the intended Sage is up
  enforce   stop the unintended side, then re-check (needs an explicit mode)
  stop-all  stop every Sage, local and container (the reset's first phase)
  parity    container: is it running the working tree? published: is it the config.json image running
            that image's own code?
  deploy    container: sync the working tree, start the container from it, prove parity.
            published: pull config.json's image, install its own code, start it, prove its identity

## Why `deploy` and `parity` exist

Container mode is opt-in and rare: the default workflow is the local tmux process, and a container
deploy should only happen when explicitly asked for. But when it does happen, one property is
invisible to the eye and has now failed twice — **is the container actually running the code you
just wrote?**

The Docker image is NOT the answer to that question. Mythic's generated compose file bind-mounts the
installed service directory over `/Mythic/`, which shadows the application code the image baked in.
So at runtime the image supplies only the Python environment (site-packages, outside `/Mythic/`),
while the code comes from the host directory, live. Two consequences:

  * Editing Python needs a directory sync plus a container restart. It does NOT need an image
    rebuild, and rebuilding to pick up a code change changes a layer nothing reads.
  * Rebuilding IS required when `requirements.txt`, `constraints.txt`, or the `Dockerfile` change,
    because those produce the environment that is not shadowed.

`parity` therefore asks the container, not the host, and checks two separate things: that the files
it can see match the working tree, and that none of them is newer than the process that imported
them. A Python process does not reload changed modules, so matching files with an older container
start time still means stale code is executing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

import readiness_contract  # noqa: E402


def _mythic_cli() -> Path:
    """`MYTHIC_CLI` only. A checkout-name guess would drive the wrong install where two exist."""
    value = os.environ.get("MYTHIC_CLI", "").strip()
    if not value:
        raise SystemExit(
            "ERR: MYTHIC_CLI is not set. Point it at your mythic-cli (see .env.example)."
        )
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SystemExit(f"ERR: MYTHIC_CLI is not executable: {path}")
    return path


def _stop_container() -> dict:
    cli = _mythic_cli()
    # Sage's own .env carries Docker service hostnames that are valid inside the container and
    # poison mythic-cli when inherited here, the same reason mythic_reset.sh strips them.
    env = {k: v for k, v in os.environ.items()
           if k not in ("RABBITMQ_HOST", "MYTHIC_SERVER_HOST", "NGINX_HOST")}
    result = subprocess.run(
        [str(cli), "stop", readiness_contract.SAGE_CONTAINER_NAME],
        cwd=str(cli.parent), env=env, capture_output=True, text=True, timeout=300, check=False,
    )
    return {
        "action": "stop-sage-container",
        "returncode": result.returncode,
        "stdout_tail": result.stdout.strip()[-400:],
        "stderr_tail": result.stderr.strip()[-400:],
    }


# Runtime code only. Tests, evals and archived databases are not what the container executes, and
# hashing them would make parity fail for edits that cannot change its behaviour.
_RUNTIME_PATHS = ("ai", "sage_chat", "prompts", "main.py", "mcp_tool_policy.json")

# Never sync: archived/live databases (the container writes its own into this directory, and rsync
# --delete deliberately does NOT remove excluded files on the receiver, so the live one survives),
# bytecode, and developer-local virtualenvs.
_SYNC_EXCLUDES = (
    "sage*.db", "sage*.db-*", "sage*.db.zst", "__pycache__", ".venv", ".phoenix",
    # `.env` is OPERATOR-OWNED state in the installed copy, exactly like the live database above.
    # Sage ships it tracked and inert precisely so a Mythic operator can open it from the web UI,
    # fill in credentials, and restart — no shell, no `docker cp`. Syncing the repo's blank copy
    # over it deletes that configuration on every deploy, and silently: the container comes back
    # up, chat still answers, and BloodHound is simply gone. Seeded on first deploy below, so a
    # fresh install still receives the documented template.
    ".env", ".env.local",
    # Engagement evidence, same reason as the live database above: the container writes it, and
    # `decision_record.py` seals each record `0o444`. Because the container runs as root over the
    # bind mount, those files are root-owned and read-only on the host, so mirroring a developer's
    # own `.sage_engagement/` over them fails the whole sync with rsync code 23 and no deploy
    # happens at all. Evidence belongs to the deployment that produced it, never to the code sync.
    # The glob is load-bearing: archived engagements sit beside the live one as
    # `.sage_engagement_<stamp>`, and `--delete` would try to unlink those sealed files too, which
    # fails the sync just as hard as overwriting them.
    ".sage_engagement*",
)

# One program, run on both sides, so the two digests cannot disagree through implementation drift.
# Sorted relative paths keep it order-independent; the content hash makes it exact.
_DIGEST_PROGRAM = """
import hashlib, os, sys
root = sys.argv[1]
names = sys.argv[2:]
h = hashlib.sha256()
files = []
for name in names:
    target = os.path.join(root, name)
    if os.path.isfile(target):
        files.append(name)
    for base, dirs, found in os.walk(target):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(found):
            if f.endswith((".py", ".md", ".json")):
                files.append(os.path.relpath(os.path.join(base, f), root))
newest = 0.0
for rel in sorted(set(files)):
    full = os.path.join(root, rel)
    h.update(rel.encode())
    with open(full, "rb") as fh:
        h.update(hashlib.sha256(fh.read()).digest())
    newest = max(newest, os.path.getmtime(full))
print(h.hexdigest(), len(set(files)), repr(newest))
"""


def _payload_source_dir() -> Path:
    """The service directory inside this repo, derived from this file's own location."""
    return REPO_ROOT / "Payload_Type" / readiness_contract.SAGE_CONTAINER_NAME


def _installed_service_dir() -> Path:
    """Mythic's copy of the service, which the compose file bind-mounts into the container.

    Derived from `MYTHIC_ENV_PATH` rather than guessed: on a machine with two Mythic checkouts a
    name guess silently deploys into the wrong install, and a refusal is strictly more useful than
    a wrong default.
    """
    value = os.environ.get("MYTHIC_ENV_PATH", "").strip()
    if not value:
        raise SystemExit(
            "ERR: MYTHIC_ENV_PATH is not set. Point it at your Mythic .env (see .env.example)."
        )
    mythic_root = Path(value).expanduser().resolve().parent
    installed = mythic_root / "InstalledServices" / readiness_contract.SAGE_CONTAINER_NAME
    if not installed.is_dir():
        raise SystemExit(
            f"ERR: {installed} does not exist. Install the service into Mythic first "
            f"(`mythic-cli install folder <repo> -f` from the Mythic directory)."
        )
    return installed


def _digest_local(root: Path) -> tuple[str, int, float]:
    result = subprocess.run(
        [sys.executable, "-c", _DIGEST_PROGRAM, str(root), *_RUNTIME_PATHS],
        capture_output=True, text=True, timeout=300, check=True,
    )
    digest, count, newest = result.stdout.split()
    return digest, int(count), float(newest.strip("'"))


def _digest_container() -> tuple[str, int, float]:
    result = subprocess.run(
        ["docker", "exec", readiness_contract.SAGE_CONTAINER_NAME,
         "python", "-c", _DIGEST_PROGRAM, "/Mythic", *_RUNTIME_PATHS],
        capture_output=True, text=True, timeout=300, check=True,
    )
    digest, count, newest = result.stdout.split()
    return digest, int(count), float(newest.strip("'"))


def _container_started_at() -> float:
    """Epoch seconds when the running container started, or 0.0 if it is not running."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", readiness_contract.SAGE_CONTAINER_NAME],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode != 0:
        return 0.0
    stamp = result.stdout.strip()
    # Docker emits RFC3339 with nanosecond precision, which datetime cannot parse; trim to micros.
    if "." in stamp:
        head, _, tail = stamp.partition(".")
        stamp = f"{head}.{tail[:6].rstrip('Z')}+00:00"
    else:
        stamp = stamp.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return 0.0


def _parity() -> dict:
    """Does the container execute this working tree? Two independent ways to answer no."""
    blockers: list[str] = []
    source = _payload_source_dir()
    repo_digest, repo_files, repo_newest = _digest_local(source)

    try:
        container_digest, container_files, container_newest = _digest_container()
    except subprocess.CalledProcessError as exc:
        return {
            "schema": "sage-container-parity-v1",
            "ready": False,
            "blockers": [f"could not read code from the container: {exc.stderr.strip()[-300:]}"],
            "repo_digest": repo_digest,
        }

    if repo_digest != container_digest:
        blockers.append(
            f"the container's code differs from the working tree "
            f"(repo {repo_files} files / {repo_digest[:12]}, "
            f"container {container_files} files / {container_digest[:12]}); run `deploy`"
        )

    started = _container_started_at()
    if not started:
        blockers.append("the Sage container is not running")
    elif container_newest > started:
        # Files can match byte-for-byte and the PROCESS still be stale: Python imports once at
        # startup, so a file written after the container started is not the code in memory.
        blockers.append(
            "the container's code is newer than the running process; it imported an older "
            "version — restart it"
        )

    return {
        "schema": "sage-container-parity-v1",
        "ready": not blockers,
        "blockers": blockers,
        "repo_digest": repo_digest,
        "container_digest": container_digest,
        "repo_files": repo_files,
        "container_files": container_files,
        "container_started_at": started,
        "container_code_mtime": container_newest,
    }


def _sync_into_mythic() -> dict:
    """Mirror the working tree into Mythic's installed copy, which the container mounts."""
    source = _payload_source_dir()
    destination = _installed_service_dir()
    command = ["rsync", "-a", "--delete"]
    for pattern in _SYNC_EXCLUDES:
        command += ["--exclude", pattern]
    # Trailing slashes: copy the CONTENTS of source into destination, not source itself.
    command += [f"{source}/", f"{destination}/"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
    seeded = _seed_missing_env_files(source, destination)
    return {
        "action": "sync-working-tree-into-mythic",
        "returncode": result.returncode,
        "stderr_tail": result.stderr.strip()[-400:],
        "seeded_env_files": seeded,
    }


def _seed_missing_env_files(source: Path, destination: Path) -> list:
    """Copy an excluded env file only when the installed copy does not have one yet.

    Excluding `.env` from the sync protects an operator's edits, but on a FIRST deploy it would
    leave the container with no env file at all — and the shipped one is not merely a template: it
    carries `LANGGRAPH_STRICT_MSGPACK=true`, which is a security default that is unsafe when absent
    rather than merely unset. Seeding when absent keeps both properties, and never overwrites.
    """
    seeded = []
    for name in (".env",):
        src = source / name
        dst = destination / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            seeded.append(name)
    return seeded


def _published_image() -> str:
    """The image `mythic-cli install` would pull for this version: config.json's remote_images entry."""
    config = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    image = str((config.get("remote_images") or {}).get(readiness_contract.SAGE_CONTAINER_NAME) or "").strip()
    if not image:
        raise SystemExit("ERR: config.json has no remote_images.sage entry")
    return image


def _mythic_settings(mode: str) -> tuple[tuple[str, str], ...]:
    """Mythic settings each container mode needs.

    Both keep `sage_use_volume=false`, so Mythic bind-mounts the installed directory over /Mythic/ and
    the databases stay on the host. (A volume would hold them instead, and `mythic-cli start` without
    --keep-volume deletes it under REBUILD_ON_START.) `container` builds the image from the installed
    copy of the working tree; `published` uses config.json's image, and `deploy` copies that image's own
    code into the installed directory so the bind mount does not shadow it with the working tree.
    """
    if mode == "published":
        return (("sage_use_build_context", "false"), ("sage_remote_image", _published_image()),
                ("sage_use_volume", "false"))
    return (("sage_use_build_context", "true"), ("sage_use_volume", "false"))

#: Sage's own .env carries Docker hostnames valid only inside the container; they poison host-side
#: mythic-cli and docker compose (the same reason mythic_reset.sh strips them).
_HOST_SIDE_STRIP = ("RABBITMQ_HOST", "MYTHIC_SERVER_HOST", "NGINX_HOST", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME")


def _host_side_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _HOST_SIDE_STRIP}


def _mythic_dir() -> Path:
    """The Mythic install both variables name. AGENTS.md: MYTHIC_ENV_PATH and MYTHIC_CLI must identify
    the same install; otherwise deploy syncs into one install's copy and starts the other's container."""
    cli_dir = _mythic_cli().resolve().parent
    value = os.environ.get("MYTHIC_ENV_PATH", "").strip()
    if not value:
        raise SystemExit("ERR: MYTHIC_ENV_PATH is not set. Point it at your Mythic .env (see .env.example).")
    env_dir = Path(value).expanduser().resolve().parent
    if cli_dir != env_dir:
        raise SystemExit(
            f"ERR: MYTHIC_CLI ({cli_dir}) and MYTHIC_ENV_PATH ({env_dir}) name different Mythic installs; "
            "point both at the same one."
        )
    return cli_dir


def _run_host_side(command: list[str], action: str, timeout: int = 900) -> dict:
    result = subprocess.run(
        command, cwd=str(_mythic_dir()), env=_host_side_env(),
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return {
        "action": action,
        "returncode": result.returncode,
        "stdout_tail": result.stdout.strip()[-400:],
        "stderr_tail": result.stderr.strip()[-400:],
    }


def _mythic_env_values() -> dict:
    from dotenv import dotenv_values

    value = os.environ.get("MYTHIC_ENV_PATH", "").strip()
    if not value:
        raise SystemExit("ERR: MYTHIC_ENV_PATH is not set. Point it at your Mythic .env (see .env.example).")
    return {str(k).casefold(): str(v or "") for k, v in dotenv_values(Path(value).expanduser()).items()}


def _ensure_mythic_settings(mode: str) -> dict:
    """Make Mythic's generated compose entry for Sage match the mode, and say what changed.

    `mythic-cli add sage` regenerates the compose entry from Mythic's .env; it is how a settings
    change takes effect without a `mythic-cli start`, which would rebuild the image.
    """
    def mismatched() -> list[tuple[str, str]]:
        current = _mythic_env_values()
        return [(key, value) for key, value in _mythic_settings(mode)
                if current.get(key, "").strip().strip("'\"").casefold() != value.casefold()]

    changed = mismatched()
    actions = []
    for key, value in changed:
        actions.append(_run_host_side([str(_mythic_cli()), "config", "set", key, value], f"set-{key}", 120))
        if actions[-1]["returncode"] != 0:
            return {"action": "ensure-mythic-settings", "returncode": 1, "changed": changed, "steps": actions}
    # `config set` exits 0 even when it finds no matching key, so read the file back.
    still_wrong = mismatched()
    if still_wrong:
        return {
            "action": "ensure-mythic-settings", "returncode": 1, "changed": [key for key, _ in changed],
            "not_applied": [f"{key}={value}" for key, value in still_wrong], "steps": actions,
        }
    # Always regenerate: Mythic's .env is not the compose file, and an entry left behind by an earlier
    # mode can name another image while parity (which hashes code only) still passes.
    actions.append(_run_host_side([str(_mythic_cli()), "add", readiness_contract.SAGE_CONTAINER_NAME],
                                  "regenerate-compose-entry", 120))
    return {
        "action": "ensure-mythic-settings",
        "returncode": max([0, *(step["returncode"] for step in actions)]),
        "changed": [key for key, _ in changed],
        "steps": actions,
    }


def _image_present(image: str) -> bool:
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True,
                            timeout=60, check=False)
    return result.returncode == 0


def _pull_image(image: str) -> dict:
    result = subprocess.run(["docker", "pull", image], env=_host_side_env(), capture_output=True,
                            text=True, timeout=1800, check=False)
    return {"action": "pull-published-image", "image": image, "returncode": result.returncode,
            "stderr_tail": result.stderr.strip()[-400:]}


def _extract_image_code(image: str, destination: Path) -> dict:
    """Copy the image's own /Mythic/ out of a created (never started) container with no mounts."""
    name = f"sage-published-extract-{os.getpid()}"
    created = subprocess.run(["docker", "create", "--name", name, image], capture_output=True, text=True,
                             timeout=300, check=False)
    if created.returncode != 0:
        return {"action": "extract-published-code", "returncode": created.returncode,
                "stderr_tail": created.stderr.strip()[-400:]}
    try:
        copied = subprocess.run(["docker", "cp", f"{name}:/Mythic/.", str(destination)], capture_output=True,
                                text=True, timeout=900, check=False)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=120, check=False)
    return {"action": "extract-published-code", "returncode": copied.returncode,
            "stderr_tail": copied.stderr.strip()[-400:]}


#: Operator-owned directories in the installed copy. The image ships them empty (.dockerignore), so
#: mirroring the image's /Mythic/ with --delete would remove the operator's TLS bundle and tools.
_OPERATOR_DIRS = ("certs", "tools")


def _sync_published_code(extracted: Path) -> dict:
    """Make the installed copy run the image's code while keeping what the operator and runtime own.

    Same excludes as the working-tree sync (databases, .env, engagement evidence), plus the operator
    directories, which are then refreshed from the working tree exactly as a container deploy would.
    """
    destination = _installed_service_dir()
    # rsync -a copies the source directory's own mode onto the destination; the scratch directory is
    # 0700, which would lock group and other users out of the installed service directory.
    Path(extracted).chmod(destination.stat().st_mode & 0o7777)
    command = ["rsync", "-a", "--delete"]
    for pattern in (*_SYNC_EXCLUDES, *(f"{name}/" for name in _OPERATOR_DIRS)):
        command += ["--exclude", pattern]
    result = subprocess.run([*command, f"{Path(extracted)}/", f"{destination}/"], capture_output=True,
                            text=True, timeout=1800, check=False)
    steps = [{"action": "sync-published-code", "returncode": result.returncode,
              "stderr_tail": result.stderr.strip()[-400:]}]
    for name in _OPERATOR_DIRS:
        source = _payload_source_dir() / name
        if result.returncode == 0 and source.is_dir():
            copied = subprocess.run(["rsync", "-a", f"{source}/", f"{destination / name}/"], capture_output=True,
                                    text=True, timeout=600, check=False)
            steps.append({"action": f"copy-operator-{name}", "returncode": copied.returncode,
                          "stderr_tail": copied.stderr.strip()[-400:]})
    seeded = _seed_missing_env_files(_payload_source_dir(), destination) if result.returncode == 0 else []
    return {"action": "sync-published-code", "returncode": max(step["returncode"] for step in steps),
            "seeded_env_files": seeded, "steps": steps}


def _published_parity(image: str) -> dict:
    """Is the serving container the published image, running that image's own code?

    Three ways to answer no: the container runs a different image (a local build still named in the
    compose entry); the bind-mounted code differs from the code baked into the image (a working tree
    left in the installed copy); or the files are newer than the process that imported them.
    """
    name = readiness_contract.SAGE_CONTAINER_NAME
    blockers: list[str] = []
    image_id = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                              capture_output=True, text=True, timeout=60, check=False)
    running = subprocess.run(["docker", "inspect", "-f", "{{.Image}}", name],
                             capture_output=True, text=True, timeout=60, check=False)
    if image_id.returncode != 0:
        blockers.append(f"the published image {image} is not present locally; pull it")
    if running.returncode != 0:
        blockers.append("the Sage container is not running")
    elif image_id.returncode == 0 and running.stdout.strip() != image_id.stdout.strip():
        blockers.append(f"the running container is not the published image {image}")

    image_digest = container_digest = None
    if image_id.returncode == 0:
        baked = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python3", image,
             "-c", _DIGEST_PROGRAM, "/Mythic", *_RUNTIME_PATHS],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if baked.returncode == 0 and baked.stdout.split():
            image_digest = baked.stdout.split()[0]
        else:
            blockers.append(f"could not read the published image's code: {baked.stderr.strip()[-300:]}")
    container_newest = 0.0
    if running.returncode == 0:
        try:
            container_digest, _, container_newest = _digest_container()
        except subprocess.CalledProcessError as exc:
            blockers.append(f"could not read code from the container: {exc.stderr.strip()[-300:]}")
    if image_digest and container_digest and image_digest != container_digest:
        blockers.append(
            f"the container's code differs from the published image's code (container {container_digest[:12]}, "
            f"image {image_digest[:12]}); run `deploy --mode published`"
        )
    started = _container_started_at() if running.returncode == 0 else 0.0
    if container_digest and not started:
        blockers.append("could not read the container's start time, so staleness cannot be ruled out")
    elif container_digest and container_newest > started:
        blockers.append("the container's code is newer than the running process; restart it")
    return {
        "schema": "sage-published-identity-v1",
        "ready": not blockers,
        "blockers": blockers,
        "image": image,
        "image_id": image_id.stdout.strip() or None,
        "running_image_id": running.stdout.strip() or None,
        "image_digest": image_digest,
        "container_digest": container_digest,
        "container_started_at": started,
    }


def _start_container(*, rebuild: bool = False, image: str | None = None) -> dict:
    """Start Sage's container from the image already built, recreating the container.

    Not `docker restart`: `mythic-cli stop sage` removes the container, so there is often nothing to
    restart (2026-10-30). Not `mythic-cli start`: with Mythic's REBUILD_ON_START it runs
    `docker compose up --build`, which rebuilt the image and silently replaced the one under test
    (2026-11-03). The image is built only when it is missing or a rebuild is asked for, which is
    what a change to requirements, constraints or the Dockerfile needs.
    """
    name = readiness_contract.SAGE_CONTAINER_NAME
    steps = []
    if image is not None:
        # Published: never build. The image comes from the registry, pulled before anything changed.
        if not _image_present(image):
            return {"action": "start-sage-container", "returncode": 1,
                    "error": f"image {image} is not present locally", "steps": steps}
    elif rebuild or not _image_present(name):
        steps.append(_run_host_side([str(_mythic_cli()), "build", name], "build-sage-image", 3600))
        if steps[-1]["returncode"] != 0:
            return {"action": "start-sage-container", "returncode": steps[-1]["returncode"], "steps": steps}
    steps.append(_run_host_side(
        ["docker", "compose", "up", "-d", "--no-build", "--pull", "never", "--force-recreate", name],
        "compose-up-sage", 600,
    ))
    return {"action": "start-sage-container", "returncode": steps[-1]["returncode"], "steps": steps}


def _stop_local() -> dict:
    script = HERE / "sage_stop.sh"
    result = subprocess.run(
        ["/bin/bash", str(script)], capture_output=True, text=True, timeout=300, check=False
    )
    return {
        "action": "stop-local-sage",
        "returncode": result.returncode,
        "stdout_tail": result.stdout.strip()[-400:],
        "stderr_tail": result.stderr.strip()[-400:],
    }


#: A published deploy that stops after changing Mythic's settings can leave Mythic pointing at the
#: image while the installed copy still holds other code; the identity check reports that state.
_RERUN_HINT = ("re-run `deploy --mode published` once the failure is fixed, or `deploy --mode container` to go "
               "back; check the actions above for what changed")


def _deploy_published() -> int:
    """Run config.json's GHCR image with its own code. Pull and extract first: nothing changes until the
    image is here and readable."""
    _mythic_dir()  # refuse a split install before writing anything into either one
    image = _published_image()
    actions = [_pull_image(image)]
    with tempfile.TemporaryDirectory(prefix="sage-published-") as scratch:
        if actions[-1]["returncode"] == 0:
            actions.append(_extract_image_code(image, Path(scratch)))
        if actions[-1]["returncode"] == 0:
            actions.append(_ensure_mythic_settings("published"))
        if actions[-1]["returncode"] == 0:
            actions.append(_sync_published_code(Path(scratch)))
    if actions[-1]["returncode"] != 0:
        print(json.dumps({"ready": False, "actions": actions, "next": _RERUN_HINT}, indent=2, sort_keys=True))
        return 1
    deployment = readiness_contract.sage_deployment_status(
        mode="published", repo_root=REPO_ROOT, require_intended_running=False,
    )
    if deployment["local_process_running"]:
        actions.append(_stop_local())
    actions.append(_start_container(image=image))
    if actions[-1]["returncode"] != 0:
        print(json.dumps({"ready": False, "actions": actions, "next": _RERUN_HINT}, indent=2, sort_keys=True))
        return 1
    deadline = time.monotonic() + 90
    result = _published_parity(image)
    while not result["ready"] and time.monotonic() < deadline:
        time.sleep(3)
        result = _published_parity(image)
    final_deployment = readiness_contract.sage_deployment_status(
        mode="published", repo_root=REPO_ROOT, require_intended_running=True,
    )
    blockers = list(result["blockers"]) + [f"deployment: {b}" for b in final_deployment["blockers"]]
    payload = {**result, "ready": not blockers, "blockers": blockers,
               "sage_deployment": final_deployment, "actions": actions}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ready"] else 1


def _stop_all() -> int:
    """Stop every Sage, whichever kind is running. Mode-independent: the reset archives databases next,
    and no Sage of either kind may still be writing them."""
    actions = []
    if readiness_contract.probe_sage_container_running():
        actions.append(_stop_container())
    if readiness_contract.probe_local_sage_running(REPO_ROOT):
        actions.append(_stop_local())
    container_running = readiness_contract.probe_sage_container_running()
    local_running = readiness_contract.probe_local_sage_running(REPO_ROOT)
    blockers = []
    if container_running is None:
        blockers.append("could not determine whether the Sage container is running; docker did not answer")
    elif container_running:
        blockers.append("the Sage container is still running")
    if local_running:
        blockers.append("a local Sage process is still running")
    print(json.dumps({"ready": not blockers, "blockers": blockers, "actions": actions}, indent=2, sort_keys=True))
    return 0 if not blockers else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("check", "enforce", "stop-all", "parity", "deploy"))
    parser.add_argument(
        "--mode",
        choices=readiness_contract.SAGE_DEPLOYMENT_MODES,
        default=None,
        help="which Sage should serve Mythic (default: $SAGE_DEPLOYMENT_MODE, else local)",
    )
    parser.add_argument(
        "--conflict-only",
        action="store_true",
        help=(
            "require only that the UNINTENDED Sage is down, not that the intended one is up. "
            "This is the mid-reset state: Mythic restarts long before Sage does."
        ),
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="deploy: rebuild the image first (needed when requirements, constraints or the Dockerfile change)",
    )
    args = parser.parse_args(argv)

    if args.command == "stop-all":
        return _stop_all()

    mode = readiness_contract.resolve_sage_deployment_mode(args.mode)

    if args.command == "parity":
        result = _published_parity(_published_image()) if mode == "published" else _parity()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ready"] else 1

    if args.command == "deploy":
        # Deliberately explicit. Container mode is the exception, not the default, and a deploy
        # that ran because SAGE_DEPLOYMENT_MODE happened to be unset would replace the installed
        # tree while the operator believed they were working in tmux.
        if mode not in readiness_contract.CONTAINER_DEPLOYMENT_MODES:
            raise SystemExit(
                "ERR: deploy targets the container, but the resolved mode is "
                f"{mode!r}. Pass --mode container or --mode published (or set SAGE_DEPLOYMENT_MODE) to "
                "say so explicitly; local development uses sage_restart.sh instead."
            )
        if mode == "published":
            return _deploy_published()
        _mythic_dir()  # refuse a split install before writing anything into either one
        actions = [_sync_into_mythic()]
        if actions[-1]["returncode"] != 0:
            print(json.dumps({"ready": False, "actions": actions}, indent=2, sort_keys=True))
            return 1
        actions.append(_ensure_mythic_settings(mode))
        if actions[-1]["returncode"] != 0:
            print(json.dumps({"ready": False, "actions": actions}, indent=2, sort_keys=True))
            return 1

        # Stop the local Sage FIRST, before the container comes back up. Deploying into a split
        # brain is worse than not deploying: both processes register as the `sage` service, one wins
        # the RabbitMQ queue, and parity would then be measuring a container that is not the one
        # answering Mythic. `enforce` owns this rule; deploy must not reimplement or skip it.
        deployment = readiness_contract.sage_deployment_status(
            mode=mode, repo_root=REPO_ROOT, require_intended_running=False,
        )
        if deployment["local_process_running"]:
            actions.append(_stop_local())

        actions.append(_start_container(rebuild=args.rebuild))
        if actions[-1]["returncode"] != 0:
            print(json.dumps({"ready": False, "actions": actions}, indent=2, sort_keys=True))
            return 1
        # The restart is asynchronous; poll rather than assume, so parity does not read a container
        # that is still coming up and report a false mismatch.
        deadline = time.monotonic() + 90
        result = _parity()
        while not result["ready"] and time.monotonic() < deadline:
            time.sleep(3)
            result = _parity()

        # Re-read deployment AFTER the restart. Parity alone would happily go green on a container
        # that is not the process answering Mythic, so a deploy is only done when both hold.
        final_deployment = readiness_contract.sage_deployment_status(
            mode=mode, repo_root=REPO_ROOT, require_intended_running=True,
        )
        blockers = list(result["blockers"]) + [
            f"deployment: {b}" for b in final_deployment["blockers"]
        ]
        payload = {
            **result,
            "ready": not blockers,
            "blockers": blockers,
            "sage_deployment": final_deployment,
            "actions": actions,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1

    def _status() -> dict:
        return readiness_contract.sage_deployment_status(
            mode=mode,
            repo_root=REPO_ROOT,
            require_intended_running=not args.conflict_only,
        )

    status = _status()
    actions: list[dict] = []

    if args.command == "enforce" and not status["ready"] and not readiness_contract.deployment_mode_is_explicit(args.mode):
        # A defaulted `local` once stopped the container the operator meant to run, because container
        # mode had been exported in another shell. Report instead of guessing which Sage to stop.
        print(json.dumps({
            **status, "actions": [],
            "refused": "set SAGE_DEPLOYMENT_MODE (local, container or published) or pass --mode; "
                       "enforce stops a Sage only when told which one is intended",
        }, indent=2, sort_keys=True))
        return 1

    if args.command == "enforce" and not status["ready"]:
        # Only ever stop the side that is not wanted. A missing intended Sage is reported, never
        # started here: starting one belongs to sage_restart.sh or mythic-cli, which own its env.
        if mode == "local" and status["container_running"]:
            actions.append(_stop_container())
        elif mode in readiness_contract.CONTAINER_DEPLOYMENT_MODES and status["local_process_running"]:
            actions.append(_stop_local())
        status = _status()

    print(json.dumps({**status, "actions": actions}, indent=2, sort_keys=True))
    return 0 if status["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
