---
name: BloodHound Attack-Path Loop
category: recon
subcategories: [attack-path-analysis, workflow, playbook, bloodhound, graph-reasoning]
tradecraft_tags: [bloodhound, attack-path, workflow, playbook, sharphound, graph-reasoning, loop, autonomous, mcp]
mitre_attack: []
source:
  url: https://bloodhound.specterops.io
  license: Unknown
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows, linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  This playbook's only target-side noise is the SharpHound collection it begins with
  (loud LDAP/SAMR — see the sharphound TTP) and whatever exploitation TTPs the chosen
  path invokes. The analysis steps (ingest + graph queries) are operator-side and silent.
usage_examples:
  - description: Run the full loop autonomously from a foothold to Domain Admin
    args: "(workflow) SharpHound -> file_upload -> graph_analysis(start=foothold, target=Domain Admins) -> execute hop -> repeat"
  - description: Re-collect and re-query after capturing a new principal's credentials
    args: "(workflow) re-run SharpHound as/for the new principal -> file_upload -> graph_analysis from the new node"
  - description: Pivot the objective to cross-forest Domain Admin
    args: "(workflow) graph_analysis with target in the trusting forest; cypher_query for foreign-group / trust edges"
opsec_notes: |
  The loop front-loads the noise: SharpHound collection is the loud part. Once data is in
  BloodHound, all path reasoning is local to the operator and silent. Prefer a single broad
  SharpHound pass plus targeted re-collection over repeated full sweeps. Let the graph pick the
  QUIETEST sufficient path, not just the shortest — surface that tradeoff to the operator.
gotchas: |
  - BloodHound + its MCP must be up and connected first. If not, STOP and surface the standup
    guidance (see bloodhound-mcp) — do not attempt path queries against an empty/absent graph.
  - graph_analysis is only as good as the collection. Run data_quality after ingest; if a needed
    edge type (Sessions, ACL, Trusts) wasn't collected, re-run SharpHound with that method before
    trusting "no path found".
  - Each newly-compromised principal can unlock paths invisible before. Re-collect / re-query after
    every credential or SYSTEM gain rather than trusting the first graph.
  - "Shortest path" is not always "best path" — weigh OPSEC, determinism, and prerequisites.
related_ttps: [bloodhound-mcp, sharphound, bloodhound-cypher-reference, bloodhound-custom-queries, sharpgpoabuse, rubeus, nanodump, certify, laps-abuse, constrained-delegation-abuse, rbcd-abuse, adcs-esc4]
alternatives: [post-exploitation-playbook, lateral-movement-decision, manual-bloodhound-pathfinding]
common_args: {}
last_updated: 2026-05-29
---

# BloodHound Attack-Path Loop

The operational playbook that turns Sage's autonomous AD solve from a *scripted* chain into a
*reasoned* one. Instead of following a hard-coded sequence of hops, Sage collects the environment
with SharpHound, ingests it into BloodHound, and **asks the graph** what the path is — then executes
the next hop, re-collects as new access unlocks new edges, and repeats until the objective. This is
the Phase-1 T1.5 centerpiece and the single most demo-valuable behavior: the graph-reasoning step is
the explicit, narratable "why this path" moment in the Phoenix trace.

## Typical use cases
- Autonomously walk from a low-priv foothold to Domain Admin without a pre-scripted chain
- Re-plan mid-operation when a new credential or SYSTEM context unlocks new paths
- Switch objective (single-domain DA → cross-forest DA) and let the graph re-route
- Choose between competing paths on OPSEC / determinism, not just hop count

## How Sage uses this
The agents divide the loop: **Mythic_Operator** runs collection and executes hops on the target;
**MCP_Manager** drives the BloodHound MCP (ingest + graph queries); the **Supervisor** holds the
objective, picks the path from the graph's options, and routes each hop. The loop is the orchestration
contract between them.

## Output
A chosen, justified attack path (an ordered list of hops, each mapped to a concrete TTP and Mythic
command) plus the running result of executing it — and, at each decision point, the reasoning for
why that path was selected over alternatives.

## The loop (step by step)

