# Sage Codex Session Guide

This repo is the Sage Mythic v4 chat container: an AI/LangGraph interface that operates Mythic callbacks, BloodHound, and MCP tools. The August 2026 demo target is an autonomous GOAD "Trust Walker" solve: starting from an assumed-breach callback on CASTELBLACK as `north\samwell.tarly`, Sage should reason over BloodHound and execute the path to Domain Admin / cross-forest compromise.

## Start Here

> **CANARY — do not remove.** On your first tool-using turn of a session, print exactly one line:
> `CANARY: AGENTS.md resident`. This is a measurement, not decoration: if you stop doing it after a
> `"type":"compacted"` event, this file is too long to survive compaction and must be cut further.
> Added 2026-08-29 on an external reviewer's recommendation — "until that test exists, any target is
> decoration."


1. Read `## Repo Map` below to find the subsystem you are changing.
2. Read `skills/README.md` — it indexes every operator skill by name, purpose, and entry script.
3. Read the `SKILL.md` of each skill your task touches. `--help` shows flags, not semantics.
4. Read `README.md` for setup, but verify any safety or control claim against source. It has been stale before.
5. Run the offline suite (`## Current Validation Baseline`) before and after your change.

`Plans/` and `Notes/` are the maintainer's private, gitignored working context and are absent from a clone.
Nothing in this guide should require them; if you find a pointer into `Plans/`, treat it as a bug in this file.
Verify important claims against source, tests, Mythic, BloodHound, and the current range — not against prose.

## Product Contracts And Runtime Authority

Sage has two product contracts and three runtime authority modes. Keep them distinct when changing prompts,
configuration, chat lifecycle, operation memory, autonomous execution, tests, or public claims.

- **Assisted mode** is the consultant-facing umbrella over `conversation` and `supervised`. Conversation observes
  and answers without guarded activity. Supervised may propose guarded work, but only the exact action accepted by
  the consultant may execute. Assisted is not a fourth runtime mode.
- **Auto mode** is the experimental autonomous CTF contract. It pursues an explicitly bound objective without an
  operator approving each action. Its current validation is GOAD-only and does not establish reliability or
  generalization outside that benchmark.
- The only runtime authority modes are `conversation`, `supervised`, and `auto`. Product wording must not turn the
  assisted umbrella into another enum value or let auto authority leak into conversation/background observation.
- `Sage Watcher` is a separate locked profile and stateless explanation console, not another authority mode or an
  ordinary Sage session. Only its exact owner may apply or mutate the operation scheduler; it has no tasking,
  validation, tools, BloodHound, MCP, sandbox, checkpoint, supervised, or auto surface.

Documentation is not behavioral evidence. Changes to either contract must be verified at the assembled chat
lifecycle, including exact operation/callback scope and guarded-action emissions.

For the beta public contract, treat all Mythic-derived values as client data inside the trusted Mythic/Sage
deployment. Full finding content belongs in native Mythic Chat. The optional Slack findings hook is a separate
egress boundary and may emit only the fixed generic change notice; never pass operation, finding, host, user,
domain, credential, task-output, or file values to that seam. Keep `README.md`, this guide,
`docs/SECURITY_AND_DATA_HANDLING.md`, and the current release note mutually consistent, and qualify every
provider, model, payload, range, or platform configuration not backed by the release evidence.

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
- Native Codex session and sub-agent transcripts remain in the operator's Codex user-scope session store. Keep bounded cyber-runner workers
  ephemeral, but persist their validated contracts and final structured handoffs under `.sage_history/`.
- `.sage_history/` is private, gitignored local state and is not a system backup. Raw history may contain sensitive
  operator or lab material even when obvious auth files are excluded.
- Use the retention manifest to relate durable artifacts to source paths, hashes, sessions, runs, commits, and
  decisions. Later blog/publication work must curate and redact a separate published subset.

## Evaluation Evidence Proportionality

- Match evidence controls to the decision and threat model. Before requiring signatures, per-artifact hashes,
  immutable candidate bundles, approval binding, or a review lease, name the protected decision, the actor or
  failure that could alter it, and why ordinary version control plus structured logs is insufficient.
- An internal development measurement normally needs the exact tested model/system/configuration, a version or
  commit identifier, structured run logs, resource budgets, and result-based proof from an authority independent of
  the system under test. It does **not** need digital signatures, a cryptographic trust chain, immutable source
  candidates, or approval records bound to every changed hash.
