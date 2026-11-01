# Changelog

All notable changes to Sage are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Sage uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, behaviour and configuration may change
between minor releases.

## [Unreleased]

### Added

- When the Supervisor scopes a supervised step to a capability, it now sees what each capability does. The
  delegation tool lists every catalog capability with its effect, the state it already needs, and when to pick it
  over its neighbours, generated from the capability catalog so a new capability appears automatically and nobody
  maintains a second copy. A specialist that declares no guarded tools of its own no longer receives the
  step-scoping fields it could never act on.

- In supervised mode each specialist step is bounded. A step returns control to the Supervisor after its one
  guarded action has run or been rejected, or earlier at 20 tool calls, 10 minutes of active time, or three
  consecutive tool errors. Time awaiting an operator's approval and time inside the guarded action itself,
  including the Mythic task wait, never count as active time. After its action the specialist may still read and
  summarize the result; a further guarded proposal in the same step is removed before an approval card is shown,
  so it needs a new step. Sage then gives the Supervisor a typed outcome for the step: the action ran, was
  rejected, fell outside the step's scope, a limit was reached, a task is still pending, or the specialist
  finished on its own, with the tasks issued and the time spent. Reaching a limit is not a failure; the
  Supervisor chooses the next step, and returning control never cancels a task on the target. Set the limits
  with `SAGE_STEP_MAX_TOOL_CALLS`, `SAGE_STEP_MAX_ACTIVE_SECONDS` and `SAGE_STEP_MAX_CONSECUTIVE_ERRORS`.
  Autonomous mode and conversation requests are unchanged.

- A supervised step's outcome now references everything the step did, read by Sage from Mythic rather than taken
  from the specialist's summary: each task it issued with its recorded state, command, callback and output row
  count; the credentials Mythic links to those tasks or that the step added, by account, realm and type only (never
  the secret); and the graph reads it made. Every issued task reaches the Supervisor with an output excerpt or a
  stated reason it has none (still pending, no output, read failed, or over the excerpt budget). Sage reads an
  excerpt only for a task the specialist did not read itself, within 8,000 characters per step, so output that
  lands after the specialist looked is delivered without another delegation. At most 16 tasks per step are read;
  any beyond that are still listed.

- Supervised steps now record what they proved in the engagement ledger, which tracks progress rather than every
  task. A new callback, whether on a new host or as a new user, records a session on that host. A callback at high
  or SYSTEM integrity, or one whose integrity rises after a step's task on it, records admin or SYSTEM on that host.
  A credential whose source task belongs to the step is recorded by account and realm, never with its secret.
  A callback gain is credited to the Sage task issued before the callback first checked in, within 10 minutes
  (`SAGE_STEP_LEDGER_ATTRIBUTION_SECONDS`), so a payload that checks in after its step ended is still credited.
  Each row is proven against Mythic's own callback record or the credential's source task; a gain credited to a
  task that ended in error is kept unproven. A proven row is never downgraded by a weaker observation, and a
  repeated gain updates its row. Losing a callback or changing a token records nothing, and a gain no Sage task can
  be credited with is reported but not recorded. Autonomous mode and conversation requests are unchanged.

