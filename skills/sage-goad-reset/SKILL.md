---
name: sage-goad-reset
description: Repo-local Sage GOAD/Trust Walker full lab reset and readiness workflow. Use when Codex, Claude Code, or an operator asks to reset everything, perform a full reset, archive Sage/Phoenix runtime databases, reset Docker-backed Mythic, reset or verify Ludus GOAD, wipe/check BloodHound CE, restart local Sage safely, restore a baked Apollo callback config, generate payloads, or preflight before a guided Sage GOAD solve.
---

# Sage GOAD Reset

Use from `/home/john/dev/sage`. Sage runs locally in the `sage` tmux session for this workflow, not in a
Docker/Mythic Sage container. Mythic remains Docker-backed and is managed through `mythic-cli`. Do not delete
runtime databases or retained archives.

## Invocation

In Codex, use:

```text
$sage-goad-reset full reset
```

Treat `full reset`, `reset everything`, or an equivalent request as the entire workflow below, including fresh
Sage creation and Apollo callback restoration through `$sage-callback-bootstrap`. Until a baked Apollo callback
config has been exported, the bootstrap command falls back to fresh Apollo payload generation/download. A
Ludus-only rollback must be requested explicitly as a range-only or GOAD-only reset.

## Order

1. Stop local Sage before moving its databases:

```bash
/bin/bash skills/sage-goad-reset/scripts/sage_stop.sh
```

2. Archive the active databases. The helper moves them beside their originals as
   `sage_YYYYMMDD-HHMM.db` and `phoenix_YYYYMMDD-HHMM.db`. It never overwrites an existing archive:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/archive_runtime_dbs.py
```

3. Reset and restart Mythic. This uses the Docker-backed Mythic CLI without `sudo`:

```bash
/bin/bash skills/sage-goad-reset/scripts/mythic_reset.sh --yes
```

4. Reset/verify GOAD and BloodHound.
5. Restart local Sage in tmux:

```bash
/bin/bash skills/sage-goad-reset/scripts/sage_restart.sh SAGE_ENGAGEMENT_GATE=1 SAGE_BLOODHOUND_MCP_DIR=/home/john/dev/bloodhound_mcp
```

6. Run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset
```

   This creates Sage first, then imports the retained Apollo callback config when present. On a clean Mythic
   database, Sage is callback `1`. If the config is absent, it creates and downloads a fresh Apollo payload after
   Sage as before.
7. Rediscover callback IDs. Never trust historical IDs.

## GOAD

```bash
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py snapshot <name> --include-ram
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py rollback --yes
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py poweron all
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status
```

Use `--include-ram` only when a snapshot must preserve running process state, such as a baked Apollo callback.
Without it, rollback restores disk state and powers the VM off rather than resuming guest memory.

The default rollback snapshot is `eval-defender-apollo`, which preserves CASTELBLACK's Defender exclusions and
the running Apollo process through RAM state. Pass an explicit snapshot name before `--yes` only when
intentionally selecting another baseline.

Expected IPs: router `10.4.10.254`, DC01 `.10`, DC02 `.11`, DC03 `.12`, CASTELBLACK/SRV02 `.22`,
BRAAVOS/SRV03 `.23`. Poll until Windows guest IPs populate.

Optional MEEREEN PKINIT readiness check before launching Apollo:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/pkinit_padata_probe.py --kdc 10.4.10.12 --realm ESSOS.LOCAL --user administrator
```

Proceed when the JSON shows `error_name: KDC_ERR_PREAUTH_REQUIRED` and `pkinit_advertised: true` with PA type
`16`. This does not use Sage, Mythic, Apollo, or a PFX; it only checks whether the KDC advertises PKINIT in
pre-authentication METHOD-DATA.

## BloodHound

```bash
uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-goad-reset/scripts/bh_reset.py wipe --yes
uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-goad-reset/scripts/bh_reset.py status
```

The wipe waits 10 seconds before its first verification poll, then polls every 5 seconds until
`available-domains: count=0`. Do not run a separate immediate status check.

## Bundled Scripts

- `_sage_relaunch.py`
- `archive_runtime_dbs.py`
- `bh_reset.py`
- `liveness.py`
- `ludus.py`
- `mcp_check.py`
- `mythic_reset.sh`
- `pkinit_padata_probe.py`
- `sage_restart.sh`
- `sage_stop.sh`