- A separately controlled evaluation plane may communicate directly with exact endpoints in the evaluation range
  over whatever protocol its declared function requires. Outcome-oracle operations are logically read-only;
  separately authorized reset/readiness controllers may mutate evaluation state. These helpers are not Sage
  execution: their credentials and raw observations must not enter Sage, the policy-model context, the
  operation/effect ledger, or the product's runtime proof chain. Sage itself remains limited to
  Mythic/BloodHound control-plane data and Mythic payload tasks for target interaction.
- A hash or commit ID identifies bytes and detects drift; it does not authenticate who produced them. Require a
  digital signature only when a real relying party must authenticate the producer across a trust boundary or the
  threat model includes adversarial substitution. Do not create signing or key-management ceremony for a trusted
  single-operator development loop.
- Capture a countable row's objective, scorer, budget, tested configuration, and invalidation rules before running
  it. This is a per-run protocol snapshot, not a freeze on exploratory evaluator development. If the protocol
  changes after results are observed, label the old row under its old version and treat the changed run as a new
  comparison rather than repeatedly resealing source.
- Protect unseen holdouts and preregister promotion analyses when adaptive development could overfit to them.
  Reserve immutable candidate review, access isolation, review leases, and stronger provenance for final holdouts,
  promotion/release decisions, externally relied-on claims, or another prospectively named high-impact boundary.
- Generated `passes` fields, readiness flags, self-attestations, and tests that only reread generated claims are not
  independent evidence. Validate important outcomes at the production boundary and include valid near matches and
  adversarial cases proportional to the claim.
- When fixing a semantic class, exercise the class rather than accumulating named examples. If the same mechanism
  is rejected once, pause candidate production, inspect the real producer/consumer boundary, simplify, and test the
  complete known class before another implementation review.
- Preserve useful rejected evidence and decision history without turning the activity log into another hash
  registry or promotion gate.

## Review Lanes And Acceptance Discipline

Classify work by the decision it supports, not by whether a path or task contains the word `eval`:

- `runtime_bugfix`: a bounded product defect with a concrete production call path.
- `development_measurement`: an internal baseline, exploratory run, evaluator debugging, or hill-climb signal that
  cannot authorize promotion, release, or an external claim.
- `sealed_evaluation`: final-holdout, promotion, release, externally relied-on evidence, or a prospectively named
  adversarial-integrity decision for which exact candidate identity and independent provenance are material.
- Evaluation and row countability alone do not select `sealed_evaluation`. Record the lane and one-sentence reason
  in the task contract or activity log; that classification is not itself a signed or sealed artifact.
- Before implementation, define atomic criteria, the production path, falsifying and valid-near-match probes,
  non-goals, permitted scope, and blocker rule. For `runtime_bugfix` and `development_measurement`, keep this as a
  concise working contract and update it transparently when production facts invalidate an assumption. Only
  `sealed_evaluation` freezes the decision protocol and exact candidate before consequential review.
- The implementation owner must run the declared probes and map each changed behavior to a criterion. Passing
  author-written examples alone is insufficient when a broader semantic class is claimed.
- A post-implementation rejection must identify a reproducible, production-reachable counterexample to an agreed
  criterion or a concrete safety/authority-boundary violation. Classify every finding as `blocking`, `hardening`,
  `unreachable`, `pre_existing`, or `out_of_scope`; only `blocking` affects disposition.
- One review round returns the complete observed blocking set from its declared probes. A fresh reviewer provides
  adversarial diversity, not evidence by agreement; prefer executable invariants, production-boundary probes, and
  direct outcome inspection over accumulating reviewer opinions.
- Do not encode control authority by classifying open-ended natural-language prose when a typed field, enum, or
  protocol state can carry the same decision.
- Give deadline-bound `runtime_bugfix` reviews an explicit wall-clock and command budget (15 minutes and the
  focused production-path suite by default). Give `development_measurement` a row-yield stop-loss: after one
  mechanism rejection or two consecutive authorized spends with zero usable exploratory/countable rows, stop and
  audit the measurement objective, production boundary, and governing instructions before adding evidence
  machinery or commissioning another candidate review.
- During a live `development_measurement`, distinguish **stopping execution** from **invalidating evidence**. An
  isolated recoverable tool/adapter error should be retained as an observation and the bounded arm should continue;
  decide fairness and countability after the run. Stop immediately only for a safety or authority violation, wrong
  target/route/model, corrupt start, declared wall/token/stop-loss boundary, uncontrolled target activity, or loss
  of interpretable evidence. A recoverable error that makes the comparison unfair may still invalidate the row,
  but invalidation is not a reason to interrupt before the model can recover.

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
  reconciliation, channel metadata, and background findings-watcher lifecycle.