- A supervised delegation to the Mythic operator carries a brief that Sage checks before the specialist starts:
  the callback, the hosts, domains or principals the step acts on, the result it should produce, and what its one
  guarded action may use, either catalog procedures or the specialist's guarded tools. The callback must be one
  Sage has observed (and the request's bound callback when it has one), targets must be named by the operator or
  already known, and referenced tasks and credentials must be records Sage holds. A failed check returns the
  reasons to the Supervisor; a second consecutive failure ends the request and tells the operator why. A step
  that only reads Mythic or BloodHound records declares itself `control_plane` and needs no callback, but may not
  propose any guarded action. The specialist still receives the operator's exact words, and before it acts Sage
  tells it what its one guarded action may use, its callback and its targets. The specialist is also offered only
  the guarded tools its step admits, and a step scoped to catalog procedures is not offered technique guidance,
  since Sage runs the procedure; the step boundary still removes any out-of-scope call. When a step is scoped to
  graph collection alone on a callback, with at most one target, Sage proposes that `execute_capability` call
  itself instead of asking the specialist's model to choose again: the callback binds it, a target domain is passed
  through, and without one (or when the target names a host) the collector uses the callback user's current
  domain, so nothing about the network needs to be known in advance. The call reaches the same approval card and
  step checks, and the specialist's model still reads the result. A step with several targets or procedures is
  left to the specialist. When a step scoped to a
  catalog procedure calls a tool the step does not offer, the step-boundary message names the step's procedure and
  tells the Supervisor to re-send the scope if that procedure is what the operator asked for, or else re-scope to
  what the operator asked for; it never suggests widening a procedure step to raw tasking. Such a deviation may be
  retried once under the same scope, and a second ends the request. A step whose `execute_capability` call names a
  capability outside its scope has the wrong scope, and re-sending that scope unchanged ends the request at once.

- The Supervisor can list callbacks itself in conversation and supervised mode, so "which callbacks are alive"
  is answered without a delegation and a brief can name a callback the Supervisor has seen.

- Every delegation outside the capability catalog, that is every step scoped by its guarded tools rather than a
  catalog procedure, is recorded with its title, assignment, expected result and brief, so later reviews can find
  recurring procedures worth adding to the catalog. The capability choices offered to the model come from the
  catalog itself, and a name that is not a catalog procedure is refused.

- In supervised and conversation mode, a request to reach privilege on or take control of a target now consults
  the BloodHound graph before Sage chooses a specialist. Sage reads which domains the graph has collected and,
  for each target domain the graph covers, whether a path exists from the principals Sage controls. It then
  hands that result to the Supervisor as context: covered, partially covered (naming the domains that were never
  collected), no graph, or BloodHound unreachable. A target named without its suffix ("essos") resolves only
  against domains Sage has observed. The read cannot block a request: if BloodHound is down or slow, Sage
  proceeds and says no graph analysis was available. "Administrative control", "full control" and "DA" now count
  as privilege goals.

- `list_staged_tools`, a read-only tool for the operator agent, and `/tools staged` in chat list the binaries in
  Sage's `tools/` drop zone. Before, Sage found a staged binary only when a TTP named its exact filename. Each
  file shows its sha256, its type and architecture read from the file header, the TTPs that document it, and
  whether Mythic already holds the same bytes, so Sage can use that copy without asking to upload it again.
  Renamed and per-architecture builds (`new_sharphound_x64.exe`, `SharpHound.o`) match their tool. A binary no
  TTP documents carries a hint to ask the operator what it is for, and a tool that is staged nowhere returns a
  message asking the operator to add it. `ensure_tool_uploaded` names staged variants when the exact filename
  is missing.

### Changed

- Staging a tool binary into Mythic's file store (`ensure_tool_uploaded`) no longer asks for its own approval in
  supervised mode. It writes only Mythic's operation file store from the local tool drop zone and reaches no
  callback; the command that later runs the tool on a target still needs its approval. A task whose command
  selects a registered file still registers that file before it is issued. Staging stays unavailable in
  conversation mode and in a stopped request.

- `mythic-cli install github` now pulls the published release image from GitHub Container Registry instead of
  building Sage locally. Set `sage_use_build_context` to `true` in Mythic's configuration to keep building from
  source.

### Fixed

- A supervised request no longer re-sends a step that already answered until the no-progress stop. When a step
  returns without issuing a task, adding a credential or meeting a denial (an answered read, a report that the
  operator must act, or an "already achieved" capability verdict) and the Supervisor sends that same step again
  unchanged, Sage does not dispatch it: it hands the step's answer back once, and a second repeat ends the request
  at a checkpoint with that answer. A changed step, or any step after a different one, still runs. The earlier
  repeat guard read state the Supervisor's handoff never carried, so it never fired.

- An approved supervised graph collection now collects even when the same callback identity and scope already have
  a verified graph. Before, collection preparation ran, then the collect-once gate skipped the collector and the
  approved collection was reported as failed. Rights granted on directory objects outside Sage cannot be seen
  until a new collection, so the operator's approval is the signal to collect again. The approval card says when
  the identity and scope were already collected. Without that approval the gate still skips a covered collection.

- A supervised step scoped to graph collection can now collect. Collection was offered as a capability but had no
  `execute_capability` procedure, so the specialist could only propose the collector command itself, which the
  step's scope removed before any approval card; the Supervisor re-sent the same scope until the request halted
  for making no progress. `execute_capability` for `collect-graph` now runs Sage's own collection, the same
  procedure autonomous mode uses: one approval card covers the identity checks, staging, the collector, the
  download and the BloodHound ingest, and each task it issues must match that approval's callback and domain.
  A raw command in a capability-scoped step is still removed before a card.

- A request that ends while a specialist's delegation is still open, for example when a capability
  cannot be planned or an identical re-proposal is stopped, now finishes normally. The open delegation
  failed the final consistency check, so a request whose work had finished ended as an error with its
  sub-agent card stuck on "running". Open delegations now close as stopped, as open tool calls already
  did, including one whose card was lost to an interrupted resume.
- A request's final message now appears after everything the request showed. It used to finalize the
  last main-window message in place, and Mythic orders a chat by when each message was created, so a
  checkpoint written into an early progress message sat above the sub-agent panels and tool cards that
  followed it. The final message reuses that message only while nothing newer exists; otherwise it is
  its own message at the end.
- A supervised request no longer stops at a "has not reached a terminal state" checkpoint when the task
  had already finished. The handback read the task once, and output that landed a moment later made a
  finished task look unfinished. It now re-reads for a few seconds and takes completion from the task
  record. A task whose agent completion function failed (Mythic's `Error: completion function` status) is
  reported as finished with output, with a note that the agent may not have registered credentials or
  files from it, and other `error:` statuses on a finished task are reported as failed rather than
  completed.
- Answered requests no longer end in a "no progress" stop. A BloodHound answer reached the Supervisor
  without the graph queries behind it, so the Supervisor treated a finished analysis as unsupported and
  delegated it again until the stop. The queries and bounded excerpts of their results now arrive with the
  answer, as Mythic task output already did. A Mythic_Operator re-delegation in the same request also ended
  before it ran, because an earlier delegation's finished capability result was taken as its own; only a
  result from the current delegation ends it now.
- A forged-ticket request for one domain that also named a different `source_domain` was treated as a
  request for the source domain, so it reported "already achieved" from the source domain's existing proof.
  The named domain is now the ticket's target and the source domain supplies the ticket.
- Sage can answer questions about the engagement-state tables you see. `/state` and the end-of-turn table
  were shown only to the operator, so a question such as "why is hop 1 pending?" had nothing to refer to.
  The table you were last shown now reaches Sage with your next message, without the evidence previews.
  Hops also keep permanent numbers: `/state remove` leaves a gap instead of renumbering later rows, so
  `/state set 4 …` and "hop 4" mean the same hop in every view. The end-of-turn table now uses the same
  columns and numbers as `/state`, and the `/state` count is labelled "Hops", since it includes pending
  and failed rows.
- The container no longer runs Sage as root. It runs as the owner of the installed service directory, so the
  databases and state Sage writes there belong to that user, and a later `mythic-cli install` no longer fails
  on root-owned files and leaves Sage uninstalled. The first start hands back root-owned files left by earlier
  versions. Python bytecode caches are no longer written into the service directory.
- The Phoenix UI on port 6006 no longer freezes in the container. Sage's span exporter was the process-wide
  tracer, so Phoenix's own server exported the span for each request it handled, synchronously, to the
  collector that the same server thread runs, and stopped answering on its first request. Sage's spans now go
  to Sage's own exporter, with no change to what is traced. `kill -USR1` on the Sage process, or
  `docker kill -s USR1 sage`, now writes every thread's stack to the log, and startup logs which span
  exporter Sage uses.
- The startup version banner now appears in the container logs at Mythic's default log level
  (`DEBUG_LEVEL=warning`); it was logged at INFO and hidden.
- A request's final message now records how the request ended (`completed`, `blocked`, `checkpoint`,
  `stopped`, `cancelled`, or `error`) as `turn_outcome` in its metadata. Mythic stores only `complete`,
  `error`, and `cancelled` as a request's status, so a request Sage stopped on its own, or one that ended
  blocked, still shows as `complete` there; the message content and `turn_outcome` give the real ending.
- A channel in Conversation mode with Autonomous Solve on no longer switches itself to autonomous mode.
  Conversation stays read-only; Sage ignores the toggle there and says so once in the chat. Autonomous
  execution now comes only from Mode = Autonomous.
- Tasking tools are now enabled by the `task.write` scope, which Mythic requires to create a task, instead of
  `callback.write`. A channel token with `task.write` and no `callback.write` no longer loses tasking, and one
  with `callback.write` but no `task.write` no longer fails mid-run. `ingest_collection` only reads files from
  Mythic, so it now needs `file.read`, which `file.write` also grants. Sage's declared channel scopes now
  list `task.write`.
- `sandbox_exec`, the `/sandbox` command, and `download_tool` no longer require a Mythic scope. They run
  locally and make no Mythic call, so a narrow channel token no longer disables them. Model-proposed use
  still needs approval in supervised mode and is refused in conversation mode. `callback.write` is no longer
  among Sage's declared channel scopes, since no tool uses it.
- Sage's declared channel token scopes now list everything it uses: `apitoken.write` (which Mythic requires on
  any AI channel token), the read scopes for the Mythic data it queries, and `credential.write` and
  `eventlog.write`. The unused `tag.write` is gone. Mythic does not enforce this list; it tells the operator
  what to grant, and the docs list the same set.
