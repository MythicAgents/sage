---
name: SharpHound
category: recon
recommends_mcp: bloodhound  # pairs with the BloodHound MCP for graph-reasoned attack-path analysis
subcategories: [ad-enumeration, attack-path-mapping]
tradecraft_tags: [bloodhound, acl, foreign-trust, kerberoast-discovery]
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
binary_filename: SharpHound.exe
# Pinned, tamper-evident binary source for Sage's download_tool self-provisioning flow.
# archive_sha256 verified against SpecterOps' official .sha256 sidecar on 2026-05-31.
# Bump url+version+archive_sha256 together when upgrading; never unpin.
binary_download:
  url: https://github.com/SpecterOps/SharpHound/releases/download/v2.13.0/SharpHound_v2.13.0_windows_x86.zip
  version: v2.13.0
  archive: zip
  archive_sha256: e5966ad22b90da86a0ab70e31750ce92da159d18d2a834e6d3c7f146c1f6d453
  extract_member: SharpHound.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  Heavy LDAP queries against the DC; Microsoft Defender for Identity has signatures
  for SharpHound's LDAP query patterns. Some EDRs string-match "SharpHound".
usage_examples:
  - description: Collect all domain data
    args: "-c All"
  - description: Stealth mode, DC-only collection
    args: "-c DCOnly --Stealth"
  - description: Cross-forest collection (for trust analysis)
    args: "-c All --SearchForest"
  - description: Specific collection methods
    args: "-c Group,LocalAdmin,Session,Trusts"
opsec_notes: |
  Default mode is LDAP-heavy and SAMR-heavy; defenders watching for enumeration
  patterns will see this. Use --Stealth for a quieter pass at the cost of less data.
  Output ZIP file should be exfiltrated and cleaned up; don't leave it on disk.
gotchas: |
  Some defenders alert on the literal string "SharpHound" — rename the assembly
  before upload. Newer versions output to a single ZIP by default; older expected
  multiple JSON files. Verify your BloodHound CE/Legacy compatibility.
related_ttps: [bloodhound-ingest, sharphound4cme, rusthound]
alternatives: [adexplorer, adrecon, powerview]
common_args:
  -c:
    name: --CollectionMethods
    description: What to collect; takes a comma-separated list or a meta-value like 'All'
    typical_values: [All, "DCOnly --Stealth", "Group,LocalAdmin,Session,Trusts,ACL"]
    required: true
  --SearchForest:
    description: Walk every domain in the forest (essential for cross-forest analysis)
    typical_values: [flag-only]
  --Stealth:
    description: Quieter LDAP-only collection; less data, less noise
    typical_values: [flag-only]
  -d:
    name: --Domain
    description: Specify domain to collect from (defaults to current)
    typical_values: [north.sevenkingdoms.local, essos.local]
  -o:
    name: --OutputDirectory
    description: >-
      Where to write the output ZIP. Use a directory you can BOTH write AND list/read back as the
      current (often non-admin) user — your own profile temp (%TEMP% = C:\Users\<you>\AppData\Local\Temp)
      or C:\Users\Public. NEVER C:\Windows\Temp — a non-admin can WRITE there but CANNOT list/read it
      back, so `ls` returns Access Denied and `download` returns "does not exist" even though the ZIP is
      present, and you strand your own collection.
    typical_values: ['C:\\Users\\Public', 'C:\\Users\\<you>\\AppData\\Local\\Temp']
  --ZipFilename:
    description: Custom name for output ZIP (helps with OPSEC — avoid 'BloodHound')
    typical_values: ["sysreport.zip", "out.zip"]
last_updated: 2026-05-29
---

# SharpHound

The canonical Active Directory enumeration collector for BloodHound. Walks
LDAP, SAMR, and (optionally) other protocols to gather user, group, computer,
ACL, GPO, session, and trust data into a structured output that BloodHound
ingests for graph-based attack-path analysis. The "what's possible from here"
question on any compromised AD host starts with SharpHound output.

## Typical use cases
- First-pass AD enumeration after initial foothold
- Identifying ACL-based privilege escalation paths (GenericAll, GenericWrite, WriteDACL chains)
- Locating delegation primitives (constrained, unconstrained, RBCD candidates)
- Mapping foreign-group memberships across forest trusts
- Discovering kerberoastable / AS-REP-roastable accounts (for inventory, not for offline crack)
- Finding LAPS-readable computer objects

