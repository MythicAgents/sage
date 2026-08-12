#!/usr/bin/env bun
/**
 * analyze.ts — read-only analysis of Sage's Phoenix / OpenInference trace DB.
 *
 * Phoenix persists every LangGraph/LangChain span to a SQLite store at
 * Payload_Type/sage/.phoenix/phoenix.db. This tool reads it WITHOUT locking the
 * live DB (opened read-only, WAL-respecting), so it is safe to run during a Sage run.
 *
 * Usage:
 *   bun .claude/skills/phoenix-traces/analyze.ts <command> [args] [--db PATH] [--project NAME] [--json]
 *
 * Commands:
 *   list [--limit N]          Recent traces (newest first): spans, model calls, tokens, errors
 *   latest                    Full breakdown of the most recent trace
 *   trace <trace_id|prefix>   Full breakdown of a specific trace
 *   tokens [<trace_id>]       Per-model-call prompt/completion tokens for a trace (default: latest)
 *   errors [--limit N]        Recent ERROR spans (deduped) across the project
 *   tools [--limit N]         Tool-call frequency across the project
 *   compare <baseline> <new>  Side-by-side token/latency comparison of two traces (before/after)
 */

import { Database } from "bun:sqlite";
import { join } from "node:path";
import { existsSync } from "node:fs";

// Two levels up, not three: this skill lives at skills/phoenix-traces/ like every other repo skill.
// It used to sit at .claude/skills/phoenix-traces/, one level deeper, and the relative depth moved with
// it. import.meta.dir resolves the real file, not the symlink used to invoke it, so this stays correct
// whether it is run through .claude/skills/ or $CODEX_HOME/skills/.
const DEFAULT_DB = join(import.meta.dir, "../../Payload_Type/sage/.phoenix/phoenix.db");

interface Opts { db: string; project: string; json: boolean; limit?: number; }

function parseOpts(argv: string[]): { positional: string[]; opts: Opts } {
  const positional: string[] = [];
  const opts: Opts = { db: DEFAULT_DB, project: "Sage", json: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--db") opts.db = argv[++i];
    else if (a === "--project") opts.project = argv[++i];
    else if (a === "--json") opts.json = true;
    else if (a === "--limit") opts.limit = parseInt(argv[++i], 10);
    else positional.push(a);
  }
  return { positional, opts };
}

/** Parse a Phoenix timestamp ("2026-05-31 23:44:33.001903") to epoch ms. */
function tsMs(s: string | null): number {
  if (!s) return NaN;
  const iso = s.replace(" ", "T").replace(/(\.\d{3})\d*$/, "$1") + "Z";
  return Date.parse(iso);
}
const secs = (a: string, b: string): number => Math.max(0, (tsMs(b) - tsMs(a)) / 1000);
const n = (x: number): string => x.toLocaleString("en-US");

function openDb(path: string): Database {
  if (!existsSync(path)) {
    console.error(`phoenix.db not found at: ${path}\nPass --db <path> to override.`);
    process.exit(2);
  }
  return new Database(path, { readonly: true });
}

function projectId(db: Database, name: string): number {
  const rows = db.query("SELECT id, name FROM projects").all() as { id: number; name: string }[];
  const hit = rows.find((r) => r.name === name);
  if (!hit) {
    console.error(`Project "${name}" not found. Available: ${rows.map((r) => r.name).join(", ")}`);
    process.exit(2);
  }
  return hit.id;
}

interface SpanRow {
  name: string; span_kind: string; start_time: string; end_time: string;
  status_code: string; status_message: string;
  llm_token_count_prompt: number | null; llm_token_count_completion: number | null;
}

interface TraceRow { id: number; trace_id: string; start_time: string; end_time: string; }

function traceSpans(db: Database, traceRowid: number): SpanRow[] {
  return db.query(
    `SELECT name, span_kind, start_time, end_time, status_code, status_message,
            llm_token_count_prompt, llm_token_count_completion
       FROM spans WHERE trace_rowid = ? ORDER BY start_time`
  ).all(traceRowid) as SpanRow[];
}

function traceStats(spans: SpanRow[]) {
  const llm = spans.filter((s) => s.span_kind === "LLM");
  const prompts = llm.map((s) => s.llm_token_count_prompt ?? 0);
  const comps = llm.map((s) => s.llm_token_count_completion ?? 0);
  const totalTokens = prompts.reduce((a, b) => a + b, 0) + comps.reduce((a, b) => a + b, 0);
  const errors = spans.filter((s) => s.status_code === "ERROR");
  const wall = spans.length ? secs(spans[0].start_time, spans.reduce((mx, s) => (tsMs(s.end_time) > tsMs(mx) ? s.end_time : mx), spans[0].end_time)) : 0;
  return {
    spanCount: spans.length,
    modelCalls: llm.length,
    toolCalls: spans.filter((s) => s.span_kind === "TOOL").length,
    totalTokens,
    maxPromptPerCall: prompts.length ? Math.max(...prompts) : 0,
    avgPromptPerCall: prompts.length ? Math.round(prompts.reduce((a, b) => a + b, 0) / prompts.length) : 0,
    maxCompletionPerCall: comps.length ? Math.max(...comps) : 0,
    errorCount: errors.length,
    firstError: errors[0]?.status_message?.split("\n")[0]?.slice(0, 160) ?? null,
    wall,
  };
}