- Max Steps now caps each chat request. The model-step count used to add up across every request in a channel
  until the session was rebuilt, so a later request could halt after only a few steps while the notice blamed
  "this request". It now starts again with each new request; approving a card continues the budget of the
  request it belongs to.
- A deliberate wait of 300 seconds or more, such as waiting for a Group Policy refresh, no longer fails with
  `NodeTimeoutError: Node 'Mythic_Operator' exceeded its idle timeout`. `wait_for_seconds` and the GPO
  capability's wait now keep the agent's idle timer alive for as long as they wait, as a Mythic task wait
  already did.
- A supervised `execute_capability` proposal whose inputs arrive as a JSON string now reaches its approval card.
  The card's plan preview read `inputs` only when it was already an object, so it planned with no inputs and
  refused with a missing field (for example `system_action` on a GPO execution) even though the capability
  itself accepts the string form.
- Approval cards for raw tasks now show the intended effect with its target filled in, for example
  `graph-built:north.sevenkingdoms.local|baseline` for a SharpHound collection, instead of the template
  `graph-built:{target}`. When the target can't be worked out yet, the card shows only the expected evidence.
- A SharpHound collection scoped to another domain with `-d <domain>` or `--domain=<domain>` is now recognised
  as that domain's collection, as `--Domain <domain>` already was. Before, Sage treated it as a repeat of the
  callback's own baseline collection, so the approval card named the wrong target and Sage could skip the
  collection as already done.
