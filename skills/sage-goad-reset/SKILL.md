---
name: sage-goad-reset
description: Repo-local Sage GOAD/Trust Walker full lab reset and readiness workflow. Use when Codex, Claude Code, or an operator asks to reset everything, perform a full reset, archive Sage/Phoenix runtime databases, reset Docker-backed Mythic, reset or verify Ludus GOAD, wipe/check BloodHound CE, restart local Sage safely, generate fresh payloads, import a retained foothold config, establish a Samwell callback, or preflight before a guided Sage GOAD solve.
---

# Sage GOAD Reset

Use from the repository root. The reset brings back the Sage named by `SAGE_DEPLOYMENT_MODE`: `local` (the working
tree in the `sage` tmux session) or `container` (Mythic's Sage container built from the working tree). See
[Deployment modes](#deployment-modes). Mythic remains Docker-backed and is managed through `mythic-cli`. Do not
delete runtime databases or retained archives.

## Invocation

In Codex, use:

```text
$sage-goad-reset full reset
```

Treat `full reset`, `reset everything`, or an equivalent request as the entire workflow below, including Sage
chat verification, fresh Apollo creation, and Samwell callback establishment through `$sage-callback-bootstrap`. A Ludus-only rollback
must be requested explicitly as a range-only or GOAD-only reset.

For repeatable operator execution, use the resumable orchestrator. Previewing the plan is read-only:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/reset_orchestrator.py plan --snapshot <exact-snapshot> --callback-host <mythic-callback-url> --deployment-mode <local|container>
```

Start the live reset only with explicit authorization:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/reset_orchestrator.py start --snapshot <exact-snapshot> --callback-host <mythic-callback-url> --deployment-mode <local|container> --yes
```

`start` refuses without a deployment mode (the flag, else `SAGE_DEPLOYMENT_MODE`); the mode is saved in the
checkpoint and passed to every phase, so a resume restarts the same Sage. The orchestrator composes the existing
helpers in canonical order, writes an atomic redacted checkpoint under
`.sage_engagement/reset_runs/`, and pauses at `await-operator-foothold-launch`. It never deploys or launches the
payload itself. Checkpoint load and write validate the exact phase order, legal state progression, command
derivation, operator boundary, terminal state, and zero-task observation lineage; malformed or ambiguous state
fails closed before another phase can run. After the operator completes the documented interactive RDP launch:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/reset_orchestrator.py resume --checkpoint <checkpoint.json> --operator-foothold-launched --yes
```

It establishes an observed Mythic task-count baseline immediately after Mythic reset and requires an exact zero
delta after bootstrap, operator payload launch, post-callback preflight, and final readiness. A nonzero delta
blocks completion.

The orchestrator defaults to `--no-prepare-chat`, leaving operation and chat creation to the operator. Pass
`--prepare-chat` only when the empty `Sage GOAD Ready` channel is wanted. The standalone `bootstrap-reset`
command retains its historical prepare-chat default.

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

Before step 1, select and retain the bootstrap mode from
`skills/sage-mythic-payload-deploy/SKILL.md`:

- A pre-staged Apollo snapshot plus imported retained callback metadata uses the interactive
  `launch_apollo_foothold.sh` → `launch-existing` path and performs no Mythic payload download.
- A clean baseline without staged Apollo uses a newly built, verified-downloadable Mythic payload and `deploy`.
- `sage-foothold-launch` is the batch-logon path only when the operation explicitly accepts that token type and
  the selected Mythic payload has real downloadable bytes.

Record the snapshot, payload source, token type, launcher, download expectation, settle window, and callback
uniqueness rule before live allocation. Retain bounded launcher output on failure. Do not infer bootstrap mode from
the caller being interactive or headless.

For the qualified Trust Walker retained foothold, use the complete immutable fixture named in
`skills/sage-mythic-payload-deploy/SKILL.md`. Validate its snapshot, config hash, payload UUID, task state,
executable path, and executable hash together before starting the task.

1. Stop every Sage, local and container, before moving databases:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/sage_deployment.py stop-all
```

It stops whichever is running (tmux through `sage_stop.sh`, the container through `mythic-cli stop sage`) and
exits non-zero if either is still up. An absent `sage` tmux session is an already-stopped state.

2. Preserve the run's evidence, then archive the active databases. The helper first copies the installed
   container's `sage*.db`, `.phoenix/phoenix*.db` and `.sage_engagement/` with SQLite online backup (safe while
   the container runs), and dumps Mythic's database with `pg_dump` inside its Postgres container, into
   `.sage_history/runtime-db-archive/reset-YYYYMMDD-HHMM/` with a hashed `MANIFEST.json`. Only after every copy
   verifies does it move the local databases beside their originals as `sage_YYYYMMDD-HHMM.db` and
   `phoenix_YYYYMMDD-HHMM.db`. A failed copy stops here and nothing is moved, so the reset does not continue.
   It never overwrites an existing archive. The installed directory comes from `MYTHIC_ENV_PATH`; without it the
   container copy is skipped and recorded in the manifest:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/archive_reset_evidence.py                     # local
.venv/bin/python skills/sage-goad-reset/scripts/archive_reset_evidence.py --target installed  # container
```

   `--target installed` moves the container's own state instead: `sage.db`, `.phoenix/phoenix.db` and
   `.sage_engagement/` inside the installed service directory, leaving the repo's databases (the local Sage's)
   alone. Mythic reuses channel and operation ids after its reset, so state left there makes the next run
   refuse its first prompt or start from an old run's facts.

   Step 3 wipes Mythic's database, including every task's output. Pass `--skip-mythic-dump` only when that
   history is deliberately not needed; the manifest records the skip. `archive_runtime_dbs.py` remains the
   local-only move for callers that do not reset Mythic.

3. Reset and restart Mythic. This uses the Docker-backed Mythic CLI without `sudo`:

```bash
/bin/bash skills/sage-goad-reset/scripts/mythic_reset.sh --yes
```

   It never starts Sage, in either mode: with Mythic's `REBUILD_ON_START`, `mythic-cli start` rebuilds the Sage
   image, which silently replaced the image under test. Step 5 brings back the intended Sage.

4. Reset/verify GOAD, then wipe BloodHound and require zero domains before continuing:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/sage_runtime_env.py mcp-run skills/sage-goad-reset/scripts/bh_reset.py wipe --yes
```

   The wipe command includes its own delayed verification and must finish with `available-domains: count=0`.
   A nonzero exit or any remaining domain blocks the reset. `mcp-run` runs the script inside the BloodHound
   MCP checkout's environment; see [Where the BloodHound MCP checkout comes from](#where-the-bloodhound-mcp-checkout-comes-from).
5. Bring back the intended Sage. Local tmux:

```bash
/bin/bash skills/sage-goad-reset/scripts/sage_restart.sh SAGE_ENGAGEMENT_GATE=1
```

   Container (sync the working tree, start from the existing image, prove parity):

```bash
.venv/bin/python skills/sage-goad-reset/scripts/sage_deployment.py deploy --mode container
```

   The rest of this step describes the tmux launcher.

Any extra `KEY=VAL` positional args are applied as env overrides to the relaunched Sage, winning over the snapshotted env (last value wins). An empty `KEY=` clears that variable for the relaunched Sage, except `SAGE_BLOODHOUND_MCP_DIR`: the BloodHound check runs after the overrides and falls back to the env files, so `KEY="$KEY"` with an unexported variable cannot erase the configured directory. The eval-gauge Gate Experiment uses this to pin `SAGE_ENGAGEMENT_ID=<run token>` (and per-config settings) so Sage writes its ledger under the token the gauge reads — see `skills/sage-eval-gauge/SKILL.md`.
The launcher is canonical for local Sage restarts: it always uses the repo virtualenv, leaves Sage's runtime env
files to Sage's own loader (`Payload_Type/sage/.env.local` before `.env`; exporting them would freeze their values
into later restarts), applies its own defaults only when no source sets them, **fails closed** if
`SAGE_BLOODHOUND_MCP_DIR` is still unset, creates the maintained `sage` tmux session when it is absent, and records a redacted startup identity under
`/tmp/sage_startup_identity.json` for later readiness/manifest inspection.

