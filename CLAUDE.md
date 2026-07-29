# Sage Assistant Compatibility Guide

`AGENTS.md` is the canonical instruction file for Sage development, regardless of whether the active assistant is
Codex, Claude Code, or another tool. Read it before changing code or operating the lab.

@AGENTS.md

The `@`-import above makes that load mechanical for Claude Code, matching the auto-load Codex already gets from
the AGENTS.md convention. Added 2026-07-28: the sentence alone was advisory, and an assistant that never opened
`AGENTS.md` ran a full autonomous solve against the live range while believing it was issuing a read-only probe.
Everything below is the condensed always-relevant subset; `AGENTS.md` remains the single source of truth.

## Current system

Sage is a native Mythic v4 chat container. It runs locally in the `sage` tmux session during development and
registers with Docker-backed Mythic. It is a control-plane service, not a target implant or a Sage callback.

Verify important claims against source, tests, Mythic, BloodHound, and the current range — not against prose.

The maintainer's operational frontier lives in `Plans/`, which is private and gitignored. If you are working
from a clone, it is absent and nothing here should require it. Russel's own pointers into it live in
`CLAUDE.local.md`, which is gitignored and loads automatically alongside this file.

## Required workflows

- Start or restart local Sage with `skills/sage-goad-reset/scripts/sage_restart.sh`.
- Archive active Sage and Phoenix databases with
  `skills/sage-goad-reset/scripts/archive_runtime_dbs.py`; never delete them for a clean run.
- Use `skills/sage-goad-reset` for the complete reset and readiness lifecycle.
- Use `skills/sage-callback-bootstrap` for payload and foothold establishment.
- Use `skills/sage-live-runner` for native chat requests and monitoring.
- Use `skills/sage-focused-capability-tests` before a full offline suite or live run.
- Use `skills/sage-trace-analysis` for Phoenix/Mythic/ledger inspection.
- Use `skills/sage-architecture-governor` before high-risk architecture edits.

`skills/README.md` is the operator-tool index. Reusable scripts belong in an existing or new skill, not in
`Plans/`.

## Hard boundaries

- Sage may query Mythic and BloodHound control-plane data.
- Sage must not connect directly to target LDAP, SMB, Kerberos, WinRM, RPC, HTTP, or equivalent services.
- Target-facing activity and proof must come through Mythic payload tasks or payload-derived BloodHound data.
- Do not hardcode GOAD identities or a Trust Walker path into product code or base prompts.
- Do not delete runtime databases, rewrite retained evidence, or silently reseal rejected evaluation artifacts.
- Do not commit unless the maintainer explicitly requests it.

## Testing

Run focused tests first, then the maintained offline tier:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py
```

One tier, no exclusions. The four rejected successor-portfolio suites moved to `.sage_history/` as retained
evaluation evidence. See `docs/development/TEST_TIERS.md`.

## Mythic output

Native chat renders markdown and updating tool-use cards. Use tables for enumerations and fenced blocks for
commands or raw output. Do not add textual timestamps; Mythic already timestamps messages. Do not duplicate tool
cards with legacy text output on the chat path.

## Repository architecture

The monorepo boundary and future migration criteria are documented in
`docs/architecture/REPOSITORY_BOUNDARIES.md`. Product runtime must never import development evaluation, range,
operator-skill, private-plan, or generated-evidence packages.
