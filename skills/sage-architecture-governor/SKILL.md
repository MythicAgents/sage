---
name: sage-architecture-governor
description: Sage architecture governance workflow and deterministic edit gate. Use when Codex is asked to change Sage autonomous execution, prompts, LangGraph harness code, tool lists, engagement state/gating, reconcilers, capability planning/adapters, trajectory learning, eval harnesses, live-run drivers, or any multi-step Sage architecture/refactor work that could add prompt bloat, symbolic planner logic, GOAD coupling, or model-hostile harness complexity.
---

# Sage Architecture Governor

## Overview

Use this skill before high-risk Sage architecture edits. It forces a falsifiable architecture gate, opens a
short-lived scoped edit token only after explicit user approval, and runs budget checks that protect Sage from
prompt bloat, GOAD overfitting, and unbounded harness complexity.

## Gate Workflow

Before editing high-risk files, produce this gate brief for the user:

```markdown
Architecture Gate

Hypothesis:
Review lane: runtime_bugfix | development_measurement | sealed_evaluation
Mechanism ID:
Ideal State Criteria:
Production call path:
Adversarial and valid-near-match probes:
Blocker policy:
Non-goals:
Complexity budget:
Falsifier:
Stop-loss:
Verification:
Risk to generality:
Files likely touched:
What I will not add:
```

Wait for explicit user approval before opening a token. Approval must name or clearly accept the gate, not just
ask for a plan. Once approved, open the narrowest token possible:

```bash
python3 skills/sage-architecture-governor/scripts/open_gate.py open \
  --reason "short reason" \
  --approved-by "user" \
  --approval-source "exact approval phrase or short summary" \
  --minutes 90 \
  --files 'Payload_Type/sage/prompts/**' \
  --files 'Payload_Type/sage/ai/langgraph/model.py'
```

Use `open_gate.py status` to inspect the current token and `open_gate.py close` after finishing the high-risk edit.

## Review Lanes And Prospective Contract

Choose the lane before the first edit:

- `runtime_bugfix`: a bounded product defect with a concrete production call path. Use atomic criteria, exact
  adversarial/near-match probes, focused tests, and the maintained product suite when required. Do not add sealed
  manifests, promotion gates, or evaluation lifecycle artifacts unless the approved contract names a separate
  high-impact integrity threat.
- `development_measurement`: an internal baseline, exploratory eval, evaluator-debugging run, or hill-climb signal
  that cannot authorize promotion, release, or an external claim. Record exact model/system/configuration and code
  version, structured logs, budgets, and independent result proof. Snapshot each row's protocol before execution,
  but do not require signed approvals, immutable source candidates, per-artifact hash chains, or review leases.
- `sealed_evaluation`: a final holdout, promotion, release, externally relied-on claim, or a prospectively named
  adversarial-integrity decision where exact candidate identity and independent provenance are material. Use the
  full candidate, provenance, lifecycle, and independent-review discipline in `AGENTS.md`.

Evaluation, countability, and evidence admission do not by themselves select `sealed_evaluation`. The gate brief
must give a one-sentence lane rationale. Any proposal to add signing, per-artifact hashes, immutable bundles, or a
review lease must name the protected decision, the actor or failure that could alter it, and why version control plus
structured logs is insufficient.

The approved gate records a prospective working contract. For `runtime_bugfix` and `development_measurement`, update
it transparently when direct production inspection falsifies an assumption; the changed evaluator or protocol gets a
new version and does not rewrite prior results. For `sealed_evaluation`, freeze the decision protocol and exact
candidate before consequential review. A reviewer may reject only for a reproducible production-reachable
counterexample to an atomic criterion or a concrete safety/authority-boundary violation. Classify all other findings
as hardening, unreachable, pre-existing, or out-of-scope. The implementation owner runs the declared probes before
review; the reviewer completes all of its predeclared probes in one round.

For deadline-bound `runtime_bugfix` work, include a review wall-clock and command budget in the contract. Default to
15 minutes and the focused production-path suite. The reviewer completes those probes and returns; it does not
expand into unrelated history or sealed-lifecycle archaeology. Prefer typed fields, enums, and protocol state for
control authority. After a language-classification rejection, replace prose inference with structured authority
when practical instead of growing a blacklist or regex grammar.

## Candidate Freeze And Review

**Use the ordinary git diff, focused probes, and one-writer discipline for `runtime_bugfix` and
`development_measurement`.** That is nearly all work. Run these:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py
python3 skills/sage-architecture-governor/scripts/check_arch_budget.py
```

### `sealed_evaluation` — archived, not available by default

The sealed-evaluation lifecycle (design-accept preflight, source-candidate breaker, epochs, freeze manifests,
protected-path hashes, review leases) was **archived out of this file on 2026-08-29** to
`.sage_history/2026/08/governance/ARCHIVED_sealed_evaluation_section.md`. `scripts/review_lease.py` is unchanged
and still runnable; only the procedure text left this file.

**It applies only when a named external party actually relies on a Sage claim. If you think you need it, you are
in `development_measurement`.**

*Why it was archived, measured: this file is what an agent reads at the moment it selects a review lane. It
carried three runnable command blocks for the sealed lane and zero for the cheap lanes, so the sealed lane won on
salience — even after `AGENTS.md:108` was added specifically because the model kept selecting it, and kept
selecting it anyway. A prose prohibition does not outcompete a copy-pasteable command. Restoring the text here
re-creates the asymmetry; restore it in a reference the agent opens after the lane is already chosen.*

## Complexity Budget

Default budget for high-risk edits:

- No prompt/tool count increase without an explicit waiver in the gate.
- No new GOAD-specific literals in live code or base prompts unless the gate declares demo-only scope.
- No new dated run-specific comments in live harness code.
- No new symbolic precondition/gating layer unless a thinner verifier, retrieval, or data-backed alternative is rejected with evidence.
- Every Ideal State Criterion must map to one verification probe.
- The third tactical patch to the same subsystem requires RCA before implementation.

Run the budget checker before final response when high-risk files were touched:

```bash
python3 skills/sage-architecture-governor/scripts/check_arch_budget.py --changed
```

## Scripts

- `scripts/pre_tool_use_arch_gate.py`: Codex PreToolUse hook. Blocks high-risk `apply_patch` and obvious shell writes without a valid scoped token.
- `scripts/open_gate.py`: Create, inspect, and close short-lived edit tokens under `/tmp/sage_arch_gate`.
- `scripts/check_arch_budget.py`: Inspect changed high-risk files for budget regressions.

## Hook Behavior

The project hook config in `.codex/config.toml` wires `pre_tool_use_arch_gate.py` into `PreToolUse` for
`apply_patch`/write-like shell activity. The hook is a guardrail: it catches the normal Codex edit path and common
shell write paths, but it does not replace careful review. Treat a passed hook as permission to edit only within
the approved gate scope.

The Stop hook runs `check_arch_budget.py --changed --warn-only`. It reports actionable budget violations without
failing the stop event. Manual and CI invocations remain strict by default and return exit code 1 on violations.

## Verification

Validate the skill and scripts after changes:

```bash
python3 skills/sage-architecture-governor/scripts/pre_tool_use_arch_gate.py --self-test
python3 skills/sage-architecture-governor/scripts/open_gate.py --self-test
python3 skills/sage-architecture-governor/scripts/review_lease.py --self-test
python3 skills/sage-architecture-governor/scripts/check_arch_budget.py --self-test
.venv/bin/python -m pytest -q skills/sage-architecture-governor/tests
```

Do not run live GOAD, Mythic, BloodHound, or Sage tasks just to validate this governance layer.
