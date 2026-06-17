#!/usr/bin/env python3
"""Throwaway (gitignored) — reconstruct converged run #2's action sequence to find step-inefficiency.
Pulls Mythic tasks issued during the run window and summarizes the command sequence + redundancy."""
import asyncio
import sys
from collections import Counter

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402

# Run #2 was task 1872; its sub-tasks on the footholds have higher ids. Grab everything after ~1872.
Q = """
query RunTasks {
  task(where: {id: {_gt: 1872}}, order_by: {id: asc}, limit: 200) {
    display_id
    command_name
    status
    callback { display_id payload { payloadtype { name } } }
    timestamp
    original_params
  }
}
"""


async def main():
    c = await login_to_mythic(resolve_password())
    r = await mythic.execute_custom_query(c, Q)
    tasks = r.get("task", [])
    print(f"=== {len(tasks)} Mythic sub-tasks issued during run #2 ===\n")
    cmd_counts = Counter()
    status_counts = Counter()
    for t in tasks:
        cb = (t.get("callback") or {})
        ptype = ((cb.get("payload") or {}).get("payloadtype") or {}).get("name", "?")
        cmd = t.get("command_name")
        st = t.get("status")
        cmd_counts[cmd] += 1
        status_counts[st] += 1
        params = str(t.get("original_params") or "")[:90]
        ts = str(t.get("timestamp") or "")[11:19]
        print(f"  #{t['display_id']:<5} {ts} cb{cb.get('display_id')}/{ptype:<7} {cmd:<20} [{st}]  {params}")
    print(f"\n=== command histogram ===")
    for cmd, n in cmd_counts.most_common():
        print(f"  {cmd:<22} x{n}")
    print(f"\n=== status histogram ===")
    for st, n in status_counts.most_common():
        print(f"  {st:<28} x{n}")


if __name__ == "__main__":
    asyncio.run(main())
