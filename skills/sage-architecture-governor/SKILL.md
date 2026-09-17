---
name: sage-architecture-governor
description: Use for Sage prompt, topology, tool, planner, runtime authority, and other consequential architecture changes; keep ordinary development measurement plumbing within standing task authority.
---

# Sage Architecture Governor

Use this workflow for actual architecture or authority changes. Internal evaluator plumbing does not become an
architecture change merely because its path is in the historical high-risk list.

## Choose the decision

- `runtime_bugfix`: repair a concrete production defect with focused assembled-path probes.
- `development_measurement`: an internal comparison or evaluator repair, with model/config/source identity,
  structured logs, a prospective protocol, resource limits, and independent outcome proof.
- `sealed_evaluation`: a declared final holdout, promotion/release, externally relied-on claim, or named integrity
  threat. Specify which relying decision requires stronger provenance before using the existing lease tools.

For the first two lanes, use a concise working contract: hypothesis, atomic criteria, affected path/files,
falsifying and valid-near-match probes, non-goals, complexity constraints, and a stop-loss.
Update it when production facts invalidate an assumption; version changed protocols without rewriting results.
No mandatory fresh candidate reviewer, per-repair approval, exact-hash binding, immutable bundle, or lease applies.

## Architecture approval and prompt protection

Actual prompt, topology, tool-surface, planner, or runtime authority changes require user authorization covering
that change. Existing explicit task approval can supply it; do not ask again for each in-scope repair.
When opening a prompt-edit token, briefly identify the hypothesis, scope, verification, generality risk, and
complexity tradeoff. Use the narrowest approved scope:

```bash
python3 skills/sage-architecture-governor/scripts/open_gate.py open --reason "approved change" --approved-by "user" --approval-source "applicable task approval" --minutes 90 --files 'Payload_Type/sage/prompts/**'
```

Inspect with `open_gate.py status` and close with `open_gate.py close`. A token does not grant live authority.

The project PreToolUse hook hard-denies prompt edits without a scoped token, advises on other high-risk paths,
and protects an active review lease when one was deliberately opened. Its source remains the enforcement authority.
Ordinary development measurement needs no token or lease. Do not create one merely to satisfy a historical recipe.

## Complexity and verification

- Compare a thinner verifier, retrieval, or existing data path before expanding prompts, tools, or symbolic gates.
- No new GOAD-specific strategy in product code. Declare any intended benchmark-only scope.
- Tie every changed behavior to a probe. Exercise the actual assembled producer/consumer boundary.
- After repeated tactical repairs, diagnose the mechanism and reconsider scope before adding more code.
- Run relevant tests during repairs and the full offline suite at the integration boundary.
- Use the existing budget checker for actual high-risk changes; it is advisory at session Stop:

```bash
python3 skills/sage-architecture-governor/scripts/check_arch_budget.py --changed
```

A targeted reviewer reports reproducible blocking counterexamples to the agreed claim, separately from hardening,
pre-existing, unreachable, and out-of-scope findings. Review is not a universal development pre-run prerequisite.
Source identity detects drift; it does not independently prove correctness or authenticate the producer.

## Existing tools

- `scripts/pre_tool_use_arch_gate.py`: prompt-edit protection, high-risk advisories, active lease protection.
- `scripts/open_gate.py`: scoped token lifecycle.
- `scripts/check_arch_budget.py`: architecture budget inspection.
- `scripts/review_lease.py`: optional stronger provenance for a separately justified sealed decision.

When changing these executables, run their local `--self-test` paths and the affected tests under
`skills/sage-architecture-governor/tests`. Do not run live activity to verify governance.