- The BloodHound setup steps in the docs, the BloodHound MCP guidance, and the message Sage shows when
  BloodHound isn't connected now give BloodHound CE's current path for creating an API token: a dedicated
  Power User account, then *Administration → Manage Users → Generate / Revoke API Tokens*. The old
  *Administration → API Tokens* menu no longer exists.

### Security

- OpenTelemetry moves from 1.44.0 to 1.45.1 (instrumentation and semantic conventions to 0.66b1), clearing the
  advisory against `opentelemetry-api` 1.44.0 listed under 0.2.0's known issues. No other dependency changed.

## [0.2.0] - 2026-10-23

Sage 0.2.0 makes supervised mode objective-led, makes request endings honest, and ships Sage as a published,
versioned container image.

### Added

- **Objective-led supervised mode.** A supervised channel can pursue a stated objective across turns. Sage still
  proposes one guarded action at a time, and each runs only after you approve its card. Objectives and evidence
  survive approvals, rejections, and specialist handbacks. When BloodHound has data and the request is about an
  attack path, Sage queries the graph for a path to the objective before tasking.
- **Container images on GitHub Container Registry** for `linux/amd64` and `linux/arm64`:
  `ghcr.io/mythicagents/sage:0.2.0` (also `:0.2`), `:latest` for the newest commit on main, and `:sha-<commit>`
  for an exact build. Installing with `mythic-cli install github` works as before.
