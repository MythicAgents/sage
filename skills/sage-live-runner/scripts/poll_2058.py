import asyncio, base64, sys, re
sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic
from mythic import mythic
async def main():
    c = await login_to_mythic(resolve_password())
    import time; start=time.time()
    while True:
        el=int(time.time()-start)
        r=await mythic.execute_custom_query(mythic=c, query="query S{ task(where:{display_id:{_eq:2058}}){ status completed } }", variables={})
        t=r["task"][0]; print(f"[{el}s] {t['status']!r} completed={t['completed']}", flush=True)
        if t["completed"] or (t["status"] or "").lower() in ("error","completed"): break
        if el>1400: print("deadline"); break
        await asyncio.sleep(30)
    # definitive handback check via tool-call names in subtasks is N/A; use decoded output
    out=await mythic.get_all_task_output_by_id(mythic=c, task_display_id=2058)
    txt="".join(base64.b64decode(o.get("response_text") or "").decode("utf-8","replace") for o in out if o.get("response_text"))
    ho=re.findall(r"\[Task → (\w+)\]", txt)
    print("delegations:", dict((x,ho.count(x)) for x in set(ho)))
    print("handback_to_supervisor content fires:", txt.count("Handback to Supervisor"))
    print("FINAL 1200:", txt[-1200:])
asyncio.run(main())
