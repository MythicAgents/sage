---
name: TrustedSec CS-Situational-Awareness BOFs
category: discovery
subcategories: [bof-collection, host-sa, situational-awareness, ad-enumeration]
tradecraft_tags: [bof, cobalt-strike, situational-awareness, host-info, trustedsec, apollo-gap, athena]
mitre_attack:
  - id: T1082
    name: System Information Discovery
  - id: T1016
    name: System Network Configuration Discovery
  - id: T1087.001
    name: Account Discovery — Local Account
  - id: T1049
    name: System Network Connections Discovery
source:
  url: https://github.com/trustedsec/CS-Situational-Awareness-BOF
  license: BSD-3-Clause
  maintained: true
binary_type: bof
binary_filename: (per-BOF .x64.o files)
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  Individual BOF operations run in-process inside the C2 agent — no new process is
  created. This eliminates the primary detection signal (process creation event) that
  equivalent standalone tools would generate. Behavioral EDR with kernel-level API
  monitoring (CrowdStrike Falcon, SentinelOne) may still detect unusual API call
  sequences from the agent process. Specific BOFs that make network connections
  (netstat, arp) generate network telemetry.
usage_examples:
  - description: Enumerate ADCS CA and templates (BOF equivalent of Certify find)
    args: "adcs_enum"
  - description: List domain controllers for the current domain
    args: "nslookup"
  - description: Enumerate local group members
    args: "local_group_members Administrators"
  - description: List active network connections (netstat equivalent)
    args: "netstat"
  - description: Enumerate running services
    args: "listservices"
  - description: Check currently loaded driver signatures
    args: "driversigs"
  - description: Enumerate domain information
    args: "domaininfo"
  - description: List named pipes on the system
    args: "listpipes"
opsec_notes: |
  BOFs run inside the C2 agent's process memory — there is no child process, no disk
  write for the tool itself, and no new network socket from a separate process. This
  is their defining OPSEC advantage over .NET assembly execution (inline_assembly)
  or PowerShell. The trade-off: Apollo has no BOF runner. Use Athena (`execute-bof`
  command) when BOF execution is required. Seatbelt covers most of the same SA
  ground as a .NET assembly fallback.
gotchas: |
  Apollo does NOT have a BOF runner. For Apollo engagements:
  - Use Seatbelt (.NET) for most SA checks (equivalent coverage)
  - Use Athena (has native execute-bof) if BOF-in-process execution is required
  - Inceptor can convert some BOFs to .NET assemblies, but the conversion is imperfect
  Each BOF is a separate .x64.o file — they are not distributed as a single binary.
  BOF argument passing varies by C2 framework (Cobalt Strike uses bof_pack; Athena
  has its own argument format); verify compatibility before use.
related_ttps: [seatbelt, nanodump, inceptor, outflank-remote-ops-bofs, bofnet]
alternatives: [seatbelt, powerview, sharpup]
common_args:
  adcs_enum:
    name: adcs_enum
    description: Enumerate ADCS Certificate Authorities and templates (in-process, no LDAP)
    typical_values: [flag-only]
  local_group_members:
    name: local_group_members
    description: List members of a local group
    typical_values: ["Administrators", "Remote Desktop Users"]
  domaininfo:
    name: domaininfo
    description: Enumerate domain name, DCs, domain SID, and trust information
    typical_values: [flag-only]
  listservices:
    name: listservices
    description: List all running services and their binary paths
    typical_values: [flag-only]
  driversigs:
    name: driversigs
    description: Check loaded kernel driver signatures for unsigned or suspicious drivers
    typical_values: [flag-only]
  netstat:
    name: netstat
    description: List active TCP/UDP connections and listening ports
    typical_values: [flag-only]
  listpipes:
    name: listpipes
    description: Enumerate named pipes (useful for privilege escalation discovery)
    typical_values: [flag-only]
  whoami:
    name: whoami
    description: Detailed current user context including privileges and group SIDs
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# TrustedSec CS-Situational-Awareness BOFs

TrustedSec's collection of Beacon Object Files for in-process situational awareness.
The core value of BOFs over standalone tools is execution inside the C2 agent's own
process memory — no child process creation, no disk writes for the tool binary, and
no process tree anomalies. This makes BOF-based SA significantly quieter than running
equivalent .NET assemblies via inline_assembly.

## Complete BOF Inventory

### Host Enumeration BOFs

