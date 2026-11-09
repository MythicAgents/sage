+++
title = "The Engagement State Ledger"
chapter = false
weight = 70
+++

An LLM does not remember your engagement between turns. Sage keeps that memory outside the model, in a durable
**Engagement State Ledger**: a running record of the capabilities and credential material the operation has
gained, so a multi-day assessment survives context resets, restarts, and even a change of model. You read and
correct it in chat with `/state`.

## What it is

One ledger per Mythic operation, shared live by every chat channel of that operation — a table of gains, each
`achieved` or `pending`. A `pending` row is waiting for its effect or its proof and never satisfies a capability's
precondition. Each row keeps a permanent number (the `#` column), assigned once and never reused; retracted and
"no longer holds" rows stay in place, marked, so the numbers have no gaps. The Supervisor and Mythic_Operator get a
fresh view of the ledger, with the same numbers, on every model call in every mode, so you can ask about "hop 4"
directly, even in a new chat.

When a conversation or supervised turn ends, and on `/state reconcile`, Sage reconciles the operation's Mythic
tasks from every callback and every issuer. The ledger records verified gains, not every task: a task whose effect no verifier confirms leaves no achieved row,
even when the task itself succeeded, and failed attempts stay in Mythic's task history.

## Why it is proof-gated

A row counts as *achieved* only when it traces back to real evidence: a verifier's check of Mythic task output or
files, Mythic's credential store, Mythic's callback records, or BloodHound data collected through a payload, or a
rule in Sage's code that derives it from rows that already have that proof. Each achieved row carries a **proof
envelope** recording what it rests on (the Mythic callback, task, transaction, credential, file or BloodHound
ingest it came from, or the rule and premise rows behind a derived row) and the verifier's input and result hashes
(sha256). An achieved write without an admissible envelope is stored as `pending` (`proof_missing`) and never
counts as proof. The model can read the ledger and submit Mythic evidence IDs for verification, but it never writes
rows itself, so it cannot write itself a success it did not earn.

## Where it lives

Sage's application database, `sage.db` (`SAGE_DATABASE_PATH` overrides it), keyed by the Mythic operation and the
reset marker Sage stores in that operation. Every chat channel of the operation reads and writes the same ledger:
each change touches only its own rows and appends to their history, and `/state` corrections reach running sessions
on their next read. Rows keep permanent numbers; retracted and "no longer holds" rows stay visible and marked. An
operator export (`engagement_ledger_migrate.py export`) is the portable record of the assessment; ledgers from
releases that stored `.sage_engagement/state_*.json` files are imported once with the same script, with Sage
stopped. It is not a checkpoint or a trace, both of which live elsewhere and prove nothing on their own.

## Why a ledger at all

An autonomous agent that reasons only over its own transcript re-runs steps it has already completed and attempts
steps whose preconditions it never met: it substitutes recitation for perception. The oracles do not fix this by
themselves — BloodHound shows that an edge is *abusable*, not that you already abused it, and Mythic shows
callbacks, not "I ran step X." The ledger is the one piece of state that lives in neither oracle, and it is what
lets Sage answer "already done?" and "precondition met?" before every hop.

The design draws on prior work in autonomous-pentest state and agent memory:

- **PentestGPT** — Deng et al., 2023 — [arXiv:2308.06782](https://arxiv.org/abs/2308.06782). The Pentesting Task
  Tree: an external structure that encodes the test's ongoing status and steers the next action — the direct
  precedent for an external engagement-state object.
- **Guided Reasoning in LLM-Driven Penetration Testing Using Structured Attack Trees** — Nakano et al., 2025 —
  [arXiv:2509.07939](https://arxiv.org/abs/2509.07939). A deterministic MITRE ATT&CK task tree constrains the
  agent to defined techniques.
- **CoALA: Cognitive Architectures for Language Agents** — Sumers et al., 2023 —
  [arXiv:2309.02427](https://arxiv.org/abs/2309.02427). The working-memory vs long-term-memory split for
  language agents.
- **MemGPT: Towards LLMs as Operating Systems** — Packer et al., 2023 —
  [arXiv:2310.08560](https://arxiv.org/abs/2310.08560). Keep full state external and page only the relevant slice
  into context.
- **Incalmo** — Singer et al., 2025 — [arXiv:2501.16466](https://arxiv.org/abs/2501.16466). Declarative tasks
  plus a service to manage acquired assets; names context bloat as the failure mode.
- **Can LLMs Hack Enterprise Networks?** — Happe & Cito, 2025 —
  [arXiv:2502.04227](https://arxiv.org/abs/2502.04227). Autonomous assumed-breach pentest on GOAD; documents the
  same planner-to-executor state-loss failure the ledger closes.
- **Shell or Nothing (TermiAgent)** — Mai, Hong et al., 2025 —
  [arXiv:2509.09207](https://arxiv.org/abs/2509.09207). Memory-activated agents; structured retention of recon
  facts over narrative memory.
- **PentestAgent** — Shen et al., 2024 — [arXiv:2411.05185](https://arxiv.org/abs/2411.05185). RAG-augmented
  multi-agent pentest.

## Autonomous execution

In an autonomous solve, a deterministic controller honors the model's intent to act but owns which capability
actually runs and how it is built — the mechanics stay below the model, and effects reach the ledger only through
verification. That split of LLM judgment from deterministic execution and verification draws on prior work:

- **LLM+P: Empowering Large Language Models with Optimal Planning Proficiency** — Liu et al., 2023 —
  [arXiv:2304.11477](https://arxiv.org/abs/2304.11477). The LLM translates the problem into PDDL and a classical
  planner solves it deterministically — the solver, not the model, owns correctness.
- **LLMs Can't Plan, But Can Help Planning in LLM-Modulo Frameworks** — Kambhampati et al., 2024 —
  [arXiv:2402.01817](https://arxiv.org/abs/2402.01817). Pair the LLM with sound external verifiers rather than
  trusting it to plan unaided.
- **Agents Thinking Fast and Slow: A Talker-Reasoner Architecture** — Christakopoulou et al., 2024 —
  [arXiv:2410.08328](https://arxiv.org/abs/2410.08328). A fast conversational agent over a slow reasoner that
  owns planning and produces the agent's state.
