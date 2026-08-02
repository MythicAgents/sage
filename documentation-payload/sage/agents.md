+++
title = "Agents"
chapter = false
weight = 15
+++

Sage is not one model — it is a small team of agents built on LangGraph. A **Supervisor** routes each request
to the right specialist, and the specialists own distinct slices of the work. Each agent's system prompt is a
plain markdown file under the Sage service's `prompts/` directory: edit the body, start a new chat, and that
agent's behavior changes with no rebuild and no restart. This is how you ship your own playbook with an
engagement.

## The team

| Agent | Role | Tools |
|---|---|---|
| **Supervisor** | Routes your request to the right specialist and drives the autonomous solve loop. | Handoff/routing only |
| **Mythic_Operator** | The workhorse — all Mythic C2 operations: callbacks, tasking, files, credentials, the capability layer, tool staging, and in-memory tradecraft. | 22 Mythic tools |
| **Mythic_Payload** | Builds and configures Mythic payloads and C2 profiles. | 12 payload/C2 tools |
| **BloodHound** | Owns the BloodHound graph lifecycle: ingest a collection, verify it, then query attack paths. | BloodHound MCP graph tools (at runtime) + 3 Sage TTP tools |
| **MCP_Manager** | Bridges arbitrary third-party MCP servers you connect (web fetch, external APIs, custom integrations). BloodHound is not handled here. | The connected servers' tools (at runtime) |
| **Generalist** | General Q&A, explanations, and advice that need no Mythic, TTP, or external tool access. | None |
| **Sandbox** | Local-only isolated shell/Python snippets for scratch computation. | `sandbox_exec` |

See [Tools Reference](/agents/sage/tools-reference/) for what each Mythic tool does, and
[Connecting MCP Servers](/agents/sage/connecting-mcp-servers/) for how MCP_Manager gets its tools.

## The deterministic controller

In an **autonomous** solve there is one more piece that is *not* an LLM. A deterministic controller honors the
model's intent to act, but owns which capability actually runs and how it is built — the mechanics stay below the
model, and an effect only reaches the [engagement ledger](/agents/sage/engagement-ledger/) after a verifier
confirms it. This is what keeps an autonomous run grounded in proof rather than in the model's narration.

{{% notice tip %}}
Tool availability is also filtered per agent by each prompt's frontmatter. Removing a tool name from an agent's
`prompts/<agent>.md` and starting a new chat takes that tool away from the agent — no code change required.
{{% /notice %}}
