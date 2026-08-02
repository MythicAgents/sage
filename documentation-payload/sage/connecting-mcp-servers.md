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

- **Tools are read-only by default.** For an agent to call an MCP tool, the tool must be on that server's
  `read_only_tools` allowlist, and a tool the server annotates as write/destructive is vetoed regardless. MCP
  tools are also withheld entirely during autonomous runs.

## Other `/mcp` subcommands

| Command | What it does |
|---|---|
| `/mcp list` | Connected servers and their tool counts |
| `/mcp tools [server]` | Tool names for one or all connected servers |
| `/mcp call <server> <tool> <json>` | Invoke one allowlisted tool directly (60-second timeout) |
| `/mcp connect <json>` | Connect a server from a JSON spec |
| `/mcp disconnect <name>` | Disconnect a named server |

For an agent to use a server during a turn, name it in your message — for example "using my-server, fetch …".
