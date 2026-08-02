+++
title = "Sage"
chapter = true
weight = 100
+++

![logo](/agents/sage/sage.svg?width=200px)

## Summary

Sage is an AI operator that lives inside Mythic. It reads your live Mythic operation — its callbacks, tasks,
credentials, and files — alongside your BloodHound graph, reasons over it the way an analyst would, and turns
that reasoning into real Mythic tasks. You can ask it a scoped question about the engagement and get a grounded
answer, or hand it an objective and let it plan and drive the path itself.

Sage runs on the control plane. It is **not** an implant and does not create a Sage callback: it drives Mythic
and operates through your existing payloads, and never touches the target environment itself. Sage is a native
Mythic **chat container** — it installs into a Mythic server and has no function outside one.

{{% notice info %}}
Sage requires **Mythic v4.0.0 or later**. Native chat containers do not exist before v4.0.0.
{{% /notice %}}

### Highlighted Features

- Native Mythic v4 chat container with markdown output and live, collapsible tool-use cards
- Three modes: **conversation** (read/answer), **supervised** (approve each guarded action), **auto** (autonomous solve)
- 32 Mythic tools plus a 15-step generic, range-agnostic capability chain
- BloodHound attack-graph analysis through a pre-wired MCP server
- Connects to arbitrary third-party MCP servers
- A durable, proof-gated **engagement ledger** that survives restarts and model changes
- Embedded [Arize Phoenix](https://github.com/Arize-ai/phoenix) tracing for full run observability
- Multi-provider: any OpenAI-compatible endpoint, Anthropic, Amazon Bedrock, or Ollama

## Authors

- @Ne0nd0g

{{% notice warning %}}
**Authorized use only, and still maturing.** Use Sage only for activity you are explicitly authorized to
perform. It is under active development, and its behavior in a production environment is not yet well
understood: an autonomous agent driving a C2 carries real risk of unintended impact on a live network. Weigh
production use very carefully, and prefer labs and authorized engagements until Sage has matured.
{{% /notice %}}

## Table of Contents

{{% children %}}
