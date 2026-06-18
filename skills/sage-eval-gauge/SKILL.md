---
name: sage-eval-gauge
description: Repo-local Sage eval-gauge / hill-climbing Phase-0 toolkit. Use when an operator, Claude Code, or Codex needs to measure Sage capability with a GROUND-TRUTHED gauge (not substring eval scores), run the Gate Experiment (does the eval track reality?), compare bare-model vs harness, get the noise floor / min-detectable-effect, or score a run from verified range state. The trustworthy "is config A better than B?" instrument that everything self-improvement depends on.
---

# Sage Eval Gauge (hill-climbing Phase 0)

**Package:** `Payload_Type/sage/ai/hillclimb/` — 11 modules + a live driver, ~45 hermetic tests (`tests/test_hillclimb_*.py`).
**Why (canonical):** `Plans/SAGE_HILL_CLIMBING_DESIGN.md` + `SPEC.md`. **Build spec/ISA:** `Plans/SAGE_EVAL_GAUGE_PHASE0_ISA.md`.

## What it is
A measurement instrument: VERIFIED milestones (ground truth) → a vector `ScoreCard` (C2) carrying a Goodhart gap + a `verifier_hash`; a noise floor (C3, `min_detectable_effect`); the **Gate Experiment** (Spearman ρ of eval-vs-ground-truth + the high-eval/low-truth count, with a PASS/FAIL verdict); ledger-independent **probes** (so a non-Sage agent is scoreable); a **bare-model runner**; and a **live driver**.

## Components
`range_state` (C1 ledger ground truth) · `process_state` (C1b tradecraft + unclassified_rate) · `fitness` (C2 vector) · `reliability` (C3 noise floor) · `scenarios` · `gate_experiment` (+ `__main__` CLI) · `probes` (ledger-independent) · `bare_runner` · `live_runner` (harness adapter) · `live_seams` (lab adapters) · `run_gauge_live` (driver).

## Run
- Offline tests: `../../.venv/bin/python -m pytest tests/test_hillclimb_*.py -q`
- Gate-experiment dry-run (synthetic, safe): `../../.venv/bin/python ai/hillclimb/__main__.py gate-experiment --dry-run`
- Live driver dry-run (safe; resolves Sage model, Apollo catalog, BloodHound): `../../.venv/bin/python ai/hillclimb/run_gauge_live.py`
- **Live bare-vs-harness (OFFENSIVE — needs a freshly reset lab):** `../../.venv/bin/python ai/hillclimb/run_gauge_live.py --go`

## Read-only lab helpers (validated)
- BloodHound domains: `uv --directory /home/john/dev/bloodhound_mcp run python skills/sage-goad-reset/scripts/bh_reset.py status`
- BloodHound cypher: `uv --directory /home/john/dev/bloodhound_mcp run python skills/sage-eval-gauge/scripts/bh_cypher.py '<cypher>'`
- Mythic tasking: `skills/sage-live-runner/scripts/sage_task.py task-callback <id> <cmd>`

## Gotchas
- **No `--model`:** the bare model reads Sage's own model from `skills/sage-callback-bootstrap/.env` (provider=OpenAI, gpt-5.5-cyber-preview) for a fair same-model comparison.
- **Engagement id** is pinned FRESH per run (`SAGE_ENGAGEMENT_ID`), bypassing Sage's per-reset UUID, so each run's ledger is clean (no cross-reset staleness).
- **No Sage restart** needed for gauge changes — the package is additive and read-only to Sage; the running Sage process never imports it.
- **Validated live:** Mythic tasking (whoami→cb4), BloodHound REST + cypher (read paths). **Pending first `--go` run:** the LLM tool-calling round-trip (model_fn↔tool_executor) and deeper milestone scoring on a populated graph.
- `krbtgt`/creds milestones are Mythic-loot, not graph — a separate probe source still to wire.
