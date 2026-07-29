# Sage Codex Session Guide

This repo is the Sage Mythic v4 chat container: an AI/LangGraph interface that operates Mythic callbacks, BloodHound, and MCP tools. The August 2026 demo target is an autonomous GOAD "Trust Walker" solve: starting from an assumed-breach callback on CASTELBLACK as `north\samwell.tarly`, Sage should reason over BloodHound and execute the path to Domain Admin / cross-forest compromise.

## Start Here

1. Read `## Repo Map` below to find the subsystem you are changing.
2. Read `skills/README.md` — it indexes every operator skill by name, purpose, and entry script.
3. Read the `SKILL.md` of each skill your task touches. `--help` shows flags, not semantics.
4. Read `README.md` for setup, but verify any safety or control claim against source. It has been stale before.
5. Run the offline suite (`## Current Validation Baseline`) before and after your change.

`Plans/` and `Notes/` are the maintainer's private, gitignored working context and are absent from a clone.
Nothing in this guide should require them; if you find a pointer into `Plans/`, treat it as a bug in this file.
Verify important claims against source, tests, Mythic, BloodHound, and the current range — not against prose.

## Durable Artifact Retention

- Treat `/tmp` as scratch space, never as the source of truth for a durable Sage artifact.
- Store durable-private contracts, final worker handoffs, decision-bearing reviews, external panel packets and
  responses, full chat transcripts used for analysis, accepted or rejected evaluation evidence, closed governance
  receipts, and manifests under `.sage_history/` through `skills/sage-artifact-retention`.
- An artifact must be durable before task completion when a final response cites it, it approves or rejects a
  decision, it proves an evaluation or live-run result, it contains a worker contract or final handoff, or exact
  bytes are required to resume accurately after reboot.
- Keep active locks and leases, temporary clones, fixtures, staging, downloaded payloads, environment snapshots,
  credentials, and reproducible intermediates in `/tmp`. Never promote secrets or payloads automatically.
- For Sage panel review, allocate `--output-dir` under `.sage_history/` and record the completed directory with
  `sage-artifact-retention`; do not use a `/tmp` panel directory for a decision-bearing review.
- Native Codex session and sub-agent transcripts remain in `~/.codex/sessions`. Keep bounded cyber-runner workers
  ephemeral, but persist their validated contracts and final structured handoffs under `.sage_history/`.
- `.sage_history/` is private, gitignored local state and is not a system backup. Raw history may contain sensitive
  operator or lab material even when obvious auth files are excluded.
- Use the retention manifest to relate durable artifacts to source paths, hashes, sessions, runs, commits, and
  decisions. Later blog/publication work must curate and redact a separate published subset.

## Sealed Evaluation Review Discipline

- Generated `passes` fields, readiness flags, self-attestations, and tests that only reread generated claims are
  not independent evidence. Inject adversarial inputs at the production boundary.
- Cover semantic failure classes with recursive/property matrices, protocol boundaries, and valid near-match
  controls before generating the first candidate. Named regression examples are necessary but insufficient.
- Bind every allowed semantic delta to an artifact, exact pointer, delta kind, and exact expected old/new value or
  a named independent validator. Do not allow generic whole-subtree, generic-hash, or candidate-self rules.
- Validate the exact bytes selected for writing. Eliminate shadow/prepared/payload/serialized duals or prove full
  equality before comparison and write.
- Bind each approval record atomically to one gate's exact path, hash, phrase, operator, and scope. Never compose a
  provenance tuple from multiple gates or rely on an implicit detached contextual join to cure a false assertion.
- Define test suites by lifecycle stage. A pre-generation absence assertion must not be required during
  post-generation candidate review; every sealed candidate needs a hermetic post-generation suite.
- Treat source/test acceptance, artifact acceptance, phase exit, development/live authority, countability, and
  promotion as separate state transitions. Changed hashes never inherit authority.
