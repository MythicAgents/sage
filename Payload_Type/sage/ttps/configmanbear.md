---
name: ConfigManBearPig (SCCM BloodHound Collector)
category: recon
subcategories: [sccm-attack-paths, bloodhound-extension, configmgr]
tradecraft_tags: [sccm, mecm, configmgr, bloodhound, attack-path, specterops, powershell]
mitre_attack:
  - id: T1072
    name: Software Deployment Tools
source:
  url: https://github.com/SpecterOps/ConfigManBearPig
  license: Unknown
  maintained: true
binary_type: powershell-script
binary_filename: ConfigManBearPig.ps1
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  PowerShell queries to SCCM management points and LDAP queries for SCCM-related
  objects. SCCM audit logging captures enumeration requests.
usage_examples:
  - description: Collect SCCM data for BloodHound CE
    args: "Import-Module ConfigManBearPig.ps1; Invoke-ConfigManBearPig"
  - description: Target specific SCCM environment
    args: "Invoke-ConfigManBearPig -SMSProvider SCCMSRV01.domain.local"
opsec_notes: |
  ConfigManBearPig is SpecterOps' BloodHound OpenGraph collector for SCCM. It maps
  SCCM device collections, administrative users, roles, and push installation accounts
  into BloodHound's graph — revealing attack paths from SCCM admin to all managed devices.
  Combined with SharpSCCM and SCCMHunter, this provides complete SCCM attack surface coverage.
gotchas: |
  PowerShell module — requires AMSI bypass before loading. Requires SCCM access.
  For Apollo: use powershell_import with AMSI bypass first. Most valuable in
  environments with many SCCM-managed endpoints where the lateral movement potential
  is high.
related_ttps: [sharpsccm, sccmhunter, sccmwtf, bloodhound-ingest]
alternatives: [sharpsccm, sccmhunter]
common_args:
  -SMSProvider:
    description: SCCM SMS Provider hostname
    typical_values: ["SCCMSRV01.domain.local"]
last_updated: 2026-05-29
---

# ConfigManBearPig

SpecterOps' BloodHound OpenGraph collector for Microsoft Endpoint Configuration Manager
(SCCM/MECM). Maps SCCM relationships into BloodHound CE for attack path analysis —
showing how SCCM admin roles translate into lateral movement opportunities against
all managed endpoints.

## SCCM BloodHound Attack Paths

```
BloodHound SCCM nodes:
  [SCCM Site] → [Device Collections] → [Devices]
  [SCCM Admin Role] → [User] → lateral movement to all collection devices

Key attack path:
  Compromise account with SCCM Full Administrator →
  Deploy payload to All Systems collection →
  Code execution on EVERY managed device in the organization
```

## Integration with Other SCCM Tools

| Tool | Purpose | Approach |
|------|---------|---------|
| ConfigManBearPig | Attack path mapping | BloodHound graph + query |
| SharpSCCM | Targeted SCCM attacks (NAA, deployment) | Apollo inline_assembly |
| SCCMHunter | Initial SCCM discovery (Python) | Infrastructure-side |
| sccmwtf | Reference for attack techniques | Technique documentation |
