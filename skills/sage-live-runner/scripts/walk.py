#!/usr/bin/env python3
"""Manual ground-truth walk helper. Issue ONE task directly to a foothold
callback (bypassing Sage's LLM), wait, print decoded output. Usage:
  python skills/sage-live-runner/scripts/walk.py <cb> <command> '<json-or-string params>' [wait_iters]
"""
import asyncio, sys, base64
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"))
from evals.harness import resolve_password, login_to_mythic
from mythic import mythic


async def main():
    cb = int(sys.argv[1]); cmd = sys.argv[2]
    params = sys.argv[3] if len(sys.argv) > 3 else ""
    wait = int(sys.argv[4]) if len(sys.argv) > 4 else 30
    c = await login_to_mythic(resolve_password())
    t = await mythic.issue_task(mythic=c, command_name=cmd, parameters=params, callback_display_id=cb)
    tid = t.get("display_id")
    print(f"[task {tid}] cb{cb} {cmd} {params[:160]}")
    st = None
    for _ in range(wait):
        await asyncio.sleep(3)
        r = await mythic.execute_custom_query(
            mythic=c,
            query="query Q($id:Int!){ task(where:{display_id:{_eq:$id}}){ status completed } }",
            variables={"id": tid})
        st = r.get("task", [])
        if st and st[0]["completed"]:
            break
    out = await mythic.get_all_task_output_by_id(mythic=c, task_display_id=tid)
    s = ""
    for o in out or []:
        rt = o.get("response_text", "") or ""
        try:
            s += base64.b64decode(rt).decode("utf-8", "replace")
        except Exception:
            s += str(rt)
    print(f"[status {st[0]['status'] if st else '?'}]")
    print(s[:4500])


asyncio.run(main())
