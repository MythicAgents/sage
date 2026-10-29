# Changelog

All notable changes to Sage are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Sage uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, behaviour and configuration may change
between minor releases.

## [Unreleased]

### Added

- `list_staged_tools`, a read-only tool for the operator agent, and `/tools staged` in chat list the binaries in
  Sage's `tools/` drop zone. Before, Sage found a staged binary only when a TTP named its exact filename. Each
  file shows its sha256, its type and architecture read from the file header, the TTPs that document it, and
  whether Mythic already holds the same bytes, so Sage can use that copy without asking to upload it again.
  Renamed and per-architecture builds (`new_sharphound_x64.exe`, `SharpHound.o`) match their tool. A binary no
  TTP documents carries a hint to ask the operator what it is for, and a tool that is staged nowhere returns a
  message asking the operator to add it. `ensure_tool_uploaded` names staged variants when the exact filename
  is missing.

### Changed

- `mythic-cli install github` now pulls the published release image from GitHub Container Registry instead of
  building Sage locally. Set `sage_use_build_context` to `true` in Mythic's configuration to keep building from
  source.

### Fixed

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
