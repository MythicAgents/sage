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
| **sage-cyber-runner** | Goal-aware GPT-5.6 → `gpt-5.5-cyber-preview` routing fallback with contract validation, control-plane network allowlists, and live-run binding. | `scripts/run_cyber.py` |
| **sage-phase17-structural-readiness** | Exact Phase 17 structural Ludus evidence capture, competing-range poweroff, burned-range rebuild/provisioning, and snapshot guards with Phase 18 closed. | `scripts/phase17_ludus.py` |
| **sage-forge-ops** | Sage-specific Forge/Codex helper automation. Sage offensive-security code must use the cyber-capable model config in `CLAUDE.md`; do not change global Forge defaults from here. | `scripts/sage_forge.sh` |

> Keep this index in sync when adding/removing a skill: a skill is a directory under `skills/` with a `SKILL.md`
> (frontmatter `name` + `description`) and a `scripts/` directory. Update the row here when you add one.