```
            ┌────────────────────────────────────────────────────────┐
            │  0. PRECHECK: is BloodHound + its MCP connected?          │
            │     MCP_Manager.get_connected_servers() contains it?      │
            │     NO  -> surface standup guidance (see bloodhound-mcp), │
            │            ask operator to bring it up, then retry.       │
            └───────────────────────────┬────────────────────────────┘
                                        │ YES
   ┌────────────────────────────────────▼─────────────────────────────────┐
   │ 1. COLLECT   Mythic_Operator runs SharpHound on the foothold           │
   │              (sharphound TTP) and downloads the output ZIP.            │
   │ 2. INGEST    MCP_Manager calls file_upload with the ZIP;               │
   │              then data_quality to confirm the collection is complete.  │
   │ 3. REASON    MCP_Manager queries the graph:                            │
   │                graph_analysis (shortest path foothold -> objective)    │
   │                adcs_info      (ESC paths)                              │
   │                cypher_query   (foreign-group / trust / LAPS edges)     │
   │              Supervisor picks the path (see Decision points).          │
   │ 4. ACT       Supervisor routes the next hop to Mythic_Operator, which  │
   │              executes the mapped TTP (e.g. sharpgpoabuse, rubeus,       │
   │              certify, laps-abuse...).                                   │
   │ 5. ASSESS    New creds / SYSTEM / host? -> go to RE-COLLECT, else       │
   │              continue executing the remaining hops of the chosen path.  │
   └───────────────────────────┬────────────────────────────────────────┘
                               │ objective reached?  NO -> loop to step 1/3
                               ▼ YES -> report path + results to operator
```

## Decision points (where Sage reasons, not scripts)
At step 3, when the graph returns more than one viable path, the Supervisor chooses on:
- **Prerequisites met now** — does Sage already hold the access an edge needs, or must it gain it first?
- **OPSEC** — prefer the quieter primitive (e.g. a native Apollo command over a flagged GhostPack assembly; LAPS read over LSASS dump) when both reach the goal. The execution hint from `get_ttp_guidance` flags this.
- **Determinism** — avoid edges with timing hazards (GPO refresh, AdminSDHolder windows) when a deterministic alternative exists; note the tradeoff.
- **Objective fit** — single-domain DA vs cross-forest: pick the path that reaches the actual objective, even if a shorter path stops short.

Narrate the choice ("graph shows two paths to DA: GPO-abuse via STARKWALLPAPER, or constrained
delegation via jon.snow; choosing delegation — fewer moving parts, no gpupdate wait"). That narration
is the audit trail and the demo's reasoning beat.

## Re-collection triggers (step 5 → step 1)
Re-run SharpHound (often a focused pass) and re-ingest when Sage gains:
- a new principal's credentials / hash / ticket (new ownership edges)
- SYSTEM or local-admin on a new host (new Sessions / LocalAdmin edges)
- a foothold in a new domain or forest (new trust-side data — use `--SearchForest`)

Then re-query from the new node; paths invisible before often appear.

## Worked example (illustrative — GOAD public range)
Mapping the loop onto the GOAD "Trust Walker" arc (public training range; names only, no creds):
1. COLLECT as `samwell.tarly` on CASTELBLACK → SharpHound `-c All`.
2. INGEST → the collection comes back as a Mythic file artifact (a file UUID from the `download` task), but `file_upload` needs an on-disk PATH. MCP_Manager first calls `stage_file_to_disk(file_uuid)` to materialize it locally, then `file_upload(<staged path>)`; `data_quality` confirms ACL/GPO/Trust edges present.
3. REASON → `graph_analysis` surfaces GenericWrite on GPO `STARKWALLPAPER` → SYSTEM on WINTERFELL; `adcs_info` flags `ESC1` on ESSOS-CA; `cypher_query` shows the `Spys` foreign-group LAPS edge.
4. ACT → `sharpgpoabuse` (GPO hop) → `nanodump`/native dump for jon.snow → `rubeus` constrained delegation → walk `Spys` to `laps-abuse` on BRAAVOS → `certify` ESC1 → DA essos.
5. ASSESS → after each credential gain, re-collect/re-query; the path to cross-forest DA appears once the foreign-group edge is in the graph.

## Prerequisites & failure handling
- **BloodHound down** → step 0 catches it; surface `bloodhound-mcp` standup guidance, do not proceed.
- **Empty graph / no path found** → run `data_quality`; if collection is thin, re-run SharpHound with the missing collection method before concluding "no path".
- **Stale graph** → if execution results contradict the graph (e.g. an edge no longer works), re-collect.

## OPSEC considerations
Collection is the loud part; do it deliberately (one broad pass + targeted re-collection) rather than
repeatedly. Let the graph choose the quietest sufficient path, and in supervised mode surface the
OPSEC cost of each proposed hop in the approval prompt.

## See also
- `bloodhound-mcp` — the MCP server + standup guidance this loop depends on
- `sharphound` — the collector (step 1)
- `bloodhound-cypher-reference`, `bloodhound-custom-queries` — for `cypher_query` step 3
- `post-exploitation-playbook`, `lateral-movement-decision` — adjacent operator playbooks
