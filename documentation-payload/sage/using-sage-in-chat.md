+++
title = "Using Sage in Chat"
chapter = false
weight = 20
+++

Everything below happens inside a Mythic chat channel with the **Sage** model.

## Modes

Set `mode` when you create the channel, or switch it live with `/mode`:

| Mode | What Sage can do | When to use |
|---|---|---|
| `conversation` (default) | Reads Mythic and BloodHound state, queries read-only tools (callbacks, graph, credentials, tradecraft), and answers. Cannot fire a guarded/offensive action. | Asking questions, reviewing the graph, getting tradecraft advice, planning. The safe default. |
| `supervised` | Proposes guarded actions; each one waits for your approval before it runs. | Interactive engagement work where you want Sage to act but keep a hand on every trigger. |
| `auto` | Runs the autonomous kernel (observe, select, execute, verify) with no per-action approval. | A prepared, scoped objective with a live callback and an ingested graph. |

`/mode` with no argument reports the channel's current mode.

## Supervised approvals

In supervised mode every **guarded** action — issuing a command, executing a capability, creating a payload,
adding a credential — pauses as a native Mythic approval request before it runs. Approval is **default-deny**:
only an explicit Accept lets it through. **One action, one card** — when the model proposes multiple guarded
actions, Sage presents them one at a time. Accept runs the action; Reject denies it. After the action
completes, the next guarded action is proposed. Read-only tools never prompt.

Guarded tools are also **scope-preflighted**: each maps to a required Mythic bot-token scope (`callback.write`,
`payload.write`, `credential.write`, `file.write`), and a tool whose scope your channel token lacks is disabled
up front rather than failing mid-run.

## Slash commands

| Command | What it does |
|---|---|
| `/findings` | Show the operation's canonical evidence-backed findings plus watcher health (`/finding` remains a compatibility alias) |
| `/watcher status` | From either model, show the redacted owner generation, route sources, cadence, scheduler health, poll count, pending notices, and last scans without creating a watcher |
| `/watcher apply` | From the exact active locked `Sage Watcher` channel, claim or advance the immutable operation profile and rehydrate any requesting-user secret after restart |
| `/watcher scan` · `/watcher pause` · `/watcher resume` | From the exact owner only, request an immediate read-only poll, pause the scheduler, or resume it |
| `/watcher interval <seconds>` · `/watcher interval default` | From the exact owner only, persist cadence for the applied profile; accepted range is 5–86,400 integer seconds |
| `/state` | Show the [engagement ledger](/agents/sage/engagement-ledger/) — the hop table of proven effects for this channel |
| `/state objective <text>` | Set the engagement objective |
| `/state reconcile [task_id] [apply]` | Import verified effects and credentials from completed Mythic tasks — dry-run unless `apply` is given |
| `/state set <row> <status>` · `/state remove <row>` · `/state wipe` | Edit or clear ledger rows (you cannot hand-promote a row to `achieved`) |
| `/mode [conversation\|supervised\|auto]` | Show or switch the channel's mode |
| `/list` | List active Sage chat sessions |
| `/stop` | Cooperatively stop the running agent on this channel |
| `/bloodhound [force] [dir]` | Connect, report, or rebind the [BloodHound MCP](/agents/sage/bloodhound/) |
| `/mcp <list\|tools\|call\|connect\|disconnect\|policy>` | Manage [MCP servers](/agents/sage/connecting-mcp-servers/) |
| `/sandbox [shell\|python] <code>` | Run an isolated local snippet (requires the `callback.write` scope) |

## Background findings watcher

`Sage Watcher` is a separate Mythic AI model. Creating its channel is inert: configure it, lock it, and run
`/watcher apply` to establish the sole owner generation for the beta deployment's one supported operation. A
**scan** is one read-only poll of Mythic callbacks, tasks, responses/task output, credentials, and files. It never
issues a payload task or contacts a target. Every poll attempt increments `scans`, but it is not automatically an
LLM inference: unchanged durable evidence is reconciled without a model call. When evidence changes, Sage may use
one bounded, tool-free direct-reasoner call to rank already-admitted candidates. Watcher conversation is a separate
stateless one-node graph with no tools, checkpoint, mode, tasking, validation proposal, or ordinary Sage fallback.

Watcher LLM settings are the eight collision-free `SAGE_WATCHER_*` provider/model/endpoint/credential keys. They
use Sage Chat's config → declared user secret → environment → default algorithm and never read ordinary Sage's
unprefixed keys. Cadence defaults to 300 seconds and pause/cadence persist across restart. UI/environment-backed
profiles auto-resume; a user-secret-backed generation reports `credentials-required` until the locked owner
reapplies it. Full updates remain in `#sage-findings`; Slack receives only the fixed generic change notice.
Modern Slack app incoming webhooks always use the channel selected when that webhook was installed, so provision
one webhook URL per destination. Only a legacy custom-integration webhook may honor the optional
`SAGE_FINDINGS_SLACK_CHANNEL_ID` C/G channel override.