- Treat a governor result with no classified paths as no enforcement evidence. Fix coverage or disclose the limit;
  do not count an exit-zero result as approval proof.
- Preserve rejected bundles append-only, but centralize integrity in one canonical machine-readable ledger rather
  than copying large hash tables into every prose result.
- After the first rejection of the same mechanism, stop resealing. Perform RCA, simplify the mechanism, and
  property-test the complete failure class before another candidate or external review.

## Review Lane And Acceptance-Contract Discipline

- Classify work before implementation as either `runtime_bugfix` or `sealed_evaluation`. Runtime bugfixes use the
  lightest review process that can prove the production behavior; do not introduce sealed manifests, promotion
  gates, or evaluation lifecycle machinery merely to repair a bounded runtime defect. Sealed evaluation work keeps
  the full provenance and lifecycle discipline above.
- Freeze a prospective acceptance contract before the first edit. It must name the mechanism, lane, atomic Ideal
  State Criteria, exact production call path, adversarial and valid-near-match probes, non-goals, permitted files,
  and the rule that distinguishes a blocker from non-blocking hardening.
- The implementation owner must run the acceptance contract's declared probes before candidate review and map each
  changed behavior to one criterion. Passing author-written examples alone is not sufficient when the contract
  declares a broader semantic class.
- A post-implementation rejection must identify a reproducible, production-reachable counterexample to a frozen
  criterion or a concrete safety/authority-boundary violation. New preferences or requirements discovered after
  implementation are non-blocking unless they meet that standard.
- Every review finding must be classified as `blocking`, `hardening`, `unreachable`, `pre_existing`, or
  `out_of_scope`. Only `blocking` findings affect the candidate disposition.
- One review round must return the complete observed blocking set from all predeclared probes. Do not stop at the
  first defect and reveal the rest across serial review rounds.
- A fresh reviewer provides adversarial diversity, not evidence by agreement. Prefer executable invariants,
  production-boundary probes, and frozen criteria over changing models or accumulating reviewer opinions.
- Do not encode control authority by classifying open-ended natural-language prose when a typed field, enum, or
  protocol state can carry the same decision. If a rejection exposes a new paraphrase in the same language class,
  simplify to structured authority instead of extending keyword, negation, or regex lists.
- Give deadline-bound `runtime_bugfix` reviews an explicit wall-clock and command budget in the task contract
  (15 minutes and the focused production-path suite by default). Complete the declared probes, then return a
  disposition. Do not expand into unrelated history, whole-repository archaeology, or lifecycle review; the
  maintained supported tier remains the implementation owner's regression evidence unless the frozen contract
  identifies a concrete reason the reviewer must rerun it.

## Repo Map

- `Payload_Type/sage/ai/langgraph/model.py`: LangGraph topology, agent creation, middleware, HITL, stop handling, checkpoint recovery, streaming.
- `Payload_Type/sage/ai/langgraph/mythic_tools.py`: Mythic API tool surface, guarded/offensive actions, engagement advisor, command issue path, BloodHound ingest, credentials, TTP guidance, tool download, sandbox.
- `Payload_Type/sage/ai/langgraph/command_builder.py`: deterministic Mythic command parameter construction and result classification.
- `Payload_Type/sage/ai/langgraph/capabilities.py`: generic capability candidates and structured verifiers; GOAD is a benchmark, not a source of hardcoded strategy.
- `Payload_Type/sage/ai/langgraph/engagement_state.py`: STRIPS-like techniques, effects, preconditions, planner candidates, rendered state.
- `Payload_Type/sage/ai/langgraph/graph_reconciler.py`: BloodHound graph facts projected into engagement predicates.
- `Payload_Type/sage/ai/langgraph/access_reconciler.py`: Mythic callback/liveness projected into footholds.
- `Payload_Type/sage/sage_chat/`: native Mythic v4 chat container, request lifecycle, config, streaming, and sessions.
- `Payload_Type/sage/ai/langgraph/intent_classifier.py`: maps Mythic tool calls to modeled engagement techniques.
- `Payload_Type/sage/sage_chat/service.py`: native Mythic request lifecycle, session reuse, HITL resume, visibility
  reconciliation, and channel metadata.
