# Sage Session Guide

Sage is a native Mythic v4 chat service built on LangGraph. Inspect source and assembled behavior before relying
on documentation claims.

On the first tool-using turn of a session, print exactly one line: `CANARY: AGENTS.md resident`.

## Start and scope

- Read relevant source before editing. Use `rg` for search and `apply_patch` for manual edits.
- For substantial work, state atomic success criteria, the production path, falsifying probes, and scope before
  implementation. Keep the contract short and update assumptions when evidence changes them.
- Read `skills/README.md` to locate an operator workflow, then its `SKILL.md` before running its scripts.
  A script's `--help` explains flags, not operational side effects. Specialized governance, trajectory, and
  promotion guidance is needed only when the task enters that subject.
- Preserve existing source changes and rejected evidence. Do not reset, revert, commit, tag, or publish without
  explicit authorization. Ordinary authorized development does not require approval for each repair.
- Use the smallest change that exercises the real producer/consumer boundary. Avoid adding prompts, tool
  surfaces, symbolic planning layers, or GOAD-specific strategy when a thinner verifier or existing data suffices.

## Product contracts and safety boundaries

- Assisted mode covers `conversation` and `supervised`; it is not a fourth runtime enum. Conversation observes
  and answers without guarded activity. Supervised executes only the exact guarded action the consultant accepts.
- `auto` pursues an explicitly bound CTF objective without approval of each action. Its validation is GOAD-only;
  do not claim reliability or generality outside that benchmark.
- Sage Watcher is a locked, stateless explanation profile. Only its exact owner may mutate the scheduler.
  It has no tasking, validation, tools, BloodHound, MCP, sandbox, checkpointer, supervised, or auto surface.
- Sage runtime may use Mythic/BloodHound control-plane data. All target-facing interaction must execute as Mythic
  tasks on live payload callbacks. Never connect from Sage directly to target LDAP, SMB, Kerberos, WinRM, RPC,
  HTTP, or equivalent services, or use Sage-local attack artifacts as compromise proof.
- Runtime effects and objective completion require Mythic task output/artifacts, Mythic credential-store state,
  or BloodHound facts derived from payload-collected artifacts. Self-reported success is not proof.
- A separately controlled evaluation plane may use exact authorized range endpoints for logically read-only
  outcome observations. Reset/readiness mutations need their own live authority. Evaluator credentials and raw
  observations must stay outside Sage, policy-model context, the effect ledger, and runtime proof.
- Full findings belong in native Mythic Chat inside the trusted deployment. The optional Slack hook may emit only
  the fixed generic change notice, with no operation, finding, host, user, domain, credential, task, or file values.
  Keep `README.md`, `docs/SECURITY_AND_DATA_HANDLING.md`, and release claims consistent with this boundary.
- Do not put secrets in scripts, skills, logs, or public artifacts. `read_credentials` can expose raw secrets to
  model context and traces; inspect credential changes deliberately.
- Do not hardcode GOAD identities or a Trust Walker path into product strategy. Overridable GOAD defaults belong
  only in operator tools intended for that range.

## Local operations

- Sage runs as a local process in the `sage` tmux session for this workflow. Use the reset skill's
  `sage_restart.sh`; do not substitute Docker/Mythic container restarts without authorization.
- Never delete runtime databases. Stop local Sage and archive them with
  `skills/sage-goad-reset/scripts/archive_runtime_dbs.py` before a clean reset.
- After every rollback, synchronize range clocks. Rediscover live payload callback IDs after resets;
  Sage itself uses a fresh chat channel, not a callback.
- Before live work, settle exact task/targets, model/route, wall/token/action budgets, retries, start-state
  checks, and cleanup scope. Do not infer live or provider spend from permission to edit local files.
- A private active standing-live-authority contract may cover repeated GOAD/Trust Walker evaluation and recovery
  runs. Revalidate it at the CLI boundary on every invocation against the exact environment, endpoint, protocol,
  budgets, acceptance criteria, safety limits, and cleanup contract; retain a separate ledger for every run.
  In-scope evaluator or staging repairs may advance source identities without another conversational approval when
  every other binding is unchanged. The standing contract is not consumed by a completed run. Stop and obtain new
  authority for a new target or environment, broader runtime authority, changed criteria or route, increased
  budgets, uncontrolled activity, publication, commits, Apollo diagnostics, Mythic upstream work, or any action
  outside the standing scope.
- External dependency paths must be explicitly configured, never guessed from sibling checkout names.
  `MYTHIC_ENV_PATH` and `MYTHIC_CLI` must identify the same install; `SAGE_BLOODHOUND_MCP_DIR` identifies
  BloodHound's integration. Missing configuration fails closed naming the variable to set.
- For standalone HTTPS clients or a TLS connection failure, inspect Sage's existing
  `Payload_Type/sage/ssl_bootstrap.py` and the optional `Payload_Type/sage/certs/bundle.pem` before changing
  trust settings or blaming the endpoint. Reuse `configure_sage_ssl_cert_file`; clients that disable HTTPX
  environment trust must pass the selected CA bundle in an explicit verified SSL context. Verify the connection
  with that context. The bundle may be absent, in which case use normal platform trust; never disable TLS
  verification to make a probe pass.
