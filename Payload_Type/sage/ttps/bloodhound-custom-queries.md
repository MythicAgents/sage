---
name: BloodHound Custom Query Library
category: recon
subcategories: [bloodhound, cypher, custom-queries, operational-queries]
tradecraft_tags: [bloodhound, cypher, neo4j, attack-path, custom, operational, adcs, delegation]
mitre_attack: []
source:
  url: https://github.com/CompassSecurity/BloodHoundQueries
  license: Unknown
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [linux, windows, macos]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  BloodHound runs on operator infrastructure — no detection signal.
usage_examples:
  - description: Load custom queries into BloodHound CE
    args: "(BloodHound CE UI: Custom Queries → import JSON)"
opsec_notes: |
  Operator-side tooling. No network contact with target.
gotchas: |
  Query syntax differs between BloodHound Legacy (neo4j 3.x) and BloodHound CE (5.x).
  CE uses updated property names in some cases. Test queries before relying on results.
  ADCS-specific queries require BloodHound CE with ADCS data collection (Certipy find -bloodhound).
related_ttps: [bloodhound-ingest, bloodhound-cypher-reference, sharphound, certipy]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# BloodHound Custom Query Library

Operational Cypher queries for BloodHound CE beyond the built-in set. These queries
cover ADCS paths, cross-domain exploitation, GPO abuse discovery, and other scenarios
that built-in queries miss.

## ADCS Attack Path Queries

```cypher
-- Find all users/computers that can enroll in any certificate template
MATCH (n)-[:Enroll|GenericAll|AllExtendedRights]->(t:GPO)
WHERE t.type = 'CertTemplate'
RETURN n.name, t.name, t.enrolleeSuppliesSubject

-- ESC1 vulnerable templates (enrollee supplies subject)
MATCH (t:GPO {type:'CertTemplate', enrolleeSuppliesSubject:true})
MATCH (p)-[:Enroll|GenericAll|AllExtendedRights]->(t)
RETURN p.name, t.name ORDER BY p.name

-- Find owned principals that can enroll in ESC1 templates
MATCH (u)-[:Enroll|GenericAll|AllExtendedRights]->(t:GPO {type:'CertTemplate', enrolleeSuppliesSubject:true})
WHERE u.owned = true
RETURN u.name, t.name

-- Certificate Authorities and their templates
MATCH (ca:GPO {type:'EnterpriseCA'})-[:IssuedSignedBy|EnrollsIn]->(t)
RETURN ca.name, t.name
```

## High-Value Target Discovery

```cypher
-- Find DA users with active sessions (where to find DA tokens)
MATCH (u:User {admincount:true})-[:HasSession]->(c:Computer)
RETURN u.name, c.name ORDER BY u.name

-- Find computers with DA sessions accessible from owned machines
MATCH (owned:Computer {owned:true})-[:AdminTo|CanRDP]->(pivot)
MATCH (da:User {admincount:true})-[:HasSession]->(pivot)
RETURN owned.name, pivot.name, da.name

-- Shortest paths from any user to Enterprise Admins
MATCH (u:User), p=shortestPath((u)-[*1..]->(g:Group {name:'ENTERPRISE ADMINS@SEVENKINGDOMS.LOCAL'}))
WHERE u.owned = true
RETURN p LIMIT 10

-- All kerberoastable accounts in admin groups
MATCH (u:User {hasspn:true})
MATCH (u)-[:MemberOf*1..]->(g:Group)
WHERE g.name CONTAINS 'ADMIN' OR g.admincount = true
RETURN DISTINCT u.name, g.name
```

## ACL Abuse Discovery

```cypher
-- All ACL edges from owned principals (full depth)
MATCH p=(u)-[:GenericAll|GenericWrite|WriteDACL|WriteOwner|Owns|AddSelf|AddMember|ForceChangePassword|AllowedToDelegate|ReadLAPSPassword|AllowedToAct*1..5]->(t)
WHERE u.owned = true AND NOT t.owned = true
RETURN p LIMIT 50

-- GPO abuse paths from owned to interesting scopes
MATCH (u)-[:GenericAll|GenericWrite|WriteDACL|WriteOwner]->(g:GPO)-[:GpLink]->(ou)
WHERE u.owned = true
RETURN u.name, g.name, ou.name

-- Find all objects where owned users have WriteDACL
MATCH (u:User {owned:true})-[:WriteDACL]->(t)
RETURN u.name, t.name, labels(t)
```

## Delegation Discovery

```cypher
-- Unconstrained delegation paths from owned machines
MATCH (c:Computer {unconstraineddelegation:true})
WHERE NOT c.name STARTS WITH 'DC' AND NOT c.name CONTAINS 'DOMAIN CONTROLLER'
RETURN c.name, c.operatingsystem

-- Constrained delegation (protocol transition) accounts
MATCH (u)-[:AllowedToDelegate]->(b:Computer)
RETURN u.name, b.name, labels(u)

-- RBCD writable paths from owned principals
MATCH p=(a)-[:GenericAll|GenericWrite|WriteDACL]->(b:Computer)
WHERE a.owned = true
RETURN p LIMIT 25
```

## Cross-Domain Paths

```cypher
-- Foreign group memberships (cross-domain)
MATCH (u:User)-[:MemberOf]->(g:Group)
WHERE u.domain <> g.domain
RETURN u.name, u.domain, g.name, g.domain

-- Trust paths between domains
MATCH (d1:Domain)-[r:TrustedBy]->(d2:Domain)
RETURN d1.name, type(r), d2.name

-- Cross-domain attack paths
MATCH p=shortestPath((u:User)-[*1..]->(g:Group))
WHERE u.owned = true AND g.domain <> u.domain AND g.admincount = true
RETURN p LIMIT 10
```

## Housekeeping / Marking

```cypher
-- Mark multiple principals as owned at once:
MATCH (u:User) WHERE u.name IN ['JON.SNOW@NORTH.SEVENKINGDOMS.LOCAL','ATTACKER@...']
SET u.owned = true
RETURN u.name

-- Clear all owned flags:
MATCH (n) WHERE n.owned = true SET n.owned = false
RETURN count(n)
```
