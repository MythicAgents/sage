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
| `provider` | `openai` (OpenAI, the default), `litellm` (LiteLLM Gateway), `openai_compatible` (Custom (OpenAI-compatible)), `bedrock`, `anthropic`, or `ollama` |
| `model` | Provider model identifier |
| `API_ENDPOINT` | Base URL. Required for `litellm` (the gateway URL) and `openai_compatible`; optional for `openai`, where blank means `https://api.openai.com/v1` |
| `API_KEY` | Provider API key when required; for `litellm`, the gateway key |
| `mode` | `conversation` (default), `supervised`, or `auto` |
| `autonomous_solve` | Enable objective-led solving in `supervised` mode; each guarded action still needs approval. `auto` always enables it. Ignored in `conversation`, which stays read-only; Sage says so once in the chat |
| `policy_mode` | `hybrid` (default), `symbolic`, or `llm` capability selection |
| `max_steps` | Model-step ceiling per chat request; unset defaults to `200`, `0` means unlimited |
| `SAGE_OPENAI_API` | **OpenAI API**: `chat_completions` or `responses` selects one; blank means Responses on OpenAI's own API and Chat Completions everywhere else (OpenAI, LiteLLM Gateway and Custom) |
| `SAGE_OPENAI_ACCESS_PROGRAM` | **OpenAI Daybreak**: `standard` (Off), `daybreak_blue` or `daybreak_red`. Any value, Off included, requires the Responses API; blank sends Off on Responses |
| `SAGE_OPENAI_SAFETY_IDENTIFIER` | **OpenAI Safety Identifier**: optional stable pseudonym for the person using the chat |
| `SAGE_REASONING_EFFORT` | **Reasoning Effort**: blank sends no effort; `none` through `max` is sent as chosen (OpenAI, LiteLLM Gateway and Custom) |

## Providers

OpenAI, LiteLLM Gateway, Bedrock, Anthropic and Ollama are the tested set. OpenAI, LiteLLM Gateway and Custom
(OpenAI-compatible) all use LangChain's OpenAI adapter; Custom is for any other server that speaks the OpenAI API,
and individual servers are unverified. Sage builds the model through LangChain's `init_chat_model`, so other
providers it supports may work but are unverified. Bedrock is the only provider with dedicated AWS credential and
region handling. Never infer the effective backend from the model name alone; record the provider and route for
live runs.

## LiteLLM Gateway and other OpenAI-compatible servers

Choose **LiteLLM Gateway** for a [LiteLLM](https://docs.litellm.ai/docs/simple_proxy) gateway: set **API Endpoint**
to the gateway URL, **API Key** to the gateway key (not an upstream vendor key) and **Model** to the gateway's model
alias. Choose **Custom (OpenAI-compatible)** for any other server that speaks the
[OpenAI API](https://developers.openai.com/api/reference/overview), such as a self-hosted model or an internal
gateway. Both need **API Endpoint**: without it Sage refuses the chat before any request, so a gateway key is never
sent to OpenAI. Both use Chat Completions unless **OpenAI API** says otherwise; Responses can be selected, but not
every gateway or server supports every Responses field, and Sage makes no claim that a gateway replays encrypted
reasoning. Existing chats configured as `provider: openai` with an `API_ENDPOINT` that points at a gateway keep
sending what they sent before; switching one to **LiteLLM Gateway** starts a new chat.

## Which API a chat uses

An explicit **OpenAI API** always wins. With it blank at every layer, **OpenAI** on OpenAI's own API uses
**Responses** (stateless, with encrypted reasoning and the Daybreak program named, Off by default), and everything
else uses **Chat Completions**. OpenAI's own API means the endpoint the adapter calls — **API Endpoint**, else
`OPENAI_API_BASE`, else an enabled `LANGSMITH_GATEWAY`, else `OPENAI_BASE_URL`, else `https://api.openai.com/v1` —
is `https` on `api.openai.com` or one of its subdomains. To stay on Chat Completions with OpenAI's API, set
**OpenAI API** to **Chat Completions**.

## Reasoning effort and tools

**Reasoning Effort** follows the same chain. Blank at every layer sends no effort, so the provider's default for
the model applies; that differs from **None**, which Sage sends as an explicit value. Supported values depend on
the model, so check its model page (OpenAI documents that GPT-6.1 Sol does not accept None). Several OpenAI
reasoning models accept function tools with a reasoning effort only on the Responses API, and Sage always offers
its tools, so those models need the Responses API (the default on OpenAI's own API; elsewhere set **OpenAI API**
to **Responses**) and an effort the model supports. The supported setup for GPT-5.6 Sol and GPT-6.1 Sol with tools
is the Responses API, **Reasoning Effort = Low** and **OpenAI Daybreak = Off**. If the provider refuses an API or effort combination, the error names the
setting, the effort sent and its source, and the change to make; changing **OpenAI API** or **Reasoning Effort**
continues the same chat. Sage 0.3.0 cannot select the Responses API from a chat's settings: if the chat's model
settings do not list **OpenAI API**, upgrade Sage first. Through a gateway or another OpenAI-compatible server, it
decides which route and efforts reach the model; Sage does not infer them from a model name.

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
