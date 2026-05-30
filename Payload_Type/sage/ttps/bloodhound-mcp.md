---
name: BloodHound MCP
category: recon
subcategories: [attack-path-analysis, bloodhound, mcp-integration, graph-query]
tradecraft_tags: [bloodhound, mcp, attack-paths, cypher, adcs, graph-analysis, shortest-path, specterops, setup, standup, deploy, prerequisite]
mitre_attack: []
source:
  url: https://github.com/mwnickerson/bloodhound_mcp
  license: Unknown
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [linux]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  Operator-side infrastructure — no target detection signal. The BloodHound MCP server
  talks only to the operator's own BloodHound CE/Enterprise REST API + Neo4j. The
  detectable activity is upstream: the SharpHound collection that feeds BloodHound
  (see the sharphound TTP for that detection profile).
usage_examples:
  - description: Stand up / set up BloodHound CE and connect the MCP before any attack-path analysis
    args: "(prerequisite) ./bloodhound-cli install  then register the stdio MCP server in Sage"
  - description: Ingest a SharpHound collection ZIP into BloodHound
    args: "(MCP tool) file_upload  — point at the SharpHound output ZIP"
  - description: Find the shortest attack path from a foothold principal to Domain Admins
    args: "(MCP tool) graph_analysis  — start node = SAMWELL.TARLY@NORTH..., target = Domain Admins"
  - description: Detect ADCS ESC paths in the environment
    args: "(MCP tool) adcs_info  — surfaces vulnerable certificate templates (ESC1/ESC4/ESC6/ESC8...)"
  - description: Run a custom Cypher query for foreign-group / cross-forest membership
    args: "(MCP tool) cypher_query  — e.g. MATCH paths across the Spys group LAPS readers"
opsec_notes: |
  No target-side footprint — this is analysis of data already collected. The OPSEC cost
  lives in the SharpHound collection that feeds it (loud LDAP/SAMR). Once data is in
  BloodHound, all querying is local to the operator. Keep the BloodHound instance and its
  Neo4j on the operator's own infrastructure; do not expose its ports to the target network.
gotchas: |
  - The MCP server is stdio-only — it must be launched where the client (Sage) can spawn it
    and reach BloodHound's REST API. In Sage this runs INSIDE the Sage container.
  - BloodHound must already contain ingested data for path queries to return anything. The
    workflow is collect (SharpHound) -> ingest (file_upload) -> query (graph_analysis/adcs_info)
    -> act. Querying before ingest yields empty graphs.
  - API token id/key are created in the BloodHound UI (Administration -> API Tokens) and are
    distinct from the admin login. The token key is shown ONCE on creation — capture it then.
  - Repo license is unmarked at time of writing (TBD-verify before redistribution).
related_ttps: [sharphound, bloodhound-ingest, bloodhound-cypher-reference, bloodhound-custom-queries, certify, adcs-esc4, sharpgpoabuse, laps-abuse]
alternatives: [bloodhound-cypher-reference, manual-cypher-via-neo4j-browser, bloodhound-python]
common_args: {}
last_updated: 2026-05-29
---

# BloodHound MCP

`mwnickerson/bloodhound_mcp` is a Model Context Protocol (MCP) server that exposes a
BloodHound Community Edition / Enterprise instance to an LLM agent as a set of composite,
natural-language-friendly tools. It connects to BloodHound's REST API (and the underlying
Neo4j graph) and lets an agent ingest collection data, enumerate AD entities, and — most
importantly — **reason over the attack graph**: shortest paths, edge composition, ADCS ESC
detection, and arbitrary Cypher. For Sage this is the component that turns a SharpHound dump
into *decisions* — "from here, the shortest path to Domain Admin runs through this GPO, then
this delegation, then this foreign group" — instead of a scripted chain.

