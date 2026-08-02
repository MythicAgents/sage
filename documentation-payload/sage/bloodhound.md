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

The MCP server needs to reach your BloodHound CE instance. These resolve through Sage's normal configuration
chain, so the usual place to put them is the Mythic chat configuration or your Mythic user secrets — the same
place your model API key lives:

| Setting | Purpose |
|---|---|
| `BLOODHOUND_DOMAIN` | BloodHound CE host |
| `BLOODHOUND_TOKEN_ID` | API token ID |
| `BLOODHOUND_TOKEN_KEY` | API token key |
| `BLOODHOUND_PORT` | Optional; MCP default `443`, BloodHound CE web UI is commonly `8080` |
| `BLOODHOUND_SCHEME` | Optional; default `https` |

Sage forwards whatever it resolves into the MCP subprocess.

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
