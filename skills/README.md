# Sage Repo-Local Skills — Index

**Tool-agnostic.** These skills are self-contained operational workflows usable by **any** coding assistant
(Claude Code, Codex, Grok, …) or a human operator — they are plain directories of scripts + a `SKILL.md`, not
tied to any one tool's skill-loading mechanism. This `skills/` directory is the **source of truth** for Sage
operational tooling (per `CLAUDE.md` / `AGENTS.md`); do **not** put helper scripts in `Plans/`.

**How to use:** find the skill below, then read its `skills/<name>/SKILL.md` for full usage and run its
`scripts/`. Each `SKILL.md` frontmatter has a `description:` that states exactly when to use it.

| Skill | Purpose | Primary entry |
|-------|---------|---------------|
| **sage-goad-reset** | Full GOAD lab reset + readiness: archive Sage/Phoenix runtime DBs, reset Docker-Mythic, reset/verify Ludus GOAD, wipe/check BloodHound, restart local Sage, preflight before a solve. | `scripts/sage_restart.sh`, `scripts/archive_runtime_dbs.py`, `scripts/ludus.py`, `scripts/bh_reset.py` |
| **sage-callback-bootstrap** | Post-Mythic-reset Sage chat/foothold bootstrap: verify native chat, create Apollo or import retained footholds, run readiness, rediscover Apollo. | `scripts/bootstrap_payloads.py` |
| **sage-foothold-launch** | Rebuild a live Apollo foothold as `NORTH\samwell.tarly` unaided — pure WinRM scheduled-task (batch logon), NO operator/RDP/X display. Full reset→foothold→verify or launch-only. Supersedes `launch_apollo_foothold.sh` for headless/cron use. | `scripts/foothold_launch.py` |
| **sage-mythic-payload-deploy** | Download a built Mythic payload and stage/launch it on a Ludus Windows host over WinRM, including the clean-baseline interactive Samwell Apollo path. | `scripts/deploy_payload_via_ludus.py` |
| **sage-live-runner** | Native Mythic v4 Sage chat runs and monitoring with a fresh locked channel per one-shot solve. | `scripts/native_chat.py` |
| **sage-conversation-control** | Repeated configured-model perturbation trials for the frozen native-chat conversation-control constitution, with pass^k and event/terminal scoring. | `scripts/run_model_trials.py` |
| **sage-eval-gauge** | Reset-per-seed, ground-truthed native-chat capability gauge and five-seed one-shot proof. | `scripts/orchestrate.py` (+ `Payload_Type/sage/ai/hillclimb/`) |
| **sage-focused-capability-tests** | Narrow live/semi-live smoke tests for one capability/adapter (ADCS / DCSync / GPO / LAPS / Kerberos) before burning a full autonomous run. | `scripts/build_capability_smoke.py`, `scripts/run_focused_*.py` |
| **sage-trace-analysis** | Read-only forensics: mine historical failures, inspect Phoenix spans, backfill task IDs, audit solve steps, diagnose ingestion/file/output behavior. | `scripts/mine_failures.py`, `scripts/step_audit.py` |
| **sage-trajectory-learning** | Convert retained runs into normalized `state → action → observation → verifier → repair` records; label repeated blocker classes; replay repair-policy decisions. | see `SKILL.md` (module CLI) |
| **sage-artifact-retention** | Keep high-value Sage contracts, handoffs, reviews, transcripts, and evidence in private project-local history; warn when durable artifacts remain only in `/tmp`. | `scripts/artifact_retention.py`, `scripts/retention_guard.py` |
| **sage-architecture-governor** | Falsifiable architecture gate + scoped edit token for high-risk Sage harness/prompt/planner/adapter changes — guards against prompt bloat, symbolic-planner creep, and GOAD coupling. | `scripts/open_gate.py`, `scripts/check_arch_budget.py` |
| **sage-forge-ops** | Sage-specific Forge/Codex helper automation. Sage offensive-security code must use the cyber-capable model config in `CLAUDE.md`; do not change global Forge defaults from here. | `scripts/sage_forge.sh` |
| **sage-canary-attestation** | Independently attest a bounded native Mythic canary by reconstructing the run from Mythic's own records with a read-only Spectator credential, then diffing them against the frozen conversation-case expected trace — a verdict that does not trust the driver's own manifest. | `scripts/attest_canary.py`, `scripts/egress_probe.py` |
| **phoenix-traces** | Read-only analysis of Sage's Phoenix/OpenInference trace store: latest run, per-call token usage, errors, tool-call frequency, and before/after trace comparison. Opens the DB read-only, so it is safe during a live run. | `analyze.ts` (no `scripts/` dir — see note below) |

> Keep this index in sync when adding/removing a skill: a skill is a directory under `skills/` with a `SKILL.md`
> (frontmatter `name` + `description`) and a `scripts/` directory. Update the row here when you add one.
>
> Two rows above were added on 2026-08-11 after a count found 13 rows against 14 directories.
> `sage-canary-attestation` had simply never been indexed. `phoenix-traces` is the newer arrival: it lived at
> `.claude/skills/phoenix-traces/` — inside a gitignored directory — so it was untracked, absent from every
> clone, and reachable by Claude Code alone. Moving it here made it one source both assistants link to, which is
> the whole point of this directory. It is also the one skill whose entry point is a top-level `analyze.ts`
> rather than a `scripts/` directory; that is a deviation from the convention above, not a licence to add more.
>
> If you move a skill in or out of here, **its depth changes and relative paths move with it.** `analyze.ts`
> resolved its default database with `../../../`, correct at three levels deep and wrong at two. Run the skill
> after any such move; a file that still parses is not a file that still works.
