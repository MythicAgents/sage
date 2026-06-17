import asyncio, base64, sys, re, collections, time
sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic
from mythic import mythic
async def main():
    c=await login_to_mythic(resolve_password()); s=time.time()
    while True:
        el=int(time.time()-s)
        r=await mythic.execute_custom_query(mythic=c, query="query S{ task(where:{display_id:{_eq:2101}}){ status completed } }", variables={})
        t=r["task"][0]; print(f"[{el}s] {t['status']!r} done={t['completed']}", flush=True)
        if t["completed"] or (t["status"] or "").lower() in ("error","completed"): break
        if el>1400: print("deadline"); break
        await asyncio.sleep(30)
    out=await mythic.get_all_task_output_by_id(mythic=c, task_display_id=2101)
    full="".join(base64.b64decode(o.get("response_text") or "").decode("utf-8","replace") for o in out if o.get("response_text"))
    cnt=collections.Counter(re.findall(r"Tool Request: '([a-zA-Z_]+)'", full))
    base={"get_all_active_callbacks":35,"check_callback_alive":30,"list_callbacks":0,"get_all_commands_for_payloadtype":16,"get_all_task_output_by_task_id":39}
    print("=== FINAL counts (vs 2058) ===")
    for t in ["list_callbacks","get_all_active_callbacks","check_callback_alive","get_all_commands_for_payloadtype","get_all_task_output_by_task_id"]:
        print(f"  {t:36} {cnt.get(t,0):>3}  (2058:{base.get(t)})")
    # task-output repeats this run
    ids=[re.findall(r":\s*'?(\d+)'?", m.group(1))[0:1] for m in re.finditer(r"Tool Request: 'get_all_task_output_by_task_id', Args: '(\{[^}]*\})'", full)]
    flat=[x[0] for x in ids if x]; rc=collections.Counter(flat)
    print("get_all_task_output: distinct=%d repeats=%s"%(len(rc), {k:v for k,v in rc.items() if v>1}))