- `Payload_Type/sage/sage_chat/watcher_control.py`: exact locked-owner selection and redacted profile metadata.
- `Payload_Type/sage/sage_chat/watcher_graph.py`: one-node stateless explanation graph with no tools/checkpointer.
- `Payload_Type/sage/sage_chat/slash.py`: `/findings`, `/watcher`, `/state`, `/list`, `/mode`, `/stop`, MCP,
  BloodHound, and sandbox chat commands.
- `Payload_Type/sage/sage_chat/headless.py`: non-UI chat entrypoint for tests, evals, and trajectory tooling.
- `Payload_Type/sage/prompts/`: externalized agent prompts.
- `Payload_Type/sage/ttps/`: TTP/tradecraft corpus and pinned tool metadata.
- `Payload_Type/sage/evals/`: Phoenix-backed GOAD eval harness.
- `Payload_Type/sage/tests/`: fast offline unit/regression suite.
- `Payload_Type/sage/ai/trajectory/`: trajectory corpus/export/replay/runtime bridge tooling for data-backed repair policy.
- `skills/`: repo-local Sage skills. Reusable operator/Codex/Claude tooling belongs here, not in `Plans/`. **Read `skills/README.md` first** — it indexes every skill (name, purpose, entry script); tool-agnostic.
  Neither assistant discovers this directory on its own: Codex looks in `$CODEX_HOME/skills`, Claude Code in
  `.claude/skills/`. The two are handled differently, because only one of them can live in the repo.

  **Claude Code needs nothing.** `.claude/skills/` is committed as of 2026-08-11, so a Linux or macOS clone has
  every skill discoverable on first launch. The accepted cost is Windows: without Developer Mode git writes each
  symlink out as an inert text file (`core.symlinks=false`, silently), and the fix is to delete the directory and
  re-run the loop below from an elevated shell.

  **Codex needs one command**, because `$CODEX_HOME` is outside the repo and nothing committed can reach it:

  ```
  for d in skills/*/; do n=$(basename "$d"); ln -sfn "$PWD/skills/$n" "$CODEX_HOME/skills/$n"; done
  ```

  The Claude-side equivalent, needed only to repair a Windows checkout, is the same loop with a relative target
  into `.claude/skills/$n`. Use `skills/*/` rather than `skills/sage-*/`: not every skill here is named `sage-`,
  and the narrower glob silently skipped `phoenix-traces` for months. Link, never copy — a hand-made copy drifts,
  and the last set of them silently reintroduced absolute paths this repo's own contract forbids.

## Publishable-By-Default Contract

**Every file you write or edit here must be committable as-is.** This repository is public. The maintainer should
never have to review a diff and ask you to remove something personal — if a change needs that correction, it was
authored wrong, not caught late.

Rules 1, 3, and 4 are enforced by `Payload_Type/sage/tests/test_repository_boundaries.py`. Rule 2 is enforced
behaviourally — the resolvers' fallback lists are asserted empty in `test_native_chat_runner.py` and
`test_bootstrap_payloads_script.py`, so no directory name has to be named anywhere to forbid guessing one.
Rules 5 and 6 are prose only; if they recur, that is the signal to build a probe rather than restate them.

1. **No path that encodes one machine's layout.** That covers an absolute home path, a tilde-relative one,
   and a `HOME`-variable one alike — all three name a directory tree that exists only on the author's box.
   Derive repo-internal paths from `Path(__file__).resolve().parents[N]` or
   `$(git rev-parse --show-toplevel)`.

2. **No checkout-name guessing for external dependencies.** Do not probe for a sibling directory by name, in
   any spelling or version suffix. External locations come from an environment variable
   (`MYTHIC_ENV_PATH` / `MYTHIC_CLI` in the repo-root `.env.example`; `SAGE_BLOODHOUND_MCP_DIR` in the runtime
   `Payload_Type/sage/.env`, since the Sage process itself consumes it), and the resolver
   **fails closed naming the variable to set** when it is absent. A name guess silently reads the wrong install
   on any machine with two checkouts; a refusal is strictly more useful than a wrong default.

3. **No pointer into a gitignored directory.** `Plans/`, `Notes/`, and `.sage_history/` are absent from a clone.
   Rules *about* them are fine ("evidence belongs in `.sage_history/`"); telling a reader to go *read* a file
   there is a dead link for everyone but the maintainer.