| BOF | Description | Privilege | Notes |
|-----|-------------|-----------|-------|
| `whoami` | Current user, SID, group memberships, privileges | user | More detail than Windows whoami |
| `listdrives` | Enumerate local drive letters and types | user | — |
| `listfiles` | Directory listing with metadata | user | Supports wildcards |
| `driversigs` | Check loaded kernel driver signatures | user | Finds unsigned/unsigned-but-whitelisted |
| `listpipes` | Enumerate named pipes | user | Useful for pipe-based privesc research |
| `listservices` | Running services + binary paths | user | Equivalent to sc query |
| `regsave` | Save registry hive to file | admin | SYSTEM hive for NTDS.dit decryption |
| `regquery` | Query registry key/value | user | — |
| `env` | Enumerate environment variables | user | — |
| `ipconfig` | IP configuration per interface | user | — |
| `netstat` | Active network connections and listeners | user | TCP + UDP |
| `arp` | ARP table / neighbor cache | user | — |
| `routeprint` | IP routing table | user | — |

### Active Directory / Domain Enumeration BOFs

| BOF | Description | Privilege | Notes |
|-----|-------------|-----------|-------|
| `domaininfo` | Domain name, DCs, SID, functional level, trusts | domain-user | No LDAP required (uses DsGetDcName) |
| `nslookup` | DNS resolution (forward and reverse) | domain-user | — |
| `adcs_enum` | ADCS CAs and all certificate templates | domain-user | In-process LDAP; equiv to `Certify find` |
| `adcs_enum_com` | ADCS enumeration via COM interface | domain-user | Alternative ADCS enum path |
| `adcs_enum_com2` | ADCS cert template details via COM | domain-user | — |
| `ldapsearch` | Generic LDAP query | domain-user | Full LDAP filter support |
| `gplink` | Enumerate GPO links on OUs | domain-user | — |
| `smbinfo` | SMB connection info for a target | domain-user | — |
| `vssenum` | Enumerate VSS snapshots | user | DC snapshot existence check |

### Process and Token BOFs

| BOF | Description | Privilege | Notes |
|-----|-------------|-----------|-------|
| `ProcessListAllHandles` | List all process handles across the system | user | — |
| `tasklist` | Process list with PID, parent PID, user | user | — |
| `findLoadedModule` | Find all processes with a DLL loaded | user | Useful for EDR fingerprinting |
| `enumLocalSessions` | Enumerate local logon sessions and LUIDs | user | — |

### Network Discovery BOFs

| BOF | Description | Privilege | Notes |
|-----|-------------|-----------|-------|
| `arpscan` | ARP scan a subnet for live hosts | user | — |
| `netscan` | TCP connect scan for open ports | user | — |
| `wifissids` | List known WiFi SSIDs from profiles | user | May contain PSK |
| `windowlist` | List visible window titles | user | Session/activity awareness |

### Persistence and Registry BOFs

| BOF | Description | Privilege | Notes |
|-----|-------------|-----------|-------|
| `reg_add` | Add registry key or value | user/admin | — |
| `reg_delete` | Delete registry key or value | user/admin | — |
| `reg_query` | Query registry | user | — |
| `ScheduledTaskPersist` | Add scheduled task via COM (no schtasks.exe) | admin | — |
| `ScheduledTaskDelete` | Delete scheduled task | admin | — |

## Typical use cases
- In-process host SA immediately post-foothold (no disk drop, no child process)
- ADCS enumeration without uploading Certify (`adcs_enum`)
- Domain enumeration without LDAP overhead (`domaininfo`, `nslookup`)
- Named pipe enumeration for SeImpersonate exploit hunting (`listpipes`)
- Driver enumeration for BYOVD opportunity identification (`driversigs`)

## How Sage uses this
With Athena as the Mythic agent, Sage runs these BOFs via `execute-bof` for initial
post-foothold SA with minimal footprint. The BOF collection is complementary to
Seatbelt — many checks overlap, but BOFs run cleaner (in-process). For Apollo engagements,
Seatbelt is the fallback.

## Apollo-specific note
Apollo does NOT have a BOF runner. Options in order of preference:
1. **Seatbelt** (.NET assembly) — covers most checks, Apollo-compatible
2. **Athena** (`execute-bof`) — if switching agents is viable
3. **Inceptor** — BOF-to-assembly conversion (imperfect; test first)

See `mythic_agents/apollo.md` and `mythic_agents/athena.md` for execution model details.
