#!/usr/bin/env python3
"""Pull the narrated answer/handbacks from recent run traces and Mythic task 1886 output."""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals import phoenix_reader as pr  # noqa: E402
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402

DB = "/home/john/dev/sage/Payload_Type/sage/.phoenix/phoenix.db"
OUT_DIR = Path(os.environ.get("SAGE_TRACE_OUTPUT_DIR", "/tmp/sage-trace-analysis"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = str(OUT_DIR / "diag_ingest_narration.out")
lines = []
def w(s=""): lines.append(str(s))

# Recent traces (the afternoon run set) — pull per-trace narration.
for rid in [2691, 2851, 2861, 2692, 2612, 2531, 2694]:
    ans = pr.extract_answer_with_fallback(DB, [rid])
    rows = pr.span_rows(DB, [rid])
    agent = rows[0]["agent"] if rows else "?"
    w(f"\n===== trace {rid} root={agent} =====")
    w(ans[:2200] if ans else "(no narration extracted)")

async def mythic_part():
    try:
        c = await login_to_mythic(resolve_password())
        for tid in [1886]:
            out = await mythic.get_all_task_output_by_id(mythic=c, task_display_id=tid)
            txt = "\n".join(str((o or {}).get("response_text", "")) for o in (out or []))
            w(f"\n##### MYTHIC TASK {tid} OUTPUT ({len(txt)} chars) #####")
            w(txt[:4000] if txt else "(empty)")
    except Exception as e:
        w(f"mythic-part-error: {e}")

asyncio.run(mythic_part())
with open(OUT, "w") as f:
    f.write("\n".join(lines))
print("WROTE", OUT)
