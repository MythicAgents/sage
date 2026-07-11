#!/usr/bin/env python3
"""Local dev harness: issue a Mythic task to the Sage callback and print its output.

Usage:
  python3 sage_task.py callbacks
  python3 sage_task.py task <command> [json-params] [timeout] [--verbose true]
  python3 sage_task.py task-callback <callback_id> <command> [json-params] [timeout] [--verbose true]

Examples:
  python3 sage_task.py task mcp-list
  python3 sage_task.py task chat '{"prompt":"Using BloodHound, list all domains it knows about."}' 240
"""
import sys
import json
import asyncio
import os
from pathlib import Path
from mythic import mythic

SERVER = "127.0.0.1"
USER = "mythic_admin"
MYTHIC_ENV_PATHS = (
    Path("/home/john/dev/mythic_v4/.env"),
    Path("/home/john/dev/mythic/.env"),
)


def resolve_password(env_paths: Path | tuple[Path, ...] = MYTHIC_ENV_PATHS) -> str:
    env_value = os.environ.get("MYTHIC_ADMIN_PASSWORD")
    if env_value:
        return env_value
    if isinstance(env_paths, Path):
        env_paths = (env_paths,)
    for env_path in env_paths:
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "MYTHIC_ADMIN_PASSWORD" and value.strip():
                return value.strip().strip("'\"")
    raise RuntimeError(
        "Set MYTHIC_ADMIN_PASSWORD or provide a Mythic .env with MYTHIC_ADMIN_PASSWORD."
    )


def strip_verbose_args(argv: list[str]) -> tuple[list[str], bool | None]:
    cleaned: list[str] = []
    explicit: bool | None = None
    idx = 0
    while idx < len(argv):
        item = argv[idx]
        if item == "--verbose":
            value = "true"
            if idx + 1 < len(argv) and not argv[idx + 1].startswith("--"):
                value = argv[idx + 1]
                idx += 1
            explicit = str(value).strip().casefold() not in {"0", "false", "no", "off"}
        elif item.startswith("--verbose="):
            _, _, value = item.partition("=")
            explicit = str(value).strip().casefold() not in {"0", "false", "no", "off"}
        else:
            cleaned.append(item)
        idx += 1
    return cleaned, explicit


def normalize_task_parameters(command: str, params, *, explicit_verbose: bool | None = None):
    """Force verbose Sage query/chat output at the local runner boundary."""
    if command not in {"query", "chat"}:
        return params
    if not isinstance(params, dict):
        params = {} if params in ("", None) else {"prompt": str(params)}
    normalized = dict(params)
    normalized["verbose"] = True
    return normalized


async def main():
    argv, explicit_verbose = strip_verbose_args(sys.argv[1:])
    mode = argv[0] if argv else "callbacks"
    client = await mythic.login(server_ip=SERVER, username=USER, password=resolve_password())
    cbs = await mythic.get_all_active_callbacks(client)

    def ptname(c):
        return ((c.get("payload") or {}).get("payloadtype", {}) or {}).get("name") \
            or (c.get("payloadtype", {}) or {}).get("name")

    if mode == "callbacks":
        for c in cbs:
            print(f"  id={c.get('display_id')}  payloadtype={ptname(c)}  host={c.get('host')}  user={c.get('user')}  active={c.get('active')}")
        return

    if mode == "task-callback":
        sage_id = int(argv[1])
        command = argv[2]
        params = json.loads(argv[3]) if len(argv) > 3 and argv[3] else ""
        timeout = int(argv[4]) if len(argv) > 4 else 240
    else:
        override = os.environ.get("SAGE_CB")
        if override:
            sage_id = int(override)
        else:
            sage = [c for c in cbs if ptname(c) == "sage"]
            if not sage:
                print("ERROR: no active 'sage' callback found. All callbacks:")
                for c in cbs:
                    print("  ", c.get("display_id"), ptname(c))
                return
            sage_id = sage[0]["display_id"]
        command = argv[1]
        params = json.loads(argv[2]) if len(argv) > 2 and argv[2] else ""
        timeout = int(argv[3]) if len(argv) > 3 else 240

    params = normalize_task_parameters(command, params, explicit_verbose=explicit_verbose)
    print(f"[*] Sage callback display_id={sage_id} | issuing '{command}' params={params}")
    task = await mythic.issue_task(
        client,
        command_name=command,
        parameters=params,
        callback_display_id=sage_id,
        wait_for_complete=False,
    )
    task_id = task["display_id"]
    print(f"[*] task display_id={task_id} issued; waiting up to {timeout}s for output...")
    final = await mythic.waitfor_for_task_output(client, task_display_id=task_id, timeout=timeout)
    if isinstance(final, (bytes, bytearray)):
        final = final.decode(errors="replace")
    print("\n----- FINAL OUTPUT -----")
    print(final)


if __name__ == "__main__":
    asyncio.run(main())
