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

`bootstrap-reset` must report a running `sage_chat_container`, a wildcard-scoped autonomous API token, an empty locked prepared channel named
`Sage GOAD Ready`, and `sage_payload_created: false`.

The standalone command keeps that prepare-chat behavior for compatibility. When the operator will create the
operation and demo chat manually, use:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset --no-prepare-chat
```

Pair it with `readiness --no-require-prepared-channel`. This relaxes only the prepared-channel section; Sage
container, token, range, clock, BloodHound, and unique foothold requirements remain enforced.

After Apollo is launched as `NORTH\samwell.tarly` on CASTELBLACK, run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py post-callback-preflight
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived
```

Readiness delegates to the shared `sage-goad-reset` readiness contract. It is true only when the sanitized
startup identity, runtime DB archival, exact six-VM/IP state, clock check, BloodHound API/domains, BloodHound
MCP checkout and exact required tool set, Mythic chat/token, unique live foothold, and prepared channel are all
ready. `selected_sage_cb` is intentionally null.

`post-callback-preflight` is now strictly task-free. It waits for the selected live Apollo callback through
Mythic control-plane observation, synchronizes range clocks out of band, and returns explicit zero-task metadata.
It does not issue callback tasks, purge Kerberos tickets, or claim target-probed domain/identity output.
The `sage-goad-reset` orchestrator additionally proves this with observed Mythic task-count deltas before
admitting reset completion.

## Canonical Apollo Bootstrap Path

Do not re-derive Apollo bootstrap manually when these helpers cover the case.

- Fresh clean-baseline foothold: `bootstrap-reset` → payload deploy skill `deploy` with the interactive Apollo
  launch path → `post-callback-preflight` → `readiness --runtime-dbs-archived`.
- Retained or purpose-range foothold: `bootstrap-reset --use-retained-callback ...` → payload deploy skill
  `launch-existing` or `launch_apollo_foothold.sh` with a settle window and unique-callback gate →
  `post-callback-preflight` → `readiness --runtime-dbs-archived`.

For retained or purpose-range launches, do not call the foothold ready until the callback lane is unique after the
settle window. Always rediscover the active callback ID after launch; imported callback rows are historical metadata,
not tasking targets.

## Retained Foothold

Export before reset:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py export-callback-config --callback <foothold-display-id> --output skills/sage-callback-bootstrap/merlin_callback_config.json
```

Import after reset:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset --use-retained-callback --retained-callback-config skills/sage-callback-bootstrap/merlin_callback_config.json
```

Imported callback rows are forced inactive so they remain historical metadata and cannot be selected for
tasking. Launching the retained payload creates the active callback row used by Sage.

Then launch the retained payload and run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived --foothold-payload-type merlin
```

The baked Apollo import remains available through `--use-baked-apollo` for legacy snapshots only.