- `Payload_Type/sage/sage_chat/slash.py`: `/state`, `/list`, `/mode`, `/stop`, MCP, and BloodHound chat commands.
- `Payload_Type/sage/sage_chat/headless.py`: non-UI chat entrypoint for tests, evals, and trajectory tooling.
- `Payload_Type/sage/prompts/`: externalized agent prompts.
- `Payload_Type/sage/ttps/`: TTP/tradecraft corpus and pinned tool metadata.
- `Payload_Type/sage/evals/`: Phoenix-backed GOAD eval harness.
- `Payload_Type/sage/tests/`: fast offline unit/regression suite.
- `Payload_Type/sage/ai/trajectory/`: trajectory corpus/export/replay/runtime bridge tooling for data-backed repair policy.
- `skills/`: repo-local Sage skills. Reusable operator/Codex/Claude tooling belongs here, not in `Plans/`. **Read `skills/README.md` first** — it indexes every skill (name, purpose, entry script); tool-agnostic.

## Current Validation Baseline

Run this from repo root before and after code changes:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py supported
```

There is one tier and no exclusions: a green run means the tree is green. The trailing `supported` argument is
accepted and ignored so older handoffs keep working.

The four rejected successor-portfolio suites this command used to exclude are rejected *evaluation evidence*, and
per § Durable Artifact Retention that belongs in `.sage_history/`, not the product tree. They are preserved
append-only at `.sage_history/evaluation/architecture-policy/rejected-successor-portfolios/`. Do not rewrite or
reseal them, and do not move them back. Record the observed suite count in the current handoff rather than
preserving a fast-staling count here.

Sealed evaluation evidence belongs under `.sage_history/`, never `Plans/`, which is the maintainer's own
documents and is gitignored for an unrelated reason. The Phase 16R/17 campaign's evidence and source now live at
`.sage_history/evaluation/architecture-policy/`. **Known gap:** `phase10_evidence_bundle`, `phase12`, `phase13`,
`phase14` (both), and `phase15` still write outputs under `Plans/` — ten path anchors across six modules. Route
new evidence writes to `.sage_history/`, and prefer a shared resolver over another per-module `/ "Plans" /`
constant when those six are migrated.

For live evals, use the Phoenix-backed harness in `Payload_Type/sage/evals/`. Do not trust the Mythic task poller for long Sage runs; it can hang. Read results from `Payload_Type/sage/.phoenix/phoenix.db` or decoded Mythic task output.

## Operating Rules

- Do not commit unless the user explicitly asks. Prior project convention is that Russel reviews and commits.
- Use `rg`/`rg --files` first for search.
- Use `apply_patch` for manual edits.
- Sage runs as a local process in the `sage` tmux session for this workflow. Do not use Docker/Mythic
  container restart commands for Sage unless the user explicitly asks for that future deployment mode.
- Hard execution boundary: Sage runtime may reason over Mythic/BloodHound control-plane data and build task
  parameters, but it must never connect directly to target hosts/services or perform target-facing tradecraft
  from the Sage process/container. LDAP/LDAPS, SMB, Kerberos, WinRM, RPC, HTTP, and similar target interactions
  must be issued as Mythic tasks to live payload callbacks.
- Objective/proof boundary: no capability effect or objective completion may be recorded from Sage-host target
  I/O or Sage-local attack-artifact generation/use. Valid proof comes from Mythic task output/artifacts,
  Mythic credential-store state, or BloodHound facts derived from payload-collected artifacts. Operator
  reset/readiness/eval helpers may inspect the lab out of band, but they are not Sage execution or proof.
- Never delete runtime databases. Before a clean reset, stop local Sage and use
  `skills/sage-goad-reset/scripts/archive_runtime_dbs.py` to move active databases to timestamped
  `sage_YYYYMMDD-HHMM.db` and `phoenix_YYYYMMDD-HHMM.db` archives.
- Do not create new reusable tools, operator scripts, Codex helpers, or Claude Code helpers in `Plans/`.
  Put them in repo-local skills under `skills/<skill-name>/scripts/` and document the workflow in that skill's
  `SKILL.md`. `Plans/` is for minimal current planning/handoff markdown plus archived historical notes.
- Preserve user changes; do not reset, checkout, or revert unrelated files.
- Do not start expensive live GOAD/inference runs without clear user intent. A full autonomous solve can take ~25 minutes and depends on external lab state.
- Always re-discover live payload callback IDs after lab resets. Sage itself uses a fresh chat channel, not a callback.
- When reporting long live work, separate engineering/debug time from live-lab execution time. Include the current
  row or attempt, last accepted row, current operation, retry count, and best ETA instead of reporting only elapsed
  wall time.
- End every goal/tranche completion or handoff with an `ACTION ITEMS FOR RUSSEL` section. Write `None` when no
  operator action is required.
- Prefer single-line shell commands in operator instructions. Avoid backslash-continued commands when one line is practical.
- For Sage operator prompts, `--verbose true` is usually necessary for useful Mythic-side visibility.
- If touching autonomous execution, run focused tests plus the supported offline tier above.
- High-risk Sage architecture work must pass the architecture governor before edits. Use
  `skills/sage-architecture-governor` for any change touching prompts, agent topology, tool lists,
  `Payload_Type/sage/ai/langgraph/model.py`, `mythic_tools.py`, `engagement_state.py`, reconcilers,
  capability planning/adapters, trajectory, eval harnesses, or live-run drivers. The required gate is:
  hypothesis, Ideal State Criteria, non-goals, complexity budget, falsifier, stop-loss, verification plan,
  generality risk, expected file scope, and explicit user approval before opening a scoped edit token.
- Do not grow prompts, tool surfaces, GOAD-specific live code, or symbolic planning/gating logic as a
  tactical fix without first comparing a thinner verifier/retrieval/data-backed alternative. The third
  tactical patch to the same subsystem requires RCA before more code.
- Project hooks in `.codex/config.toml` enforce the architecture gate for high-risk edits. If a legitimate
  high-risk edit is blocked, prepare the gate brief, get explicit user approval, then open a short-lived
  scoped token with `python3 skills/sage-architecture-governor/scripts/open_gate.py open ...`.

## Mythic Chat Container — Output Rendering

Sage now runs as a **native Mythic v4 chat container** (`Payload_Type/sage/sage_chat/`), and the Mythic
chat UI renders operator-facing output richly. Leverage this; don't fight it.

- **Markdown is rendered.** Tables, fenced code blocks, bold/inline code, and lists all display. Prefer
  **tables for enumerations** (callbacks, credentials, hosts, domains, users) and **fenced code blocks**
  for commands, payloads, and raw output. Slash commands already do this (`/state`, `/list`, `/mcp tools`).
- **Do NOT add self-timestamps.** Mythic renders a native per-message timestamp. Sage no longer stamps
  `[HH:MM:SS]` on outbound messages (removed from `Model._stream_message_to_mythic`); don't reintroduce it.
- **Tool calls render as collapsible cards, not text.** Emitting a `ChatResponse` with
  `metadata.special_type="tool_use"` + a `tool_use` snapshot (`status` started/completed/error,
  `tool_name`, `tool_source` mythic|mcp, `tool_call_id`, `arguments`, `result_preview`) makes the React UI
  (`ChatToolUseEvent`) draw a "Running/Finished/Failed" card with a collapsible Details pane. Started and
  finished emissions reuse one `response_key` (`tool_use:{id}:{name}`) so the card updates in place. The
  card's `content` + `result_preview` are what show in Details — put the request (name+args) and the
  response there. Wired in `MessageCaptureCallback.on_llm_end`/`on_tool_end` → `Model._emit_tool_use_card`
  → `ChatStreamEmitter.emit_tool_use`. On the chat path the legacy verbose `🛠️`/`🔧` text is suppressed so
  the card is the single representation; the PayloadType task path keeps the verbose text.
- **LLMs already emit markdown by default**, and the Supervisor prompt already asks for a "well-formatted
  markdown" final report — so a blanket "use markdown" prompt line is redundant. The only prompt nudge worth
  adding is *structure*: tell the Supervisor's `respond_to_user` to use a table for enumerations and code
  fences for commands/output (targeted, not generic).

## Lab Reset Tools

Official repo-local Sage skills now carry reusable reset/run/analyze tooling:

- Use `skills/sage-goad-reset` for the clean GOAD/Ludus/BloodHound/Sage rehearsal reset and readiness preflight.
- `$sage-goad-reset full reset` means the complete workflow: archive Sage/Phoenix, reset Mythic, roll back GOAD,
  wipe BloodHound, restart local Sage chat, and generate a fresh Apollo payload. Do not interpret it as Ludus-only.
- Use `skills/sage-callback-bootstrap` after Mythic reset to verify the Sage chat container and establish the
  Apollo or retained foothold callback.
- Use `skills/sage-live-runner` for native chat solves, request monitoring, and inspection.
- Use `skills/sage-focused-capability-tests` for narrow capability/adaptor validation.
- Use `skills/sage-trace-analysis` for Phoenix/Mythic/log analysis.
- Use `skills/sage-trajectory-learning` for corpus manifests, transition export, and repair-policy replay.
  Runtime capability failures append redacted records to `Payload_Type/sage/.trajectory/transitions.jsonl` by
  default and return `trajectory_repair` in failed `execute_capability` responses.

Do not store lab passwords in skills or copied helper scripts. Prefer session environment variables, local gitignored
`.env` files owned by each tool, or an OS keychain/secret manager. Current Mythic-facing reset helpers should resolve
`MYTHIC_ADMIN_PASSWORD` first, then `MYTHIC_ENV_PATH`. There is no checkout-name default — see `.env.example`.

### Range Source, Runtime, and Evidence States

- For reusable AD ranges intended for publication or transfer, prefer a DreadGOAD-format source definition and use
  Ludus as the Sage execution/reset substrate. A Sage-only benchmark fixture may remain Ludus-first in
  `ludus/sage-purpose-ranges/` when portability is not a goal. A source definition is not evidence that a Ludus
  range exists, is provisioned, or is ready.
- Use precise range state words in updates and handoffs instead of the ambiguous word `deployed`:
  - `defined`: the source/profile exists, but no runtime claim is made.
  - `provisioned`: the Ludus VMs exist.
  - `snapshotted`: the intended clean baseline snapshot exists.
  - `callback-ready`: the expected foothold is live and readiness/preflight passed.
  - `countable`: all gates for a live row passed and the attempt may be used as evidence.
  - `burned`: the attempt cannot be used as evidence because setup, measurement, or isolation failed.
  - `complete`: the required evidence was accepted and artifacts were captured.
- A holdout attempt is `countable` only after the relevant clean-reset, clock-sync, BloodHound, callback uniqueness,
  backend/route, and validator gates pass. If a duplicate callback lane, clock skew, route mismatch, setup defect, or
  measurement defect is discovered after an attempt starts, mark that attempt `burned`, preserve its artifacts, fix
  the gate, and use a fresh attempt for evidence. Do not retroactively promote a burned attempt.
- For sealed evaluators and live-row validators, do not use substring membership for structured capability targets,
  domains, or callback-scoped effect keys. Parse exact fields and keep a regression for suffix-collision cases such
  as `zeta.branch.local` versus `branch.local`.
- Before any countable live spend, sealed evaluators and live-row validators must have fixture coverage for every
  plan-permitted evidence mode and terminal interpretation, including valid no-branch/kernel-only evidence and
  fail-closed negative cases when the plan allows them. Do not derive the validator contract from only the typical
  success row.
- Power down ranges that are not needed for the next active operation after a stop-loss, burned attempt, or completed
  tranche. The Ludus host is resource constrained; leaving old purpose ranges running is not a neutral default.
- For each live eval row, record the effective model provider/route, Sage startup env overrides that change behavior,
  range/snapshot identity, foothold callback identity, chat channel/request IDs, and validator artifact path. State
  explicitly whether a run used Bedrock, a local `127.0.0.1` API route, or another backend; do not infer it from the
  model name alone.

Use this order for clean GOAD/BloodHound/Mythic rehearsal setup. Mythic is Docker-backed, but Sage runs locally
in tmux throughout current development.

### Clean Rehearsal Order

1. **Stop local Sage and archive its active databases.** Run
   `/bin/bash skills/sage-goad-reset/scripts/sage_stop.sh`, then
   `.venv/bin/python skills/sage-goad-reset/scripts/archive_runtime_dbs.py`. Never overwrite or delete retained
   archives.
2. **Reset Mythic through its CLI.** Run
   `/bin/bash skills/sage-goad-reset/scripts/mythic_reset.sh --yes`, which executes `mythic-cli stop`,
   `mythic-cli database reset -f`, and `mythic-cli start`.
3. **Reset GOAD and BloodHound.** Roll back/power on GOAD, wipe BloodHound, and require
   `available-domains: count=0` before ingest. Then run
   `.venv/bin/python skills/sage-goad-reset/scripts/sync_range_time.py sync --yes` and require `"ready": true`;
   RAM-backed snapshots can restore guests with clocks days apart.
4. **Start/restart local Sage in the `sage` tmux session after DB archival and Mythic reset**, with the engagement gate and BloodHound MCP directory:
   `/bin/bash skills/sage-goad-reset/scripts/sage_restart.sh SAGE_ENGAGEMENT_GATE=1 SAGE_BLOODHOUND_MCP_DIR="$SAGE_BLOODHOUND_MCP_DIR"`.
5. **Verify Sage chat and create Apollo.** Run
   `.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py bootstrap-reset`. It requires
   a running Sage chat container and builds/downloads a fresh Apollo payload. It does not create Sage payloads.
6. **Establish the foothold callback.** Open an active RDP session as `NORTH\samwell.tarly` on CASTELBLACK, then stage/launch the fresh Apollo payload with
   `skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py deploy` using
   `--launch-method scheduled-task-interactive --add-defender-exclusion`. The exclusion is scoped to the staged
   bootstrap payload file, which defaults to `C:\Users\Public\apollo.exe`; clean-baseline Defender otherwise
   quarantines stock Apollo. After a new callback is observed, the helper disconnects the RDP session with
   `tsdiscon` by default so the local client exits without logging off the Windows session.
7. **Run the callback preflight.** Run
   `.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py post-callback-preflight`; it
   waits for the live Samwell Apollo callback through Mythic control-plane observation, synchronizes clocks out of
   band, and returns explicit zero-task metadata. It issues no Mythic payload tasks and does not claim Kerberos
   purge or target-probed UTC/domain/identity output.
8. **Only then rediscover Apollo and run Sage.** After DB archival and Sage restart, run
   `.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived` as a non-destructive
   preflight; it must show `ready: true`. Then run
   `.venv/bin/python skills/sage-live-runner/scripts/native_chat.py run --autonomous --prompt 'From the current foothold, achieve administrative control of essos.local.' --timeout 5400`.

The GOAD and BloodHound reset helpers below are still used for lab state, but they do not replace the Mythic
payload/callback lifecycle above.

- **GOAD Ludus range:** `skills/sage-goad-reset/scripts/ludus.py` reads Ludus credentials from `.mcp.json`.
  - Check state: `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status`
  - List snapshots: `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py snapshots`
  - Treat `clean-baseline` as a logical state name, not a guaranteed Ludus snapshot ID. List the live restore
    targets first, then pass the intended name explicitly. For range #4 the clean restore target verified on
    July 20, 2026 is `sage-seed-baseline-20260710`:
    `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py rollback sage-seed-baseline-20260710 --yes`
    Stop and reconcile the handoff if that exact target is absent; never guess among multiple snapshots.
  - Power on all range VMs: `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py poweron all`
  - Verify all six VMs are ON and reporting IPs: router `10.4.10.254`, DC01 `.10`, DC02 `.11`, DC03 `.12`,
    SRV02/CASTELBLACK `.22`, SRV03/BRAAVOS `.23`. DC01/DC02 can briefly show `ip=null` after rollback; wait and
    poll `status` until the guest agent reports IPs.
  - Synchronize Windows clocks after every rollback:
    `.venv/bin/python skills/sage-goad-reset/scripts/sync_range_time.py sync --yes`
  - Read-only clock gate:
    `.venv/bin/python skills/sage-goad-reset/scripts/sync_range_time.py check`
- **BloodHound CE reset:** `skills/sage-goad-reset/scripts/bh_reset.py` uses the BloodHound MCP environment at
  `"$SAGE_BLOODHOUND_MCP_DIR"`.
  - Status: `uv --directory "$SAGE_BLOODHOUND_MCP_DIR" run python skills/sage-goad-reset/scripts/bh_reset.py status`
  - Wipe collected graph data: `uv --directory "$SAGE_BLOODHOUND_MCP_DIR" run python skills/sage-goad-reset/scripts/bh_reset.py wipe --yes`
  - `clear-database: 204 (empty body)` is expected. The wipe is asynchronous; verify `available-domains:
    count=0` before any ingest. Do not start SharpHound ingest while the wipe is still settling.

## Safety/Control Reality

- `chat` supports `mode=supervised`, and supervised mode inserts default-deny HITL for guarded tools.
- `query` currently has no supervised mode parameter and runs through auto behavior.
- The engagement gate is currently an advisor for most missing-precondition DEFERs because hard enforcement deadlocked live Trust Walker runs. Verify-on-record, achieved-hop SKIP, collect-once, capped DCSync precheck, circuit breakers, and supervised HITL are the remaining controls.
- Sage is a control-plane agent, not an alternate implant. A direct Sage-process connection to a target service
  is a boundary violation even when it is only intended as a verifier or fallback.
- `read_credentials` can expose raw secrets to model context/traces. Be deliberate when changing credential behavior or observability.

## Highest-Value Demo Work

GOAD/Trust Walker is the benchmark, not the strategy source. Do not hardcode a GOAD path, GOAD domain names, or a Trust Walker step script into the agent. The highest-value work is a generic capability-driven autonomous execution system that can solve GOAD because GOAD is an instance of the problem, and can transfer to other AD CTF ranges with different names, paths, and primitives.

The target architecture is:

1. **Observe:** build current state from oracles: Mythic callbacks/liveness/credentials/files/task history plus BloodHound graph facts.
2. **Model capabilities:** represent generic actions as typed capabilities with preconditions, effects, command-intent builders, OPSEC notes, and verifiers. Examples: collect graph, abuse controlled GPO, grant directory rights, DCSync account, forge/use ticket, read LAPS, abuse ADCS, move laterally.
3. **Plan:** choose next candidate actions from the observed state and graph edges, not from a static demo overlay.
4. **Execute:** convert selected capability intent into exact Mythic command parameters deterministically.
5. **Verify:** prove the effect via Mythic task output/artifacts, Mythic credential-store state, or BloodHound
   facts derived from payload-collected artifacts before updating the ledger. Never use direct Sage-to-target
   sockets or Sage-local attack execution as proof.
6. **Learn/repair:** classify failures as construction, transient, or genuine environment blockers; repair mechanics once, then re-plan instead of looping.

The reusable capability layer is implemented in `capabilities.py`:
**`gpo-controlled-system-exec`** requires real SYSTEM execution proof,
**`grant-directory-rights`** unlocks only from verified GPO/SYSTEM execution and records only confirmed
DS-Replication ACL evidence, and **`dcsync-krbtgt`** unlocks only from explicit/verified
`ds-replication-rights:<domain>` and records only real secret material. The live/probe bridge is also in
place: `engagement_state.record_effect_result`, `capabilities.record_capability_result`, structured probe
extractors, and `MythicTools.record_capability_result` can turn verifier success into durable achieved
effects without relying on legacy `gpo-abuse` implications. Deterministic execution builders are split on
purpose: `capabilities.build_capability_execution_plan` emits payload-agnostic primitives, while
`mythic_capability_adapter.build_mythic_capability_commands` and `MythicTools.build_capability_commands`
translate those primitives into Mythic command/parameter plans for the active payload schema.

Concrete next sequence:

1. Audit and remove any off-agent runtime path that touches target services directly or performs a compromise
   primitive outside a Mythic payload task. The current known violation is host-side Schannel LDAP proof after
   ADCS PKINIT failure; Sage-local certificate forging also needs an explicit admissibility decision under this
   boundary.
2. Add deterministic guards/tests so a capability cannot record achieved effects from off-agent target I/O or
   off-agent compromise primitives.
3. Run one measured clean one-shot GOAD solve using `skills/sage-live-runner/scripts/native_chat.py` only after
   the boundary fix is in place. Inspect Phoenix/decoded Mythic output, ledger rows, repeated tool calls, skips,
   failures, and the final proof chain.
4. Reset GOAD/BloodHound/Mythic to a clean state and repeat until the guided one-shot reliably reaches verified
   ESSOS administrative control from the initial CASTELBLACK foothold using only Mythic-task-derived proof.
5. Only after the guided one-shot is reliable, remove the GOAD-specific guidance from the prompt/driver and test
   whether the generic capability system reaches the same objective from observed state and graph facts.

This is higher value than another prompt iteration or another full autonomous run because it turns Sage from a guided GOAD solver into a domain-agnostic CTF solver: the model decides which capability to try, while code owns exact mechanics and verification.

## Eval Gauge / Hill-Climbing (Phase 0)

A ground-truthed measurement instrument lives at `Payload_Type/sage/ai/hillclimb/` — the eval **gauge**: "is config A better than B?", bare-model-vs-harness, the Gate Experiment (Spearman ρ of eval-vs-ground-truth), and the noise floor. It exists because substring-match eval scores are gameable; a hill-climber optimizes whatever the metric measures, so the gauge must track real range state, not trace text.

- **Entry point:** the `sage-eval-gauge` skill (run commands, helpers, gotchas).
- **Why:** substring-match eval scores are gameable, so the gauge is validated against real range state via the
  Gate Experiment rather than against trace text. The original design/spec documents are maintainer-private.
- **Additive + read-only to Sage:** the running Sage process never imports it; **no Sage restart** is needed for gauge changes.
- **Offline tests:** `tests/test_hillclimb_*.py` (hermetic). **Live driver** `ai/hillclimb/run_gauge_live.py --go` runs OFFENSIVE solves and is operator-gated.
- The bare model uses Sage's own model from `skills/sage-callback-bootstrap/.env` (no `--model`); BloodHound ground truth is read-only via the CE REST API.

## Common Pitfalls

- RESUME can be stale; trust but verify against code and tests.
- BloodHound ingest is asynchronous. Use job status verification, not an immediate domain list.
- `query` auto-connects BloodHound; `chat` currently does not share that preflight.
- The latest saved eval markdown files are single-case smoke baselines; do not overclaim them as a fresh 10-case sweep.
- `sage.db` is checkpoint/state and can be recreated; `.phoenix/phoenix.db` contains trace history used by eval/debugging.
