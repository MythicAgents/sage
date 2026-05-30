---
name: PingCastle
category: recon
subcategories: [ad-audit, risk-assessment, domain-health]
tradecraft_tags: [ad-audit, risk-score, security-assessment, html-report, dotnet, lolbin-adjacent]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/vletoux/pingcastle
  license: Non-Commercial
  maintained: true
binary_type: .net-assembly
binary_filename: PingCastle.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  PingCastle performs extensive LDAP queries and SMB connections to enumerate AD
  security posture. It has a "legitimate" auditing context but generates the same
  LDAP query patterns as SharpHound. Some defenders whitelist PingCastle; others
  flag the enumeration pattern regardless. The generated HTML report is a high-value
  output artifact to protect.
usage_examples:
  - description: Full domain audit (produces HTML report)
    args: "--healthcheck --server DC01.north.sevenkingdoms.local"
  - description: Scanner mode (enumerate machines, find admin shares)
    args: "--scanner local --server DC01.north.sevenkingdoms.local"
  - description: Check for specific stale objects
    args: "--healthcheck --dateoption Score"
opsec_notes: |
  PingCastle is widely used by IT administrators for legitimate AD health assessments.
  Running it during an engagement may appear legitimate if discovered — but the output
  (a comprehensive AD risk report) is highly sensitive. The LDAP queries are similar
  to SharpHound in scope. PingCastle's healthcheck produces a risk-scored HTML report
  that directly enumerates most high-value attack surfaces.
gotchas: |
  Non-commercial license — restricted to non-commercial use. .NET assembly but produces
  a standalone HTML file that must be exfiltrated. For engagements, run with `--no-enum-limit`
  for complete data. PingCastle's output is more human-readable than BloodHound but
  less suitable for automated attack-path analysis. Use SharpHound+BloodHound for automated
  analysis; PingCastle for a quick risk overview.
related_ttps: [sharphound, seatbelt, grouper2, bloodhound-ingest]
alternatives: [sharphound, seatbelt, grouper2]
common_args:
  --healthcheck:
    description: Run full domain health/security assessment
    typical_values: [flag-only]
  --server:
    description: Domain controller to assess
    typical_values: ["DC01.north.sevenkingdoms.local"]
  --scanner:
    description: Network scanner mode
    typical_values: ["local", "smb", "null"]
last_updated: 2026-05-29
---

# PingCastle

A comprehensive Active Directory security audit tool by Vincent Le Toux. PingCastle
performs a deep AD health check and produces a risk-scored HTML report covering misconfigurations,
stale accounts, delegation settings, trust issues, GPO problems, and more. The output
provides a prioritized attack-surface overview that complements BloodHound's graph-based
analysis.

## Typical use cases
- Quick AD security posture assessment with human-readable HTML output
- Identify misconfigured accounts, stale objects, and delegation primitives
- Audit domain trusts and forest configuration
- Complement BloodHound data with risk-scored priority listing

## How Sage uses this
PingCastle's healthcheck provides a risk-prioritized view of the domain's attack surface.
Sage can run it early in an engagement to identify high-value targets quickly. The HTML
report must be exfiltrated and reviewed by the operator — it's not automatically parsed
by Sage but provides excellent operational intelligence.

## Output
HTML report (`PingCastleReport_DOMAIN_DATE.html`) with risk score, critical finding
summaries, and detailed finding tables organized by risk category.