- **Version reporting.** The startup log reports Sage's version, the image's build commit, and a fingerprint of
  the running source, so you can tell exactly which code a deployment runs. A plain local `mythic-cli` build
  reports `unknown` for the version and commit.
- **`/tools`** lists Sage's tools and their access requirements without a model call, and **`/report`** produces
  an operation-wide identity report.
- **Provider settings.** For the `openai` provider, `SAGE_REASONING_EFFORT` sets the model's reasoning effort
  (unset uses the provider default), and `SAGE_PROVIDER_RETRY_POLICY=transient-v1` retries confirmed temporary
  429/503 provider responses. Both are optional.
- **Database location.** `SAGE_DATABASE_PATH` and `PHOENIX_WORKING_DIR` move the Sage and Phoenix databases off
  the bind-mounted service directory.

### Changed

- Mythic now shows Sage's own release version for the chat container, rather than the Mythic framework version.
- Commands for registered executables are built from each tool's contract, which repairs legacy verbs and option
  syntax before the approval card is shown. Contract identity uses Mythic's own file hash metadata, so a rebuilt
  binary with the same filename is still recognised.
- Approval cards carry decision context and list all three operator actions. Guard errors name their remedy, and
  the engagement ledger is surfaced at the end of each turn.
- All Sage tables share one database, `sage.db`.
- Dependency pins were refreshed to clear most of the fixable known vulnerabilities, including an updated
  `certifi` CA bundle.

### Fixed

- A request that Sage stops on its own (a routing loop, no progress, or a blocker) no longer reports
  "Completed." Every turn now ends as completed, blocked, or checkpoint.
- When a request ends inside a specialist agent, the final message and reason now reach the main Sage window.
- Repeated failures of the same command on the same callback now stop the request at a per-request cap, even
  when credentials or arguments rotate, and repeated failed tasking is detected across requests.
- Startup fails with an error naming the path when SQLite cannot open or lock `sage.db`, instead of failing
  later.

### Known issues

- **macOS Docker Desktop and OrbStack.** SQLite cannot lock files on the bind-mounted service directory there.
  Set `PHOENIX_WORKING_DIR` and `SAGE_DATABASE_PATH` as described in the README section "SQLite on macOS Docker
  Desktop / OrbStack". With that setting, the databases do not survive a container rebuild or reinstall.
- **Dependency vulnerabilities.** Some reported vulnerabilities remain because `mythic-container` and
  `arize-phoenix` pin the affected packages. They clear when those projects update their pins. An advisory against
  `opentelemetry-api` 1.44.0 is fixed in 1.45.1; that update moves the whole OpenTelemetry package family and is
  planned for a follow-up release.
- **Root-owned runtime state.** Sage writes `.sage_engagement*` state into the installed service directory as
  root, which can block a later reinstall by a non-root user. Archive or remove that state with `sudo` before
  reinstalling.

### Validation

The maintained offline suite (9,023 tests), the repository-boundary guards, and the repo-hygiene checks pass on
this release, and the end-of-request fixes are covered by scripted tests of the assembled chat path. The
clean-clone Docker qualification and live-range runs recorded for 0.1.0-beta were **not** repeated, so the
0.1.0-beta support matrix is the most recent live-validated configuration, not a validation of this release.
Auto mode is exercised only against GOAD. See [README Limitations](README.md#limitations) and
[Security and Data Handling](docs/SECURITY_AND_DATA_HANDLING.md) for the complete boundaries.

## [0.1.0-beta] - 2026-08-26

First beta: a bounded assisted-mode capability with a background findings watcher and the locked Sage Watcher
profile. See the [0.1.0-beta release notes](docs/releases/v0.1.0-beta.md) for the validated support matrix and
limits.

[Unreleased]: https://github.com/MythicAgents/sage/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/MythicAgents/sage/compare/v0.1.0-beta...v0.2.0
[0.1.0-beta]: https://github.com/MythicAgents/sage/releases/tag/v0.1.0-beta