6. For a fresh clean baseline, run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset
```

   This verifies the running Sage chat container, ensures the scoped Mythic API token, creates or reuses one
   empty locked AI channel named `Sage GOAD Ready`, then builds/downloads a fresh Apollo payload for the
   clean-baseline workflow. It does not create a Sage payload or callback.

   For an intentional retained foothold reuse, replace that command with:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset --use-retained-callback --retained-callback-config skills/sage-callback-bootstrap/merlin_callback_config.json
```

   This imports the exported callback config and stops before target-side payload execution. After the operator
   launches the retained payload, use `readiness --runtime-dbs-archived --foothold-payload-type merlin`.
7. For the fresh clean-baseline mode, open an RDP session as `NORTH\samwell.tarly` on CASTELBLACK, then launch the fresh Apollo
   payload with `skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py deploy` using
   `--launch-method scheduled-task-interactive --add-defender-exclusion`. The exclusion is scoped to the staged
   bootstrap payload file, which defaults to `C:\Users\Public\apollo.exe`; without it, Defender quarantines stock
   Apollo on clean-baseline. After a new callback is observed, the deploy helper disconnects the RDP session with
   `tsdiscon` by default so the local RDP client exits without logging off Apollo's Windows session.

   For a pre-staged snapshot, do not repeat this download/deploy step. Run the retained interactive launcher from
   the bootstrap decision above so it starts the preserved `SageApolloBootstrap` task through `launch-existing`.
