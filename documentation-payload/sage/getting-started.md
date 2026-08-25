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

## Enable the background findings watcher

Create a `Sage Watcher` AI chat in Mythic, configure its Watcher-namespaced model route, lock the channel, and run
`/watcher apply` in that channel. Channel creation alone is inert. Ordinary `Sage` chats have separate settings
and cannot claim or change the Watcher owner generation.

| Setting | Purpose |
|---|---|
| `SAGE_WATCHER_PROVIDER` / `SAGE_WATCHER_MODEL` | Watcher-only model route. The Watcher never falls back to ordinary `provider` or `model`. |
| `SAGE_WATCHER_API_ENDPOINT` / `SAGE_WATCHER_API_KEY` | Optional Watcher-only endpoint and credential. Both may instead be identically named Mythic user secrets or environment values. |
| `SAGE_WATCHER_AWS_ACCESS_KEY_ID` / `SAGE_WATCHER_AWS_SECRET_ACCESS_KEY` / `SAGE_WATCHER_AWS_SESSION_TOKEN` / `SAGE_WATCHER_AWS_DEFAULT_REGION` | Watcher-only Bedrock credentials and region. |
| `SAGE_WATCHER_INTERVAL_SECONDS` | Applied cadence. Default: 300 seconds; accepted range: 5–86,400 integer seconds. |
| `SAGE_WATCHER_APITOKEN` | Persistent API token for an active Mythic bot whose current operation is the watched operation. Its stored grants must be exactly `callback.read`, `chat-ai.read`, `chat.write`, `credential.read`, `eventlog.write`, `file.read`, `operation.read`, `response.read`, and `task.read`; wildcard and excess scopes fail closed. Mythic expands the two write grants with `chat.read` and `eventlog.read`, and Sage checks that effective closure exactly. |
| `SAGE_FINDINGS_SLACK_WEBHOOK_URL` | Optional approved Slack destination. Sage sends only a fixed generic change notice; full findings remain in Mythic. Modern Slack app webhooks are bound to their installed channel, so use one URL per destination. |
| `SAGE_FINDINGS_SLACK_CHANNEL_ID` | Optional C/G channel ID for a **legacy custom-integration** webhook. Modern Slack app incoming webhooks cannot override their installed channel. |

Watcher setting resolution matches Sage Chat: channel config → identically named declared user secret →
environment → default. Provider/model are not user-secret fields. UI- and environment-backed profiles auto-resume
after restart; a user-secret-backed generation reports `credentials-required` until the locked owner runs
`/watcher apply` again. The two `SAGE_FINDINGS_SLACK_*` settings are Sage runtime-environment/dotenv settings,
not Watcher AI-channel configuration fields. Use `/watcher status` to confirm owner, generation, cadence, and
health.

An operator can send the exact production notice with
`.venv/bin/python skills/sage-focused-capability-tests/scripts/probe_slack_findings_webhook.py --send`.
Use `--webhook-env NAME` to select another modern channel-bound webhook from an environment variable, or
`--channel-id C0123456789` only when testing a legacy custom-integration override. The probe never prints the URL.

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
