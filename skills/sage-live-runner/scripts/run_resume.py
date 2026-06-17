#!/usr/bin/env python3
"""Throwaway (gitignored) — fire a DIRECTED-RESUME autonomous solve at cb15 that resumes the engagement
at its furthest position (cb29 holds in-memory ESSOS tickets, one WriteDacl from DA) and drives it through
the final ACL abuse to Domain Admin on essos.local. Then poll to terminal + dump decoded output."""
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
DEADLINE_S = 1500

OBJECTIVE = (
    "You are operating an autonomous red-team engagement against the GOAD lab, RESUMING an in-progress "
    "engagement. Do NOT restart from scratch and do NOT repeat hops that are already complete.\n\n"
    "CURRENT POSITION (already achieved):\n"
    "- You hold active Mythic callbacks on host CASTELBLACK as NORTH\\samwell.tarly: Merlin callback 29 and "
    "Apollo callback 30.\n"
    "- You have ALREADY crossed from the NORTH/SEVENKINGDOMS forest into the ESSOS.LOCAL forest. Merlin "
    "callback 29 holds IN-MEMORY Kerberos service tickets to the essos domain controller MEEREEN.ESSOS.LOCAL "
    "(cifs/ldap/HOST) in its ticket cache.\n"
    "- The earlier NORTH and WINTERFELL constrained-delegation hops are DONE. Do not repeat them. Do not "
    "re-collect SharpHound on NORTH/WINTERFELL.\n\n"
    "THE FINAL STEP TO THE OBJECTIVE:\n"
    "- BloodHound has shown that the principal you control in ESSOS has WriteOwner / WriteDacl / GenericWrite "
    "over the group DOMAIN ADMINS@ESSOS.LOCAL. You are ONE access-control edit away from Domain Admin on essos.\n"
    "- OBJECTIVE: complete that final edge NOW. Abuse your control over DOMAIN ADMINS@ESSOS.LOCAL (take "
    "ownership if needed, write a DACL granting yourself the right to modify its membership, then add a "
    "principal you control to DOMAIN ADMINS@ESSOS.LOCAL) using your in-memory ESSOS tickets on callback 29. "
    "Then VERIFY you have Domain Admin on essos.local (confirm the new DOMAIN ADMINS membership via "
    "LDAP/BloodHound, or authenticate to MEEREEN.ESSOS.LOCAL with the new rights).\n\n"
    "METHOD AND TRADECRAFT:\n"
    "- Begin by listing callback 29's ticket cache to confirm your ESSOS tickets, then run a SINGLE BloodHound "
    "query for the inbound control edges on DOMAIN ADMINS@ESSOS.LOCAL to confirm the final edge. Then EXECUTE "
    "the ACL abuse. Do NOT stop to write a status/'Final Report' describing the next step — only stop when you "
    "have Domain Admin, or when you hit a genuine, verified blocker.\n"
    "- For C# tooling already registered in Mythic (SharpView, StandIn, Rubeus, SharpGPOAbuse): you do NOT "
    "upload a new file. Read the command's parameter-group schema and reference the ALREADY-REGISTERED file by "
    "its NAME using the correct parameter group. Do NOT pass a registered file's UUID into the new-file/upload "
    "group, and never mix parameters from different groups. Prefer in-process execution (load-assembly + "
    "invoke-assembly) over spawning a sacrificial process.\n"
    "- Hard rule: NO offline credential cracking of any kind (no kerberoast-and-crack, no AS-REP cracking, no "
    "hash cracking, no asking the operator to crack).\n"
    "- Persist through recoverable obstacles: if a tool returns no output, first verify whether it actually ran "
    "(correct parameter group? correct already-registered file reference?) and correct the call, rather than "
    "concluding you need a different tool.\n\n"
    "Begin now: list callback 29's tickets, confirm the DOMAIN ADMINS@ESSOS.LOCAL inbound edge in BloodHound, "
    "then perform the final ACL abuse to reach Domain Admin on essos.local and verify it. Narrate each step."
)


async def dump(client, task_id):
    out = await mythic.get_all_task_output_by_id(mythic=client, task_display_id=task_id)
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
        mythic=client,
        command_name="query",
        parameters=json.dumps({
            "prompt": OBJECTIVE,
            "verbose": True,
            "autonomous_solve": True,
            "max_steps": 300,
        }),
        callback_display_id=15,
    )
    tid = task.get("display_id") or task.get("id")
    print(f"issued task: {json.dumps(task, default=str)[:300]}")
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

    # new offensive subtasks issued this run
    q2 = ("query t($id:Int!){ task(where:{display_id:{_gt:$id}}, order_by:{display_id:asc}){ "
          "display_id command_name status callback{ display_id payload{ payloadtype{ name } } } } }")
    r2 = await mythic.execute_custom_query(mythic=client, query=q2, variables={"id": tid})
    print(f"\n=== NEW SUBTASKS issued after {tid}: {len(r2.get('task', []))} ===")
    for t in r2.get("task", []):
        cb = t.get("callback") or {}
        pt = ((cb.get("payload") or {}).get("payloadtype") or {}).get("name")
        print(f"  #{t['display_id']} cb{cb.get('display_id')} {pt} {t['command_name']} {t['status']}")

    print("\n=== DECODED OUTPUT (last 9000 chars) ===")
    print((await dump(client, tid))[-9000:])


if __name__ == "__main__":
    asyncio.run(main())
