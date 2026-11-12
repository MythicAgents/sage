# Sage Agent Prompts — Operator Guide

Each file in this directory is the **system prompt for one Sage agent**. Edit a file, restart
Sage, and that agent's behavior changes — no Python edits, no rebuild. This is how you ship your
own playbook with an engagement.

| File | Agent | Role |
|------|-------|------|
| `supervisor.md` | Supervisor | Routes your request to the right specialist; drives the autonomous solve loop. |
| `mythic_operator.md` | Mythic_Operator | All Mythic C2 operations + in-memory offensive tradecraft. |
| `mythic_payload.md` | Mythic_Payload | Builds/configures Mythic payloads. |
| `bloodhound.md` | BloodHound | Dedicated BloodHound graph agent: ingest a collection, verify it, query attack paths via the BloodHound MCP server. |
| `mcp_manager.md` | MCP_Manager | Arbitrary third-party MCP servers (web fetch, external APIs, custom integrations). BloodHound is not handled here. |
| `generalist.md` | Generalist | General Q&A with no Mythic/TTP/tool access. |
| `sandbox.md` | Sandbox | Local-only isolated shell/Python snippets for scratch computation. |

## How to edit a prompt

1. Open the `<agent>.md` file and edit the body (everything below the second `---`).
2. Start a **new chat** — prompt edits are picked up automatically. Each new chat session builds
   its agents fresh and re-reads every `<agent>.md`, so your change takes effect on the next chat
   with **no restart**. (An already-running multi-turn conversation keeps the prompts it started
   with until you begin a new chat. A Sage **restart** is only needed for *code* changes — not
   prompt edits.)
3. Watch `sudo ./mythic-cli logs sage` if something looks off (see *Troubleshooting* below).

## File anatomy

```markdown
---
name: Mythic_Operator
description: Drives ALL Mythic C2 operations and in-memory offensive tradecraft.
variables:                       # ← the runtime values you may reference in the body
  - name: commands_text
    description: Available Mythic commands for pre-loaded payloads (auto-injected).
tools:                           # ← the tools this agent is allowed to call
  - get_all_active_callbacks
  - ...
---
<the system prompt body — this is what you edit>
```

The **leading** `---`…`---` block is YAML frontmatter (metadata). Everything after it is the prompt
body. Your body may contain `---` markdown rules freely — only the first block is treated as
frontmatter.

## Variables — what you can interpolate, and the rules

The body is rendered with Python's `str.format()`. That means:

- **`{name}`** (single braces) is a placeholder — Sage replaces it at runtime with a value it
  computes. The valid placeholders for a file are listed in that file's `variables:` frontmatter.
- **`{{` and `}}`** (doubled braces) render as literal `{` and `}`. **If you want a literal brace
  in your prompt text, you must double it.** (PowerShell `${env:X}`, JSON `{...}` examples, etc.)
- **You can only use the variables a file already lists.** These values are produced by Sage's
  code, one set per agent (see table below). You cannot invent a new `{variable}` just by writing
  it in the file — there is no value behind it, and referencing an unknown name triggers the
  fallback below. Adding a *new* variable currently requires a code change (see
  `ai/langgraph/model.py` call sites); a pluggable provider system is on the roadmap.

### Available variables per agent

| File | Variables available |
|------|--------------------|
| `supervisor.md` | *(none)* |
| `mythic_operator.md` | `{commands_text}` — installed-payload command reference, auto-injected |
| `mythic_payload.md` | `{installed_payloads_text}`, `{installed_c2_profiles_text}` |
| `bloodhound.md` | `{servers_text}` — connected MCP servers + tool preview |
| `mcp_manager.md` | `{servers_text}` — connected MCP servers + tool preview |
| `generalist.md` | *(none)* |
| `sandbox.md` | *(none)* |

> Each file's own `variables:` frontmatter is the authoritative list for that file.

## Tools

The `tools:` frontmatter is the set of tools that agent may call. Remove a tool name and start a
new chat, and the agent can no longer use it (Sage filters its tool set against this list).

**Exception — MCP_Manager:** tools provided by connected MCP servers are discovered at runtime and
are *always* available; they can't be listed here. The `tools:` list for `mcp_manager.md` governs
only Sage's own built-in tools.

## Step bounds

A worker's `step_bounds:` frontmatter limits each supervised step it runs (one delegation from the
Supervisor):

```yaml
step_bounds:
  tool_calls: 20          # tool calls the step may run; routing calls do not count
  active_seconds: 600     # the worker's own time; approval waits and guarded actions do not count
  consecutive_errors: 3   # tool errors in a row
```

A missing field, or a missing block, uses the default shown. A valid `SAGE_STEP_MAX_TOOL_CALLS`,
`SAGE_STEP_MAX_ACTIVE_SECONDS` or `SAGE_STEP_MAX_CONSECUTIVE_ERRORS` value overrides that field for every
worker; an invalid one is ignored. Values must be positive, and the two counts whole numbers: Sage
refuses to build its agents from a malformed block rather than guess. A step that reaches a bound
gets one last model call with no tools, so it can report what it found, and then returns to the
Supervisor.

## Troubleshooting

- **My whole prompt shows literal `{something}` text.** You referenced a variable that isn't
  available for that agent (typo, or a name not in `variables:`). Sage logs
  `prompt_loader: template render FAILED … using RAW body` and falls back to the un-rendered text
  so it won't crash. Fix the placeholder name or double a stray brace (`{` → `{{`).
- **A literal `{` or `}` disappeared / caused an error.** Double it: `{{` or `}}`.
- **My edit didn't take effect.** Start a **new chat** — prompts are re-read when a new chat
  session is created. A conversation already in progress keeps its original prompts until you
  start a fresh chat. (Restarting Sage is only for *code* changes.)

See also the loader at
`ai/langgraph/prompt_loader.py`.