## Typical use cases
- Ingesting a SharpHound collection so the graph is queryable (`file_upload`)
- Asking for the shortest path from a compromised principal to a high-value target (`graph_analysis`)
- Detecting ADCS ESC abuse paths without hand-writing Cypher (`adcs_info`)
- Enumerating a specific user/group/computer's rights, sessions, and controllers (`user_info`, `group_info`, `computer_info`)
- Running custom Cypher for cross-forest / foreign-group / LAPS-reader analysis (`cypher_query`)
- Checking collection completeness before trusting a path (`data_quality`)

## How Sage uses this
This is the core of the Phase-1 T1.5 attack-path loop. The pattern:

1. **Collect** — Mythic_Operator runs SharpHound on the foothold (see the `sharphound` TTP) and downloads the output ZIP.
2. **Ingest** — MCP_Manager calls `file_upload` to push that ZIP into BloodHound.
3. **Reason** — MCP_Manager calls `graph_analysis` / `adcs_info` / `cypher_query` to identify the path (e.g. samwell.tarly → GPO `STARKWALLPAPER` → SYSTEM on WINTERFELL → jon.snow delegation → Spys/LAPS → ESSOS-CA ESC1).
4. **Act** — Supervisor routes the chosen next hop back to Mythic_Operator, which executes it through the relevant TTP (SharpGPOAbuse, Rubeus, Certify, ...).
5. **Repeat** until Domain Admin / cross-forest.

The graph-reasoning step (3) is the most demo-valuable moment — it is the explicit, narratable
"why this path" reasoning that shows up in the Phoenix trace.

**Prerequisite check / operator guidance (required behavior):** before the loop can run,
BloodHound and this MCP must be up and connected. If `MCP_Manager.get_connected_servers()`
does NOT list the bloodhound server, Sage MUST NOT silently fail — it must return the standup
guidance below and ask the operator to bring BloodHound up, then retry the connection.

## Output
Structured findings from BloodHound's graph: entity dossiers, ranked shortest paths (node/edge
sequences), ADCS ESC findings, Cypher result sets, and collection-quality stats. Sage relays the
chosen path and its reasoning back to the operator and uses it to drive the next Mythic task.

## Standing up BloodHound CE + the MCP server

> This is the exact guidance Sage should surface when BloodHound isn't reachable.

**1. Stand up BloodHound Community Edition (operator host):**
```bash
# Download and install via the official BloodHound CLI
wget https://github.com/SpecterOps/bloodhound-cli/releases/latest/download/bloodhound-cli-linux-amd64.tar.gz
tar -xzf bloodhound-cli-linux-amd64.tar.gz
./bloodhound-cli install      # pulls Docker images, writes docker-compose.yml with defaults
```
- Web UI: `http://localhost:8080/ui/login`
- Initial admin password is printed in the install output:
  `[+] You can log in as 'admin' with this password: <Password>`
- Lost it? `./bloodhound-cli resetpwd`

**2. Create an API token (for the MCP):**
- Log into the BloodHound UI → **Administration → API Tokens → Create**.
- Copy the **Token ID** and **Token Key**. The key is shown only once — capture it now.

