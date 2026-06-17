---
name: sage-goad-reset
description: Repo-local Sage GOAD/Trust Walker lab reset and readiness workflow. Use when Codex, Claude Code, or an operator needs to reset or verify Ludus GOAD, wipe/check BloodHound CE, restart Sage safely, coordinate operator-owned Mythic and runtime DB cleanup gates, or preflight before a guided Sage GOAD solve.
---

# Sage GOAD Reset

Use from `/home/john/dev/sage`. Sage runs locally in the `sage` tmux session for this workflow, not in a
Docker/Mythic Sage container. Do not delete files. Runtime cleanup of `Payload_Type/sage/sage.db`,
`Payload_Type/sage/.phoenix/phoenix.db`, and retained historical DBs is operator-owned.

## Order

1. Operator resets Mythic first.
2. Operator removes current runtime DBs only when they intend a clean run. Codex never removes files.
3. Restart Sage after operator confirmation:

```bash
/bin/bash skills/sage-goad-reset/scripts/sage_restart.sh SAGE_ENGAGEMENT_GATE=1 SAGE_BLOODHOUND_MCP_DIR=/home/john/dev/bloodhound_mcp
```

4. Use `$sage-callback-bootstrap` to create fresh Sage/Apollo payloads and callbacks.
5. Reset/verify GOAD and BloodHound before any guided solve.
6. Rediscover callback IDs. Never trust historical IDs.

## GOAD

```bash
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py rollback clean-baseline --yes
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py poweron all
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status
```

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

Require `available-domains: count=0` before ingest. The wipe is asynchronous.

## Bundled Scripts

- `_sage_relaunch.py`
- `bh_reset.py`
- `liveness.py`
- `ludus.py`
- `mcp_check.py`
- `pkinit_padata_probe.py`
- `sage_restart.sh`
