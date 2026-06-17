#!/usr/bin/env python3
"""Throwaway (gitignored) — monitor autonomous run task 1866 to terminal status, logging
heartbeat + notable sage-pane events. Exits on completion or after ~45 min."""
import asyncio
import subprocess
import sys
import time

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from evals import phoenix_reader  # noqa: E402
from mythic import mythic  # noqa: E402

DB = "/home/john/dev/sage/Payload_Type/sage/.phoenix/phoenix.db"
TASK = 1910
KEYS = ["🛡️ ARGVAL", "av_hint", "ARGVAL rejected", "WINTERFELL", "KINGSLANDING", "BRAAVOS",
        "essos", "ESSOS", "Domain Admin", "jump_wmi", "jump_psexec", "create_payload",
        "merlin", "Merlin", "transfer_to", "DCSync", "dcsync", "LSASS", "lsass", "ADCS",
        "Certify", "shortest", "max_steps", "STOP —",
        "🛡️ OPSEC", "SAGE OPSEC", "SAGE HINT", "list_open_artifacts", "ARGVAL validated",
        "ARGVAL failed_open", "does not exist", "Final Report", "respond_to_user",
        "Recursion Limit", "ingest", "cypher", "GenericWrite", "GPLink", "SharpGPOAbuse",
        "AddLocalAdmin", "delegation", "LAPS", "ESC1", "shortest path", "load_open_artifacts"]


async def main():
    c = await login_to_mythic(resolve_password())
    seen = set()
    terminal = None
    for i in range(45):
        try:
            tasks = await mythic.get_all_tasks(mythic=c, custom_return_attributes="display_id status completed")
            st = next((t for t in (tasks or []) if t.get("display_id") == TASK), None)
        except Exception as e:
            st = {"status": f"poll-err:{e}", "completed": False}
        rid = phoenix_reader.max_trace_rowid(DB)
        try:
            pane = subprocess.run(["tmux", "capture-pane", "-t", "sage", "-p", "-S", "-3000"],
                                  capture_output=True, text=True).stdout
        except Exception:
            pane = ""
        for ln in pane.splitlines():
            if any(k in ln for k in KEYS) and ln not in seen:
                seen.add(ln)
                print(f"EVT {ln.strip()[-180:]}", flush=True)
        status = (st or {}).get("status")
        done = (st or {}).get("completed")
        print(f"[t+{i}m] task{TASK}={status} completed={done} phoenix_rowid={rid}", flush=True)
        if done:
            terminal = status
            break
        time.sleep(60)
    print(f"=== MONITOR EXIT terminal={terminal} ===", flush=True)
    if terminal:
        try:
            ans = phoenix_reader.extract_answer_with_fallback(DB, phoenix_reader.trace_summaries_since(DB, 2840) and None or None)
        except Exception:
            ans = None
        # best-effort final task output
        try:
            out = await mythic.get_all_task_output_by_id(mythic=c, task_display_id=TASK)
            txt = " ".join(str((o or {}).get("response_text", "")) for o in (out or []))[:1500]
            print("FINAL_OUTPUT:", txt, flush=True)
        except Exception as e:
            print("final-output-err:", e, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
