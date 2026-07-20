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

## Range Source and Lifecycle

For reusable AD ranges intended for publication or transfer, keep the portable definition in DreadGOAD format and
use Ludus as the runtime/reset substrate. A Sage-only benchmark fixture may be Ludus-first in
`ludus/sage-purpose-ranges/` when portability is not a goal. This skill operates on runtime state; it does not
prove that a source definition is publishable or that a range is ready for evidence.

- If the range should be reusable outside Sage, author the portable source in DreadGOAD format and keep a verified
  Ludus execution path.
- If the range exists only as a Sage-specific benchmark fixture, prefer the standalone
  `ludus/sage-purpose-ranges/` pattern.
- In both cases, use Ludus state for reset/readiness claims and report source format separately from runtime state.

Use these state labels in updates: `defined`, `provisioned`, `snapshotted`, `callback-ready`, `countable`,
`burned`, and `complete`. Do not say a range is ready or deployed when only the source exists or provisioning has
started. After a burned attempt, stop-loss, or completed tranche, power down any range not needed for the next
operation before leaving the lab idle.

### Custom DreadGOAD Purpose-Range Workflow

For a custom DreadGOAD-format purpose range, keep source validation and runtime targeting separate:

- `dreadgoad config show` is not sufficient proof that the custom lab's playbook sequence is the one provisioning
  will resolve. Validate through the actual provision resolver with a deliberate sentinel such as
  `dreadgoad provision --from __not_a_playbook__` before the long run, then require the returned playbook list to
  be the intended custom sequence.
- DreadGOAD acts against the current Ludus default range for the API-key user. Before `dreadgoad infra apply` or
  `dreadgoad provision`, create the isolated Ludus range object for the purpose range, set it as that user's
  default, verify the selected range identity, and keep unrelated ranges powered off.
- DreadGOAD does not consume Sage's `.mcp.json` automatically. Pass the intended `LUDUS_URL` and `LUDUS_API_KEY`
  explicitly from the correct local credential source for every infra/provision command.
- For cross-forest ACL/bootstrap scripts, do not assume ambient `NTAccount.Translate()` or one forest's AD cmdlets
  can resolve a foreign principal. Validate the exact auth context before the long playbook; when needed, perform an
  explicit least-privileged SID lookup in the principal's home domain and apply the ACL locally with that SID.

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

4. Reset/verify GOAD, then wipe BloodHound and require zero domains before continuing:

```bash
uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-goad-reset/scripts/bh_reset.py wipe --yes
```

   The wipe command includes its own delayed verification and must finish with `available-domains: count=0`.
   A nonzero exit or any remaining domain blocks the reset.
5. Restart local Sage in tmux:

```bash
/bin/bash skills/sage-goad-reset/scripts/sage_restart.sh SAGE_ENGAGEMENT_GATE=1 SAGE_BLOODHOUND_MCP_DIR=/home/john/dev/bloodhound_mcp
```

Any extra `KEY=VAL` positional args are applied as env overrides to the relaunched Sage, winning over the snapshotted env (last value wins). The eval-gauge Gate Experiment uses this to pin `SAGE_ENGAGEMENT_ID=<run token>` (and per-config settings) so Sage writes its ledger under the token the gauge reads — see `skills/sage-eval-gauge/SKILL.md`.
The launcher is canonical for local Sage restarts: it always uses the repo virtualenv, defaults
`SAGE_BLOODHOUND_MCP_DIR` when it is absent, and records a redacted startup identity under
`/tmp/sage_startup_identity.json` for later readiness/manifest inspection.

6. Run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset
```

   This verifies the running Sage chat container, ensures the scoped Mythic API token, creates or reuses one
   empty locked AI channel named `Sage GOAD Ready`, then builds/downloads a fresh Apollo payload for the
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

10. Run the shared readiness contract before a live row:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived
```

That report is the single readiness source for callback bootstrap and native chat. It aggregates sanitized
startup identity, runtime DB archival, exact six-VM/IP state, clock readiness, BloodHound API/domains,
BloodHound MCP checkout plus exact required tools, Mythic chat/token state, unique foothold selection, and
prepared channel readiness.

## GOAD

```bash
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py snapshots
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py snapshot <name> --include-ram
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py rollback <snapshot-name> --yes
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py poweron all
.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status
.venv/bin/python skills/sage-goad-reset/scripts/sync_range_time.py sync --yes
.venv/bin/python skills/sage-goad-reset/scripts/sync_range_time.py check
```

Use `--include-ram` only when a snapshot must preserve running process state. The logical clean-baseline workflow
does not require RAM-backed snapshots, but `clean-baseline` is not guaranteed to be the literal Ludus snapshot
name. Always list restore targets and record the exact selected identity.

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

A range is not `callback-ready` or `countable` after rollback alone. Clock sync, BloodHound reset/settling, Sage
restart env, foothold uniqueness, and callback readiness still have to pass before any live row can count.

Optional MEEREEN PKINIT readiness check before launching Apollo:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/pkinit_padata_probe.py --kdc 10.4.10.12 --realm ESSOS.LOCAL --user administrator
```

Treat this as a method-data diagnostic, not a go/no-go readiness gate. JSON with
`error_name: KDC_ERR_PREAUTH_REQUIRED` and `pkinit_advertised: true` means only that the
KDC advertised PA type `16`; it does not prove the DC currently has a usable
KDC-authentication certificate. A real PKINIT attempt or target-side KDC certificate
inspection is still required to establish usable PKINIT. This probe does not use Sage,
Mythic, Apollo, or a PFX.

## BloodHound

```bash
uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-goad-reset/scripts/bh_reset.py wipe --yes
uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-goad-reset/scripts/bh_reset.py status
```

The wipe is mandatory for every full reset. It waits 10 seconds before its first verification poll, then polls
every 5 seconds until `available-domains: count=0`. Do not run a separate immediate status check.

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
- `readiness_contract.py`
- `mythic_reset.sh`
- `pkinit_padata_probe.py`
- `sage_restart.sh`
- `sage_stop.sh`
