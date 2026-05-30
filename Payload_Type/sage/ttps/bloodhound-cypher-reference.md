---
name: BloodHound Cypher Query Reference
category: recon
subcategories: [bloodhound, cypher, attack-path, query-reference]
tradecraft_tags: [bloodhound, cypher, neo4j, attack-path, graph-query, reference]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/SpecterOps/BloodHound
  license: GPL-3.0
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [linux, windows, macos]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  BloodHound runs on operator infrastructure — no detection signal. Queries are local
  to the BloodHound database.
usage_examples:
  - description: Shortest path from owned principals to DA
    args: "MATCH p=shortestPath((u)-[*1..]->(g:Group {name:'DOMAIN ADMINS@DOMAIN.LOCAL'})) WHERE u.owned=true RETURN p"
  - description: All ACL paths from owned users
    args: "MATCH p=(u:User)-[:GenericAll|GenericWrite|WriteDACL|WriteOwner|Owns*1..]->(t) WHERE u.owned=true RETURN p"
  - description: Computers with unconstrained delegation
    args: "MATCH (c:Computer {unconstraineddelegation:true}) WHERE NOT c.name STARTS WITH 'DC' RETURN c"
  - description: Kerberoastable accounts with paths to DA
    args: "MATCH (u:User {hasspn:true}), p=shortestPath((u)-[*1..]->(g:Group {name:'DOMAIN ADMINS@DOMAIN.LOCAL'})) RETURN p"
opsec_notes: |
  BloodHound is operator infrastructure. Query the local BloodHound database — no target network contact.
gotchas: |
  Reference document only. Domain name in queries must match what's in BloodHound (uppercase, e.g. DOMAIN.LOCAL not domain.local). MATCH p=shortestPath can be slow on large graphs — add limits (LIMIT 25).
related_ttps: [bloodhound-ingest, sharphound, certipy]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# BloodHound Cypher Query Reference

A curated library of Cypher queries for BloodHound CE attack-path analysis. All queries
run against the local BloodHound database — no network contact with the target.

## Fundamental Queries

### Mark owned principals
```cypher
MATCH (u:User {name:'ATTACKER@DOMAIN.LOCAL'})
SET u.owned = true
RETURN u
```

### Shortest path from any owned to DA
```cypher
MATCH p=shortestPath(
  (u)-[*1..]->(g:Group {name:'DOMAIN ADMINS@DOMAIN.LOCAL'})
)
WHERE u.owned = true
RETURN p LIMIT 25
```

### All paths (not just shortest) from owned to DA
```cypher
MATCH p=(u)-[*1..10]->(g:Group {name:'DOMAIN ADMINS@DOMAIN.LOCAL'})
WHERE u.owned = true
RETURN p LIMIT 50
```

## Delegation Queries

```cypher
-- Unconstrained delegation (non-DC):
MATCH (c:Computer {unconstraineddelegation:true})
WHERE NOT c.name STARTS WITH 'DC'
RETURN c.name, c.operatingsystem

-- Constrained delegation chains:
MATCH p=(a)-[:AllowedToDelegate]->(b:Computer)
RETURN p LIMIT 50

-- RBCD-writable computers from owned:
MATCH p=(a)-[:GenericWrite|GenericAll|WriteDACL]->(b:Computer)
WHERE a.owned = true
RETURN p LIMIT 50
```

## ADCS Queries (requires BloodHound CE ADCS collection)

```cypher
-- ESC1 vulnerable templates:
MATCH (t:GPO) WHERE t.type='CertTemplate' AND t.enrolleesuppliessubject = true
RETURN t.name, t.template

-- Principals with enrollment rights on ESC1 templates:
MATCH (p)-[:Enroll|GenericAll|GenericWrite]->(t:GPO)
WHERE t.type='CertTemplate' AND t.enrolleesuppliessubject = true
RETURN p.name, t.name
```

## Kerberoastable Accounts

```cypher
-- Kerberoastable with path to DA:
MATCH (u:User {hasspn:true})
MATCH p=shortestPath((u)-[*1..]->(g:Group {name:'DOMAIN ADMINS@DOMAIN.LOCAL'}))
RETURN u.name, p LIMIT 25

-- All kerberoastable accounts with admin count:
MATCH (u:User {hasspn:true})
RETURN u.name, u.admincount, u.description
ORDER BY u.admincount DESC
```

## LAPS Queries

```cypher
-- Which owned principals can read LAPS?
MATCH p=(u)-[:ReadLAPSPassword]->(c:Computer)
WHERE u.owned = true
RETURN p LIMIT 50

-- All computers with LAPS enabled:
MATCH (c:Computer {haslaps:true})
RETURN c.name
```

## Foreign Principals (Cross-Domain)

```cypher
-- Foreign principals in sensitive groups:
MATCH (u:User)-[:MemberOf]->(g:Group)
WHERE u.domain <> g.domain
RETURN u.name, g.name

-- Paths from foreign principals to domain admin:
MATCH (u:User)
WHERE u.foreign = true
MATCH p=shortestPath((u)-[*1..]->(g:Group {name:'DOMAIN ADMINS@TARGET.LOCAL'}))
RETURN p LIMIT 25
```

## Session-Based Queries (from session collection)

```cypher
-- Where do Domain Admins have sessions?
MATCH (u:User)-[:HasSession]->(c:Computer)
WHERE u.admincount = true
RETURN u.name, c.name

-- Computers with DA sessions that we can reach:
MATCH (u:User {admincount:true})-[:HasSession]->(c:Computer)
MATCH (owned)-[:AdminTo|HasSession]->(c)
WHERE owned.owned = true
RETURN DISTINCT c.name, u.name
```
