+++
title = "Getting Started"
chapter = false
weight = 10
+++

## Install

Install Sage into a running Mythic the same way as any agent, from your Mythic directory:

```
sudo ./mythic-cli install github https://github.com/MythicAgents/sage
```

{{% notice info %}}
Sage requires **Mythic v4.0.0 or later**. As of this writing that release lives on Mythic's `Mythic-v4.0.0`
branch and has not been merged to the default branch, so you must install Mythic from that branch. Once it
merges, a normal Mythic installation will satisfy this requirement.
{{% /notice %}}

## Create a Sage chat

After the container registers, open Mythic's chat view and create a new chat channel using the **Sage** model.
Configure the following in the channel (or in your Mythic user secrets) — see
[Model Configuration](/agents/sage/model-configuration/) for the full list:

| Setting | Purpose |
|---|---|
| `provider` | `openai` (default), `bedrock`, `anthropic`, or `ollama` |
| `model` | Provider model identifier |
| `API_KEY` | Provider API key, when required |
| `API_ENDPOINT` | Optional — point `openai` at any OpenAI-compatible server |
| `mode` | `conversation` (default), `supervised`, or `auto` |

## Talk to Sage

Sage renders markdown, and each tool it runs appears as an updating, collapsible card.

- **Ask a question.** In the default `conversation` mode, Sage reads Mythic and BloodHound state and answers.
  It cannot fire an offensive action, so it is the safe place to start.
- **Act with approval.** Switch to `supervised` with `/mode supervised`, and every guarded action pauses for
  your approval before it runs. See [Using Sage in Chat](/agents/sage/using-sage-in-chat/).
- **Run an objective.** With a live foothold and an ingested graph, `/mode auto` lets Sage plan and drive the
  path itself.

## Next steps

- [Using Sage in Chat](/agents/sage/using-sage-in-chat/) — modes, approvals, and slash commands
- [Connecting BloodHound](/agents/sage/bloodhound/) — required for graph-driven work
- [The Engagement Ledger](/agents/sage/engagement-ledger/) — how Sage tracks proven progress