function latestTrace(db: Database, pid: number): TraceRow | null {
  return db.query(
    "SELECT id, trace_id, start_time, end_time FROM traces WHERE project_rowid = ? ORDER BY start_time DESC LIMIT 1"
  ).get(pid) as TraceRow | null;
}

function findTrace(db: Database, pid: number, idOrPrefix: string): TraceRow | null {
  return db.query(
    "SELECT id, trace_id, start_time, end_time FROM traces WHERE project_rowid = ? AND trace_id LIKE ? ORDER BY start_time DESC LIMIT 1"
  ).get(pid, idOrPrefix + "%") as TraceRow | null;
}

function printDetail(t: TraceRow, st: ReturnType<typeof traceStats>, json: boolean) {
  if (json) { console.log(JSON.stringify({ trace_id: t.trace_id, start: t.start_time, ...st }, null, 2)); return; }
  console.log(`\n=== TRACE ${t.trace_id} ===`);
  console.log(`start: ${t.start_time}   wall: ${st.wall.toFixed(1)}s`);
  console.log(`spans: ${st.spanCount}   model calls: ${st.modelCalls}   tool calls: ${st.toolCalls}`);
  console.log(`tokens total: ${n(st.totalTokens)}`);
  console.log(`  max prompt / call: ${n(st.maxPromptPerCall)}   avg prompt / call: ${n(st.avgPromptPerCall)}   max completion / call: ${n(st.maxCompletionPerCall)}`);
  console.log(`errors: ${st.errorCount}${st.firstError ? `   first: ${st.firstError}` : ""}`);
}

function cmdList(db: Database, pid: number, opts: Opts) {
  const limit = opts.limit ?? 10;
  const traces = db.query(
    "SELECT id, trace_id, start_time, end_time FROM traces WHERE project_rowid = ? ORDER BY start_time DESC LIMIT ?"
  ).all(pid, limit) as TraceRow[];
  const rows = traces.map((t) => { const st = traceStats(traceSpans(db, t.id)); return { trace: t.trace_id.slice(0, 16), start: t.start_time, ...st }; });
  if (opts.json) { console.log(JSON.stringify(rows, null, 2)); return; }
  console.log(`\nRecent traces (newest first) — project "${opts.project}":\n`);
  console.log("trace_id".padEnd(18) + "start".padEnd(28) + "spans".padStart(6) + "calls".padStart(7) + "tokens".padStart(12) + "maxP/call".padStart(11) + "err".padStart(5));
  for (const r of rows) {
    console.log(r.trace.padEnd(18) + r.start.padEnd(28) + String(r.spanCount).padStart(6) + String(r.modelCalls).padStart(7) + n(r.totalTokens).padStart(12) + n(r.maxPromptPerCall).padStart(11) + String(r.errorCount).padStart(5));
  }
}

function cmdTokens(db: Database, pid: number, idArg: string | undefined, opts: Opts) {
  const t = idArg ? findTrace(db, pid, idArg) : latestTrace(db, pid);
  if (!t) { console.error("No matching trace."); process.exit(2); }
  const spans = traceSpans(db, t.id).filter((s) => s.span_kind === "LLM");
  const st = traceStats(traceSpans(db, t.id));
  if (opts.json) { console.log(JSON.stringify({ trace_id: t.trace_id, calls: spans.map((s) => ({ prompt: s.llm_token_count_prompt ?? 0, completion: s.llm_token_count_completion ?? 0 })), summary: st }, null, 2)); return; }
  console.log(`\nPer-model-call tokens — trace ${t.trace_id.slice(0, 16)} (${spans.length} calls):\n`);
  console.log("#".padStart(4) + "prompt".padStart(12) + "completion".padStart(13));
  spans.forEach((s, i) => console.log(String(i + 1).padStart(4) + n(s.llm_token_count_prompt ?? 0).padStart(12) + n(s.llm_token_count_completion ?? 0).padStart(13)));
  console.log(`\nmax prompt/call: ${n(st.maxPromptPerCall)}   avg prompt/call: ${n(st.avgPromptPerCall)}   total tokens: ${n(st.totalTokens)}`);
}