**3. Stand up the MCP server (inside the Sage container — stdio transport):**
```bash
git clone https://github.com/mwnickerson/bloodhound_mcp.git
cd bloodhound_mcp
uv sync
# .env (or pass as Sage build/secret params — do NOT hardcode the token):
#   BLOODHOUND_DOMAIN=<bloodhound-host>     # e.g. 127.0.0.1 or the CE host reachable from Sage
#   BLOODHOUND_TOKEN_ID=<token id>
#   BLOODHOUND_TOKEN_KEY=<token key>
#   BLOODHOUND_PORT=8080                    # optional; MCP default is 443
#   BLOODHOUND_SCHEME=http                  # optional; MCP default is https
```
Run command (this is what Sage's `mcp_connect` STDIO entry invokes):
```bash
uv --directory /path/to/bloodhound_mcp run main.py
```

## Connecting from Sage (stdio)

Sage already has a full MCP subsystem (`ai/mcp.py`, the `MCP_Manager` agent, and the runtime
`mcp_connect`/`mcp_list`/`mcp_call`/`mcp_disconnect` Mythic commands). Register the BloodHound
MCP as a STDIO server:

- transport: `stdio`
- command: `uv`
- args: `["--directory", "/path/to/bloodhound_mcp", "run", "main.py"]`
- env: `BLOODHOUND_DOMAIN`, `BLOODHOUND_TOKEN_ID`, `BLOODHOUND_TOKEN_KEY` (+ optional `BLOODHOUND_PORT`, `BLOODHOUND_SCHEME`)

After connecting, confirm the 13 tools surface via `MCP_Manager.get_tools_summary()`.

## MCP tools (13 composite tools)

| Tool | Purpose |
|------|---------|
| `domain_info` | Domain entities, relationships, and security-relevant data |
| `user_info` | User accounts: sessions, rights, control relationships |
| `group_info` | Group membership, admin rights, controllers |
| `computer_info` | Computer sessions, admins, delegation, access rights |
| `ou_info` | Organizational-unit structure and linked objects |
| `gpo_info` | Group Policy Objects and their controllers |
| `graph_analysis` | Shortest paths, edge composition, graph search |
| `adcs_info` | Certificate templates and ESC vulnerability paths |
| `cypher_query` | Execute Cypher; manage saved queries (saved_list / saved_get) |
| `data_quality` | Collection statistics and platform info |
| `asset_groups` | List/query custom asset-group membership |
| `custom_nodes` | Manage custom node types / OpenGraph extensions (v8.0+) |
| `file_upload` | Ingest BloodHound collection data |

## Full Reference

> Captured 2026-05-29 from the `mwnickerson/bloodhound_mcp` repo README and the BloodHound CE
> Community-Edition Quickstart. Verify against the live repo before relying on exact flags;
> the repo license was unmarked at capture time (TBD-verify).

### Environment variables

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `BLOODHOUND_DOMAIN` | yes | — | BloodHound instance hostname reachable from the Sage container |
| `BLOODHOUND_TOKEN_ID` | yes | — | API token identifier (BloodHound UI → Administration → API Tokens) |
| `BLOODHOUND_TOKEN_KEY` | yes | — | API token secret (shown once at creation) |
| `BLOODHOUND_PORT` | no | 443 | BloodHound API port (CE web UI defaults to 8080) |
| `BLOODHOUND_SCHEME` | no | https | http or https |

### Transport & run

- Transport: **stdio** (no SSE/HTTP server upstream as of capture date).
- Run: `uv --directory /path/to/bloodhound_mcp run main.py`
- The MCP process must reach BloodHound's REST API over the network; in Sage it runs inside the
  Sage container, so the container needs `uv`, the cloned repo, the env vars, and network reach to
  the BloodHound host.

### BloodHound CE standup (BloodHound CLI)

1. `wget https://github.com/SpecterOps/bloodhound-cli/releases/latest/download/bloodhound-cli-linux-amd64.tar.gz`
2. `tar -xzf bloodhound-cli-linux-amd64.tar.gz`
3. `./bloodhound-cli install` (pulls images, writes docker-compose.yml)
4. UI at `http://localhost:8080/ui/login`; admin password printed in install output; reset with `./bloodhound-cli resetpwd`.

### Version-specific notes

- `custom_nodes` / OpenGraph features require BloodHound v8.0+.
- `cypher_query` supports saved-query management (`saved_list`, `saved_get`).
- Direct-Neo4j access (bypassing the REST API) is listed as a future roadmap item upstream; current
  path is REST-API only.

### Sources for this reference

- https://github.com/mwnickerson/bloodhound_mcp (README — tool list, env vars, run command, setup), captured 2026-05-29
- https://bloodhound.specterops.io/get-started/quickstart/community-edition-quickstart (CE standup via BloodHound CLI), captured 2026-05-29

## See also
- `sharphound` — the collector that feeds BloodHound (step 1 of the loop)
- `bloodhound-ingest`, `bloodhound-cypher-reference`, `bloodhound-custom-queries` — graph ingest + query knowledge
- `certify`, `adcs-esc4`, `sharpgpoabuse`, `laps-abuse` — TTPs the path analysis routes Sage toward
