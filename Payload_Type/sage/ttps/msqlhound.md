---
name: MSSQLHound
category: recon
subcategories: [mssql-enumeration, sql-attack-paths, bloodhound-extension]
tradecraft_tags: [mssql, sql-server, bloodhound, attack-path, linked-servers, specterops, golang]
mitre_attack:
  - id: T1505.001
    name: Server Software Component — SQL Stored Procedures
source:
  url: https://github.com/SpecterOps/MSSQLHound
  license: Unknown
  maintained: true
binary_type: native-exe
binary_filename: mssqlhound
supported_os: [linux, windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  MSSQLHound connects to MSSQL instances and enumerates linked server chains,
  database permissions, and login mappings. SQL Server audit logging captures
  all queries. Unusual SQL enumeration from non-application accounts is anomalous.
usage_examples:
  - description: Enumerate MSSQL instances and output BloodHound data
    args: "mssqlhound -domain north.sevenkingdoms.local -username jon.snow -password Password123 -dc-ip 192.168.56.10"
  - description: Target a specific SQL server
    args: "mssqlhound -target sqlsrv01.north.sevenkingdoms.local -username jon.snow -password Password123"
opsec_notes: |
  MSSQLHound adds MSSQL attack paths to BloodHound CE — linked server chains can
  create attack paths from low-privilege SQL logins to DBA-level access or even
  OS-level code execution via xp_cmdshell chains. The tool connects to SQL servers
  and enumerates trust chains, adding MSSQL nodes and edges to the BloodHound graph.
  Go binary — not Apollo-compatible; infrastructure-side.
gotchas: |
  Native Go binary — not Apollo-compatible. Requires BloodHound CE for data ingestion.
  SQL Server audit logging captures all enumeration queries. Most valuable in environments
  with complex MSSQL linked-server topologies (common in large enterprises).
  PowerUpSQL is the Apollo-compatible (.NET / PowerShell) alternative for SQL attacks.
related_ttps: [powerupsql, sharphound, bloodhound-ingest]
alternatives: [powerupsql, impacket-mssqlclient]
common_args:
  -domain:
    description: Target domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
  -username:
    description: Authentication username
    typical_values: ["jon.snow"]
    required: true
  -password:
    description: Authentication password
    typical_values: ["Password123"]
  -dc-ip:
    description: Domain controller IP for LDAP SPN discovery
    typical_values: ["192.168.56.10"]
  -target:
    description: Specific SQL server to target
    typical_values: ["sqlsrv01.north.sevenkingdoms.local"]
last_updated: 2026-05-29
---

# MSSQLHound

SpecterOps' Go-based MSSQL enumeration tool that outputs attack paths for BloodHound CE.
MSSQLHound discovers MSSQL instances via SPN enumeration (LDAP), connects to each,
maps linked-server chains, database permissions, and login-to-user mappings, then
exports this as BloodHound-compatible graph data.

## Why MSSQL Attack Paths Matter

MSSQL linked-server chains can enable privilege escalation:
```
Attack path:
  low-priv user → MSSQL login → linked server → execute-as → xp_cmdshell → OS code exec
  
In BloodHound:
  [User] --SQLLoginAs--> [MSSQL Instance] --LinkedServerTo--> [MSSQL Instance] --ExecuteAs--> [SA] --OSCommand--> [Server]
```

## Integration with BloodHound CE

```
1. Run MSSQLHound → exports JSON
2. Import into BloodHound CE → nodes for MSSQL instances + edges
3. Query for MSSQL attack paths:
   MATCH p=(u:User)-[:SQLLoginAs*1..]->(i:MSSQL)-[:LinkedServerTo*1..]->(target:MSSQL)
   WHERE u.owned = true AND target.hasXPCmdshell = true
   RETURN p
```

## Apollo-specific note
Go native binary — not Apollo-compatible. Run from attacker infrastructure.
PowerUpSQL is the Apollo inline_assembly equivalent for MSSQL attacks.