- The repo-root `.env` is operator discovery only and is not auto-loaded. `Payload_Type/sage/.env` is tracked,
  ships commented out, and configures the deployed runtime. Existing environment wins; empty values set nothing.
  Do not commit real values or duplicate keys between these two scopes.

## Development measurements and review

Internal comparisons use exact model/config/source identity, structured logs, a prospective per-run protocol,
declared resources, and independent outcome evidence. They do not require a fresh reviewer, review lease,
signature, immutable candidate, exact-hash approval, or a new campaign after each repair.

Keep protocol validity separate from objective success and hypothesis agreement. An observed timeout, recoverable
tool failure, false completion, unexpectedly high cost, or unfavorable Sage result remains an outcome when the
protocol permits interpretation. Wrong target/model/start or missing evidence can invalidate a comparison.
Preserve each attempt and completed arm; pair only compatible starts. Protocol changes create a new version and
do not relabel old observations.

A same-model comparison measures the whole Sage system against an explicitly defined minimal raw-tool loop.
Record tool/knowledge asymmetries. Do not require controller replication, composite OPSEC scoring, or non-GOAD
validation for a bounded GOAD-only development result. These remain separate decisions.

During an authorized bounded run, retain recoverable adapter errors and allow recovery within budget. Stop for
an authority/safety violation, wrong target/route/model, corrupt start, exhausted resource limit, uncontrolled
activity, or loss of interpretable evidence. Decide evidence validity separately from execution stopping.

Use targeted review only when it resolves a concrete scoring, fairness, architecture, or authority question.
A blocking finding needs a reproducible production-reachable counterexample to an agreed criterion or an actual
authority violation; classify other findings as hardening, unreachable, pre-existing, or out of scope.
After repeated repairs without usable observations, inspect the failing production boundary and reduce scope;
do not create another admission framework or correction/review cycle.

Use `skills/sage-architecture-governor` for actual prompt, topology, tool, planner, or runtime authority changes.
Development evaluator plumbing uses focused verification within standing authority. Stronger review and provenance
controls apply only to a declared final holdout, promotion/release, externally relied-on claim, or named integrity
threat. The project hook retains prompt-edit protection and advisory architecture/retention checks.

## Verification

Exercise the affected assembled path early. Run focused tests during repairs and the full offline suite at the
integration boundary, rather than between every routine edit:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py
```

- Changes to graph construction must pass `tests/test_graph_builds.py`; cover each new configuration branch.
- Assert on built objects and actual output, not source text or generated `passes` claims.
- Execute changed scripts through a safe local path; parsing alone does not detect runtime errors.
- Test the semantic class with relevant positive, negative, and near-match probes. Demonstrate a new guard can fail.
- Run `Payload_Type/sage/tests/test_repository_boundaries.py` before handoff.
- Verify the actual deliverable from a fresh clean extraction at integration/release. Keep the user's index intact;
  identify the tested tree separately from HEAD and do not claim uncommitted changes are released.
- A green offline suite does not prove a live run. See `docs/development/TEST_TIERS.md` for build, scripted,
  recorded, and live boundaries. No live test is implicit in this section.

## Repository map and artifacts

| Surface | Location |
|---|---|
| Graph construction, middleware, checkpoints | `Payload_Type/sage/ai/langgraph/model.py` |
| Mythic tools, command issue, ingest | `Payload_Type/sage/ai/langgraph/mythic_tools.py` |
| Command construction | `Payload_Type/sage/ai/langgraph/command_builder.py` |
| Capabilities, engagement state, reconcilers, intent | `Payload_Type/sage/ai/langgraph/` |
| Native chat lifecycle, slash commands, Watcher, headless | `Payload_Type/sage/sage_chat/` |
| `/findings`, `/watcher`, and other slash commands | `Payload_Type/sage/sage_chat/slash.py` |
| Prompts and tradecraft corpus | `Payload_Type/sage/prompts/`, `Payload_Type/sage/ttps/` |
| Development evaluation and trajectory | `Payload_Type/sage/ai/hillclimb/`, `Payload_Type/sage/evals/`, `Payload_Type/sage/ai/trajectory/` |
| Tests and operator tools | `Payload_Type/sage/tests/`, `skills/` |

Public files must be committable as-is: no personal paths, private model pins, maintainer tooling, or instructions
to read files absent from a clone. Runtime must not import development evaluation/range/operator packages.
Reusable tools belong under `skills/`, not private planning directories.

Keep durable private contracts, decisions, transcripts, rejected/accepted evidence, and handoffs under
`.sage_history/` using `skills/sage-artifact-retention`. Preserve useful originals; do not overwrite history.
Keep credentials, payloads, active locks, fixtures, clones, and reproducible staging in temporary storage.
Private history is not a system backup; publication requires a separate curated and redacted subset.

Native Mythic Chat renders markdown and updating tool-use cards. Use tables for enumerations, code fences for raw
output, and the existing cards for tool activity. Do not add timestamps or duplicate cards with verbose text.
