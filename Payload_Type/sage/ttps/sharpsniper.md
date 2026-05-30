---
name: SharpSniper
category: recon
subcategories: [user-hunting, logon-discovery, high-value-target-location]
tradecraft_tags: [user-hunting, logon-events, event-log, domain-admin-location, dotnet, apollo-runnable]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
  - id: T1033
    name: System Owner/User Discovery
source:
  url: https://github.com/HunnicCyber/SharpSniper
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpSniper.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  SharpSniper reads Windows Security Event Logs on domain computers to find where
  specific users (typically Domain Admins) have recently logged in. Reading remote
  event logs requires authentication and generates Event 4624 on the target. Bulk
  event log queries from a single source are anomalous.
usage_examples:
  - description: Find where the Domain Administrator has logged in recently
    args: "SharpSniper.exe administrator"
  - description: Find a specific user's recent logon locations
    args: "SharpSniper.exe jon.snow KINGSLANDING.north.sevenkingdoms.local"
  - description: Hunt across all DCs for a user's last logon
    args: "SharpSniper.exe administrator /dc"
opsec_notes: |
  SharpSniper reads remote event logs to find logon events for a target user.
  This generates authentication events on each queried machine (Event 4624 type 3)
  and is detectable as unusual event log querying from a workstation. PowerView's
  `Find-DomainUserLocation` and SharpHound session loop are alternative approaches
  with different detection profiles. SharpSniper is most useful for targeted
  hunting of a specific high-value user.
gotchas: |
  Remote event log querying requires the Remote Event Log Management firewall exception
  to be enabled on targets (or network admin access). Some DCs restrict event log reads
  to domain admins. Not actively maintained. For real-time session hunting, Rubeus
  monitor + coercion is more efficient.
related_ttps: [sharp-hound-session-loop, powerview, sharphound, unconstrained-delegation-abuse]
alternatives: [find-domainuserlocation-powerview, sharphound-session-loop]
common_args:
  username:
    description: Target username to find logon locations for
    typical_values: ["administrator", "jon.snow"]
    required: true
  /dc:
    description: Check domain controllers for logon events
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpSniper

A .NET assembly for locating specific high-value users by querying Windows Security
Event Logs across domain computers. Reads logon event entries (Event 4624) to determine
where a target user (typically a Domain Admin) has recently authenticated.

## Typical use cases
- Find where Domain Admins are currently or recently logged in
- Identify machines to target for token theft (steal_token after finding DA session)
- Pre-unconstrained-delegation: find which machine to coerce from based on DA session location

## How Sage uses this
SharpSniper is used when Sage needs to find where a specific DA account has an active
session — for token steal, make_token reuse, or as a coercion target for unconstrained
delegation. PowerView's `Find-DomainUserLocation` provides similar capability.

## Output
Text listing of machines where the target user has recent logon events, with timestamp
and logon type.
