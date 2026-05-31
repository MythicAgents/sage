---
name: SharpHound Cross-Forest Collection
category: recon
recommends_mcp: bloodhound  # pairs with the BloodHound MCP for graph-reasoned attack-path analysis
subcategories: [cross-forest, trust-enumeration, attack-path-mapping]
tradecraft_tags: [bloodhound, cross-forest, trusts, foreign-principals, attack-path]
mitre_attack:
  - id: T1482
    name: Domain Trust Discovery
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
  Same as SharpHound — heavy LDAP queries, but now against DCs in MULTIPLE domains/forests.
  Cross-domain LDAP queries from a non-DC workstation are anomalous and more detectable
  than same-domain enumeration. May require explicit credentials for each domain if trusts
  are not transitive or if cross-domain auth is restricted.
usage_examples:
  - description: Collect all data across entire forest (walks all domain trusts)
    args: "-c All --SearchForest --ZipFilename forest.zip"
  - description: Collect DCOnly across forest (quiet multi-domain pass)
    args: "-c DCOnly --SearchForest --Stealth --ZipFilename forest_stealth.zip"
  - description: Target a specific external trust domain
    args: "-c All -d essos.local --ZipFilename essos.zip"
opsec_notes: |
  Cross-forest collection generates LDAP queries against every DC in every discovered
  domain/forest — this is significantly noisier than single-domain collection. Each domain's
  MDI instance may alert independently. Use --Stealth with --SearchForest for minimal-noise
  cross-forest enumeration. The output ZIP contains all domains' data for unified BloodHound analysis.
gotchas: |
  --SearchForest walks trust chains — it will discover and attempt to collect from every
  reachable domain. If trust relationships are complex or some trusts are external (not
  transitive), SharpHound may fail on some domains silently. For specific domain targeting,
  use -d to specify one domain at a time. Forest-wide collection generates much larger
  ZIP files (proportional to all domain objects combined).
related_ttps: [sharphound, bloodhound-ingest, powerview, sid-history-abuse]
alternatives: [powerview-get-foresttrust, bloodhound-python-cross-forest]
common_args:
  --SearchForest:
    description: Walk all domain trusts from the current domain (collect entire forest)
    typical_values: [flag-only]
  -d:
    description: Specify a single domain to collect (for targeted cross-domain)
    typical_values: ["essos.local", "sevenkingdoms.local"]
  -c:
    description: Collection methods (All or DCOnly for cross-forest stealth)
    typical_values: [All, DCOnly]
  --Stealth:
    description: LDAP-only quiet mode for cross-forest enumeration
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpHound Cross-Forest Collection

SharpHound's `--SearchForest` mode for collecting attack-path data across all domains in
a forest and across forest trust relationships. When BloodHound shows no path from a
controlled principal to DA in the current domain, cross-forest collection may reveal
escalation paths via foreign principal membership, forest trust exploitation, or SID History.

## Typical use cases
- Identify cross-forest attack paths (foreign group memberships, trust misconfigurations)
- Collect data from child domains when parent domain compromise is the goal
- Map forest-wide attack surface for complex multi-domain environments

## How Sage uses this
After initial single-domain SharpHound collection yields no path to domain compromise,
Sage runs `--SearchForest` to discover cross-forest opportunities. The unified BloodHound
dataset then reveals paths that span domain boundaries.

## Output
Single ZIP containing BloodHound JSON for all collected domains, importable into BloodHound
CE as a multi-domain dataset.
