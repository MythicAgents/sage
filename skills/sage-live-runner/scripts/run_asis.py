#!/usr/bin/env python3
"""Throwaway (gitignored) — UNSTEERED autonomous Trust Walker on the as-is range, post-restart, to validate
the handback-contract + supervisor-dedup + registration-reflex doctrine (less churn, StandIn registers/uses).
Fires the standard foothold objective at cb15, polls to terminal, dumps subtask list + decoded output."""
import asyncio, base64, json, sys, time

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from evals import phoenix_reader  # noqa: E402
from mythic import mythic  # noqa: E402

DB = "/home/john/dev/sage/Payload_Type/sage/.phoenix/phoenix.db"
DEADLINE_S = 1500

OBJECTIVE = (
    "You are operating an autonomous red-team engagement against the GOAD lab. You already hold a "
    "foothold: an active Mythic callback running as NORTH\\samwell.tarly on the host CASTELBLACK. "
    "OBJECTIVE: reach Domain Admin on the essos.local forest. "
    "Method: drive path discovery with BloodHound — collect with SharpHound from the foothold, ingest "
    "into BloodHound, and query the attack graph for the shortest path toward the objective; then "
    "traverse it hop by hop using the Mythic C2, re-collecting and re-querying the graph after each hop. "
    "Hard rule: NO offline credential cracking (no kerberoast-and-crack, no AS-REP cracking, no hash "
    "cracking, no asking the operator to crack). If a Windows host blocks your beacon (Defender/EDR), "
    "switch agents rather than re-permuting arguments. Begin now and narrate each step."
)


async def dump(client, tid):
    out = await mythic.get_all_task_output_by_id(mythic=client, task_display_id=tid)
    chunks = []
    for o in out:
        raw = o.get("response_text") or o.get("response") or ""
        if not raw:
            continue
        try:
            chunks.append(base64.b64decode(raw).decode("utf-8", "replace"))
        except Exception:
            chunks.append(str(raw))
    return "\n".join(chunks)


async def main():
    client = await login_to_mythic(resolve_password())
    pre_rowid = phoenix_reader.max_trace_rowid(DB)
    print(f"pre_rowid={pre_rowid}")
    task = await mythic.issue_task(
        mythic=client, command_name="query",
        parameters=json.dumps({"prompt": OBJECTIVE, "verbose": True, "autonomous_solve": True, "max_steps": 300}),
        callback_display_id=15,
    )
    tid = task.get("display_id") or task.get("id")
    print(f"issued task: {json.dumps(task, default=str)[:200]}")
    print(f"TASK_ID={tid}")

    start = time.time()
    while True:
        elapsed = int(time.time() - start)
        q = "query t($id: Int!){ task(where:{display_id:{_eq:$id}}){ display_id status completed } }"
        res = await mythic.execute_custom_query(mythic=client, query=q, variables={"id": tid})
        rows = res.get("task", [])
        if rows:
            t = rows[0]
            print(f"[{elapsed}s] status={t.get('status')!r} completed={t.get('completed')}")
            if t.get("completed") or (t.get("status") or "").lower() in ("error", "completed"):
                break
        else:
            print(f"[{elapsed}s] task {tid} not found yet")
        if elapsed > DEADLINE_S:
            print(f"[{elapsed}s] DEADLINE hit")
            break
        await asyncio.sleep(20)

    q2 = ("query t($id:Int!){ task(where:{display_id:{_gt:$id}}, order_by:{display_id:asc}){ "
          "display_id command_name status callback{ display_id payload{ payloadtype{ name } } } } }")
    r2 = await mythic.execute_custom_query(mythic=client, query=q2, variables={"id": tid})
    subs = r2.get("task", [])
    print(f"\n=== NEW SUBTASKS issued after {tid}: {len(subs)} ===")
    for t in subs:
        cb = t.get("callback") or {}
        pt = ((cb.get("payload") or {}).get("payloadtype") or {}).get("name")
        print(f"  #{t['display_id']} cb{cb.get('display_id')} {pt} {t['command_name']} {t['status']}")

    print("\n=== DECODED OUTPUT (last 11000 chars) ===")
    print((await dump(client, tid))[-11000:])


if __name__ == "__main__":
    asyncio.run(main())
