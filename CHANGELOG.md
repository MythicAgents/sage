# Changelog

All notable changes to Sage are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Sage uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, behaviour and configuration may change
between minor releases.

## [Unreleased]

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
