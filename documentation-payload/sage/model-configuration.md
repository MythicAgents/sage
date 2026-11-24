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
| `autonomous_solve` | Enable objective-led solving in `supervised` mode; each guarded action still needs approval. `auto` always enables it. Ignored in `conversation`, which stays read-only; Sage says so once in the chat |
| `policy_mode` | `hybrid` (default), `symbolic`, or `llm` capability selection |
| `max_steps` | Model-step ceiling per chat request; unset defaults to `200`, `0` means unlimited |
| `SAGE_OPENAI_API` | **OpenAI API**: blank keeps the adapter's usual route; `chat_completions` or `responses` selects one (OpenAI provider only) |
| `SAGE_OPENAI_ACCESS_PROGRAM` | **OpenAI Daybreak**: `standard` (Off), `daybreak_blue` or `daybreak_red`. Any value, Off included, requires `SAGE_OPENAI_API=responses`; blank sends Off on Responses |
| `SAGE_OPENAI_SAFETY_IDENTIFIER` | **OpenAI Safety Identifier**: optional stable pseudonym for the person using the chat |
| `SAGE_REASONING_EFFORT` | **Reasoning Effort**: blank sends no effort; `none` through `max` is sent as chosen (OpenAI provider only) |

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

## Reasoning effort and tools

**Reasoning Effort** follows the same chain. Blank at every layer sends no effort, so the provider's default for
the model applies; that differs from **None**, which Sage sends as an explicit value. Supported values depend on
the model, so check its model page (OpenAI documents that GPT-6.1 Sol does not accept None). Several OpenAI
reasoning models accept function tools with a reasoning effort only on the Responses API, and Sage always offers
its tools, so for those models set **OpenAI API** to **Responses** and pick an effort the model supports. The
supported setup for GPT-5.6 Sol and GPT-6.1 Sol with tools is **OpenAI API = Responses**, **Reasoning Effort =
Low** and **OpenAI Daybreak = Off**. If the provider refuses an API or effort combination, the error names the
setting, the effort sent and its source, and the change to make; changing **OpenAI API** or **Reasoning Effort**
continues the same chat. Sage 0.3.0 cannot select the Responses API from a chat's settings: if the chat's model
settings do not list **OpenAI API**, upgrade Sage first. Through a proxy, the proxy decides which route and efforts
reach the model; Sage does not infer them from a model name.

## Policy

`policy_mode` controls how the next capability is chosen:

- **`hybrid`** (default) — deterministic code constrains the admissible actions and the model picks among them.
  The intended production mode.
- **`symbolic`** — a deterministic, model-free baseline. Reproducible, good for debugging without LLM variance.
- **`llm`** — the model chooses from the full catalog. Most latitude, weakest guardrails; mainly for research and
  eval comparisons.

Choose `hybrid` unless you have a reason not to.

## Step budget

`max_steps` bounds the number of model steps in each chat request; the count starts again with every new request,
and approving a card continues the same request's budget. Left unset it defaults to 200; set it to `0` for
unlimited. A small ceiling caps cost and stops runaway loops on scoped work. For a full autonomous objective, raise
it or set `0` — but the autonomous controller also enforces hard stop-losses independent of step count (60 cycles, a
45-minute wall clock, a 3M-token budget, and a no-progress loop-breaker), tunable through
`SAGE_CONTROLLER_MAX_CYCLES`, `SAGE_CONTROLLER_WALL_S`, and `SAGE_CONTROLLER_TOKEN_BUDGET`.
