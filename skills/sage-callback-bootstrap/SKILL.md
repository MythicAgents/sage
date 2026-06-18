---
name: sage-callback-bootstrap
description: Repo-local post-Mythic-reset Sage/Apollo payload and callback bootstrap workflow. Use when Codex, Claude Code, or an operator needs to inspect Mythic payload types, create fresh Sage and Apollo payloads after Mythic reset, check callback readiness, or rediscover live callback IDs for a Sage GOAD solve.
---

# Sage Callback Bootstrap

Use after `$sage-goad-reset` archives the active runtime databases, resets Mythic, and restarts local Sage.
Mythic reset changes payload crypto keys, so old Sage/Apollo payload files and callback IDs are invalid.

## Credentials

Do not store passwords in skills. Use `MYTHIC_ADMIN_PASSWORD`, a local gitignored Mythic `.env`, or an operator
secret manager. The bundled bootstrap script resolves environment credentials first.

## Payload Defaults

Sage and Apollo payload defaults belong in the repo-local skill `.env`, not in ad hoc command lines:

```bash
skills/sage-callback-bootstrap/.env
```

The tracked template is:

```bash
skills/sage-callback-bootstrap/.env.example
```

The file stores Sage model/API settings plus Apollo filename, callback, C2 timing, output, and download settings.
Set `APOLLO_CALLBACK_HOST` to the Mythic address reachable from GOAD. Shell variables and CLI arguments still
override the file for one-off builds.

## Workflow

Inspect first:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py inspect
```

Create fresh payloads from the skill `.env`:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py create-all
```

After Apollo launches on CASTELBLACK, verify readiness and discover callbacks:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived
.venv/bin/python skills/sage-live-runner/scripts/sage_task.py callbacks
```

Do not run a solve unless readiness reports `ready: true`.

## Bundled Scripts

- `bootstrap_payloads.py`
- `check_merlin_c2.py`
