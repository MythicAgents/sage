+++
title = "Model Configuration"
chapter = false
weight = 30
+++

Every native-chat setting resolves through one chain, first non-empty wins: the Mythic **channel
configuration**, then your Mythic **user secrets**, then the Sage **process environment**, then a safe default.
The main model settings are:

| Setting | Purpose |
|---|---|
| `provider` | `openai` (default), `bedrock`, `anthropic`, or `ollama` |
| `model` | Provider model identifier |
| `API_ENDPOINT` | Optional endpoint override — point `openai` at any OpenAI-compatible server (see below) |
| `API_KEY` | Provider API key when required |
| `mode` | `conversation` (default), `supervised`, or `auto` |
| `autonomous_solve` | Force the autonomous execution kernel for the channel (equivalent to `mode: auto`) |
| `policy_mode` | `hybrid` (default), `symbolic`, or `llm` capability selection |
| `max_steps` | Model-step ceiling; unset defaults to `200`, `0` means unlimited |

## Providers

The four above are the tested set. Sage builds the model through LangChain's `init_chat_model`, so other
providers it supports may work but are unverified. Bedrock is the only provider with dedicated AWS credential and
region handling. Never infer the effective backend from the model name alone; record the provider and route for
live runs.

## OpenAI-compatible endpoints

Selecting `provider: openai` does not tie you to OpenAI. It speaks the
[OpenAI API](https://developers.openai.com/api/reference/overview) — the de-facto standard most inference servers
and gateways implement — so you can set `API_ENDPOINT` to any server that exposes it: a self-hosted model, an
internal gateway, or a proxy. For example, run a [LiteLLM](https://docs.litellm.ai/docs/simple_proxy) proxy and
point `API_ENDPOINT` at it to reach 100+ providers behind a single OpenAI-compatible endpoint.

## Policy

`policy_mode` controls how the next capability is chosen:

- **`hybrid`** (default) — deterministic code constrains the admissible actions and the model picks among them.
  The intended production mode.
- **`symbolic`** — a deterministic, model-free baseline. Reproducible, good for debugging without LLM variance.
- **`llm`** — the model chooses from the full catalog. Most latitude, weakest guardrails; mainly for research and
  eval comparisons.

Choose `hybrid` unless you have a reason not to.

## Step budget

`max_steps` bounds the number of model steps in a run. Left unset it defaults to 200; set it to `0` for
unlimited. A small ceiling caps cost and stops runaway loops on scoped work. For a full autonomous objective,
raise it or set `0` — but the autonomous controller also enforces hard stop-losses independent of step count
(60 cycles, a 45-minute wall clock, a 3M-token budget, and a no-progress loop-breaker), tunable through
`SAGE_CONTROLLER_MAX_CYCLES`, `SAGE_CONTROLLER_WALL_S`, and `SAGE_CONTROLLER_TOKEN_BUDGET`.
