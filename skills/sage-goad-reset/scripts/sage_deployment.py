#!/usr/bin/env python3
"""Enforce that exactly one Sage serves Mythic: the local process or the Docker container.

`mythic-cli start` starts **every** registered service, so resetting Mythic brings the Sage
container up whether or not it is wanted. If a tmux Sage is also running, both register as the
`sage` service, one wins the RabbitMQ queue, and Mythic requests are answered by whichever won.

That is not a theoretical race. On 2026-08-01 the container answered an autonomous chat request
using its baked image rather than the working tree, and failed with `BloodHound MCP is not
connected` because it had no `SAGE_BLOODHOUND_MCP_DIR`. The losing tmux process logged
`Another instance of this service, sage, is running` on a loop, and the readiness contract still
reported `ready: true` because nothing looked for a second instance.

Mode comes from `--mode`, else `SAGE_DEPLOYMENT_MODE`, else `local` — the mode this repo's
workflow uses (see `AGENTS.md`: Sage runs locally in the `sage` tmux session).

  check    report status; exit 0 when exactly the intended Sage is up
  enforce  stop the unintended side, then re-check
  parity   report whether the container is running the working tree (container mode only)
  deploy   sync the working tree into Mythic, restart the container, then prove parity

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


def _restart_container() -> dict:
    result = subprocess.run(
        ["docker", "restart", readiness_contract.SAGE_CONTAINER_NAME],
        capture_output=True, text=True, timeout=300, check=False,
    )
    return {
        "action": "restart-sage-container",
        "returncode": result.returncode,
        "stderr_tail": result.stderr.strip()[-400:],
    }


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("check", "enforce", "parity", "deploy"))
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
    args = parser.parse_args(argv)

    mode = readiness_contract.resolve_sage_deployment_mode(args.mode)

    if args.command == "parity":
        result = _parity()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ready"] else 1

    if args.command == "deploy":
        # Deliberately explicit. Container mode is the exception, not the default, and a deploy
        # that ran because SAGE_DEPLOYMENT_MODE happened to be unset would replace the installed
        # tree while the operator believed they were working in tmux.
        if mode != "container":
            raise SystemExit(
                "ERR: deploy targets the container, but the resolved mode is "
                f"{mode!r}. Pass --mode container (or set SAGE_DEPLOYMENT_MODE=container) to say "
                "so explicitly; local development uses sage_restart.sh instead."
            )
        actions = [_sync_into_mythic()]
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

        actions.append(_restart_container())
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

    if args.command == "enforce" and not status["ready"]:
        # Only ever stop the side that is not wanted. A missing intended Sage is reported, never
        # started here: starting one belongs to sage_restart.sh or mythic-cli, which own its env.
        if mode == "local" and status["container_running"]:
            actions.append(_stop_container())
        elif mode == "container" and status["local_process_running"]:
            actions.append(_stop_local())
        status = _status()

    print(json.dumps({**status, "actions": actions}, indent=2, sort_keys=True))
    return 0 if status["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
