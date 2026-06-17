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
Ideal State Criteria:
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

## Verification

Validate the skill and scripts after changes:

```bash
python3 /home/john/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/sage-architecture-governor
python3 skills/sage-architecture-governor/scripts/pre_tool_use_arch_gate.py --self-test
python3 skills/sage-architecture-governor/scripts/open_gate.py --self-test
python3 skills/sage-architecture-governor/scripts/check_arch_budget.py --self-test
```

Do not run live GOAD, Mythic, BloodHound, or Sage tasks just to validate this governance layer.
