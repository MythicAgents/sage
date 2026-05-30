---
name: SharpHoundCE
category: recon
subcategories: [ad-enumeration, attack-path-mapping, bloodhound-ce]
tradecraft_tags: [bloodhound-ce, sharphound, collector, ad-enumeration, attack-path]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
  - id: T1069.002
    name: Permission Groups Discovery — Domain Groups
source:
  url: https://github.com/SpecterOps/SharpHound
  license: GPL-3.0
  maintained: true
binary_type: .net-assembly
binary_filename: SharpHoundCE.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  Same as SharpHound — heavy LDAP queries against DC. SharpHoundCE is functionally
  SharpHound v2.x, maintained in the SpecterOps SharpHound repository for BloodHound CE
  (Community Edition) compatibility.
usage_examples:
  - description: Full collection for BloodHound CE
    args: "-c All --ZipFilename sysreport.zip"
  - description: DC-only quiet collection
    args: "-c DCOnly --Stealth --ZipFilename data.zip"
  - description: Forest-wide collection
    args: "-c All --SearchForest --ZipFilename full.zip"
opsec_notes: |
  SharpHoundCE is the BloodHound CE-compatible version of SharpHound. Detection profile
  is identical to SharpHound. Always rename before upload.
gotchas: |
  SharpHoundCE produces BloodHound CE-compatible output (different JSON schema from
  BloodHound Legacy v4.x). If the operator's BloodHound instance is Legacy v4, use
  SharpHound v1.x instead. SharpHoundCE = SharpHound v2.x essentially.
related_ttps: [sharphound, bloodhound-ingest, rusthound]
alternatives: [sharphound, rusthound, adexplorer]
common_args:
  -c:
    name: --CollectionMethods
    description: Collection methods (same as SharpHound)
    typical_values: [All, "DCOnly --Stealth", "Group,LocalAdmin,Session,Trusts,ACL"]
    required: true
  --ZipFilename:
    description: Output ZIP name
    typical_values: ["sysreport.zip"]
  --Stealth:
    description: Quieter LDAP-only mode
    typical_values: [flag-only]
  --SearchForest:
    description: Collect across entire forest
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpHoundCE

The BloodHound Community Edition-specific collector — essentially SharpHound v2.x,
maintained alongside the BloodHound CE project. Functionally identical to SharpHound
v2.x but the name distinguishes it from the legacy SharpHound v1.x that targets
BloodHound Legacy. For Apollo engagements, use this when the operator's BloodHound
instance is CE (v5+).

## Typical use cases
- Identical to SharpHound — first-pass AD enumeration for BloodHound CE
- CE-compatible data collection when legacy SharpHound output is incompatible with CE

## How Sage uses this
Same workflow as SharpHound. Use SharpHoundCE when the BloodHound instance is CE;
use SharpHound v1.x-compatible binary when the instance is Legacy.

## Output
Same as SharpHound — ZIP file with CE-compatible JSON schema.
