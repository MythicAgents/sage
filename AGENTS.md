# Sage Codex Session Guide

This repo is the Sage Mythic payload: an AI/LangGraph interface that operates Mythic callbacks, BloodHound, and MCP tools. The August 2026 demo target is an autonomous GOAD "Trust Walker" solve: starting from an assumed-breach callback on CASTELBLACK as `north\samwell.tarly`, Sage should reason over BloodHound and execute the path to Domain Admin / cross-forest compromise.

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
- `Payload_Type/sage/ai/langgraph/intent_classifier.py`: maps Mythic tool calls to modeled engagement techniques.
- `Payload_Type/sage/container/agent_functions/query.py`: one-shot Mythic command; auto-connects BloodHound before graph construction.
- `Payload_Type/sage/container/agent_functions/chat.py`: interactive/sessionful Mythic command; supports `mode=auto|supervised`.
- `Payload_Type/sage/container/agent_functions/state.py`: operator-facing durable engagement ledger viewer/editor.
- `Payload_Type/sage/prompts/`: externalized agent prompts.
- `Payload_Type/sage/ttps/`: TTP/tradecraft corpus and pinned tool metadata.
- `Payload_Type/sage/evals/`: Phoenix-backed GOAD eval harness.
- `Payload_Type/sage/tests/`: fast offline unit/regression suite.
- `Payload_Type/sage/ai/trajectory/`: trajectory corpus/export/replay/runtime bridge tooling for data-backed repair policy.
- `skills/`: repo-local Sage skills. Reusable operator/Codex/Claude tooling belongs here, not in `Plans/`.

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
  container restart commands for Sage unless the user explicitly asks for that different deployment mode.
- Never delete files from this repo or runtime tree. In particular, `Payload_Type/sage/sage.db` and
  `Payload_Type/sage/.phoenix/phoenix.db` cleanup is operator-owned; wait for the operator to remove them.
- Do not create new reusable tools, operator scripts, Codex helpers, or Claude Code helpers in `Plans/`.
  Put them in repo-local skills under `skills/<skill-name>/scripts/` and document the workflow in that skill's
  `SKILL.md`. `Plans/` is for minimal current planning/handoff markdown plus archived historical notes.
- Preserve user changes; do not reset, checkout, or revert unrelated files.
- Do not start expensive live GOAD/inference runs without clear user intent. A full autonomous solve can take ~25 minutes and depends on external lab state.
- Always re-discover live callback IDs after lab resets. Historical IDs in Plans are examples, not truth.
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

## Lab Reset Tools

Official repo-local Sage skills now carry reusable reset/run/analyze tooling:

- Use `skills/sage-goad-reset` for the clean GOAD/Ludus/BloodHound/Sage rehearsal reset and readiness preflight.
- Use `skills/sage-callback-bootstrap` after the operator resets Mythic and confirms DB cleanup to build fresh Sage/Apollo
  payloads, establish callbacks, and rediscover live callback IDs.
- Use `skills/sage-live-runner` for guided solves, verbose Sage tasking, monitoring, and inspection.
- Use `skills/sage-focused-capability-tests` for narrow capability/adaptor validation.
- Use `skills/sage-trace-analysis` for Phoenix/Mythic/log analysis.
- Use `skills/sage-trajectory-learning` for corpus manifests, transition export, and repair-policy replay.
  Runtime capability failures append redacted records to `Payload_Type/sage/.trajectory/transitions.jsonl` by
  default and return `trajectory_repair` in failed `execute_capability` responses.

Do not store lab passwords in skills or copied helper scripts. Prefer session environment variables, local gitignored
`.env` files owned by each tool, or an OS keychain/secret manager. Current Mythic-facing reset helpers should resolve
`MYTHIC_ADMIN_PASSWORD` from the environment first and `/home/john/dev/mythic/.env` second.

Use this order for clean GOAD/BloodHound/Mythic rehearsal setup. Mythic reset, runtime database cleanup, and
payload creation are operator-owned unless the user explicitly asks Codex to help with non-deletion steps.

### Clean Rehearsal Order

1. **Operator resets Mythic first.** Do not start Sage or a GOAD solve against a half-reset Mythic instance.
2. **Operator removes local Sage runtime databases before starting Sage back up.** At minimum the operator removes
   `Payload_Type/sage/sage.db` and `Payload_Type/sage/.phoenix/phoenix.db` so checkpoints/traces from the prior
   Mythic operation cannot contaminate the next run. Codex must not delete these files; wait for explicit operator
   confirmation that cleanup is complete before restarting Sage.
3. **Start/restart local Sage in the `sage` tmux session after operator-confirmed DB cleanup**, with the engagement gate and BloodHound MCP directory:
   `/bin/bash skills/sage-goad-reset/scripts/sage_restart.sh SAGE_ENGAGEMENT_GATE=1 SAGE_BLOODHOUND_MCP_DIR=/home/john/dev/bloodhound_mcp`.
