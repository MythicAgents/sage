---
name: phoenix-traces
description: Analyze Sage's Phoenix/OpenInference trace database (phoenix.db) for the Sage LangGraph multi-agent system. Inspect the latest run, per-model-call token usage, errors, tool-call frequency, and compare two traces (e.g. before/after a context/token optimization). Use when asked to read the Phoenix DB, check Sage trace tokens, verify token reduction, find Sage run errors, see which tools an agent called, or analyze/compare Sage runs.
---

# Phoenix Trace Analysis

Read-only analysis of Sage's Phoenix trace store. Phoenix records every LangGraph/LangChain span (model calls, tool calls, chains, handoffs) to a SQLite DB while Sage runs. This skill queries that DB **without locking it** (opened read-only, WAL-respecting), so it is safe to run during a live Sage run.

## How to run

```bash
bun skills/phoenix-traces/analyze.ts <command> [args] [--db PATH] [--project NAME] [--json]
```

Default DB: `Payload_Type/sage/.phoenix/phoenix.db` (resolved relative to the skill). Default project: `Sage`. Add `--json` to any command for machine-readable output.

## Commands

| Command | What it shows |
|---------|---------------|
| `latest` | Full breakdown of the most recent trace: wall time, span/model/tool counts, total tokens, **max & avg prompt tokens per model call**, errors. (default if no command given) |
| `list [--limit N]` | Recent traces, newest first — one row each with spans, model calls, total tokens, max-prompt-per-call, error count. |
| `trace <trace_id\|prefix>` | Same breakdown as `latest` for a specific trace (accepts a trace_id prefix). |
| `tokens [<trace_id>]` | Per-model-call prompt/completion token table for a trace (default: latest). The view for verifying the per-call context ceiling. |
| `errors [--limit N]` | Recent `ERROR` spans across the project, deduped by message with counts. |
| `tools [--limit N]` | Tool-call frequency across the project (which tools agents call most). |
| `compare <baseline_id> <new_id>` | Side-by-side token/latency comparison of two traces — total tokens, max & avg prompt/call, model calls, wall time, with Δ% and reduction factor. |

## Common tasks

- **Did a context/token change work?** Note the baseline trace id, do a fresh Sage run, then:
  `bun skills/phoenix-traces/analyze.ts compare <baseline_id> <new_id>`
  Watch `max prompt / call` (should flat-line near the configured ceiling instead of climbing) and `total tokens` (should drop by a multiple).
- **What blew up in the last run?** `analyze.ts latest` then `analyze.ts errors`.
- **Per-step token climb?** `analyze.ts tokens` — if prompt tokens grow monotonically across calls, history is accumulating unbounded.

## Notes / gotchas

- **Read-only & WAL-safe.** Opened with `{ readonly: true }`; never writes, never locks. Fine to run mid-run, but a trace still in flight may show partial spans until Sage finishes flushing.
- **Token columns:** per-call values come from `spans.llm_token_count_prompt` / `llm_token_count_completion` on `span_kind='LLM'` spans; totals sum those over the trace.
- **Trace ids:** `list` prints shortened ids; `trace`/`tokens`/`compare` accept a prefix, so the first ~12 chars are enough.
- **Different DB or project:** `--db /path/to/phoenix.db` and `--project <name>` (the projects table currently holds `Sage` and `default`).
- Zero dependencies — uses Bun's built-in `bun:sqlite`. Requires `bun`.