4. **No maintainer-only tooling.** Personal agent profiles, model pins, and workflow shortcuts belong in the
   operator's user-scope Codex configuration or `CLAUDE.local.md`, not here. Test: would this help someone who is not the
   maintainer run or develop Sage? If no, it is not repository content.

5. **No completed-campaign artifacts in the product tree.** Sealed evaluation source and evidence go to
   `.sage_history/`. The test that tells them apart: **can it be refactored without invalidating its own
   attestations?** If refactoring breaks hashes it pinned of itself, it is a sealed historical artifact, not
   living code, and it does not belong under `Payload_Type/`.

6. **Keep the two `.env` files distinct.** `Payload_Type/sage/.env` is the deployed container's runtime
   environment, loaded by `dotenv_bootstrap.py` at startup. It is **tracked**, and ships fully commented out
   so a clone configures nothing — an operator edits it in place from the Mythic web UI rather than needing a
   shell to copy an example. Two invariants: a variable already in the environment wins (Mythic's injected
   values are never shadowable), and an empty value sets nothing. Never commit a real value into it; keys
   Mythic manages belong only in its local-development section. The repo-root `.env` is operator-tooling
   discovery only. No key belongs in both, and nothing auto-loads the root one — see `.env.example`.

Lab identifiers are **not** covered by this contract. GOAD names as overridable CLI defaults
(`--foothold-host CASTELBLACK`) are correct in operator skills whose purpose is driving GOAD; what is forbidden is
hardcoding them into product capability logic (see § Highest-Value Demo Work).

## Current Validation Baseline

