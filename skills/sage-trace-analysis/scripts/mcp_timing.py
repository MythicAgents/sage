#!/usr/bin/env python3
"""Decompose run 1910's wall-clock: BloodHound-MCP tool time vs LLM time vs Mythic-tool time,
to answer whether MCP request/response latency contributes to the run delays."""
import os
from pathlib import Path
import sqlite3
from datetime import datetime
from statistics import mean, median

DB = "file:/home/john/dev/sage/Payload_Type/sage/.phoenix/phoenix.db?mode=ro"
OUT_DIR = Path(os.environ.get("SAGE_TRACE_OUTPUT_DIR", "/tmp/sage-trace-analysis"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = str(OUT_DIR / "mcp_timing.out")
PRE_ROWID = 2896  # run 1910 fired with pre_rowid=2896

BLOODHOUND = {"cypher_query","graph_analysis","domain_info","user_info","group_info","computer_info",
              "gpo_info","ou_info","adcs_info","data_quality","file_upload","custom_nodes",
              "container_info","cert_template_info","gmsa_info","tier_zero_info","search"}
MYTHIC_PREFIX = ("issue_task","get_","stage_file","download","upload","ensure_tool","check_callback",
                 "list_","transfer_","summarize_","respond_to_user","get_ttp","get_operations")

def parse(t):
    if not t: return None
    try: return datetime.strptime(t, "%Y-%m-%d %H:%M:%S.%f")
    except Exception:
        try: return datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except Exception: return None

def cls(name, kind, prompt):
    if (prompt or 0) > 0 or (kind or "").upper()=="LLM": return "LLM"
    if (kind or "").upper()=="TOOL" or name in BLOODHOUND:
        if name in BLOODHOUND: return "MCP"
        if any(name.startswith(p) for p in MYTHIC_PREFIX): return "MYTHIC"
        return "TOOL_OTHER"
    return "OTHER"

c = sqlite3.connect(DB, uri=True)
rows = c.execute("""
  SELECT name, span_kind, start_time, end_time, COALESCE(llm_token_count_prompt,0)
  FROM spans WHERE trace_rowid > ? """, (PRE_ROWID,)).fetchall()

cats = {}
per_tool = {}
all_start, all_end = None, None
for name, kind, st, et, prompt in rows:
    s, e = parse(st), parse(et)
    if s and (all_start is None or s < all_start): all_start = s
    if e and (all_end is None or e > all_end): all_end = e
    if not (s and e): continue
    d = (e - s).total_seconds()
    if d < 0: continue
    cat = cls(name or "", kind, prompt)
    cats.setdefault(cat, []).append(d)
    if cat in ("MCP","MYTHIC"):
        per_tool.setdefault(name, []).append(d)

lines = []
def w(s=""): lines.append(str(s))

wall = (all_end - all_start).total_seconds() if all_start and all_end else 0
w(f"=== RUN 1910 (traces > {PRE_ROWID}) — wall-clock {wall:.1f}s ({wall/60:.1f} min) ===")
w(f"{'category':<12}{'count':>7}{'sum_s':>10}{'%wall':>8}{'mean_s':>9}{'p50':>8}{'p95':>8}{'max_s':>8}")
def pct(v): return f"{100*v/wall:.1f}%" if wall else "-"
def p95(xs): xs=sorted(xs); return xs[int(0.95*(len(xs)-1))] if xs else 0
for cat in ["LLM","MCP","MYTHIC","TOOL_OTHER","OTHER"]:
    xs = cats.get(cat, [])
    if not xs: continue
    w(f"{cat:<12}{len(xs):>7}{sum(xs):>10.1f}{pct(sum(xs)):>8}{mean(xs):>9.3f}{median(xs):>8.3f}{p95(xs):>8.3f}{max(xs):>8.3f}")
tot_span = sum(sum(v) for v in cats.values())
w(f"{'SUM-spans':<12}{'':>7}{tot_span:>10.1f}{pct(tot_span):>8}")
w(f"{'untraced/gap':<12}{'':>7}{wall-tot_span:>10.1f}{pct(wall-tot_span):>8}  (concurrency overlaps + gaps)")

w("\n=== MCP tools (BloodHound) — per-tool latency ===")
w(f"{'tool':<22}{'count':>7}{'sum_s':>9}{'mean_ms':>10}{'p95_ms':>9}{'max_ms':>9}")
for name in sorted(per_tool, key=lambda n: -sum(per_tool[n])):
    xs = per_tool[name]
    if name not in BLOODHOUND: continue
    w(f"{name:<22}{len(xs):>7}{sum(xs):>9.1f}{1000*mean(xs):>10.0f}{1000*p95(xs):>9.0f}{1000*max(xs):>9.0f}")

w("\n=== slowest individual MCP calls ===")
flat = [(d,n) for n in per_tool if n in BLOODHOUND for d in per_tool[n]]
for d,n in sorted(flat, reverse=True)[:8]:
    w(f"  {d*1000:>8.0f} ms  {n}")

w("\n=== Mythic tools — per-tool latency (for contrast; issue_task waits on the agent) ===")
w(f"{'tool':<38}{'count':>6}{'sum_s':>9}{'mean_s':>9}{'max_s':>9}")
for name in sorted(per_tool, key=lambda n: -sum(per_tool[n])):
    if name in BLOODHOUND: continue
    xs = per_tool[name]
    w(f"{name[:37]:<38}{len(xs):>6}{sum(xs):>9.1f}{mean(xs):>9.2f}{max(xs):>9.2f}")

with open(OUT,"w") as f: f.write("\n".join(lines))
print("WROTE", OUT)
