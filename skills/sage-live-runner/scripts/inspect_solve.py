#!/usr/bin/env python3
"""Read-only Mythic solve inspector.

Usage:
  .venv/bin/python skills/sage-live-runner/scripts/inspect_solve.py <solve_task_id|latest> [tail_chars]
"""

import asyncio
import base64
import sys

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402


TID_ARG = sys.argv[1]
TID = None if TID_ARG == "latest" else int(TID_ARG)
TAIL = int(sys.argv[2]) if len(sys.argv) > 2 else 5000


def _payload_type(task):
    cb = task.get("callback") or {}
    return (((cb.get("payload") or {}).get("payloadtype") or {}).get("name")
            or ((cb.get("payloadtype") or {}).get("name")))


def _decode_output(rows):
    chunks = []
    for row in rows or []:
        raw = row.get("response_text") or ""
        if raw:
            try:
                chunks.append(base64.b64decode(raw).decode("utf-8", "replace"))
                continue
            except Exception:
                pass
        chunks.append(str(row.get("response") or raw or ""))
    return "\n".join(part for part in chunks if part)


async def main():
    client = await login_to_mythic(resolve_password())
    tid = TID
    if tid is None:
        latest_query = """
        query Latest {
          task(
            where: {command_name: {_eq: "query"}, callback: {display_id: {_eq: 1}}},
            order_by: {display_id: desc},
            limit: 1
          ) { display_id }
        }
        """
        latest = (await mythic.execute_custom_query(mythic=client, query=latest_query)).get("task") or []
        if not latest:
            raise SystemExit("no Sage query task found")
        tid = int(latest[0]["display_id"])
    query = """
    query Solve($id: Int!) {
      task(where: {display_id: {_eq: $id}}) {
        display_id command_name status completed callback { display_id host user payload { payloadtype { name } } }
      }
      subtasks: task(where: {display_id: {_gt: $id}}, order_by: {display_id: asc}) {
        display_id command_name status completed callback { display_id host user payload { payloadtype { name } } }
      }
    }
    """
    result = await mythic.execute_custom_query(mythic=client, query=query, variables={"id": tid})
    solve = (result.get("task") or [{}])[0]
    print(f"SOLVE #{solve.get('display_id')} {solve.get('command_name')} status={solve.get('status')!r} completed={solve.get('completed')}")
    print("SUBTASKS:")
    for task in result.get("subtasks") or []:
        cb = task.get("callback") or {}
        print(
            f"  #{task.get('display_id')} cb{cb.get('display_id')}({cb.get('host')}) "
            f"{_payload_type(task)} {task.get('command_name')} status={task.get('status')!r} "
            f"completed={task.get('completed')}"
        )

    solve_out = _decode_output(await mythic.get_all_task_output_by_id(mythic=client, task_display_id=tid))
    if solve_out:
        print(f"\nSOLVE OUTPUT TAIL ({min(TAIL, len(solve_out))} chars):")
        print(solve_out[-TAIL:])

    latest = list(result.get("subtasks") or [])[-5:]
    for task in latest:
        tid = task.get("display_id")
        out = _decode_output(await mythic.get_all_task_output_by_id(mythic=client, task_display_id=tid))
        if out:
            print(f"\nOUTPUT #{tid} {task.get('command_name')} TAIL ({min(1600, len(out))} chars):")
            print(out[-1600:])


if __name__ == "__main__":
    asyncio.run(main())
