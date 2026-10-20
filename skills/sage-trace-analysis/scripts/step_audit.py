#!/usr/bin/env python3
"""Reconstruct a run's Mythic action sequence to find step-inefficiency.
Pulls Mythic tasks issued after a given task id and summarizes the command sequence + redundancy."""
import argparse
import asyncio
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"))
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402

# A run's sub-tasks on the footholds have higher ids than the task that started it.
Q = """
query RunTasks($after: Int!, $limit: Int!) {
  task(where: {id: {_gt: $after}}, order_by: {id: asc}, limit: $limit) {
    display_id
    command_name
    status
    callback { display_id payload { payloadtype { name } } }
    timestamp
    original_params
  }
}
"""


async def main(after_task: int, limit: int):
    c = await login_to_mythic(resolve_password())
    r = await mythic.execute_custom_query(c, Q, {"after": after_task, "limit": limit})
    tasks = r.get("task", [])
    print(f"=== {len(tasks)} Mythic sub-tasks issued after task {after_task} ===\n")
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after-task", type=int, required=True,
                        help="Mythic task id that started the run; tasks with higher ids are audited")
    parser.add_argument("--limit", type=int, default=200, help="maximum tasks to fetch (default 200)")
    args = parser.parse_args()
    asyncio.run(main(args.after_task, args.limit))
