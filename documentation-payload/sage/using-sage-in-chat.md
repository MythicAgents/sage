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
