---
name: bloodhound-python
category: recon
subcategories: [ad-enumeration, attack-path-mapping, bloodhound-collector]
tradecraft_tags: [bloodhound, python, linux-side, ad-enumeration, attack-path, impacket]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
  - id: T1069.002
    name: Permission Groups Discovery — Domain Groups
source:
  url: https://github.com/dirkjanm/BloodHound.py
  license: MIT
  maintained: true
binary_type: python-script
binary_filename: bloodhound-python
supported_os: [linux, windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Same detection surface as SharpHound — heavy LDAP queries against DCs. Python-based
  LDAP produces slightly different protocol patterns but is functionally equivalent.
  MDI detects mass LDAP enumeration. Unlike SharpHound, bloodhound-python has no
  assembly string to detect but the query pattern is equivalent.
usage_examples:
  - description: Full collection for BloodHound CE (Linux-side)
    args: "bloodhound-python -u jon.snow -p Password123 -d north.sevenkingdoms.local -c All --zip"
  - description: DC-only quiet collection
    args: "bloodhound-python -u jon.snow -p Password123 -d north.sevenkingdoms.local -c DCOnly --zip"
  - description: Pass-the-hash collection
    args: "bloodhound-python -u administrator --hashes :nthash -d north.sevenkingdoms.local -c All --zip"
  - description: Target specific DC
    args: "bloodhound-python -u jon.snow -p Password123 -d north.sevenkingdoms.local -dc DC01.north.sevenkingdoms.local -c All --zip"
opsec_notes: |
  LDAP query pattern is the same as SharpHound. The advantage over SharpHound in
  some scenarios: runs from Linux infrastructure, doesn't require a Windows foothold,
  supports pass-the-hash authentication. The collected ZIP imports directly into
  BloodHound CE.
gotchas: |
  Python-only — not Apollo-runnable. For Apollo-based collection, use SharpHound (inline_assembly).
  bloodhound-python may have schema compatibility issues between BloodHound Legacy and CE —
  check BloodHound version before importing. Session collection may be incomplete compared
  to SharpHound (some SAMR operations depend on Windows-specific APIs).
related_ttps: [sharphound, sharphound4cme, bloodhound-ingest, powerview]
alternatives: [sharphound, rusthound, adexplorer]
common_args:
  -u:
    description: Username
    typical_values: ["jon.snow"]
    required: true
  -p:
    description: Password
    typical_values: ["Password123"]
  --hashes:
    description: NTLM hash (LM:NT format) for pass-the-hash
    typical_values: [":nthash"]
  -d:
    description: Domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
    required: true
  -c:
    description: Collection methods (same as SharpHound)
    typical_values: [All, DCOnly, "Group,LocalAdmin,Session,Trusts,ACL"]
    required: true
  --zip:
    description: Output as ZIP file (BloodHound-ready)
    typical_values: [flag-only]
  -dc:
    description: Specific DC to collect from
    typical_values: ["DC01.north.sevenkingdoms.local"]
last_updated: 2026-05-29
---

# bloodhound-python

Dirk-jan Mollema's Python-based BloodHound data collector. The Linux-infrastructure
equivalent of SharpHound — performs the same LDAP-based AD enumeration and produces
BloodHound CE-compatible ZIP files. Supports pass-the-hash authentication, making it
useful when credentials are available without a Windows foothold.

## Typical use cases
- BloodHound data collection from Linux attack infrastructure
- Collection when .NET assembly delivery isn't available
- Pass-the-hash collection using recovered NTLM hashes

## How Sage uses this
Infrastructure-side Python collector. For Apollo-based collection, SharpHound is
preferred. bloodhound-python is the go-to when Sage is orchestrating collection from
Linux infrastructure.

## Apollo-specific note
Python-only — not Apollo-runnable. Use SharpHound for in-agent collection.
