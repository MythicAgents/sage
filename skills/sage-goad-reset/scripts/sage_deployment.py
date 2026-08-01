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
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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
    parser.add_argument("command", choices=("check", "enforce"))
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
