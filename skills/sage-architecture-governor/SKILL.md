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

A pre-implementation `DESIGN_ACCEPT` review is mandatory only for `sealed_evaluation`. Root freezes the prospective
gate contract and owns its operator-approval binding; a fresh `sage_eval_reviewer` returns the read-only design
result. There is no source candidate, lease, or source/test disposition during this preflight.

The source-candidate breaker uses this fixed transition:

`DESIGN_ACCEPT` → candidate 1 → rejection → RCA/simplification/full-class probes → fresh `RECOVERY_ACCEPT` →
candidate 2 → rejection → exhausted epoch.

Root owns the gate, recovery inputs, candidate bytes, and final evidence admission. The reviewer owns the design
or recovery disposition and attests the route and probes it actually reproduced. There is at most one initial
candidate and one rejected-candidate successor per design epoch. A higher epoch requires a fresh design result
that binds the exhausted predecessor and the same operator-approved superseding scope.

The sealed guard assumes a trusted repository operator. Manifest and hash checks detect accidental drift and bind
exact bytes to a review; they do not cryptographically authenticate reviewer identity or defend against a malicious
repository administrator. If authentication or hostile substitution is actually in scope, use a separately designed
signing and key-trust mechanism rather than describing local hashes as signatures. Freeze validates recorded probe
evidence and never executes probe commands.

## Candidate Freeze And Review

Use the ordinary git diff, focused probes, and one-writer discipline for `runtime_bugfix` and
`development_measurement`. Do not invoke `review_lease.py` merely because a change touches an eval path.

For `sealed_evaluation`, after implementation and focused verification, stop all writers and stage only the exact
candidate paths. A candidate path may not have unstaged bytes layered over its staged form. The manifest-bound gate
contract's `governed_paths` must exactly match `--paths`, and the freeze requires the fresh design-review result plus
the recovery result after one rejection:

```bash
python3 skills/sage-architecture-governor/scripts/review_lease.py freeze --paths Payload_Type/sage/ai/langgraph/model.py --protected Payload_Type/sage/ai/langgraph/turn_authority.py --review-stage source_candidate --review-domain conversation_behavior --independence-class internal_subagent --mechanism-id supervised-turn-authority --review-round 1 --governing-gate "approved architecture gate" --gate-contract "$SAGE_GATE_CONTRACT" --design-receipt "$SAGE_DESIGN_RECEIPT"
```

For sealed round 2, append `--recovery-receipt "$SAGE_RECOVERY_RECEIPT"`. Caller-supplied domain,
mechanism, gate label, and round do not override the gate-derived values. The command records the gate and receipt
hashes, stable process key, epoch, HEAD, complete git-index fingerprint, scoped status, staged candidate blob
hashes, protected-path hashes, candidate ID, and lease ID. It does not modify the git index. The PreToolUse hook
blocks writes to candidate and protected paths while the lease is active.

The reviewer must verify the lease at review start, after its declared commands, and immediately before its
disposition:

```bash
python3 skills/sage-architecture-governor/scripts/review_lease.py verify
```

Any HEAD or index drift invalidates the candidate. Worktree and scoped-status drift is checked for candidate and
protected paths; unrelated unstaged worktree changes do not invalidate it. Drift produces
`INVALIDATED_CANDIDATE_DRIFT`. Close the exact lease after review and preserve its archived receipt:

```bash
python3 skills/sage-architecture-governor/scripts/review_lease.py close --lease-id <lease-id> --disposition accepted
```

Active review leases remain under `/tmp/sage_arch_review`; successful close writes the immutable closed receipt
under `.sage_history/<year>/<month>/governance/architecture-reviews/` before removing the active lease. If the
durable write fails, close fails and leaves the active lease in place.

Do not edit a rejected candidate while its review lease is active. Close it as `rejected`, open the next
prospectively approved edit tranche, and freeze a new candidate.

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
