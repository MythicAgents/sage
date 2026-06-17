#!/usr/bin/env python3
"""Probe live Mythic filemeta schema + the callback->task->filemeta join for downloaded files.
Confirms the exact GraphQL shape BEFORE writing stage-by-callback."""
import asyncio, os, sys, json
from pathlib import Path
sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402

OUT_DIR = Path(os.environ.get("SAGE_TRACE_OUTPUT_DIR", "/tmp/sage-trace-analysis"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = str(OUT_DIR / "probe_filemeta.out")
lines = []
def w(s=""): lines.append(str(s))

async def main():
    c = await login_to_mythic(resolve_password())

    # 1) Which callbacks have downloaded files? Find candidates by scanning recent filemeta.
    q_recent = """
      query Recent {
        filemeta(where: {is_download_from_agent: {_eq: true}}, order_by: {id: desc}, limit: 12) {
          id agent_file_id complete deleted filename_utf8 is_download_from_agent timestamp
          task { id display_id callback { id display_id host } }
        }
      }
    """
    try:
        r = await mythic.execute_custom_query(c, q_recent)
        rows = (r or {}).get("filemeta", []) if isinstance(r, dict) else []
        w(f"=== RECENT is_download_from_agent filemeta ({len(rows)}) ===")
        for row in rows:
            t = row.get("task") or {}
            cb = (t.get("callback") or {})
            fn = row.get("filename_utf8")
            try:
                fn = bytes(fn, "utf-8").decode("unicode_escape") if isinstance(fn, str) else fn
            except Exception:
                pass
            w(json.dumps({
                "agent_file_id": row.get("agent_file_id"),
                "complete": row.get("complete"), "deleted": row.get("deleted"),
                "filename": row.get("filename_utf8"),
                "task_display_id": t.get("display_id"),
                "cb_display_id": cb.get("display_id"), "cb_host": cb.get("host"),
                "timestamp": row.get("timestamp"),
            }))
    except Exception as e:
        w(f"recent-query-error: {e}")

    # 2) The exact resolver query: latest complete download zip for a given callback display_id.
    #    Try the nested-relationship where-filter (callback.display_id) that the resolver will use.
    for cbid in [28, 27, 29, 22]:
        q = """
          query LatestDl($cbid: Int!) {
            filemeta(
              where: {
                is_download_from_agent: {_eq: true},
                complete: {_eq: true},
                deleted: {_eq: false},
                task: {callback: {display_id: {_eq: $cbid}}}
              },
              order_by: {id: desc}, limit: 3
            ) {
              agent_file_id filename_utf8 complete timestamp
              task { display_id callback { display_id host } }
            }
          }
        """
        try:
            r = await mythic.execute_custom_query(c, q, variables={"cbid": cbid})
            rows = (r or {}).get("filemeta", []) if isinstance(r, dict) else []
            w(f"\n=== resolver test cb_display_id={cbid}: {len(rows)} rows ===")
            for row in rows[:3]:
                w(f"  uuid={row.get('agent_file_id')} file={row.get('filename_utf8')} ts={row.get('timestamp')}")
        except Exception as e:
            w(f"\n=== resolver test cb_display_id={cbid}: ERROR {e} ===")

asyncio.run(main())
with open(OUT, "w") as f:
    f.write("\n".join(lines))
print("WROTE", OUT)