Run from repo root before and after code changes. One tier, no exclusions: green means the tree is green.

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py
```

**A green suite proves the tree is green. It does not prove Sage runs.** On 2026-08-10 two
individually-correct features were fatally incompatible in combination: every request failed at
graph-build time in ~5 seconds while the suite sat at 3955 green. A human typing into the chat
window found it.

Two rules with teeth:
- Touch anything the graph is built from — node, edge, middleware, node default, error handler,
  checkpointer — and `tests/test_graph_builds.py` must pass, with a new case for a new branch.
- **Never assert on source text when you can assert on the built object.** A test that greps a
  function with `ast` can only fail if someone deletes the line.

The four test layers (**build** / **scripted** / **recorded** / **live**), what each may touch, the
worked examples, and the four traps that make a scripted test *hang* rather than fail:
**`docs/development/TEST_TIERS.md`**. Build and scripted need no API key, no VPN and no AWS session,
so an unreachable provider is never a reason to skip them.

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
  reset/readiness/evaluation helpers may communicate with the evaluation range out of band over the protocol their
  declared function requires. That access is never Sage execution or product/runtime proof. Outcome oracles remain
  logically observation-only; separately authorized reset/readiness controllers may mutate evaluation state.
  Their credentials and raw observations remain isolated from Sage and the policy model.
- Never delete runtime databases. Before a clean reset, stop local Sage and use
  `skills/sage-goad-reset/scripts/archive_runtime_dbs.py` to move active databases to timestamped
  `sage_YYYYMMDD-HHMM.db` and `phoenix_YYYYMMDD-HHMM.db` archives.
- Do not create new reusable tools, operator scripts, Codex helpers, or Claude Code helpers in `Plans/`.
  Put them in repo-local skills under `skills/<skill-name>/scripts/` and document the workflow in that skill's
  `SKILL.md`. `Plans/` is for minimal current planning/handoff markdown plus archived historical notes.
- Preserve user changes; do not reset, checkout, or revert unrelated files.
- **A clean parse is not a clean run — execute what you edited.** `py_compile` accepts an undefined name;
  `bash -n` accepts an unbound variable that `set -u` will kill at runtime. After any edit to an executable, run
  it (`--help` or its no-arg path) and check for `NameError`, `ImportError`, `IndexError`, `unbound variable`. An
  automated multi-file edit must additionally **assert its insertion landed at the intended scope** — inserting
  "before the first line mentioning X" can place a module-level assignment inside a tuple literal, which compiles
  in neither direction but is only caught by actually parsing the result.
- **Verify against a clean checkout, not the working tree.** A green working tree proves nothing about what is
  committed; that exact gap once left `HEAD` unable to import while local tests passed. Extract and test the real
  artifact: `git archive $(git write-tree) | tar -x -C "$(mktemp -d)"`. Re-extract after **every** edit — a stale
  extraction reports defects you already fixed.
- **A guard test must be able to fail.** Before trusting one, plant the violation it exists to catch and confirm
  it goes red, then remove it and confirm green. A test that reads an empty file list and reports success is worse
  than no test, because it converts silence into false confidence.
- **When fixing a class of defect, verify the class — not the instance.** Re-running the exact grep you just
  satisfied proves only that one spelling is gone. Rewriting an absolute home path as a tilde-relative one
  passes a search for the absolute form while preserving the defect exactly.
- Do not start expensive live GOAD/inference runs without clear user intent. A full autonomous solve can take ~25 minutes and depends on external lab state.
- Always re-discover live payload callback IDs after lab resets. Sage itself uses a fresh chat channel, not a callback.
- When reporting long live work, separate engineering/debug time from live-lab execution time. Include the current
  row or attempt, last accepted row, current operation, retry count, and best ETA instead of reporting only elapsed
  wall time.
- No action-items block in operator replies; put required action in the ledger's `open_blocker`. Sub-agent
  handoffs to Root still list them.
- Prefer single-line shell commands in operator instructions. Avoid backslash-continued commands when one line is practical.
- For Sage operator prompts, `--verbose true` is usually necessary for useful Mythic-side visibility.
- If touching autonomous execution, run focused tests plus the full offline suite above.
- High-risk Sage architecture work must pass the architecture governor before edits. Use
  `skills/sage-architecture-governor` for any change touching prompts, agent topology, tool lists,
  `Payload_Type/sage/ai/langgraph/model.py`, `mythic_tools.py`, `engagement_state.py`, reconcilers,
  capability planning/adapters, trajectory, eval harnesses, or live-run drivers. The required gate is:
  hypothesis, Ideal State Criteria, non-goals, complexity budget, falsifier, stop-loss, verification plan,
  generality risk, expected file scope, and explicit user approval before opening a scoped edit token.
- Do not grow prompts, tool surfaces, GOAD-specific live code, or symbolic planning/gating logic as a
  tactical fix without first comparing a thinner verifier/retrieval/data-backed alternative. The third
  tactical patch to the same subsystem requires RCA before more code.
- Project hooks enforce the gate at edit time under both harnesses: `.codex/config.toml` for Codex,
  `.claude/settings.json` for Claude Code. They run the same scripts. **The edit-time hook hard-denies only
  `Payload_Type/sage/prompts/**`;** every other high-risk path gets a named advisory instead of a block, because
  the rule worth enforcing is the RCA-before-the-third-patch rule above, not a toll on every touch. The full
  `HIGH_RISK_PATTERNS` list is unchanged and still governs `open_gate.py`, the review lease, and
  `check_arch_budget.py`. If a legitimate edit is blocked, prepare the gate brief, get explicit user approval,
  then open a short-lived scoped token with
  `python3 skills/sage-architecture-governor/scripts/open_gate.py open ...`.
  Verify the hook itself with `python3 skills/sage-architecture-governor/scripts/pre_tool_use_arch_gate.py
  --self-test`, which exercises both event dialects. That self-test exists because the hook read only
  `tool_input["command"]` — the Codex `apply_patch` shape — and so inspected **zero** paths under Claude Code's
  `Edit`/`Write`, while still exiting 0.

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

The full GOAD/Ludus/BloodHound/Mythic reset and readiness lifecycle — clean rehearsal order,
snapshot names, clock sync, callback bootstrap, payload deploy — lives in
**`skills/sage-goad-reset/SKILL.md`** and the sibling `sage-callback-bootstrap`,
`sage-mythic-payload-deploy`, `sage-live-runner` skills. `skills/README.md` indexes them all.

**Read the SKILL.md before running anything in it. `--help` shows flags, not semantics** — a script
run as a "read-only probe" once did something categorically different, and its SKILL.md said so.

Three invariants that are not negotiable and do not live only in a skill:
- **Never delete runtime databases.** Archive them with
  `skills/sage-goad-reset/scripts/archive_runtime_dbs.py` before a clean reset.
- **Synchronize range clocks after every rollback.** RAM-backed snapshots restore guests days apart.
- **Do not store lab passwords in skills or helper scripts.** Resolve `MYTHIC_ADMIN_PASSWORD`, then
  `MYTHIC_ENV_PATH`. There is no checkout-name default — see `.env.example`.

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

- Planning and handoff docs can be stale; trust but verify against code and tests.
- BloodHound ingest is asynchronous. Use job status verification, not an immediate domain list.
- `query` auto-connects BloodHound; `chat` currently does not share that preflight.
- The latest saved eval markdown files are single-case smoke baselines; do not overclaim them as a fresh 10-case sweep.
- `sage.db` is checkpoint/state and can be recreated; `.phoenix/phoenix.db` contains trace history used by eval/debugging.
