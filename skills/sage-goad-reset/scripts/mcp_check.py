#!/usr/bin/env python3
"""Throwaway (gitignored) — verify the BloodHound MCP is connected on Sage cb15; connect if not."""
import asyncio
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_BLOODHOUND_MCP_DIR = Path(
    os.environ.get("SAGE_BLOODHOUND_MCP_DIR")
    or (WORKSPACE_ROOT / "bloodhound_mcp")
)

sys.path.insert(0, str(REPO_ROOT / "Payload_Type" / "sage"))
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402

CONNECT_PARAMS = {
    "name": "BloodHound", "connection_type": "stdio", "command": "uv",
    "arguments": ["--directory", str(DEFAULT_BLOODHOUND_MCP_DIR), "run", "main.py"],
    "cwd": str(DEFAULT_BLOODHOUND_MCP_DIR), "url": "", "headers": [],
    "timeout": 30, "sse_read_timeout": 300, "terminate_on_close": True, "ssl_verify": True,
}


async def issue_and_read(client, command, params, wait=12):
    t = await mythic.issue_task(mythic=client, command_name=command,
                                parameters=params if isinstance(params, str) else json.dumps(params),
                                callback_display_id=1)
    tid = t.get("display_id")
    await asyncio.sleep(wait)
    out = await mythic.get_all_task_output_by_id(mythic=client, task_display_id=tid)
    import base64
    s = ""
    for o in out or []:
        rt = o.get("response_text", "") or ""
        try:
            s += base64.b64decode(rt).decode("utf-8", "replace")
        except Exception:
            s += str(rt)
    return tid, s


async def main():
    c = await login_to_mythic(resolve_password())
    tid, listing = await issue_and_read(c, "mcp-list", "")
    print(f"=== mcp-list (task {tid}) ===\n{listing[:1200]}")
    if "bloodhound" in listing.lower():
        print("\n>>> BloodHound MCP IS connected.")
    else:
        print("\n>>> BloodHound NOT in list — issuing mcp-connect...")
        ctid, cres = await issue_and_read(c, "mcp-connect", CONNECT_PARAMS, wait=15)
        print(f"=== mcp-connect (task {ctid}) ===\n{cres[:800]}")


if __name__ == "__main__":
    asyncio.run(main())