8. Run:

```bash
.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py post-callback-preflight
```

   This waits for the live Samwell Apollo callback through Mythic control-plane observation, synchronizes range
   clocks out of band, and returns explicit zero-task metadata. It issues no Mythic payload tasks, does not purge
   Kerberos tickets, and does not claim target-probed UTC/domain/identity output. Treat any nonzero exit as a reset
   failure.
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

Ludus addresses a range by its **ID** (`GOADf255df`), not its number (`4`, which is only the range's IP octet).
`ludus.py status` prints `range <ID> (number <N>)`. Every helper defaults to the API key's own range, so omit
`--range-id` / `--ludus-range-id` / `$SAGE_LUDUS_RANGE_ID` unless you mean another range. A bare number is
resolved to its ID when exactly one accessible range carries it and refused otherwise, before any range request.

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
.venv/bin/python skills/sage-goad-reset/scripts/sage_runtime_env.py mcp-run skills/sage-goad-reset/scripts/bh_reset.py wipe --yes
.venv/bin/python skills/sage-goad-reset/scripts/sage_runtime_env.py mcp-run skills/sage-goad-reset/scripts/bh_reset.py status
```

The wipe is mandatory for every full reset. It waits 10 seconds before its first verification poll, then polls
every 5 seconds until `available-domains: count=0`. Do not run a separate immediate status check.

After a target-scope collection on a GOAD-style cross-forest range, use the read-only bridge probe before
blaming Sage for an empty frontier:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/sage_runtime_env.py mcp-run \
  skills/sage-goad-reset/scripts/check_cross_forest_laps_bridge.py \
  --source-domain <controlled-root-domain> \
  --target-domain <trusted-target-domain>
```

The probe mirrors Sage's reconciler shape: `User -> MemberOf* -> principal -> ReadLAPSPassword -> Computer`.
It fails closed when the current BloodHound graph has no cross-forest managed-secret bridge and intentionally
does not treat `SyncLAPSPassword` as equivalent to `ReadLAPSPassword`.

### Where the BloodHound MCP checkout comes from

Every tool here finds `SAGE_BLOODHOUND_MCP_DIR` the way the running Sage does: the process environment, then
`Payload_Type/sage/.env.local` (gitignored, per developer), then `Payload_Type/sage/.env` (tracked, ships commented
out for operators to fill in). An empty value sets nothing. Nothing guesses a sibling `bloodhound_mcp` checkout; an
unset variable is a refusal that names it and the files searched. To see what a tool will use:

```bash
.venv/bin/python skills/sage-goad-reset/scripts/sage_runtime_env.py get SAGE_BLOODHOUND_MCP_DIR
```

`sage_runtime_env.py mcp-run <script> [args]` runs a script inside that checkout's own environment
(`uv --directory <dir> run python <script>`), which is what `bh_reset.py`, `check_cross_forest_laps_bridge.py`,
and the eval gauge's `bh_cypher.py` need for the MCP's signed client and its `.env`. A local tmux Sage uses the
same directory, and the MCP loads its BloodHound URL and token from its own `.env` at startup.

## Bundled Scripts

- `_sage_relaunch.py`
- `sage_runtime_env.py` (resolves a Sage runtime setting in Sage's own order; `mcp-run` for BloodHound MCP scripts)
- `archive_reset_evidence.py` (copies container databases and dumps Mythic before the reset, then archives)
- `archive_runtime_dbs.py`
- `bh_reset.py`
- `check_cross_forest_laps_bridge.py`
- `liveness.py`
- `ludus.py`
- `mcp_check.py` (read-only local BloodHound MCP readiness wrapper; never issues payload tasks)
- `readiness_contract.py`
- `mythic_reset.sh`
- `pkinit_padata_probe.py`
- `reset_orchestrator.py` (resumable canonical reset with observed zero-task proof)
- `sage_deployment.py` (enforce that exactly one Sage serves Mythic — see below)
- `sage_restart.sh`
- `sage_stop.sh`

## Deployment modes

| | `local` | `container` |
|---|---|---|
| What serves Mythic | `python main.py` from the working tree, in tmux session `sage` | Mythic's `sage` container; Mythic bind-mounts its installed copy of the working tree over `/Mythic/` |
| Mythic settings | Sage container stopped | `sage_use_build_context=true`, `sage_use_volume=false` (`deploy` sets them) |
| Databases and ledger | `Payload_Type/sage/` | `<Mythic>/InstalledServices/sage/` |
| Reset archive | `archive_reset_evidence.py` | `archive_reset_evidence.py --target installed` |
| Bring it back | `sage_restart.sh` | `sage_deployment.py deploy --mode container [--rebuild]` |
| Readiness identity | startup identity the launcher records | parity: container files match the working tree, process newer than the files |
| Phoenix database | `Payload_Type/sage/.phoenix/phoenix.db` | `<Mythic>/InstalledServices/sage/.phoenix/phoenix.db` |

`MYTHIC_CLI` and `MYTHIC_ENV_PATH` must name the same Mythic install; `deploy` refuses otherwise. The reset passes
its own saved `--mythic-env-path` to every phase as `MYTHIC_ENV_PATH`.

Persist the mode in the repo-root `.env` (`SAGE_DEPLOYMENT_MODE=container`; see `.env.example`) and load it with
`set -a; . ./.env; set +a`, so every tool in the shell agrees. A mode exported in one shell only is how a later
`enforce` once stopped the container the operator meant to run.

Exactly one Sage may serve Mythic. Both register as the `sage` service, one wins the RabbitMQ queue, and the loser
logs `Another instance of this service, sage, is running` on a loop, while requests are answered by whichever won.

```bash
.venv/bin/python skills/sage-goad-reset/scripts/sage_deployment.py check
.venv/bin/python skills/sage-goad-reset/scripts/sage_deployment.py enforce --mode <local|container>
.venv/bin/python skills/sage-goad-reset/scripts/sage_deployment.py parity
```

`enforce` stops only the **unintended** Sage, and only when the mode is explicit (flag or `SAGE_DEPLOYMENT_MODE`);
with a defaulted mode it stops nothing and exits 1 if a Sage would have needed stopping. Starting the intended one belongs to `sage_restart.sh` or `deploy`.
Add `--conflict-only` when the intended Sage is legitimately not up yet (mid-reset). The readiness contract carries
the same rule as its `sage_deployment` section, so a solve cannot start into a split brain.

### Container deploys

`deploy --mode container` refuses unless the resolved mode is `container`. It mirrors the working tree into Mythic's
installed copy, sets Mythic's Sage settings for this mode when they differ (`mythic-cli config set`, read back
because it exits 0 even when nothing matched), regenerates the compose entry (`mythic-cli add sage`), **stops a
running local Sage**, then starts the container
with `docker compose up -d --no-build --pull never --force-recreate sage` from the Mythic directory. It builds the
image (`mythic-cli build sage`) only when the image is missing or `--rebuild` is passed. It does not use
`docker restart` (`mythic-cli stop sage` removes the container) or `mythic-cli start` (which rebuilds under
`REBUILD_ON_START`). It then proves parity and reports the deployment contract; it is `ready` only when both hold.

**The thing worth understanding: the Docker image does not hold the code that runs.** Mythic's
generated compose file bind-mounts the installed service directory over `/Mythic/`, shadowing what
the image baked in. At runtime the image supplies only the Python environment, which lives outside
that path. Two consequences:

- A Python change needs a sync plus a restart, which is what `deploy` does. It does **not** need an
  image rebuild, and rebuilding to pick one up changes a layer nothing reads.
- `requirements.txt`, `constraints.txt` and the `Dockerfile` DO need a rebuild (`deploy --rebuild`),
  because they produce the environment the mount does not shadow.

`parity` asks the container, never the host, and checks two independent things: that the files it
sees match the working tree, and that none is newer than the process that imported them. Python
does not reload changed modules, so matching files plus an older start time still means stale code
is executing. Either failure mode leaves a container answering from a stale tree, or a fixed defect
undeployed while the repo is green.

Two mechanical notes. The sync excludes every database spelling, because the container writes its
live `sage.db` into that same directory and `rsync --delete` would otherwise be destructive
(rsync protects excluded files on the receiver, which is what makes this safe). And the installed
directory is resolved from `MYTHIC_ENV_PATH`, never guessed by name — a guess silently deploys into
the wrong install on a machine with two Mythic checkouts.

#### Switching between tmux and the container

Each deployment keeps its own runtime state, and switching carries some of it but not all:

- **Databases live where that Sage runs** (see the table above). A container reset archives them with
  `--target installed`; do not use `archive_runtime_dbs.py --sage-db <installed>/sage.db`, which still moves
  the local `phoenix.db` and leaves the container's and its `.sage_engagement/` in place. Stale channel
  markers make Sage refuse the first prompt in a new channel ("Sage restarted or this channel's session was
  replaced") when Mythic reuses channel IDs after its own reset.
- **Any restart ends existing chat sessions.** After `sage_restart.sh` or `deploy`, create a new channel.
- **The sync carries git-ignored operator files.** `deploy` mirrors the working tree, so TLS bundles in
  `certs/` and binaries in `tools/` reach the installed copy without copying by hand. It does not sync
  `.env`: give the container its model and BloodHound settings through the chat channel's configuration or
  the installed copy's own `.env`.
- **What needs a restart.** Sage picks the TLS bundle once at startup, so a changed bundle needs a restart.
  `tools/` is read on each tool call and needs neither restart nor rebuild.
- **Which build it is.** A local `mythic-cli build sage` (with `sage_use_build_context` set) reports
  `version=unknown` and `build_commit=unknown` in the banner. Only the published image carries them; use
  `parity` to identify a local build.
- **One Sage at a time.** Confirm with `sage_deployment.py check` after every switch.
