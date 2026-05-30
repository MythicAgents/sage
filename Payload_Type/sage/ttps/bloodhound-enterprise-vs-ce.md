---
name: BloodHound Enterprise vs Community Edition
category: recon
subcategories: [bloodhound-variants, attack-path-analysis, enterprise-features]
tradecraft_tags: [bloodhound, enterprise, community-edition, ce, attack-path, specterops, comparison]
mitre_attack: []
source:
  url: https://bloodhoundenterprise.io
  license: none
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [linux, windows, macos]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Operator-side infrastructure — no detection signal on target.
usage_examples:
  - description: BloodHound CE is the open-source self-hosted version for red team use
    args: "(operator infrastructure)"
  - description: BloodHound Enterprise is the SaaS commercial version for defensive use
    args: "(not relevant to offensive operations)"
opsec_notes: |
  Reference document for understanding which BloodHound variant Sage works with.
  Sage-relevant: BloodHound Community Edition (CE, v5+) hosted on operator infrastructure.
gotchas: |
  BloodHound CE and BloodHound Legacy have incompatible data schemas. SharpHound v2.x
  collects for CE; SharpHound v1.x collects for Legacy. Always match collector to instance.
related_ttps: [bloodhound-ingest, sharphound, sharphound4cme, bloodhound-cypher-reference,
               bloodhound-custom-queries, bloodhound-python]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# BloodHound Enterprise vs Community Edition

Reference for the BloodHound ecosystem variants. Sage uses **BloodHound Community Edition (CE)**
running on operator infrastructure.

## BloodHound CE (Community Edition)

| Property | Details |
|----------|---------|
| Version | v5.x+ (called "BloodHound") |
| License | Apache 2.0 |
| Hosting | Self-hosted (Docker-compose or Kubernetes) |
| Use case | Red team / penetration testing |
| Collector | SharpHound v2.x, RustHound, bloodhound-python |
| ADCS support | YES (with Certipy -bloodhound or ADCS collection mode) |
| Azure support | YES (AzureHound, Okta, GitHound, etc.) |
| API | REST API available for automation |

## BloodHound Enterprise

| Property | Details |
|----------|---------|
| License | Commercial (SpecterOps SaaS) |
| Use case | Defensive (continuous attack path monitoring) |
| Collector | SharpHound Enterprise (auto-deployed) |
| Difference | Real-time monitoring, automated finding triage, ticketing integration |

**Sage uses BloodHound CE.** BloodHound Enterprise is the defender-facing product.

## Version Compatibility Matrix

| BloodHound Version | SharpHound Version | Format |
|-------------------|-------------------|--------|
| BloodHound Legacy (v4) | SharpHound v1.x | Legacy JSON |
| BloodHound CE (v5+) | SharpHound v2.x | CE JSON |
| BloodHound CE (v5+) | RustHound | CE JSON |
| BloodHound CE (v5+) | bloodhound-python | CE JSON |

## CE Installation (Quick Reference)

```bash
# Docker-compose (official method):
git clone https://github.com/SpecterOps/BloodHound
cd BloodHound
cp examples/docker-compose/docker-compose.yml docker-compose.yml
docker compose up -d

# Access: http://localhost:8080
# Default credentials generated on first run
```

## Critical: Ingest SharpHound Data

```
BloodHound CE UI → Administration → File Ingest → Upload ZIP
OR
BloodHound CE API: POST /api/v2/file-upload (multipart ZIP)
```

After import, mark controlled principals as owned:
```cypher
MATCH (u:User {name:'JON.SNOW@NORTH.SEVENKINGDOMS.LOCAL'})
SET u.owned = true
```