function cmdErrors(db: Database, pid: number, opts: Opts) {
  const limit = opts.limit ?? 25;
  const errs = db.query(
    `SELECT s.name, s.status_message FROM spans s JOIN traces t ON s.trace_rowid = t.id
       WHERE t.project_rowid = ? AND s.status_code = 'ERROR' ORDER BY s.start_time DESC LIMIT ?`
  ).all(pid, limit * 4) as { name: string; status_message: string }[];
  const counts = new Map<string, number>();
  for (const e of errs) { const k = `[${e.name}] ${(e.status_message || "").split("\n")[0].slice(0, 150)}`; counts.set(k, (counts.get(k) ?? 0) + 1); }
  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, limit);
  if (opts.json) { console.log(JSON.stringify(sorted.map(([msg, count]) => ({ count, msg })), null, 2)); return; }
  console.log(`\nRecent ERROR spans (deduped) — project "${opts.project}":\n`);
  if (!sorted.length) { console.log("  (none)"); return; }
  for (const [msg, count] of sorted) console.log(`  ${String(count).padStart(3)}x  ${msg}`);
}

function cmdTools(db: Database, pid: number, opts: Opts) {
  const limit = opts.limit ?? 20;
  const rows = db.query(
    `SELECT s.name, COUNT(*) ct FROM spans s JOIN traces t ON s.trace_rowid = t.id
       WHERE t.project_rowid = ? AND s.span_kind = 'TOOL' GROUP BY s.name ORDER BY ct DESC LIMIT ?`
  ).all(pid, limit) as { name: string; ct: number }[];
  if (opts.json) { console.log(JSON.stringify(rows, null, 2)); return; }
  console.log(`\nTool-call frequency — project "${opts.project}":\n`);
  for (const r of rows) console.log(`  ${String(r.ct).padStart(5)}  ${r.name}`);
}

function cmdCompare(db: Database, pid: number, baseId: string, newId: string, opts: Opts) {
  const base = findTrace(db, pid, baseId), neu = findTrace(db, pid, newId);
  if (!base || !neu) { console.error(`Trace not found: ${!base ? baseId : newId}`); process.exit(2); }
  const b = traceStats(traceSpans(db, base.id)), a = traceStats(traceSpans(db, neu.id));
  const pct = (from: number, to: number) => from === 0 ? "n/a" : `${(((to - from) / from) * 100).toFixed(0)}%`;
  const x = (from: number, to: number) => to === 0 ? "n/a" : `${(from / to).toFixed(2)}x`;
  if (opts.json) { console.log(JSON.stringify({ baseline: { trace: base.trace_id, ...b }, current: { trace: neu.trace_id, ...a } }, null, 2)); return; }
  console.log(`\nCOMPARE  baseline ${base.trace_id.slice(0, 12)} → current ${neu.trace_id.slice(0, 12)}\n`);
  const row = (label: string, bv: number, av: number) => console.log(label.padEnd(22) + n(bv).padStart(12) + n(av).padStart(12) + pct(bv, av).padStart(9) + x(bv, av).padStart(9) + " reduction");
  console.log("metric".padEnd(22) + "baseline".padStart(12) + "current".padStart(12) + "Δ".padStart(9) + "factor".padStart(9));
  row("total tokens", b.totalTokens, a.totalTokens);
  row("max prompt / call", b.maxPromptPerCall, a.maxPromptPerCall);
  row("avg prompt / call", b.avgPromptPerCall, a.avgPromptPerCall);
  row("model calls", b.modelCalls, a.modelCalls);
  console.log("wall seconds".padEnd(22) + b.wall.toFixed(1).padStart(12) + a.wall.toFixed(1).padStart(12));
}

function main() {
  const { positional, opts } = parseOpts(process.argv.slice(2));
  const cmd = positional[0] ?? "latest";
  const db = openDb(opts.db);
  try {
    const pid = projectId(db, opts.project);
    switch (cmd) {
      case "list": cmdList(db, pid, opts); break;
      case "latest": { const t = latestTrace(db, pid); if (!t) { console.error("No traces."); process.exit(2); } printDetail(t, traceStats(traceSpans(db, t.id)), opts.json); break; }
      case "trace": { const t = findTrace(db, pid, positional[1] ?? ""); if (!t) { console.error(`Trace not found: ${positional[1]}`); process.exit(2); } printDetail(t, traceStats(traceSpans(db, t.id)), opts.json); break; }
      case "tokens": cmdTokens(db, pid, positional[1], opts); break;
      case "errors": cmdErrors(db, pid, opts); break;
      case "tools": cmdTools(db, pid, opts); break;
      case "compare": { if (!positional[1] || !positional[2]) { console.error("Usage: compare <baseline_trace_id> <new_trace_id>"); process.exit(2); } cmdCompare(db, pid, positional[1], positional[2], opts); break; }
      default: console.error(`Unknown command: ${cmd}\nCommands: list, latest, trace <id>, tokens [id], errors, tools, compare <a> <b>`); process.exit(2);
    }
  } finally { db.close(); }
}

main();
