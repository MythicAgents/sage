---
name: sage-trace-analysis
description: Repo-local Sage trace, Phoenix, Mythic task-output, and run-log analysis workflow. Use when Codex, Claude Code, or an operator needs to mine historical failures, inspect Phoenix spans, backfill task IDs, audit solve steps, or diagnose ingestion/file/output behavior without creating new Plans tools.
---

# Sage Trace Analysis

Treat `sage.db`, `.phoenix/phoenix.db`, run logs, and engagement ledgers as sensitive read-only data unless the
operator explicitly says otherwise. Do not delete, compact, or mutate retained historical DBs.

## Common Uses

Mine failure classes:

```bash
.venv/bin/python skills/sage-trace-analysis/scripts/mine_failures.py
```

Audit solve steps or task IDs:

```bash
.venv/bin/python skills/sage-trace-analysis/scripts/step_audit.py --help
.venv/bin/python skills/sage-trace-analysis/scripts/backfill_task_ids.py --help
```

Use `$sage-trajectory-learning` when the output should become normalized transition records or replay data.

## Bundled Scripts

- `backfill_task_ids.py`
- `diag_ingest_narration.py`
- `diag_ingest_run4.py`
- `drycall_stage.py`
- `ingest_verify.py`
- `mcp_timing.py`
- `mine_failures.py`
- `probe_filemeta.py`
- `step_audit.py`
