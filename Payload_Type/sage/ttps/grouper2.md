---
name: Grouper2
category: recon
subcategories: [gpo-analysis, misconfiguration-discovery, group-policy]
tradecraft_tags: [gpo, group-policy, misconfiguration, sysvol, dotnet, offline-analysis]
mitre_attack:
  - id: T1484.001
    name: Domain Policy Modification — Group Policy Modification
source:
  url: https://github.com/l0ss/Grouper2
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: Grouper2.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Grouper2 reads GPO XML files from SYSVOL (a standard SMB share). SYSVOL access
  is normal domain behavior; bulk file reading from SYSVOL may be anomalous.
  Grouper2 uses standard SMB access, generating the usual authentication events.
usage_examples:
  - description: Enumerate all GPOs for interesting misconfigurations
    args: "Grouper2.exe"
  - description: Output to JSON for offline analysis
    args: "Grouper2.exe -f json > grouper2.json"
  - description: Target a specific domain
    args: "Grouper2.exe -d north.sevenkingdoms.local"
opsec_notes: |
  Grouper2 reads SYSVOL via SMB — this is normal domain traffic. The enumeration is
  relatively quiet because it reads existing files rather than making LDAP queries.
  SYSVOL contains Group Policy templates accessible to all domain-authenticated users.
  Output can reveal weak ACLs on GPO objects that SharpGPOAbuse can then exploit.
gotchas: |
  Grouper2 finds misconfigurations but doesn't exploit them — it's recon for GPO abuse
  chains. Not actively maintained; some newer Windows GPO features may not be covered.
  Cross-reference Grouper2 findings with SharpHound's GPO data in BloodHound for a
  complete picture. Finding a misconfigured GPO → SharpGPOAbuse for exploitation.
related_ttps: [sharpgpoabuse, sharphound, bloodhound-ingest, powerview]
alternatives: [pingcastle-gpo, sharpgpoabuse-find-manual]
common_args:
  -d:
    description: Target domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
  -f:
    description: Output format
    typical_values: [json, text]
last_updated: 2026-05-29
---

# Grouper2

A .NET assembly for auditing Group Policy Objects (GPOs) for misconfigurations and
interesting settings — passwords in GPP, writable GPO paths, interesting script paths,
and more. Grouper2 reads GPO XML files from SYSVOL and reports findings that can
feed into SharpGPOAbuse exploitation chains.

## Typical use cases
- Discover GPO misconfigurations (GPP passwords, writable paths, interesting scripts)
- Pre-exploitation recon before SharpGPOAbuse targeting
- Identify GPOs linked to high-value OUs

## How Sage uses this
Grouper2 is a reconnaissance step in the GPO abuse chain. Sage runs it to identify
exploitable GPO configurations, then uses SharpGPOAbuse to exploit specific findings.

## Output
Text or JSON listing of GPO findings with severity classifications. Each finding
includes the GPO name, the misconfiguration type, and the specific value/path.
