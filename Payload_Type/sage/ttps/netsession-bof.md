---
name: NetSession / NetLocalGroupGetMembers BOF
category: discovery
subcategories: [session-enum, local-group, bof, network-discovery]
tradecraft_tags: [bof, netsession, local-group, network-enum, samr, in-process, athena]
mitre_attack:
  - id: T1049
    name: System Network Connections Discovery
  - id: T1069.001
    name: Permission Groups Discovery — Local Groups
source:
  url: https://github.com/trustedsec/CS-Situational-Awareness-BOF
  license: BSD-3-Clause
  maintained: true
binary_type: bof
binary_filename: netsession.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: true
detection_signal: |
  NetSessionEnum (SMB SRVSVC) and NetLocalGroupGetMembers (SAMR or SRVSVC) are
  standard Windows network administration calls. However, bulk enumeration from a
  single non-admin workstation is anomalous. In-process BOF execution eliminates
  the child process (net.exe) but the SMB traffic itself is still visible.
usage_examples:
  - description: Enumerate active sessions on a target machine
    args: "execute-bof netsession.x64.o WINTERFELL"
  - description: Get local administrators on a target machine
    args: "execute-bof local_group_members.x64.o WINTERFELL Administrators"
  - description: Find all active sessions on DC (who's logged in)
    args: "execute-bof netsession.x64.o DC01.north.sevenkingdoms.local"
opsec_notes: |
  NetSession enumeration via SRVSVC is the same protocol as SharpHound's Session
  collection method — just called in-process via BOF rather than from a child process.
  Useful for real-time session discovery (SharpHound session loop is better for
  continuous monitoring). Local group membership enumeration via SAMR is standard
  but generates SAMR traffic to the target.
gotchas: |
  Apollo has no BOF runner — requires Athena. For Apollo, PowerView's
  `Find-DomainUserLocation` and `Get-NetLocalGroupMember` cover this. SAMR can be
  blocked on hardened DC configurations (recent Windows Server hardening). If SAMR is
  blocked, use LDAP-based enumeration instead.
related_ttps: [trustedsec-bofs, sharphound, powerview, sharp-hound-session-loop]
alternatives: [powerview-find-domainuserlocation, sharphound-session, net-sessions-manual]
common_args:
  target:
    description: Target machine hostname or IP
    typical_values: ["WINTERFELL", "DC01.north.sevenkingdoms.local"]
    required: true
  group:
    description: Group name for local_group_members BOF
    typical_values: ["Administrators", "Remote Desktop Users"]
last_updated: 2026-05-29
---

# NetSession / NetLocalGroupGetMembers BOF

TrustedSec SA BOFs for in-process session and local group enumeration. Provides
real-time visibility into who is logged into specific machines (for token theft targeting)
and local group membership (for lateral movement target assessment).

## Use Cases

### Session Discovery (Pre-Token-Theft)

```
Goal: Find where administrator/DA accounts have active sessions
Method: execute-bof netsession.x64.o WINTERFELL
Output: List of active sessions on WINTERFELL with username and IP

Follow-up: Apollo steal_token to steal the DA session token
```

### Local Admin Discovery

```
Goal: Determine who has local admin on a specific machine
Method: execute-bof local_group_members.x64.o WINTERFELL Administrators
Output: SID-resolved list of local admin group members

Use case: Verify whether a harvested hash has local admin on the target
before attempting lateral movement
```

## Equivalent .NET Approaches (Apollo)

```powershell
# PowerView (Apollo powershell_import):
Get-NetSession -ComputerName WINTERFELL
Get-NetLocalGroupMember -ComputerName WINTERFELL -GroupName Administrators
```
