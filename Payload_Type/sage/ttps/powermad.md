---
name: Powermad
category: acl-abuse
subcategories: [machine-account-creation, rbcd, dns-abuse]
tradecraft_tags: [machine-account, rbcd, powershell, dns, computer-account, harmj0y-adjacent]
mitre_attack:
  - id: T1098
    name: Account Manipulation
source:
  url: https://github.com/Kevin-Robertson/Powermad
  license: MIT
  maintained: false
binary_type: powershell-script
binary_filename: Powermad.ps1
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  Machine account creation generates Event 4741 (computer account created) on the DC.
  DNS record manipulation (via New-MachineAccount with DNS) generates DNS audit events.
  LDAP writes from the Powermad PowerShell module are visible in DC LDAP logs.
usage_examples:
  - description: Create a new machine account (MAQ must be > 0)
    args: "New-MachineAccount -MachineAccount mypc01 -Password (ConvertTo-SecureString 'P@ssw0rd1!' -AsPlainText -Force)"
  - description: Set RBCD on a target using the new machine account
    args: "Set-MachineAccountAttribute -MachineAccount VICTIM$ -Attribute msDS-AllowedToActOnBehalfOfOtherIdentity -Value (Get-MachineAccountSID -MachineAccount mypc01)"
  - description: Check machine account quota
    args: "Get-ADObject (Get-ADDomain).DistinguishedName -Properties 'ms-DS-MachineAccountQuota'"
opsec_notes: |
  PowerShell script — requires AMSI bypass first. Machine account creation is audited.
  StandIn (.NET assembly) is preferred over Powermad for Apollo engagements (inline_assembly
  vs powershell_import). Powermad is documented for completeness and as the original
  reference tool for machine account creation in RBCD attack chains.
gotchas: |
  PowerShell — requires AMSI bypass (same as PowerView). MAQ must be > 0. The RBCD
  attribute write is a separate operation from account creation; use StandIn's `--rbcd`
  flag for a combined .NET workflow. Powermad is not actively maintained.
related_ttps: [standin, krbrelay, sharpkrbrelay, rubeus, whisker]
alternatives: [standin, impacket-addcomputer]
common_args:
  New-MachineAccount:
    description: Create a new domain machine account
    typical_values: ["-MachineAccount mypc01 -Password (ConvertTo-SecureString 'P@ssw0rd1!' -AsPlainText -Force)"]
  -MachineAccount:
    description: Name for the new machine account (without $)
    typical_values: ["mypc01", "attacker-machine"]
  -Password:
    description: Password as SecureString
    typical_values: ["(ConvertTo-SecureString 'P@ssw0rd1!' -AsPlainText -Force)"]
last_updated: 2026-05-29
---

# Powermad

Kevin Robertson's PowerShell module for machine account creation and DNS manipulation.
The original reference tool for creating machine accounts programmatically as part of
RBCD attack chains. Powermad is the PowerShell equivalent of StandIn's `--computer`
functionality. Documented for completeness; for Apollo engagements, StandIn (.NET assembly)
is preferred over powershell_import-based Powermad.

## Typical use cases
- Create a machine account for RBCD setup when StandIn is unavailable
- Check machine account quota (MAQ) before attempting RBCD-based attacks
- DNS record manipulation for coercion setups

## How Sage uses this
Powermad is the PowerShell fallback for machine account creation. StandIn is preferred
for Apollo (inline_assembly delivery). Powermad is imported via `powershell_import`
if StandIn fails or is blocked.

## Output
PowerShell object output — machine account creation confirmation, attribute values.
