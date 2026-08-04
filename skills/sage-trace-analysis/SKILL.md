---
name: sage-trace-analysis
description: Repo-local Sage trace, Phoenix, Mythic task-output, and run-log analysis workflow. Use when Codex, Claude Code, or an operator needs to mine historical failures, inspect Phoenix spans, backfill task IDs, audit solve steps, or diagnose ingestion/file/output behavior without creating new Plans tools.
---

# Sage Trace Analysis

Treat `sage.db`, `.phoenix/phoenix.db`, run logs, and engagement ledgers as sensitive read-only data unless the
operator explicitly says otherwise. Do not delete, compact, or mutate retained historical DBs.

Export the request first with `sage-live-runner`, then run the parameterized read-only audit:

```bash
TRANSCRIPT_PATH=$(.venv/bin/python skills/sage-live-runner/scripts/native_chat.py transcript --request-id <id> | jq -r .export_path)
.venv/bin/python skills/sage-trace-analysis/scripts/trace_audit.py --transcript "$TRANSCRIPT_PATH"
```

The audit reports finished sub-agents missing summaries, duplicate assistant finals, payload-task counts,
input-request pauses, and post-sub-agent activity gaps. It binds the redundant request/channel/status tuple and
reconciles exact task IDs from runtime telemetry with completed Mythic task-card evidence before certifying a
zero-task or bounded-task expectation. Add explicit canary expectations when appropriate:

```bash
.venv/bin/python skills/sage-trace-analysis/scripts/trace_audit.py --transcript "$TRANSCRIPT_PATH" --require-zero-payload-tasks --expect-halt-reason operator_input_requested
```

Optional `--phoenix-db` plus repeatable `--trace-rowid` arguments correlate the transcript with Phoenix through
the repository's read-only SQLite reader.

## Common Uses

Mine failure classes:

```bash
.venv/bin/python skills/sage-trace-analysis/scripts/mine_failures.py
```

Audit solve steps or task IDs:

```bash
.venv/bin/python skills/sage-trace-analysis/scripts/step_audit.py --help
```

Use `$sage-trajectory-learning` when the output should become normalized transition records or replay data.

## Phoenix Tracing Verdict

Answers "is the autonomous path actually traced, and is the trace structured?" in one command. A span count
cannot answer it: the untraced-kernel signature was 13 spans that each looked fine, every one a root with
nothing above it.

```bash
python3 skills/sage-trace-analysis/scripts/phoenix_verdict.py --latest-episode
```

Reads WAL-inclusively, because reading the bare `.db` misses everything not yet checkpointed. Scope with
`--since 'YYYY-MM-DD HH:MM:SS'` (UTC), `--latest-episode`, or `--db <path>` for an archive. `--json` for
machine use. Exit status is 0 only on `TRACED`, so it can gate a check.

| Verdict | Meaning |
|---|---|
| `TRACED` | kernel spans present and every tool span has a parent |
| `PARTIAL` | kernel spans present but some tool spans are parentless |
| `UNTRACED` | no `sage.kernel.*` spans; the autonomous path emitted nothing of its own |
| `EMPTY` | no spans in scope — check `--since`, or whether the run wrote to this database |

Worked contrast, both reproducible today: the 2026-08-01 database reports `UNTRACED`, 101 spans / 101 roots /
0 kernel spans; a current autonomous run reports `TRACED`, 32 spans / 1 root / 17 kernel spans / 0 parentless
tool spans.

## Callback Dispatch Probe

Use when CHAIN spans are missing from `phoenix.db` while LLM or TOOL spans survive as parentless
roots — the signature of trace loss that a span-count query alone cannot explain.

The probe is log-only and loads through `PYTHONPATH`, so it changes no product code and needs no
architecture-governor gate. It wraps `LangChainInstrumentor._instrument`, then hooks the
manager-level dispatch methods so each `on_chain_start` / `on_llm_start` / `on_tool_start` records
whether the OpenInference tracer is still among that manager's handlers.

```bash
/bin/bash skills/sage-goad-reset/scripts/sage_restart.sh PYTHONPATH="$(git rev-parse --show-toplevel)/skills/sage-trace-analysis/scripts/callback_probe" SAGE_CALLBACK_PROBE=1
```

Then run the workload and read the JSONL, which lands at
`.sage_history/diagnostics/callback-probe/<pid>.jsonl` unless `SAGE_CALLBACK_PROBE_LOG` overrides it.

Three outcomes are distinguishable, and they point at different defects:

| Observed | Meaning |
|---|---|
| `chain_start` records with `tracer_present: false` | the tracer is not reaching that manager — handler-attachment defect |
| no `chain_start` records at all | the graph is not emitting chain events; the tracer never had a chance |
| `chain_start` with `tracer_present: true`, still no CHAIN span in `phoenix.db` | loss is inside the tracer or the export path, not the callback wiring |

With `SAGE_CALLBACK_PROBE` unset the module imports and does nothing, so the `PYTHONPATH` entry is
safe to leave in place. Every hook returns the original dispatch result and swallows its own
exceptions; `tests/test_callback_probe.py` holds the green→red→green control, including a planted
tracer-stripped manager the probe must flag and a floor assertion on records actually examined.

## Bundled Scripts

- `trace_audit.py` (parameterized and read-only)
- `mine_failures.py`
- `step_audit.py`
- `callback_probe/sitecustomize.py` (log-only, `PYTHONPATH`-loaded, off unless `SAGE_CALLBACK_PROBE=1`)
