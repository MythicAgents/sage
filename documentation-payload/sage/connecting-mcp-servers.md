+++
title = "Connecting MCP Servers"
chapter = false
weight = 60
+++

Sage can drive **any** MCP server, not just BloodHound. (BloodHound is a pre-wired, dedicated MCP server with its
own agent — see [Connecting BloodHound](/agents/sage/bloodhound/).) Connect any other server from chat:

```
/mcp connect {"name":"my-server","type":"stdio","command":"uv","args":["--directory","/path","run","main.py"],"sage_execution_class":"non_target_control_plane","read_only_tools":["search","fetch"]}
```

Transports: `stdio` (default), `sse`, and `http` / `streamable_http`.

## Two rules that will otherwise trip you up

{{% notice warning %}}
**`sage_execution_class` is required.** A connect with no execution class defaults to `unclassified` and is
refused before it connects. To attach a third-party server you must set
`"sage_execution_class":"non_target_control_plane"`. This is deliberate: MCP servers are **control-plane only** —
Sage never reaches a target through one.
{{% /notice %}}

- **MCP tools default to guarded.** An MCP tool not explicitly classified is treated as guarded: it
  requires HITL approval in supervised mode and is denied in conversation mode. Classify tools via the
  `mcp_tool_policy.json` file (see below).

## MCP tool policy

`mcp_tool_policy.json` in the Sage payload root (`Payload_Type/sage/`) classifies MCP tools as
`read_only` (freely available) or `guarded` (HITL-gated in supervised, denied in conversation). Set
`SAGE_MCP_TOOL_POLICY` to override the file path. BloodHound CE ships pre-classified.

```json
{
  "default": "guarded",
  "servers": {
    "bloodhound-ce": {
      "default": "guarded",
      "tools": {
        "domain_info": "read_only",
        "graph_analysis": "read_only",
        "file_upload": "guarded"
      }
    }
  }
}
```

Lookup order: tool-level override → server default → global default → guarded (hardcoded fallback).
A missing or malformed file falls back to all-guarded. Use `/mcp policy` to view the effective policy.

## Other `/mcp` subcommands

| Command | What it does |
|---|---|
| `/mcp list` | Connected servers and their tool counts |
| `/mcp tools [server]` | Tool names for one or all connected servers |
| `/mcp call <server> <tool> <json>` | Invoke one allowlisted tool directly (60-second timeout) |
| `/mcp connect <json>` | Connect a server from a JSON spec |
| `/mcp disconnect <name>` | Disconnect a named server |
| `/mcp policy` | Show the effective tool safety classifications |

For an agent to use a server during a turn, name it in your message — for example "using my-server, fetch …".
