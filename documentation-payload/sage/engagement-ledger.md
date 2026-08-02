+++
title = "The Engagement Ledger"
chapter = false
weight = 70
+++

An LLM does not remember your engagement between turns. Sage keeps that memory outside the model, in a durable
**engagement ledger**: a running record of what has actually been achieved, so a multi-day assessment survives
context resets, restarts, and even a change of model. You read and edit it in chat with `/state`.

## What it is

One JSON file per Mythic operation — a table of hops toward the objective, each with a status.

## Why it is proof-gated

A hop flips to *achieved* only when a verifier confirms the effect from real evidence. Each achieved hop carries
a **proof envelope** recording the Mythic callback, task, and transaction ids, the terminal task status, and the
verifier's input and result hashes (sha256). A row without a valid envelope is quarantined as `legacy_unverified`
and can never count as proof — the model cannot write itself a success it did not earn.

## Where it lives

`.sage_engagement/state_<operation>.json`, next to the running Sage process (`SAGE_ENGAGEMENT_STATE_DIR`
overrides the directory). Keyed per Mythic operation, it is the portable, exportable record of the assessment —
not a checkpoint or a trace, both of which live elsewhere and prove nothing on their own.

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
