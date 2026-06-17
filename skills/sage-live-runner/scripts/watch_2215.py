import asyncio, sys, base64, time
sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic
from mythic import mythic
TID=2215
async def dump(c,t):
    out=await mythic.get_all_task_output_by_id(mythic=c, task_display_id=t)
    return "\n".join(base64.b64decode(o.get("response_text") or "").decode("utf-8","replace") if o.get("response_text") else str(o.get("response") or "") for o in out)
async def main():
    c=await login_to_mythic(resolve_password())
    start=time.time()
    while True:
        el=int(time.time()-start)
        r=(await mythic.execute_custom_query(mythic=c, query="query t{ task(where:{display_id:{_eq:2215}}){ status completed } }", variables={})).get("task",[])
        st=r[0].get("status") if r else "?"; done=r[0].get("completed") if r else False
        print(f"[{el}s] 2215 status={st!r} completed={done}", flush=True)
        if done or (st or "").lower() in ("error","completed") or el>2400: break
        await asyncio.sleep(30)
    q2="query t{ task(where:{display_id:{_gt:2215}}, order_by:{display_id:asc}){ display_id command_name status callback{display_id host} } }"
    subs=(await mythic.execute_custom_query(mythic=c, query=q2, variables={})).get("task",[])
    print(f"\n=== NEW SUBTASKS after 2215: {len(subs)} ===", flush=True)
    for t in subs: 
        cb=t.get("callback") or {}
        print(f"  #{t['display_id']} cb{cb.get('display_id')}({str(cb.get('host'))[:11]}) {t['command_name']} {t['status']}")
    print("\n=== DECODED OUTPUT (last 6000 chars) ===\n"+(await dump(c,TID))[-6000:], flush=True)
asyncio.run(main())