4. **Create fresh payloads in Mythic every reset.** Build a new Sage payload and a new Apollo payload after the
   Mythic reset because payload crypto keys change. Never reuse old Sage/Apollo payload files or old callback IDs
   across Mythic resets. If helping from the CLI, use `skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py inspect` first, then
   `skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py create-sage` / `create-apollo` / `create-all` with the live Mythic C2
   `callback_host`.
5. **Ensure GOAD and BloodHound are clean before launching footholds.** Roll back/power on GOAD and wipe/verify
   BloodHound as needed, then confirm GOAD is powered on and BloodHound shows `available-domains: count=0`.
6. **Launch fresh callbacks.** Establish the Sage callback from the new Sage payload, then launch Apollo on
   CASTELBLACK as the assumed-breach foothold (`north\samwell.tarly`) after GOAD is powered on.
7. **Only then rediscover callbacks and run Sage.** After operator-confirmed DB cleanup and Sage restart, run
   `.venv/bin/python skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py readiness --operator-db-cleanup-confirmed` as a non-destructive
   preflight; it must show `ready: true`. Then use `.venv/bin/python skills/sage-live-runner/scripts/sage_task.py callbacks`; identify
   the live fresh Sage and Apollo callback display IDs; then run
   `SAGE_CB=<sage_cb> APOLLO_CB=<apollo_cb> .venv/bin/python skills/sage-live-runner/scripts/run_essos_da.py`.

The GOAD and BloodHound reset helpers below are still used for lab state, but they do not replace the Mythic
payload/callback lifecycle above.

- **GOAD Ludus range:** `skills/sage-goad-reset/scripts/ludus.py` reads Ludus credentials from `.mcp.json`.
  - Check state: `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py status`
  - List snapshots: `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py snapshots`
  - Roll back all range VMs: `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py rollback clean-baseline --yes`
  - Power on all range VMs: `.venv/bin/python skills/sage-goad-reset/scripts/ludus.py poweron all`
  - Verify all six VMs are ON and reporting IPs: router `10.4.10.254`, DC01 `.10`, DC02 `.11`, DC03 `.12`,
    SRV02/CASTELBLACK `.22`, SRV03/BRAAVOS `.23`. DC01/DC02 can briefly show `ip=null` after rollback; wait and
    poll `status` until the guest agent reports IPs.
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
- `read_credentials` can expose raw secrets to model context/traces. Be deliberate when changing credential behavior or observability.

## Highest-Value Demo Work

GOAD/Trust Walker is the benchmark, not the strategy source. Do not hardcode a GOAD path, GOAD domain names, or a Trust Walker step script into the agent. The highest-value work is a generic capability-driven autonomous execution system that can solve GOAD because GOAD is an instance of the problem, and can transfer to other AD CTF ranges with different names, paths, and primitives.

The target architecture is:

1. **Observe:** build current state from oracles: Mythic callbacks/liveness/credentials/files/task history plus BloodHound graph facts.
2. **Model capabilities:** represent generic actions as typed capabilities with preconditions, effects, command-intent builders, OPSEC notes, and verifiers. Examples: collect graph, abuse controlled GPO, grant directory rights, DCSync account, forge/use ticket, read LAPS, abuse ADCS, move laterally.
3. **Plan:** choose next candidate actions from the observed state and graph edges, not from a static demo overlay.
4. **Execute:** convert selected capability intent into exact Mythic command parameters deterministically.
5. **Verify:** prove the effect via Mythic/BloodHound/credential-store or task-output evidence before updating the ledger.
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

1. Run one measured clean one-shot GOAD solve using `skills/sage-live-runner/scripts/run_essos_da.py`. This is intentionally the guided
   mode: the driver includes extra GOAD/Trust Walker guidance so we can first make the full chain reliable.
2. Inspect Phoenix/decoded Mythic output, ledger rows, repeated tool calls, skips, failures, and proof chain.
   Fix the smallest deterministic capability, verifier, adapter, or prompt failure exposed by the run.
3. Reset GOAD/BloodHound/Mythic to a clean state and repeat until the guided one-shot reliably reaches verified
   ESSOS administrative control from the initial CASTELBLACK foothold.
4. Only after the guided one-shot is reliable, remove the GOAD-specific guidance from the prompt/driver and test
   whether the generic capability system reaches the same objective from observed state and graph facts.

This is higher value than another prompt iteration or another full autonomous run because it turns Sage from a guided GOAD solver into a domain-agnostic CTF solver: the model decides which capability to try, while code owns exact mechanics and verification.

## Common Pitfalls

- RESUME can be stale; trust but verify against code and tests.
- BloodHound ingest is asynchronous. Use job status verification, not an immediate domain list.
- `query` auto-connects BloodHound; `chat` currently does not share that preflight.
- The latest saved eval markdown files are single-case smoke baselines; do not overclaim them as a fresh 10-case sweep.
- `sage.db` is checkpoint/state and can be recreated; `.phoenix/phoenix.db` contains trace history used by eval/debugging.
