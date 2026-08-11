+++
title = "Connecting BloodHound"
chapter = false
weight = 50
+++

BloodHound is central to Sage: it builds and reasons over the Active Directory attack graph (shortest paths,
ADCS/ESC paths, ACL abuse, and more). Sage talks to **BloodHound CE** through a
[BloodHound MCP server](https://github.com/mwnickerson/bloodhound_mcp), which it runs as a subprocess and
connects to at startup. Until that server is connected, Sage cannot ingest a SharpHound collection or answer
attack-path questions.

{{% notice info %}}
This page is also surfaced to you automatically the first time Sage needs BloodHound during an operation but
finds it not connected.
{{% /notice %}}

## Prerequisites

1. **BloodHound CE** running and reachable from the Sage host.
2. **A BloodHound API token** (BloodHound CE → *Administration → API tokens → Create*). You need the **Token ID**
   and **Token Key**.
3. **A BloodHound MCP checkout** on the Sage host (run with `uv`). The container bakes one at
   `/opt/bloodhound_mcp`, so under a standard Mythic install you do not need your own.

## Point Sage at the MCP checkout

Sage reads the MCP directory from `SAGE_BLOODHOUND_MCP_DIR` at startup. The container image already sets this to
its baked-in checkout, so leave it unset under Mythic unless you are using your own. For local development,
uncomment it in `Payload_Type/sage/.env` or export it in your shell:

```
SAGE_BLOODHOUND_MCP_DIR=/path/to/bloodhound_mcp
```

## Provide BloodHound credentials

The MCP server needs to reach your BloodHound CE instance. These resolve through the standard load order, so there are two places an operator can
set them without a shell:

1. **The Mythic chat configuration**, when you create the chat — per-chat, and the same place your
   model API key already lives. Mythic **user secrets** work too and keep the token out of plaintext
   channel config.
2. **Sage's own `.env`**, which Mythic maps into the container. Open it from the Mythic
   installed-services page, fill it in, save, restart the container. Shared by every chat in that
   container, and no shell, `docker cp`, or sudo required. This is why that file ships tracked and
   fully commented out rather than as a `.env.example` you would need a shell to copy.

Full order, highest first: chat config → user secret → container env → `.env.local` → `.env`.

A third place exists and is the least durable: the BloodHound MCP server also reads its own `.env`
from the directory `SAGE_BLOODHOUND_MCP_DIR` points at. Under Mythic that is the image's baked
`/opt/bloodhound_mcp`, which is **not** on the bind mount — anything written there is lost on the
next rebuild and cannot be edited from the UI. Prefer either option above.

| Setting | Purpose |
|---|---|
| `BLOODHOUND_URL` | Where BloodHound CE is, as one address: `scheme://host:port` (e.g. `http://localhost:8080`). Sage expands it into the three variables the MCP server reads. |
| `BLOODHOUND_TOKEN_ID` | API token ID |
| `BLOODHOUND_TOKEN_KEY` | API token key |

Sage forwards whatever it resolves into the MCP subprocess.

## When BloodHound is not connected

**Sage still works.** BloodHound is central to Sage but it is not Sage's life support, and a missing
optional dependency degrades a capability rather than the product:

- **Ordinary chat is unaffected.** Conversation and supervised chats answer normally with no
  BloodHound credentials, no MCP directory, and no MCP server on disk. A `hello` gets a reply.
- **Autonomous solves fail closed, on purpose.** A solve reasons over the attack graph to choose and
  verify each step, so running one without the graph would mean acting blind. An autonomous request
  is refused with a message that names BloodHound and repeats the setup steps.
- **Nothing is retried pointlessly.** When a required credential resolves nowhere, Sage does not
  spawn an MCP server it knows will exit, and it does not repeat that attempt on every request. Fix
  the configuration and the next request tries again.

### What you will see

In the Sage container log, at `WARNING` so it survives Mythic's default log level:

```
BloodHound auto-connect (chat): BloodHound MCP connect not attempted: required credentials are unset,
so the server would exit during startup.

Credentials Sage resolved for this attempt: NONE
Missing (required): BLOODHOUND_URL, BLOODHOUND_TOKEN_ID, BLOODHOUND_TOKEN_KEY
```

It names which credentials arrived and which did not — never their values. A bare
`McpError: Connection closed` with no explanation means you are on a build from before this was
fixed.

## Connect it

A new chat auto-connects BloodHound before it builds the graph. You can also connect or check it on demand from
chat:

- `/bloodhound` — connect, or report the existing connection. It is idempotent, so it is a safe way to ask
  whether BloodHound is connected.
- `/bloodhound force` — rebind an already-connected container to different credentials or a different BloodHound,
  using the current chat's resolved credentials (`reconnect` and `--force` also work). `/bloodhound force <dir>`
  also changes the MCP directory.

{{% notice tip %}}
The BloodHound connection is **process-global**: the first chat that connects successfully establishes it for the
whole container, and every later chat reuses it. You only fill the `BLOODHOUND_*` fields in once, and can leave
them blank in later chats.
{{% /notice %}}

If a connect fails, the returned message names which credentials Sage resolved and which required ones were
missing, so you do not have to read the container log to find out. A failed connect establishes nothing — the
next chat tries again from scratch.

{{% notice warning %}}
`/bloodhound force` disconnects before it connects, so if the new settings are wrong you are left with no
BloodHound connection rather than the old one. Plain `/bloodhound` will reconnect once you have fixed the
configuration.
{{% /notice %}}

## Autonomous sessions

Autonomous (`auto`) sessions **fail closed** if BloodHound cannot connect with the graph tools the execution
kernel requires. Ordinary supervised chat stays available in a degraded, fail-soft state so you can diagnose or
configure the integration.

To connect any *other* MCP server (not BloodHound), see [Connecting MCP Servers](/agents/sage/connecting-mcp-servers/).
