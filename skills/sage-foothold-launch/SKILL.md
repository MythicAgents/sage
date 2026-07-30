---
name: sage-foothold-launch
description: Rebuild a live Apollo foothold on the GOAD range as NORTH\samwell.tarly with NO operator, NO RDP client, and NO X display — a pure WinRM scheduled-task (batch-logon) launch. Use to reset the range and re-establish a foothold end to end unattended (headless agent or cron), or to launch a foothold against an already-booted, clock-synced range. Supersedes the RDP/Xvfb path (launch_apollo_foothold.sh) for unaided use.
---

# Sage Foothold Launch

Rebuilds a live Apollo callback as `NORTH\samwell.tarly` without an operator, an RDP session, or an
X display. Where `sage-callback-bootstrap` creates the Apollo payload and `launch_apollo_foothold.sh`
requires a live Xwayland/Xvfb display plus an `xfreerdp3` PTY to open an interactive Samwell desktop,
this skill launches the payload through a **WinRM-created scheduled task that runs as samwell (batch
logon)** — the same exec path `sync_range_time.py` uses (`winrm_session()`/`run_ps()` over the Ludus
ansible-inventory admin credentials) and the same launch primitive
`deploy_payload_via_ludus.py --launch-method scheduled-task` already implements. Nothing needs a desktop,
so a headless agent or cron can rebuild a foothold end to end.

## Why this exists

The RDP path could not run unaided: it depended on `DISPLAY` (`:0` or `:99`) and an interactive Windows
logon session for samwell. On a host with no display, or under a tool wrapper with no controlling
terminal, `xfreerdp3` dies pre-auth (exit 144). The scheduled-task **batch** logon needs none of that:
`schtasks /RU 'NORTH\samwell.tarly' /RP <password>` runs Apollo over the WinRM admin session, no desktop.

**Why launching a *domain* user non-interactively is "the real problem" (measured 2026-07-28).** Two
non-interactive WinRM paths fail out of the box: `scheduled-task-s4u` cannot even register a task for a
domain principal as the local WinRM admin (`Register-ScheduledTask: Access is denied`, 0x80070005); the
plain batch task *registers* but never starts because a member server denies **"Log on as a batch job"**
(`SeBatchLogonRight`) to a standard domain account (`the task is registered, but may fail to start. Batch
logon privilege needs to be enabled`). This is exactly why the prior author used RDP-interactive. This skill
closes it the pure-WinRM way: it **grants `SeBatchLogonRight` to the run-as user via `secedit`** over the
admin session before the batch launch (reversible, range-local), then the batch task starts and Apollo
checks in. Verified 2026-07-28: reset → grant → batch launch → unique live foothold, unaided.

Trade-off: the resulting Apollo runs under a **batch** logon, not interactive. That is sufficient for
bounded native canaries (which only need a live, taskable Apollo as samwell). If a specific attack path
requires an *interactive* logon type, use the RDP path
(`skills/sage-mythic-payload-deploy/scripts/launch_apollo_foothold.sh`), documented as the fallback below.
Pass `--no-grant-batch-right` to skip the privilege grant (e.g. if the right is already present).

## Credentials & prerequisites

- Ludus API auth (rollback/poweron/status/clock): `.mcp.json` → `LUDUS_URL` + `LUDUS_API_KEY`, via the
  bundled `skills/sage-goad-reset/scripts/ludus.py` and `sync_range_time.py`. The bare `ludus` CLI has no
  credential on this host.
- Mythic auth (payload download): `MYTHIC_ADMIN_PASSWORD` (resolved by `deploy_payload_via_ludus.py`).
- samwell run-as password: `SAGE_RUN_AS_PASSWORD`, sourced automatically from `Payload_Type/sage/.env`
  when not already exported. (Mythic's credential store is empty on this range, so `--run-as-credential-account`
  does not resolve — the env value is the source of truth.)
- `--serve-host` must be reachable from the Windows target. Default is the operator Tailscale IP
  `100.108.59.85`; override with `--serve-host` or `SAGE_SERVE_HOST`.
- Every Python invocation needs `SSL_CERT_FILE=Payload_Type/sage/certs/combined-bundle.pem`; the script
  sets it for its child processes automatically. Run the script itself with that prefix.

## Workflow

Full unaided reset → foothold → verify (destroys the current foothold, rebuilds from the snapshot):

```bash
SSL_CERT_FILE=Payload_Type/sage/certs/combined-bundle.pem \
  .venv/bin/python skills/sage-foothold-launch/scripts/foothold_launch.py rebuild \
  --snapshot isc49r-scored-20260726
```

`rebuild` runs: `ludus rollback <snapshot> --yes` → `ludus poweron all` → wait for six VMs ON →
`sync_range_time.py sync --yes` (retried until clock `ready`) → launch as samwell → verify a **unique**
live Apollo foothold by execution.

Launch only, against an already-booted and clock-synced range (does not revert):

```bash
SSL_CERT_FILE=Payload_Type/sage/certs/combined-bundle.pem \
  .venv/bin/python skills/sage-foothold-launch/scripts/foothold_launch.py launch
```

Readiness check only (no mutation):

```bash
SSL_CERT_FILE=Payload_Type/sage/certs/combined-bundle.pem \
  .venv/bin/python skills/sage-foothold-launch/scripts/foothold_launch.py verify
```

## Verdict and exit codes

`launch`/`rebuild` print JSON with a `verdict`:

- `unique_live_foothold` — exactly one new live Apollo callback and `foothold.ready == true`. Exit 0.
- `non_countable_multiple_footholds` — more than one new live callback, or a reported duplicate lane.
  Exit 1. Fix the foothold state (kill the extra callback / revert) before running a canary; a
  non-unique lane is not a countable starting state.
- `no_new_foothold` — the launch produced no live callback. Exit 1.

Uniqueness is measured against the set of live callbacks captured **before** the launch, and confirmed by
`native_chat.py inspect` (execution), never inferred from the deploy command's self-report. A slow
batch-logon check-in is handled: the launcher re-probes before retrying so a late callback does not
trigger a second deploy (which would create a duplicate lane).

## Gotchas

- **Batch vs interactive logon.** This skill produces a batch-logon Apollo. For canaries that is fine.
  For an attack path that needs an interactive token, use the RDP fallback (below).
- **`native_chat.py inspect` overall `ready` is `false` on a healthy foothold.** The blocker is
  `runtime_databases` (a clean-solve hygiene gate), not the foothold. This skill keys its verdict off
  `foothold.ready`, which is the correct signal; sessions 1–5 all ran canaries with overall `ready == false`.
- **Post-rollback clock skew is expected**, not a fault — the snapshot's CMOS time returns with the disk.
  `rebuild` re-syncs clocks after poweron and waits for `ready` before launching.
- **DC netlogon warm-up.** A domain scheduled task can fail to register while the DC is still servicing
  netlogon after a cold boot; `rebuild` waits for VMs ON and clock-ready, and the launch retries a bounded
  number of times, re-probing between attempts to avoid duplicate footholds.
- **Re-verify `foothold.ready == true` immediately before any effect-intending canary** (standing process
  rule from the 2026-07-26 operator error) — `verify` is the one-line way to do it.

## Fallback: interactive (RDP) logon

If an interactive logon type is required, use the proven RDP path (needs a live X display):

```bash
skills/sage-mythic-payload-deploy/scripts/launch_apollo_foothold.sh 10.4.10.22 'NORTH\samwell.tarly'
```
