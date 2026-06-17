#!/usr/bin/env python3
"""Throwaway poller — wait for task 1960 to reach terminal status, dump decoded output + subtask summary."""
import asyncio
import base64
import sys
import time

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402

TASK_ID = 1960
DEADLINE_S = 1500  # 25 min cap


async def main():
    client = await login_to_mythic(resolve_password())
    start = time.time()
    while True:
        elapsed = int(time.time() - start)
        q = """
        query t($id: Int!) {
          task(where: {display_id: {_eq: $id}}) {
            display_id status completed status_timestamp_processed
          }
        }
        """
        res = await mythic.execute_custom_query(mythic=client, query=q, variables={"id": TASK_ID})
        rows = res.get("task", [])
        if not rows:
            print(f"[{elapsed}s] task {TASK_ID} not found yet")
        else:
            t = rows[0]
            print(f"[{elapsed}s] status={t.get('status')!r} completed={t.get('completed')}")
            if t.get("completed") or (t.get("status") or "").lower() in ("error", "completed"):
                break
        if elapsed > DEADLINE_S:
            print(f"[{elapsed}s] DEADLINE hit — stopping poll (task may still run server-side)")
            break
        await asyncio.sleep(20)

    # dump decoded output
    try:
        out = await mythic.get_all_task_output_by_id(mythic=client, task_display_id=TASK_ID)
        print("\n=== DECODED OUTPUT (last 8000 chars) ===")
        chunks = []
        for o in out:
            raw = o.get("response_text") or o.get("response") or ""
            if not raw:
                continue
            try:
                chunks.append(base64.b64decode(raw).decode("utf-8", "replace"))
            except Exception:
                chunks.append(str(raw))
        full = "\n".join(chunks)
        print(full[-8000:])
    except Exception as e:
        print(f"output fetch failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
