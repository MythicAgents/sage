#!/usr/bin/env python3
"""Throwaway (gitignored) — backfill `evidence.mythic_task_id` onto existing engagement-ledger hops.

Going forward the agent records the task id automatically; this one-off matches the EXISTING hops to the
Mythic task that produced them, by command-name + display_params signature (the command line encodes the
technique + target, so no per-task output fetch is needed). Best-effort: prints a report; only writes ids it
can confidently match. Additive only (never removes/edits other fields).
"""
import asyncio
import re
import sys

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402
from ai.langgraph import engagement_ledger as el  # noqa: E402

ENGAGEMENT = "goad-tw-0607"


_DRIVERS = {"query", "chat", "exit", "list", "mcp-list", "mcp-connect", "mcp-disconnect", "mcp-call"}


def _flag(dp, name):
    m = re.search(rf"/{name}:(\S+)", dp, re.IGNORECASE)
    return m.group(1).lower().strip('"\'') if m else ""


def _matches(hop, task):
    """Match a task to a hop by command-name + parsed /domain,/user flags (the command line encodes the
    technique+target). Solve-driver tasks (query/chat) are excluded — their params quote the whole objective."""
    if (task.get("command_name") or "").lower() in _DRIVERS:
        return False
    dp = str(task.get("display_params") or "")
    dpl = dp.lower()
    tech = (hop.get("technique") or "").lower()
    target = (hop.get("target") or "").lower()
    dom = _flag(dp, "domain")
    user = _flag(dp, "user")
    if tech == "gpo-abuse":
        return "sharpgpoabuse" in dpl
    if tech == "lsass-dump":
        return "logonpasswords" in dpl or "nanodump" in dpl
    if tech == "dcsync-rights-grant":
        # match the target domain too (its leading label appears in the StandIn --object DN), so a
        # cross-forest essos grant doesn't get matched to the north grant.
        return "standin" in dpl and "--grant" in dpl and ("dc=" + target.split(".")[0]) in dpl
    if tech == "dcsync":
        if not target:  # junk empty-target hop — don't guess
            return False
        return "dcsync" in dpl and dom == target and user in ("krbtgt", "")
    if tech == "golden-ticket":
        return "golden" in dpl and "/sids" not in dpl and dom == target
    if tech == "sid-history-escalation":
        return "golden" in dpl and "/sids" in dpl and dom == target  # target = child domain
    if tech == "dcsync-user":
        huser, _, hdom = target.partition("@")
        return "dcsync" in dpl and user == huser and (dom == hdom or not hdom)
    return False


async def main(write: bool):
    c = await login_to_mythic(resolve_password())
    q = ("query Q{ task(where:{display_id:{_gte:2100}}, order_by:{display_id:asc}, limit:1500)"
         "{ display_id command_name display_params status timestamp callback{display_id} } }")
    tasks = (await mythic.execute_custom_query(mythic=c, query=q)).get("task", [])
    print(f"scanned {len(tasks)} tasks (display_id >= 2100)\n")

    by_id = {t["display_id"]: t for t in tasks}
    data = el.load(ENGAGEMENT)
    report = []
    for hop in data.get("hops", []):
        if not isinstance(hop.get("evidence"), dict):
            hop["evidence"] = {}
        ev = hop["evidence"]
        task, note = None, ""
        tid = ev.get("mythic_task_id")
        if tid and tid in by_id:
            task, note = by_id[tid], "task already set"
        else:
            cands = [t for t in tasks if _matches(hop, t)]
            if cands:
                # the evidence task is the one issued just before this hop was recorded.
                hop_ts = str(hop.get("timestamp") or "")
                at_or_before = [t for t in cands if str(t.get("timestamp") or "") <= hop_ts] if hop_ts else []
                task = max(at_or_before or cands, key=lambda t: str(t.get("timestamp") or ""))
                ev["mythic_task_id"] = task["display_id"]
                note = f"matched ({len(cands)} cands) {task['command_name']}"
            else:
                note = "NO MATCH"
        cb = (task.get("callback") or {}).get("display_id") if task else None
        if cb is not None:
            ev["callback_id"] = cb
        report.append((el.hop_label(hop), ev.get("mythic_task_id"), cb, note))

    print(f"{'hop':52} {'task':>6} {'cb':>4}  note")
    print("-" * 120)
    for label, tid, cb, note in report:
        print(f"{label:52} {str(tid if tid is not None else '-'):>6} {str(cb if cb is not None else '-'):>4}  {note}")

    if write:
        path = el.save(data, ENGAGEMENT)
        print(f"\nWROTE {path}")
    else:
        print("\n(dry run — pass 'write' to persist)")


if __name__ == "__main__":
    asyncio.run(main(write=(len(sys.argv) > 1 and sys.argv[1] == "write")))
