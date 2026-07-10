---
name: sage-callback-bootstrap
description: Repo-local post-Mythic-reset Sage chat and foothold bootstrap workflow. Use to verify the Sage chat container, create or restore Apollo/Merlin footholds, check readiness, or rediscover the live CASTELBLACK callback.
---

# Sage Callback Bootstrap

Use after `$sage-goad-reset` archives runtime databases, resets Mythic, and restarts Sage. Sage is a native Mythic
v4 chat container; do not create or wait for a Sage payload callback.

## Credentials

The helper resolves `MYTHIC_ADMIN_PASSWORD` first, then `MYTHIC_ENV_PATH`, then
`/home/john/dev/mythic_v4/.env`, with the v3 `.env` retained only as fallback.

## Workflow

Inspect Mythic:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py inspect
```

Verify the Sage chat container and create a fresh Apollo payload:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset
```

`bootstrap-reset` must report a running `sage_chat_container` and `sage_payload_created: false`.

After Apollo is launched as `NORTH\samwell.tarly` on CASTELBLACK, run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py post-callback-preflight
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived
```

Readiness is true only when the Sage chat container is running, the expected foothold is live, and runtime DB
cleanup was confirmed. `selected_sage_cb` is intentionally null.

## Retained Foothold

Export before reset:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py export-callback-config --callback <foothold-display-id> --output skills/sage-callback-bootstrap/merlin_callback_config.json
```

Import after reset:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset --use-retained-callback --retained-callback-config skills/sage-callback-bootstrap/merlin_callback_config.json
```

Then launch the retained payload and run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived --foothold-payload-type merlin
```

The baked Apollo import remains available through `--use-baked-apollo` for legacy snapshots only.
