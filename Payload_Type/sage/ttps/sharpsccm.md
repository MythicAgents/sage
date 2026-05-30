---
name: SharpSCCM
category: recon
subcategories: [sccm-abuse, lateral-movement, credential-access]
tradecraft_tags: [sccm, mecm, configmgr, lateral-movement, cred-access, dotnet]
mitre_attack:
  - id: T1072
    name: Software Deployment Tools
source:
  url: https://github.com/Mayyhem/SharpSCCM
  license: MIT
  maintained: true
binary_type: .net-assembly
binary_filename: SharpSCCM.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  SCCM queries generate WMI calls to the SCCM management point. Unusual WMI queries
  from non-SCCM-admin accounts are detectable. Script deployment via SCCM generates
  events in SCCM logs (SMSProv.log, smscliui.log). Network connections to the SCCM
  management point from unexpected sources.
usage_examples:
  - description: Enumerate SCCM management points and site info
    args: "local site"
  - description: Find all SCCM clients (managed devices)
    args: "get device"
  - description: Get all SCCM collection memberships
    args: "get collection"
  - description: Deploy a script to a device collection for lateral movement
    args: "invoke admin -sms SERVER -sc SITE_CODE -d DEVICE_NAME -p cmd.exe -a '/c whoami'"
  - description: Dump SCCM network access account credentials (NAA)
    args: "local naa"
opsec_notes: |
  SCCM deployment is a high-signal action — it creates scheduled scripts, deployment
  events, and leaves logs in SCCM's database. Network Access Account (NAA) credential
  dump (`local naa`) is valuable but DPAPI-protected. Only relevant in environments
  with Microsoft Endpoint Configuration Manager (SCCM/MECM) deployed.
gotchas: |
  Requires SCCM to be deployed in the environment (not universally present). Admin-level
  SCCM operations (device script deployment) require SCCM Full Administrator role.
  Non-admin users can still enumerate. The `local naa` command extracts SCCM Network
  Access Account credentials stored in WMI/DPAPI on managed clients — these are often
  domain service accounts with broad read access.
related_ttps: [seatbelt, powerview, sharphound]
alternatives: [sccmwtf, crackmapexec-sccm]
common_args:
  local:
    description: Sub-command for local client operations
    typical_values: [site, naa, secrets]
  get:
    description: Sub-command for SCCM data retrieval
    typical_values: [device, collection, application, boundary]
  invoke:
    description: Sub-command for active operations (deployment)
    typical_values: [admin]
  -sms:
    description: SCCM management point server
    typical_values: ["SCCMSERVER01"]
  -sc:
    description: SCCM site code
    typical_values: ["PS1", "CM1"]
last_updated: 2026-05-29
---

# SharpSCCM

A .NET assembly for enumerating and abusing Microsoft Endpoint Configuration Manager
(SCCM/MECM, formerly System Center Configuration Manager). In environments where SCCM
is deployed, it represents a massive lateral movement primitive — administrators can
deploy scripts to any managed device. SharpSCCM also extracts Network Access Account
(NAA) credentials stored locally on SCCM clients, which are often domain service accounts.

## Typical use cases
- Enumerate SCCM management points, device collections, and managed devices
- Extract Network Access Account credentials (domain creds stored on all SCCM clients)
- Deploy scripts to device collections for lateral movement (requires SCCM admin role)
- Discover SCCM-managed infrastructure for lateral movement target identification

## How Sage uses this
SharpSCCM is relevant in enterprises that use SCCM/MECM. The NAA credential extraction
is particularly valuable — it requires only local access to an SCCM-managed machine and
can yield domain service account credentials. In SCCM-admin contexts, device script
deployment is one of the broadest lateral movement tools available (affects entire device collections).

## Output
Text output with enumerated SCCM objects or extracted credential material.
