---
name: sage-callback-bootstrap
description: Repo-local post-Mythic-reset Sage/Apollo callback bootstrap workflow. Use when Codex, Claude Code, or an operator needs to export/import a baked Apollo callback config, inspect Mythic payload types, create Sage/Apollo payloads after Mythic reset, check callback readiness, or rediscover live callback IDs for a Sage GOAD solve.
---

# Sage Callback Bootstrap

Use after `$sage-goad-reset` archives the active runtime databases, resets Mythic, and restarts local Sage.
Without a callback-config import, Mythic reset changes payload crypto keys and old payload files are invalid.
An imported callback config restores the crypto/config identity used by an Apollo executable baked into a Ludus
snapshot.

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

## Baked Apollo Setup

Export the live Apollo callback config once, before taking the Ludus snapshot that contains the running process:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py export-callback-config --callback <apollo-display-id>
```

The default output is `skills/sage-callback-bootstrap/apollo_callback_config.json`. It is gitignored, contains
sensitive callback cryptographic material, and is written mode `0600`.

## Workflow

Inspect first:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py inspect
```

During each reset, use:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset
```

The command creates Sage first so a clean Mythic database assigns Sage callback display ID `1`. When the exported
config exists, it then imports Apollo. When it does not exist, it falls back to a fresh Apollo build/download.
`create-all` remains available as an explicit fresh-payload fallback.

After the baked Apollo process reconnects on CASTELBLACK, verify readiness and discover callbacks:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived
.venv/bin/python skills/sage-live-runner/scripts/sage_task.py callbacks
```

Do not run a solve unless readiness reports `ready: true`.

## Bundled Scripts

- `bootstrap_payloads.py`
- `check_merlin_c2.py`
