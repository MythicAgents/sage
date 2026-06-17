#!/usr/bin/env python3
"""Poll a Sage solve task's status + subtask count until it completes (or a soft cap), inference-free.
Usage: watch_solve.py <task_display_id> [cap_seconds]
Prints a status line every 30s; prints DONE when the task completes, else WATCH_TIMEOUT_STILL_RUNNING."""
import asyncio, sys, time
sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402

TID = int(sys.argv[1])
CAP = int(sys.argv[2]) if len(sys.argv) > 2 else 520


async def main():
    c = await login_to_mythic(resolve_password())
    start = time.time()
    while time.time() - start < CAP:
        s = (await mythic.execute_custom_query(
            mythic=c,
            query="query S{ task(where:{display_id:{_eq:%d}}){ status completed } }" % TID)).get("task", [])
        subs = (await mythic.execute_custom_query(
            mythic=c,
            query="query N{ task(where:{display_id:{_gt:%d}}){ display_id } }" % TID)).get("task", [])
        st = s[0] if s else {}
        print(f"[{int(time.time()-start)}s] #{TID} status={st.get('status')!r} "
              f"completed={st.get('completed')} subtasks={len(subs)}", flush=True)
        if st.get("completed") or str(st.get("status", "")).lower() in ("error", "success", "completed"):
            print("DONE", flush=True)
            return
        await asyncio.sleep(30)
    print("WATCH_TIMEOUT_STILL_RUNNING", flush=True)


asyncio.run(main())
