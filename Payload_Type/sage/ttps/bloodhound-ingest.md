---
name: BloodHound (CE/Legacy ingest)
category: recon
subcategories: [attack-path-analysis, graph-analysis, ad-visualization]
tradecraft_tags: [bloodhound, graph, attack-path, neo4j, sharphound, acl, delegation]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/SpecterOps/BloodHound
  license: Apache-2.0
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [linux, windows, macos]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  BloodHound itself is the operator's analysis tool — it does not run on the target.
  Collection (SharpHound, RustHound, etc.) is what generates detection signals.
  BloodHound CE (Community Edition) is a web application that runs on the attacker's
  infrastructure.
usage_examples:
  - description: Ingest SharpHound ZIP into BloodHound CE
    args: "Upload Data → select SharpHound ZIP → Import"
  - description: Find shortest path from any owned principal to Domain Admins
    args: "Cypher query: MATCH p=shortestPath((u)-[*1..]->(g:Group {name:'DOMAIN ADMINS@NORTH.SEVENKINGDOMS.LOCAL'})) WHERE u.owned=true RETURN p"
  - description: Find all ACL paths from controlled users to DA
    args: "Built-in: Shortest Paths to Domain Admins from Owned Principals"
opsec_notes: |
  BloodHound is an operator-side analysis tool — runs on attacker infrastructure.
  No detection risk from BloodHound itself. Ensure the SharpHound ZIP file does not
  contain identifiable information about the attacker's system path.
gotchas: |
  BloodHound CE uses different data schema from BloodHound Legacy (v4). SharpHound v2.x
  outputs CE-compatible data; SharpHound v1.x outputs Legacy-compatible data. Do NOT
  import v2.x data into Legacy BloodHound or vice versa. BloodHound CE requires Docker
  or a manual installation (neo4j + BloodHound binary + postgres). After import, manually
  mark controlled/owned principals as "owned" in the UI to enable path queries from owned.
  Cypher queries provide much more flexibility than built-in queries for complex scenarios.
related_ttps: [sharphound, sharphound4cme, rusthound, powerview]
alternatives: [plumhound, bloodhound-legacy]
common_args: {}
last_updated: 2026-05-29
---

# BloodHound (CE/Legacy ingest)

BloodHound is the attack path analysis platform — the graph database and UI that
consumes SharpHound's collection output and renders Active Directory relationships as
an exploitable graph. This TTP file documents the *ingest and analysis* side (not the
collection side; see sharphound, rusthound). BloodHound Community Edition (CE) is the
current open-source version maintained by SpecterOps; the legacy version runs on neo4j
3.x. The "shortest path to DA" query is often the first thing operators run after
SharpHound output is available.

## Typical use cases
- Find shortest attack path from initial foothold to Domain Admins
- Identify ACL-based privilege escalation opportunities (GenericWrite, WriteDACL chains)
- Discover Kerberos delegation configurations (constrained, unconstrained, RBCD candidates)
- Map foreign group memberships and cross-forest trust paths
- Identify kerberoastable and AS-REP-roastable accounts

## How Sage uses this
BloodHound is operator-side infrastructure. Sage collects data via SharpHound, reports
the output ZIP to the operator, and may execute targeted Cypher queries if a BloodHound
API is available. Sage does not currently host or query a BloodHound instance directly —
output is reported to the operator for manual BloodHound analysis. This may change in
future Sage versions that integrate with BloodHound's REST API.

## Output
BloodHound UI graph visualization and Cypher query results. Attack paths are displayed
as directed graphs with hop-by-hop edge labels (GenericAll, CanRDP, MemberOf, etc.).

## Notes on BloodHound CE vs Legacy

| Feature | BloodHound CE | BloodHound Legacy |
|---------|--------------|-----------------|
| Backend | neo4j + postgres | neo4j only |
| SharpHound compat | v2.x | v1.x |
| API | REST API available | Limited |
| Hosting | Docker-compose | Manual neo4j |
| Cypher | Full support | Full support |
| Built-in queries | Expanded | Classic |

## Key Cypher queries

```cypher
-- Shortest path from owned principals to DA
MATCH p=shortestPath((u)-[*1..]->(g:Group {name:'DOMAIN ADMINS@DOMAIN.LOCAL'}))
WHERE u.owned=true RETURN p

-- All ACL abuse paths from user to computer
MATCH p=(u:User)-[:GenericAll|GenericWrite|WriteDACL|WriteOwner|Owns*1..]->(c:Computer) RETURN p

-- Find computers with unconstrained delegation (not DCs)
MATCH (c:Computer {unconstraineddelegation:true}) WHERE NOT c.name STARTS WITH 'DC' RETURN c

-- Kerberoastable accounts with DA path
MATCH (u:User {hasspn:true}) MATCH p=shortestPath((u)-[*1..]->(g:Group {name:'DOMAIN ADMINS@...'})) RETURN p
```
