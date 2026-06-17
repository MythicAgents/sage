#!/usr/bin/env python3
"""Advisor-mandated dry-call: exercise the NEW stage_file_to_disk(callback_display_id=...) against
LIVE Mythic (not mocked), proving the filemeta join + download + /tmp write before any 16-min run.
"""
import asyncio, sys, os, json
from pathlib import Path
sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
import importlib  # noqa: E402
mt_mod = importlib.import_module("ai.langgraph.mythic_tools")

OUT_DIR = Path(os.environ.get("SAGE_TRACE_OUTPUT_DIR", "/tmp/sage-trace-analysis"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = str(OUT_DIR / "drycall_stage.out")
lines = []
def w(s=""): lines.append(str(s))

async def main():
    client = await login_to_mythic(resolve_password())
    mt = mt_mod.MythicTools("drycall-dummy-task")
    mt.client = client

    # 1) resolver alone (cb28 had the run-#4 essos collection 136a6f7c)
    for cbid in [28, 29, 22, 99]:
        row = await mt._latest_download_for_callback(cbid)
        w(f"_latest_download_for_callback({cbid}) -> {row}")

    # 2) full stage by callback id (the operator's real call)
    w("\n--- stage_file_to_disk(callback_display_id=28) ---")
    res = await mt.stage_file_to_disk(callback_display_id=28)
    w(res)
    try:
        d = json.loads(res)
        p = d.get("path")
        if p and os.path.exists(p):
            w(f"FILE ON DISK: {p} size={os.path.getsize(p)} bytes (status={d.get('status')}, resolved_by={d.get('resolved_by')}, src={d.get('source_filename')})")
        else:
            w(f"PATH MISSING ON DISK: {p}")
    except Exception as e:
        w(f"parse/disk-check error: {e}")

    # 3) error path: neither arg
    w("\n--- stage_file_to_disk() neither arg ---")
    w(await mt.stage_file_to_disk())

asyncio.run(main())
with open(OUT, "w") as f:
    f.write("\n".join(lines))
print("WROTE", OUT)
