#!/usr/bin/env python3
"""Throwaway (gitignored) — RESUME the solve from CURRENT access (no fresh SharpHound).

Validates the deterministic command-construction layer (slices 1/2/2b, loaded into the restarted Sage)
on REAL tradecraft: the next hop from the CASTELBLACK foothold is GPO abuse (samwell.tarly has GenericWrite
on STARKWALLPAPER) via SharpGPOAbuse (in-memory .NET, already registered). This fires `execute_assembly` —
the #1 construction offender — so a clean run = the layer works live. Read results from Phoenix.
"""
import asyncio
import base64
import json
import sys
import time

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from evals import phoenix_reader  # noqa: E402
from mythic import mythic  # noqa: E402

DB = "/home/john/dev/sage/Payload_Type/sage/.phoenix/phoenix.db"
DEADLINE_S = 900

OBJECTIVE = (
    "You are RESUMING an autonomous red-team engagement against the GOAD lab. Do NOT restart from scratch and "
    "do NOT run a new SharpHound collection — prior collections are already done.\n\n"
    "CURRENT ACCESS (use what you have):\n"
    "- Apollo callback 36 and Merlin callback 35 on host CASTELBLACK as NORTH\\samwell.tarly.\n"
    "- An Apollo callback 34 on WINTERFELL as samwell.tarly.\n\n"
    "OBJECTIVE — execute the NEXT hop toward Domain Admin from this position:\n"
    "samwell.tarly holds GenericWrite over the STARKWALLPAPER GPO, which applies to WINTERFELL. Abuse that GPO "
    "to gain code execution as SYSTEM on WINTERFELL, then confirm the new SYSTEM context (e.g. a new elevated "
    "callback or a whoami showing NT AUTHORITY\\SYSTEM).\n\n"
    "TRADECRAFT:\n"
    "- Use SharpGPOAbuse (in-memory .NET, ALREADY REGISTERED in Mythic) via execute-assembly / inline_assembly, "
    "referencing the registered file BY NAME (filename='SharpGPOAbuse.exe'); do NOT upload a new file.\n"
    "- Typical: SharpGPOAbuse with --AddComputerTask/--AddUserRights or an immediate scheduled task to run a "
    "payload, targeting GPO 'STARKWALLPAPER'. Pick the concrete sub-technique that fits and EXECUTE it; if a "
    "tool argument is rejected, fix the argument form and retry (do not abandon).\n"
    "- If you need the attack graph, the BloodHound MCP may need a one-time mcp-connect; existing collection "
    "data is already ingested — do NOT re-collect.\n"
    "- OPSEC-scoped: in-memory .NET / BOFs only. No offline cracking. No fresh SharpHound.\n"
    "- EXECUTE each step; only stop when WINTERFELL SYSTEM/code-exec is achieved or you hit a genuinely "
    "unrecoverable blocker (then state exactly which capability is missing). Narrate each step."
)


async def dump(client, tid):
    out = await mythic.get_all_task_output_by_id(mythic=client, task_display_id=tid)
    return "\n".join(
        base64.b64decode(o.get("response_text") or "").decode("utf-8", "replace")
        if o.get("response_text") else str(o.get("response") or "")
        for o in out
    )


async def main():
    client = await login_to_mythic(resolve_password())
    pre = phoenix_reader.max_trace_rowid(DB)
    print(f"pre_rowid={pre}")
    task = await mythic.issue_task(
        mythic=client, command_name="query",
        parameters=json.dumps({"prompt": OBJECTIVE, "verbose": True, "autonomous_solve": True, "max_steps": 200}),
        callback_display_id=15,
    )
    tid = task.get("display_id") or task.get("id")
    print(f"issued task: {json.dumps(task, default=str)[:200]}\nTASK_ID={tid}")
    start = time.time()
    while True:
        el = int(time.time() - start)
        q = "query t($id:Int!){ task(where:{display_id:{_eq:$id}}){ display_id status completed } }"
        rows = (await mythic.execute_custom_query(mythic=client, query=q, variables={"id": tid})).get("task", [])
        if rows:
            t = rows[0]
            print(f"[{el}s] status={t.get('status')!r} completed={t.get('completed')}")
            if t.get("completed") or (t.get("status") or "").lower() in ("error", "completed"):
                break
        else:
            print(f"[{el}s] task {tid} not found yet")
        if el > DEADLINE_S:
            print(f"[{el}s] DEADLINE hit")
            break
        await asyncio.sleep(20)
    q2 = ("query t($id:Int!){ task(where:{display_id:{_gt:$id}}, order_by:{display_id:asc}){ display_id "
          "command_name status callback{display_id payload{payloadtype{name}}} } }")
    subs = (await mythic.execute_custom_query(mythic=client, query=q2, variables={"id": tid})).get("task", [])
    print(f"\n=== NEW SUBTASKS after {tid}: {len(subs)} ===")
    for t in subs:
        cb = t.get("callback") or {}
        pt = ((cb.get("payload") or {}).get("payloadtype") or {}).get("name")
        print(f"  #{t['display_id']} cb{cb.get('display_id')} {pt} {t['command_name']} {t['status']}")
    print("\n=== DECODED OUTPUT (last 9000 chars) ===")
    print((await dump(client, tid))[-9000:])


if __name__ == "__main__":
    asyncio.run(main())
