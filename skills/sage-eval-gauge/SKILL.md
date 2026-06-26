---
name: sage-eval-gauge
description: Repo-local Sage eval-gauge / hill-climbing Phase-0 toolkit. Use when an operator, Claude Code, or Codex needs to measure Sage capability with a GROUND-TRUTHED gauge (not substring eval scores), run the Gate Experiment (does the eval track reality?), compare bare-model vs harness, get the noise floor / min-detectable-effect, or score a run from verified range state. The trustworthy "is config A better than B?" instrument that everything self-improvement depends on.
---

# Sage Eval Gauge (hill-climbing Phase 0)

**Package:** `Payload_Type/sage/ai/hillclimb/` — 13 modules + a live driver. Hermetic tests across `tests/test_hillclimb_*.py`, `test_gate_live.py`, `test_hermetic.py`, `test_clean_stop_signal.py`, `test_probe_completeness.py`, `test_completion_recognition_phase1.py`, `test_unproductive_loop_guard.py` (full offline suite green).
**Why (canonical):** `Plans/SAGE_HILL_CLIMBING_DESIGN.md` + `SPEC.md`. **Build spec/ISA:** `Plans/SAGE_EVAL_GAUGE_PHASE0_ISA.md`.

## What it is
A measurement instrument: VERIFIED milestones (ground truth) → a vector `ScoreCard` (C2) carrying a Goodhart gap + a `verifier_hash`; a noise floor (C3, `min_detectable_effect`); the **Gate Experiment** (Spearman ρ of eval-vs-ground-truth + the high-eval/low-truth count, with a PASS/FAIL verdict); ledger-independent **probes** (so a non-Sage agent is scoreable); a **bare-model runner**; and a **live driver**.

## Components
`range_state` (C1 ledger ground truth) · `process_state` (C1b tradecraft + unclassified_rate) · `fitness` (C2 vector; carries `objective_clean_stop` + `wall_seconds`) · `reliability` (C3 noise floor) · `scenarios` · `gate_experiment` (Spearman ρ + danger-quadrant + verdict, incl. an INVALID guard on empty ground truth) · `gate_live` (headless **live** Gate Experiment orchestration) · `hermetic` (C5 hermetic inner-loop evaluator — offline re-score + determinism + repair-policy replay) · `probes` (ledger-independent) · `bare_runner` · `live_runner` (harness adapter, reset-per-config) · `live_seams` (lab adapters) · `run_gauge_live` (driver) · `__main__` CLI.

## Run
- Offline tests: `../../.venv/bin/python -m pytest tests/ -q`
- Gate-experiment dry-run (synthetic, safe): `../../.venv/bin/python -m ai.hillclimb gate-experiment --dry-run`
- Live driver dry-run (safe; resolves Sage model, Apollo catalog, BloodHound): `../../.venv/bin/python ai/hillclimb/run_gauge_live.py`
- **Orchestrated bare-vs-harness (OFFENSIVE — resets the lab per run; run DETACHED, ~1.5–2.5h/seed):**
  `python skills/sage-eval-gauge/scripts/orchestrate.py --scenario child-da --side harness --seeds N --solve-timeout 5400 --go`
  (omit `--go` for a dry-run plan; the step cap auto-scales to `--solve-timeout`).
  Add `--controller` to restart Sage with `SAGE_AUTONOMOUS_CONTROLLER=1` so the harness solve runs the
  deterministic autonomous controller (`ai/langgraph/autonomous_controller.py`) instead of the Supervisor/worker
  astream path — a clean A/B against the default. The harness `query` already sends `mode=auto`+`autonomous_solve`.
- **Live Gate Experiment (validate the gauge):**
  `../../.venv/bin/python -m ai.hillclimb gate-experiment --live --configs <json {name:{env:{...}}}> --scenario cross-forest-objective --seeds 5`
  Resets the lab per config (restarting Sage with `SAGE_ENGAGEMENT_ID=<token>` + the config env) so each config is measured from a clean range under its own ledger. Needs ≥3 (ideally 5–8) known-different-quality configs.

## Read-only lab helpers (validated)
- BloodHound domains: `uv --directory /home/john/dev/bloodhound_mcp run python skills/sage-goad-reset/scripts/bh_reset.py status`
- BloodHound cypher: `uv --directory /home/john/dev/bloodhound_mcp run python skills/sage-eval-gauge/scripts/bh_cypher.py '<cypher>'`
- Mythic tasking: `skills/sage-live-runner/scripts/sage_task.py task-callback <id> <cmd>`

## Gotchas
- **No `--model`:** the bare model reads Sage's own model from `skills/sage-callback-bootstrap/.env` (provider=OpenAI, gpt-5.5-cyber-preview) for a fair same-model comparison.
- **Token/config seam (live gate):** the harness is only a Mythic CLIENT — env it sets never reaches the running Sage. So the gate's reset restarts Sage with `SAGE_ENGAGEMENT_ID=<run token>` (+ config env) via `full_reset_and_ready(restart_env=...)` → `sage_restart.sh KEY=VAL` overrides. Without that, ground truth reads an empty ledger and `gate_experiment` returns **INVALID** (fail-loud, by design). Which config keys change behavior depends on what Sage reads at startup; `SAGE_ENGAGEMENT_ID` always takes effect.
- **`objective_clean_stop`** (the Phase-1 efficiency signal) is gated on BOTH ground-truth terminal milestone AND `status="objective-recognized"` (emitted by `query.py` only when Sage streamed the completion report) — Goodhart-safe; a reached-but-not-recognized finish does NOT count.
- **child-da is saturated** (cap ceils at 0.444) — a regression guard, not an improvement signal; measure capability gains on `cross-forest-objective`. `GRAPH_COLLECTED` is not scored on child-da (off-path → Goodhart); `DA_CHILD` credits DA-equivalent control via the DC's Builtin\Administrators, not just Domain Admins.
- **No Sage restart** needed for *gauge package* changes (additive, read-only to Sage). The *live gate's reset* DOES restart Sage (to apply the token/config).
- **Hermetic inner loop (`hermetic.py`)** re-scores RECORDED runs offline (no lab); scoring a NEW candidate's capability hermetically (mock-Mythic re-execution) is the Phase-3 frontier (`mock_mythic_candidate_eval` is a documented `NotImplementedError` stub).
- `krbtgt`/creds milestones are Mythic-loot, not graph — a separate probe source still to wire.
