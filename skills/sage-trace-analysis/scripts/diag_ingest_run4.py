#!/usr/bin/env python3
"""Throwaway diagnostic: reconstruct run #4 (task 1886) BloodHound ingest attempt from Phoenix.
Finds recent large traces, dumps tool outputs touching the stage->ingest pipeline, and the
command histogram + final answer."""
import os
from pathlib import Path
import sys
sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals import phoenix_reader as pr  # noqa: E402

DB = "/home/john/dev/sage/Payload_Type/sage/.phoenix/phoenix.db"
OUT_DIR = Path(os.environ.get("SAGE_TRACE_OUTPUT_DIR", "/tmp/sage-trace-analysis"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = str(OUT_DIR / "diag_ingest_run4.out")
KEYS = ["stage_file_to_disk", "file_upload", "ingest", "accessibility", "does not exist",
        "FileNotFound", "sage_file_staging", "/tmp/", "download", "136a6f7c", "cypher",
        "upload_collection", "job_id", "No MCP", "not connected", "shortest"]

lines = []
def w(s=""):
    lines.append(str(s))

mx = pr.max_trace_rowid(DB)
w(f"max_trace_rowid = {mx}")
# Pull a wide recent window and rank by span count to find the autonomous runs.
summ = pr.trace_summaries_since(DB, max(0, mx - 400))
summ_sorted = sorted(summ, key=lambda s: s.spans, reverse=True)
w("\n=== TOP 15 RECENT TRACES BY SPAN COUNT (rowid, spans, last_span_time) ===")
for s in summ_sorted[:15]:
    w(f"  rowid={s.rowid:>5}  spans={s.spans:>4}  last={s.last_span}")

# Take the biggest few traces (the autonomous runs) and inspect their pipeline tool outputs.
big = [s.rowid for s in summ_sorted[:6]]
w(f"\n=== INSPECTING TRACES {big} ===")
hist = pr.command_histogram(DB, big)
w(f"\nMythic command histogram (issue_task): {hist}")

to = pr.tool_outputs(DB, big)
w(f"\ntool_outputs total chars: {len(to)}")
w("\n=== PIPELINE-RELEVANT TOOL OUTPUT LINES ===")
for ln in to.splitlines():
    if any(k.lower() in ln.lower() for k in KEYS):
        w("  | " + ln.strip()[:400])

# Per-trace: find which agent ran and whether stage/file_upload appears at all.
w("\n=== PER-TRACE staging/ingest presence ===")
for s in summ_sorted[:8]:
    t = pr.tool_outputs(DB, [s.rowid])
    has_stage = "stage_file_to_disk" in t
    has_upload = "file_upload" in t
    has_err = ("does not exist" in t) or ("accessibility" in t) or ("FileNotFound" in t)
    rows = pr.span_rows(DB, [s.rowid])
    agent = rows[0]["agent"] if rows else "?"
    w(f"  rowid={s.rowid:>5} spans={s.spans:>4} root={agent[:32]:32} stage={has_stage} upload={has_upload} ingest_err={has_err}")

ans = pr.extract_answer_with_fallback(DB, big)
w("\n=== FINAL ANSWER (best-effort, last respond_to_user / assistant msg) ===")
w(ans[:3000] if ans else "(none extracted)")

with open(OUT, "w") as f:
    f.write("\n".join(lines))
print("WROTE", OUT, "lines", len(lines))
