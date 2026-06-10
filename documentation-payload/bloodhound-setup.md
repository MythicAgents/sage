+++
title = "Connecting BloodHound to Sage"
chapter = false
weight = 30
+++

# Connecting BloodHound to Sage

BloodHound is central to Sage: it builds and reasons over the Active Directory attack graph
(shortest paths, ADCS/ESC paths, ACL abuse, etc.). Sage talks to **BloodHound CE** through a
**BloodHound MCP server**. If that MCP server is not connected, Sage cannot ingest a SharpHound
collection or answer attack-path questions — so the graph-driven parts of an engagement will not work
until you connect it.

This page is also surfaced to you automatically (via the Mythic EventFeed and in Sage's reply) the
first time Sage needs BloodHound during an operation but finds it not connected.

## Prerequisites

1. **BloodHound CE is running and reachable from the Sage container/host.**
   - BloodHound CE web/API (default `http://127.0.0.1:8080`, API on `:8083` for some deployments)
   - neo4j (default `bolt://127.0.0.1:7687`, browser `:7474`)
2. **A BloodHound API token** (BloodHound CE → *Administration → API tokens → Create*). You need the
   **Token ID** and **Token Key**.
3. **The BloodHound MCP server** checked out and runnable on the Sage host (e.g. via `uv`).

## Configure the MCP server credentials

The BloodHound MCP reads its connection settings from a `.env` in its own directory:

```
BLOODHOUND_DOMAIN=127.0.0.1
BLOODHOUND_PORT=8083
BLOODHOUND_SCHEME=http
BLOODHOUND_TOKEN_ID=<your token id>
BLOODHOUND_TOKEN_KEY=<your token key>
```

## Connect it to Sage

Issue the `mcp-connect` command to your Sage callback with the BloodHound server's stdio parameters,
for example:

```
mcp-connect {
  "name": "BloodHound",
  "connection_type": "stdio",
  "command": "uv",
  "arguments": ["--directory", "/path/to/bloodhound_mcp", "run", "main.py"],
  "cwd": "/path/to/bloodhound_mcp",
  "timeout": 30,
  "sse_read_timeout": 300,
  "terminate_on_close": true,
  "ssl_verify": true
}
```

A successful connect reports the BloodHound tools as available (e.g. `domain_info`, `cypher_query`,
`graph_analysis`, `file_upload`, …). Verify with `mcp-list`.

> **Note:** the MCP connection is tied to the running Sage process. If Sage restarts, re-run
> `mcp-connect` to re-attach BloodHound.

## Verify

After connecting, ask Sage to list the domains BloodHound knows about (`domain_info` / "list domains").
If you have not ingested a collection yet, the list will be empty — run a SharpHound collection, and
Sage's BloodHound agent will ingest it (`file_upload`) and verify the domains appear before using the
graph.