## How Sage uses this
Sage uses SharpHound as the first move in nearly every AD-targeted tradecraft
chain. The output gets ingested into BloodHound (Sage does not currently host
a BloodHound instance — output is reported back to the operator). Sage may
run multiple passes: an initial `-c All` for breadth, then a focused
`--SearchForest` pass when cross-forest paths are needed.

## Output
A single ZIP file (current versions) containing JSON files for each collected
object type. Default location is the current working directory; rename or
specify output path via `--OutputDirectory`. Size scales with domain size —
small labs produce <5 MB; production environments can produce hundreds of MB.

## OPSEC considerations
SharpHound's enumeration is loud by default. The collection methods that
matter most for attack-path analysis (Sessions, LocalAdmin, Trusts) are also
the ones most likely to trigger detection. Stealth mode trades completeness
for noise reduction; the right call depends on whether the operator believes
they're already detected.

## Full Reference

> Captured against SharpHound v2.5.x, 2026-05-29.

### All command-line arguments

| Arg | Long form | Description |
|-----|-----------|-------------|
| -c | --CollectionMethods | Required. Collection methods (see vocabulary below) |
| -d | --Domain | Target domain (defaults to current) |
| | --OutputDirectory | Output path for ZIP |
| --SearchForest | (flag) | Collect every domain in the forest |
| --Stealth | (flag) | LDAP-only, no SAMR/computer enumeration |
| --LdapUsername | | Alternative LDAP creds (string) |
| --LdapPassword | | Alternative LDAP creds (string) |
| --ZipFilename | | Custom output ZIP name |
| --NoZip | (flag) | Output raw JSON files instead of ZIP |
| --RandomFilenames | (flag) | Randomize output filenames |
| --PrettyJson | (flag) | Pretty-print JSON |
| --Throttle | | Inter-request delay in ms (anti-rate-limit) |
| --Jitter | | Jitter percent applied to Throttle |
| --DomainController | | Specific DC FQDN to talk to |
| --ExcludeDomainControllers | (flag) | Skip DCs as enumeration targets |
| -v | | 0-2; debug output level |

Do not use unsupported cache flags such as `--NoSaveCache`; SharpHound v2.5.x rejects
unknown flags by printing help text and producing no valid BloodHound collection ZIP.
On observed SharpHound v2.13.0.0, `--Verbosity` is also unsupported; use `-v 1`
or omit verbosity. The same observed build rejects short `-o`; use
`--OutputDirectory C:\Users\Public`.

### Collection method values (-c / --CollectionMethods)

- `All` — equivalent to `DCOnly,Group,Session,LocalAdmin,ACL,Trusts,Container,RDP,DCOM,PSRemote,LoggedOn`
- `DCOnly` — LDAP-only against DCs, no SAMR or computer-side queries
- `Group` — group memberships
- `LocalAdmin` — local administrators on member computers (SAMR-heavy)
- `Session` — active sessions per computer (NetSessionEnum)
- `LoggedOn` — currently logged-on users (requires admin on each)
- `Trusts` — domain trust relationships
- `ACL` — DACL inheritance on AD objects
- `ObjectProps` — extended object properties
- `Container` — OU/container hierarchy
- `RDP`, `DCOM`, `PSRemote` — specific remote service rights enumeration
- `SPNTargets` — SPN listing (kerberoastable identification, not crack)

### Output format

Default: single ZIP at `<OutputDirectory>/<timestamp>_BloodHound.zip` containing JSON files
per object type. Pre-v2 versions output multiple JSON files; use `--NoZip` for legacy
BloodHound versions.

### Environment variables

None used by SharpHound directly.

### Exit codes

- 0 = success
- non-zero = error (typically LDAP connection failure or insufficient privileges)

### Version-specific notes

- v2.x is BloodHound CE compatible
- v1.x (legacy) is BloodHound Legacy compatible (different schema)
- v2.x default behavior changed: produces single ZIP, requires `--NoZip` for JSON files
- v2.5.x added `--SearchForest` runtime improvements; older versions could OOM on large forests

### Source for this reference

- https://github.com/SpecterOps/SharpHound#usage (README arg reference)
- `SharpHound.exe -h` captured 2026-05-29
- Version: v2.5.x as of capture date
