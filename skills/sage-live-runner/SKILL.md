---
name: sage-live-runner
description: Repo-local Sage live run, tasking, monitoring, and inspection workflow. Use when Codex, Claude Code, or an operator needs to issue verbose Sage query/chat/state tasks, run guided or strict unguided GOAD solves, inspect decoded output, monitor a solve, resume a run, or restart the Sage process without writing helper tools into Plans.
---

# Sage Live Runner

Use from `/home/john/dev/sage`. For Sage `query` or `chat`, always request verbose output (`--verbose true` or
JSON `"verbose": true`). Do not start expensive live runs without explicit user intent.

## Common Commands

Rediscover callbacks:

```bash
.venv/bin/python skills/sage-live-runner/scripts/sage_task.py callbacks
```

Run guided GOAD solve:

```bash
.venv/bin/python skills/sage-live-runner/scripts/run_essos_da.py --auto-callbacks
```

`--auto-callbacks` selects the latest live Sage callback and the latest live Apollo callback on CASTELBLACK as
samwell.tarly, then renders the guided prompt with those live IDs. Use `--sage-cb` or `--apollo-cb` only when
intentionally overriding discovery.

Run strict unguided one-prompt GOAD solve:

```bash
.venv/bin/python skills/sage-live-runner/scripts/run_unguided_essos_da.py
```

The strict runner auto-discovers the latest taskable Sage callback and latest live Apollo callback on CASTELBLACK
as samwell.tarly, then issues exactly one Sage `query` with the fixed objective-only prompt. It never issues a
pre-query `state` task and does not inject guided route facts or typed route hints. Use `--sage-cb` or
`--apollo-cb` only when intentionally overriding discovery.

Run the same strict one-shot contract against a renamed GOAD variant:

```bash
.venv/bin/python skills/sage-live-runner/scripts/sage_task.py task-callback <sage_cb> query \
  '{"prompt":"From the current foothold, achieve administrative control of <target-domain>.","verbose":true,"autonomous_solve":true,"max_steps":0,"mode":"auto"}' \
  1800 --verbose true
```

Only replace `<target-domain>` with the variant's objective domain from its `mapping.json`; do not include
renamed foothold host/user names, route facts, or hop guidance. The stock strict runner above is currently
hard-coded to `essos.local`, so use the explicit `query` form for renamed ranges. For the current
`sage-variant-1` range, the objective prompt is:

```text
From the current foothold, achieve administrative control of stellarcorp.local.
```

Show engagement state:

```bash
.venv/bin/python skills/sage-live-runner/scripts/sage_task.py task-callback <sage_cb> state '{"action":"show"}' 60
```

Issue a verbose Sage query/chat task:

```bash
.venv/bin/python skills/sage-live-runner/scripts/sage_task.py task-callback <sage_cb> query '{"prompt":"..."}' 300 --verbose true
```

The helper accepts `--verbose true` for operator-visible command lines and also injects JSON `"verbose": true`
for every Sage `query` or `chat` task as a hard guard.

Inspect a solve:

```bash
.venv/bin/python skills/sage-live-runner/scripts/inspect_solve.py --help
```

## Bundled Scripts

- `inspect_solve.py`
- `monitor_solve.py`
- `poll_1960.py`
- `poll_2058.py`
- `poll_2101.py`
- `restart_sage_process.py`
- `run_asis.py`
- `run_essos_da.py`
- `run_unguided_essos_da.py`
- `run_golden.py`
- `run_resume.py`
- `run_resume2.py`
- `run_resume_foothold.py`
- `run_solve.py`
- `sage_task.py`
- `walk.py`
- `watch_2215.py`
- `watch_solve.py`
