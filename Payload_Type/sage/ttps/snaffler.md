---
name: Snaffler
category: discovery
subcategories: [file-hunting, share-enumeration, credential-discovery]
tradecraft_tags: [file-hunting, shares, credentials, lateral-movement-prep, dotnet]
mitre_attack:
  - id: T1083
    name: File and Directory Discovery
  - id: T1039
    name: Data from Network Shared Drive
source:
  url: https://github.com/SnaffCon/Snaffler
  license: GPL-3.0
  maintained: true
binary_type: .net-assembly
binary_filename: Snaffler.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Snaffler accesses file shares across the domain — each share enumeration generates SMB
  connection events and file-listing traffic. Heavy SMB enumeration from a single workstation
  is anomalous. File read events on sensitive files (if accessed, not just listed) appear
  in Windows Security logs and Sysmon. Network monitoring sees rapid SMB connections to
  multiple hosts.
usage_examples:
  - description: Enumerate all shares and hunt for credential files (default rules)
    args: "-s -o C:\\Windows\\Temp\\snaffler.log"
  - description: Target only specific hosts
    args: "-s -n DC01,FILESERVER01 -o C:\\Windows\\Temp\\snaffler.log"
  - description: Search for specific file patterns
    args: "-s --include-extensions conf,config,ini,xml,txt -o snaffler.log"
  - description: Run in domain context with specific user
    args: "-s -d north.sevenkingdoms.local -o snaffler.log"
opsec_notes: |
  Snaffler's default behavior accesses all enumerable shares on all domain computers —
  this is extremely noisy (hundreds of SMB connections in a short timeframe). For stealth,
  target specific known-valuable hosts with `-n`. The log file contains all discovered
  sensitive findings; protect it. Snaffler reads but does not modify files.
gotchas: |
  Default mode enumerates ALL domain computers and ALL accessible shares — very noisy.
  Always use `-n` to target specific hosts in sensitive engagements. Output log can be
  very large in large environments. Snaffler uses a classification system with configurable
  "triage rules" for what counts as interesting — customize for specific engagement goals.
  Read-only; does not exfiltrate files automatically.
related_ttps: [sharefinder, sharphound, seatbelt]
alternatives: [sharefinder, powerspy, manspider]
common_args:
  -s:
    name: -s
    description: Snaffler mode — enumerate shares and hunt for files (required)
    typical_values: [flag-only]
    required: true
  -o:
    name: -o
    description: Output log file path
    typical_values: ["C:\\\\Windows\\\\Temp\\\\snaffler.log"]
    required: true
  -n:
    name: -n
    description: Comma-separated target hostnames (limits scope)
    typical_values: ["DC01,FILESERVER01"]
  -d:
    name: -d
    description: Target domain (defaults to current)
    typical_values: ["north.sevenkingdoms.local"]
  -v:
    name: -v
    description: Log verbosity level (0-3)
    typical_values: [1]
last_updated: 2026-05-29
---

# Snaffler

A .NET assembly that hunts for sensitive files across SMB shares in an Active Directory
domain. Snaffler enumerates domain computers, connects to their accessible shares, and
applies configurable triage rules to identify interesting files (credentials, private keys,
configuration files with passwords, SSH keys, etc.) without downloading them. It's the
"automated interesting-file-hunter" that makes post-foothold lateral movement prep faster.

## Typical use cases
- Systematically hunt for credentials and secrets stored in accessible network shares
- Discover configuration files, private keys, and passwords across the domain
- Find password files, KeePass databases, and credential stores on file servers
- Identify sensitive data for escalation or lateral movement planning

## How Sage uses this
Snaffler is a post-foothold discovery step. After Sage establishes domain user access,
Snaffler identifies credential material in shares that can fuel further escalation or
lateral movement without needing to touch LSASS.

## Output
Log file with one entry per interesting file found. Each entry includes the host,
share path, filename, and classification reason (e.g. "contains password keyword",
"private key file", "KeePass database"). Output is human-readable and machine-parseable.
