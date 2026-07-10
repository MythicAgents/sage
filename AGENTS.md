# Sage Codex Session Guide

This repo is the Sage Mythic v4 chat container: an AI/LangGraph interface that operates Mythic callbacks, BloodHound, and MCP tools. The August 2026 demo target is an autonomous GOAD "Trust Walker" solve: starting from an assumed-breach callback on CASTELBLACK as `north\samwell.tarly`, Sage should reason over BloodHound and execute the path to Domain Admin / cross-forest compromise.

## Start Here

1. Read `Plans/RESUME.md` first. It is the fastest-changing operational state and usually names the live frontier.
2. Read `Plans/TRAJECTORY_LEARNING_RUNTIME.md` for the current data-backed autonomy build direction.
3. Read `Plans/SESSION_HANDOFF.md` when preparing a live GOAD/Mythic/BloodHound run.
4. Read `CLAUDE.md` for lab gotchas, Phoenix usage, and historical debugging notes.
5. Treat `README.md` as useful setup documentation, but verify safety/control claims against code. It has been stale before.

Plans and Notes are mostly private/gitignored working context. Do not assume they are committed history. Verify important claims against source.
Historical markdown that is not part of the current minimal handoff lives under `Plans/Archived/`.

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
- `Payload_Type/sage/container/agent_functions/query.py`: one-shot Mythic command; auto-connects BloodHound before graph construction.
- `Payload_Type/sage/container/agent_functions/chat.py`: interactive/sessionful Mythic command; supports `mode=auto|supervised`.
- `Payload_Type/sage/container/agent_functions/state.py`: operator-facing durable engagement ledger viewer/editor.
- `Payload_Type/sage/prompts/`: externalized agent prompts.
- `Payload_Type/sage/ttps/`: TTP/tradecraft corpus and pinned tool metadata.
- `Payload_Type/sage/evals/`: Phoenix-backed GOAD eval harness.
- `Payload_Type/sage/tests/`: fast offline unit/regression suite.
- `Payload_Type/sage/ai/trajectory/`: trajectory corpus/export/replay/runtime bridge tooling for data-backed repair policy.
- `skills/`: repo-local Sage skills. Reusable operator/Codex/Claude tooling belongs here, not in `Plans/`. **Read `skills/README.md` first** — it indexes every skill (name, purpose, entry script); tool-agnostic.

## Current Validation Baseline

Run this from repo root before and after code changes:

```bash
.venv/bin/python -m pytest Payload_Type/sage/tests -q
```

Latest observed baseline from the guided cb3 ESSOS completion work: `804 passed, 1 warning`.

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
- Prefer single-line shell commands in operator instructions. Avoid backslash-continued commands when one line is practical.
- For Sage operator prompts, `--verbose true` is usually necessary for useful Mythic-side visibility.
- If touching autonomous execution, run focused tests plus the full offline suite.
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
`MYTHIC_ADMIN_PASSWORD` first, then `MYTHIC_ENV_PATH`, `/home/john/dev/mythic_v4/.env`, and the legacy v3 `.env`.

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
   `/bin/bash skills/sage-goad-reset/scripts/sage_restart.sh SAGE_ENGAGEMENT_GATE=1 SAGE_BLOODHOUND_MCP_DIR=/home/john/dev/bloodhound_mcp`.
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
   waits for the live Samwell Apollo callback, synchronizes clocks, purges stale Kerberos tickets, and verifies
   UTC/domain/identity output.
8. **Only then rediscover Apollo and run Sage.** After DB archival and Sage restart, run
   `.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --runtime-dbs-archived` as a non-destructive
   preflight; it must show `ready: true`. Then run
   `.venv/bin/python skills/sage-live-runner/scripts/native_chat.py run --prompt 'From the current foothold, achieve administrative control of essos.local.' --timeout 5400`.

The GOAD and BloodHound reset helpers below are still used for lab state, but they do not replace the Mythic
payload/callback lifecycle above.

- **GOAD Ludus range:** `skills/sage-goad-reset/scripts/ludus.py` reads Ludus credentials from `.mcp.json`.
  - Check state: `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status`
  - List snapshots: `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py snapshots`
  - Roll back all range VMs to the default `clean-baseline` snapshot:
    `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py rollback --yes`
  - Power on all range VMs: `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py poweron all`
  - Verify all six VMs are ON and reporting IPs: router `10.4.10.254`, DC01 `.10`, DC02 `.11`, DC03 `.12`,
    SRV02/CASTELBLACK `.22`, SRV03/BRAAVOS `.23`. DC01/DC02 can briefly show `ip=null` after rollback; wait and
    poll `status` until the guest agent reports IPs.
  - Synchronize Windows clocks after every rollback:
    `.venv/bin/python skills/sage-goad-reset/scripts/sync_range_time.py sync --yes`
  - Read-only clock gate:
    `.venv/bin/python skills/sage-goad-reset/scripts/sync_range_time.py check`
- **BloodHound CE reset:** `skills/sage-goad-reset/scripts/bh_reset.py` uses the BloodHound MCP environment at
  `/home/john/dev/bloodhound_mcp`.
  - Status: `uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-goad-reset/scripts/bh_reset.py status`
  - Wipe collected graph data: `uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-goad-reset/scripts/bh_reset.py wipe --yes`
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
- **Why (canonical):** `Plans/SAGE_HILL_CLIMBING_DESIGN.md` + `SPEC.md`. **Build spec/ISA:** `Plans/SAGE_EVAL_GAUGE_PHASE0_ISA.md`.
- **Additive + read-only to Sage:** the running Sage process never imports it; **no Sage restart** is needed for gauge changes.
- **Offline tests:** `tests/test_hillclimb_*.py` (hermetic). **Live driver** `ai/hillclimb/run_gauge_live.py --go` runs OFFENSIVE solves and is operator-gated.
- The bare model uses Sage's own model from `skills/sage-callback-bootstrap/.env` (no `--model`); BloodHound ground truth is read-only via the CE REST API.

## Common Pitfalls

- RESUME can be stale; trust but verify against code and tests.
- BloodHound ingest is asynchronous. Use job status verification, not an immediate domain list.
- `query` auto-connects BloodHound; `chat` currently does not share that preflight.
- The latest saved eval markdown files are single-case smoke baselines; do not overclaim them as a fresh 10-case sweep.
- `sage.db` is checkpoint/state and can be recreated; `.phoenix/phoenix.db` contains trace history used by eval/debugging.
