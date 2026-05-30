---
name: SharpHound Session Loop Collection
category: recon
subcategories: [session-collection, active-sessions, user-hunting, unconstrained-delegation-prep]
tradecraft_tags: [sharphound, sessions, bloodhound, user-hunting, unconstrained-delegation, loop]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/SpecterOps/SharpHound
  license: GPL-3.0
  maintained: true
binary_type: .net-assembly
binary_filename: SharpHound.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  Session collection (NetSessionEnum) generates SMB connections to each computer in the
  domain. Repeated session collection (loop mode) generates periodic SMB bursts. Heavy
  session collection from a workstation is anomalous and detectable by network monitoring.
usage_examples:
  - description: Session-only collection for user hunting
    args: "-c Session --Loop --LoopDuration 01:00:00 --LoopInterval 0:5:00 --ZipFilename sessions.zip"
  - description: One-time session collection to find where DA users are logged in
    args: "-c Session --ZipFilename sessions_now.zip"
  - description: LoggedOn (requires admin on each target — very noisy)
    args: "-c LoggedOn --ZipFilename loggedon.zip"
opsec_notes: |
  Session collection is the noisiest SharpHound collection method because it makes SMB
  connections to every computer in the domain. Loop mode multiplies this traffic. Use
  judiciously — session collection is most valuable for the specific use case of finding
  where Domain Admin users have active sessions (for unconstrained delegation abuse or
  token theft). LoggedOn requires local admin on each target and is even noisier.
gotchas: |
  Loop mode runs continuously until stopped or the duration expires. Output ZIP files
  accumulate for each loop iteration. Session data is ephemeral — sessions change as
  users log in/out. For unconstrained delegation abuse, real-time session data is needed
  (Rubeus monitor provides this more efficiently). SharpHound session loop is better for
  patient user-hunting to find when a high-value user logs into an accessible machine.
related_ttps: [sharphound, bloodhound-ingest, rubeus, unconstrained-delegation-abuse, powerview]
alternatives: [find-domainuserlocation-powerview, netview, netsess]
common_args:
  -c Session:
    description: Collect only active session data
    typical_values: ["Session"]
    required: true
  --Loop:
    description: Run collection in a loop
    typical_values: [flag-only]
  --LoopDuration:
    description: Total time to run the loop
    typical_values: ["01:00:00", "00:30:00"]
  --LoopInterval:
    description: Time between each collection pass
    typical_values: ["0:5:00", "0:2:00"]
  --ZipFilename:
    description: Output ZIP filename
    typical_values: ["sessions.zip"]
last_updated: 2026-05-29
---

# SharpHound Session Loop Collection

The session collection mode of SharpHound, focused on finding where users have active
logon sessions. Loop mode runs collection repeatedly to catch sessions that appear at
different times. Most valuable for finding where Domain Admin or other high-value users
are logged in — enabling targeted token theft, injection, or coercion.

## Typical use cases
- Find active sessions for DA accounts for unconstrained delegation or token theft
- Identify which machines DA users log into (for lateral movement targeting)
- Build a picture of user activity patterns over time

## How Sage uses this
Session loop is used when Sage needs to find an active DA session for direct token
manipulation (steal_token) or as a target for unconstrained delegation abuse. The
BloodHound "FindDomainUserLocation" equivalent but automated.

## Alternative: PowerView Find-DomainUserLocation

```
# Faster targeted approach for specific user:
Find-DomainUserLocation -UserIdentity "administrator" -CheckAccess
```
This is equivalent but returns results in real-time without waiting for SharpHound's
collection pipeline.
