---
name: sage-goad-reset
description: Repo-local Sage GOAD/Trust Walker full lab reset and readiness workflow. Use when Codex, Claude Code, or an operator asks to reset everything, perform a full reset, archive Sage/Phoenix runtime databases, reset Docker-backed Mythic, reset or verify Ludus GOAD, wipe/check BloodHound CE, restart local Sage safely, generate fresh payloads, import a retained foothold config, establish a Samwell callback, or preflight before a guided Sage GOAD solve.
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

Treat `full reset`, `reset everything`, or an equivalent request as the entire workflow below, including Sage
chat verification, fresh Apollo creation, and Samwell callback establishment through `$sage-callback-bootstrap`. A Ludus-only rollback
must be requested explicitly as a range-only or GOAD-only reset.

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

Any extra `KEY=VAL` positional args are applied as env overrides to the relaunched Sage, winning over the snapshotted env (last value wins). The eval-gauge Gate Experiment uses this to pin `SAGE_ENGAGEMENT_ID=<run token>` (and per-config settings) so Sage writes its ledger under the token the gauge reads — see `skills/sage-eval-gauge/SKILL.md`.

6. Run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset
```

   This verifies the running Sage chat container, then builds/downloads a fresh Apollo payload for the
   clean-baseline workflow. It does not create a Sage payload or callback.

   For an intentional retained foothold reuse such as the current Merlin R-C2 flow, replace that command with:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset --use-retained-callback --retained-callback-config skills/sage-callback-bootstrap/merlin_callback_config.json
```

   This imports the exported callback config and stops before target-side payload execution. After the operator
   launches the retained payload, use `readiness --runtime-dbs-archived --foothold-payload-type merlin`.
7. Open an RDP session as `NORTH\samwell.tarly` on CASTELBLACK, then launch the fresh Apollo
   payload with `skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py deploy` using
   `--launch-method scheduled-task-interactive --add-defender-exclusion`. The exclusion is scoped to the staged
   bootstrap payload file, which defaults to `C:\Users\Public\apollo.exe`; without it, Defender quarantines stock
   Apollo on clean-baseline. After a new callback is observed, the deploy helper disconnects the RDP session with
   `tsdiscon` by default so the local RDP client exits without logging off Apollo's Windows session.
8. Run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py post-callback-preflight
```

   This waits for the live Samwell Apollo callback, synchronizes range clocks, purges stale Kerberos tickets, and
   verifies UTC/domain/identity output. Treat any nonzero exit as a reset failure.
9. Rediscover the Apollo callback ID. Never trust historical IDs. The Sage solve uses a fresh chat channel, not
   a callback.

## GOAD

```bash
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py snapshot <name> --include-ram
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py rollback --yes
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py poweron all
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status
.venv/bin/python skills/sage-goad-reset/scripts/sync_range_time.py sync --yes
.venv/bin/python skills/sage-goad-reset/scripts/sync_range_time.py check
```

Use `--include-ram` only when a snapshot must preserve running process state. The current clean-baseline workflow
does not require RAM-backed snapshots.

`rollback --yes` resolves the target snapshot automatically — there is no hardcoded default. If the range has
exactly one snapshot it uses it; if several, it prompts at a TTY, otherwise it prints the snapshot names to
stdout and exits `3` (the caller must pick one and re-invoke as `rollback <name> --yes`). An explicit `<name>`
before `--yes` is validated against the live list — an unknown name exits `2` with the available names. Pass an
explicit name only when intentionally selecting a non-obvious baseline. The old `eval-defender-apollo` RAM-backed
path is retired for normal rehearsals.

Expected IPs: router `10.4.10.254`, DC01 `.10`, DC02 `.11`, DC03 `.12`, CASTELBLACK/SRV02 `.22`,
BRAAVOS/SRV03 `.23`. Poll until Windows guest IPs populate.

The standalone sync/check commands remain available for diagnosis. The helper obtains existing WinRM credentials
from the Ludus ansible inventory, sets all three DCs before member servers, and disables Windows Time so trigger
start cannot restore stale snapshot CMOS time during the rehearsal. The next rollback restores service state. The
helper fails closed when any host remains more than 60 seconds from the controller or Windows Time is not stopped
and disabled.

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

After a target-scope collection on a GOAD-style cross-forest range, use the read-only bridge probe before
blaming Sage for an empty frontier:

```bash
uv --directory /home/john/dev/bloodhound_mcp run python \
  /home/john/dev/sage/skills/sage-goad-reset/scripts/check_cross_forest_laps_bridge.py \
  --source-domain <controlled-root-domain> \
  --target-domain <trusted-target-domain>
```

The probe mirrors Sage's reconciler shape: `User -> MemberOf* -> principal -> ReadLAPSPassword -> Computer`.
It fails closed when the current BloodHound graph has no cross-forest managed-secret bridge and intentionally
does not treat `SyncLAPSPassword` as equivalent to `ReadLAPSPassword`.

## Bundled Scripts

- `_sage_relaunch.py`
- `archive_runtime_dbs.py`
- `bh_reset.py`
- `check_cross_forest_laps_bridge.py`
- `liveness.py`
- `ludus.py`
- `mcp_check.py`
- `mythic_reset.sh`
- `pkinit_padata_probe.py`
- `sage_restart.sh`
- `sage_stop.sh`
