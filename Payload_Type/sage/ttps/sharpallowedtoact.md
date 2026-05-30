---
name: SharpAllowedToAct
category: acl-abuse
subcategories: [rbcd, msds-allowedtoact, computer-object-delegation]
tradecraft_tags: [rbcd, allowedtoact, delegation, acl-abuse, dotnet, apollo-runnable]
mitre_attack:
  - id: T1098
    name: Account Manipulation
source:
  url: https://github.com/EspressoCake/HandleKatz
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpAllowedToAct.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Writes to msDS-AllowedToActOnBehalfOfOtherIdentity on a computer object generate
  Event 5136 (directory service object modification) on the DC. MDI monitors RBCD
  attribute writes. Same detection surface as StandIn's --rbcd flag.
usage_examples:
  - description: Write RBCD — allow ATTACKER$ to act on behalf of users to VICTIM$
    args: "SharpAllowedToAct.exe VICTIM$ ATTACKER$"
  - description: Remove RBCD entry from a target
    args: "SharpAllowedToAct.exe VICTIM$ ATTACKER$ remove"
  - description: List existing RBCD delegations on a target
    args: "SharpAllowedToAct.exe VICTIM$ list"
opsec_notes: |
  SharpAllowedToAct is functionally equivalent to StandIn's --rbcd flag — they both
  write msDS-AllowedToActOnBehalfOfOtherIdentity on the target. Prefer StandIn over
  SharpAllowedToAct for Apollo engagements since StandIn is better maintained and
  also handles machine account creation in the same tool.
gotchas: |
  Requires GenericWrite or higher on the target computer object. Prefer StandIn for
  a single-tool RBCD workflow (StandIn creates the machine account AND writes RBCD).
  SharpAllowedToAct is a focused single-purpose tool — it only writes the RBCD
  attribute; machine account creation is separate.
related_ttps: [standin, rubeus, krbrelay, krbrelayup]
alternatives: [standin-rbcd, powermad-rbcd, impacket-rbcd-attack]
common_args:
  target:
    description: Target computer account$ to write RBCD on
    typical_values: ["WINTERFELL$", "VICTIM$"]
    required: true
  attacker:
    description: Attacker-controlled account to write into delegation attribute (sAMAccountName with $)
    typical_values: ["ATTACKER$", "mypc01$"]
    required: true
  remove:
    description: Remove the RBCD entry instead of adding it
    typical_values: [flag-only]
  list:
    description: List existing RBCD delegations on the target
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpAllowedToAct

A focused .NET assembly for reading and writing the `msDS-AllowedToActOnBehalfOfOtherIdentity`
attribute on computer objects (the RBCD delegation attribute). It provides a simpler
interface than StandIn for the specific RBCD-write operation when machine account creation
is handled separately.

## Typical use cases
- Write RBCD delegation when a machine account is already available
- Remove RBCD delegations for cleanup
- List existing RBCD on a target

## How Sage uses this
StandIn is Sage's primary RBCD tool (handles both machine account creation and RBCD
write). SharpAllowedToAct is the fallback when StandIn isn't available or when only
the RBCD write step is needed.

## Output
Console confirmation of the RBCD attribute write operation.
