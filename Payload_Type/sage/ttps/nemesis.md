---
name: Nemesis
category: collection
subcategories: [data-enrichment-pipeline, credential-triage, dpapi-decryption, post-collection]
tradecraft_tags: [nemesis, data-pipeline, credential-triage, dpapi, llm, specterops, infrastructure]
mitre_attack:
  - id: T1005
    name: Data from Local System
  - id: T1119
    name: Automated Collection
source:
  url: https://github.com/SpecterOps/Nemesis
  license: BSD-3-Clause
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [linux]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Nemesis runs on attacker infrastructure — no detection signal on target. It processes
  data already collected from compromised hosts. The analysis/enrichment pipeline is
  entirely operator-side.
usage_examples:
  - description: Ingest a DPAPI credential blob for automatic decryption
    args: "(Nemesis API) POST /api/v1/documents/dpapi_blob {'blob': '<base64>'}"
  - description: Submit files for automated secret scanning
    args: "(Nemesis UI) Upload Files → automatic keyword matching + LLM analysis"
  - description: Query enriched data for passwords
    args: "(Nemesis UI) Search: type:credential category:password"
opsec_notes: |
  Nemesis is operator-side infrastructure — a Docker-compose-based enrichment pipeline.
  It processes collected artifacts (files, DPAPI blobs, credential files, Chrome data,
  etc.) and automatically:
  - Decrypts DPAPI blobs using submitted master keys
  - Scans files for credential patterns (regex + ML)
  - Provides a web UI for browsing collected data
  - Optionally uses LLM (GPT-4) for natural-language querying of collected data
  No target-side footprint. The value is post-collection automation.
gotchas: |
  Nemesis requires significant infrastructure setup (Docker, Kubernetes, or docker-compose).
  It's a platform for professional red team operations, not a single tool. Key integration:
  - Apollo/Athena can be configured to automatically submit collected files to Nemesis
  - Mythic has a Nemesis integration that auto-submits downloaded files
  - DPAPI decryption requires submitting the domain backup key first
  Nemesis is most valuable in large engagements with lots of collected data that needs
  automated triage. For small engagements, manual SharpDPAPI + secretsdump is sufficient.
related_ttps: [sharpdpapi, impacket-secretsdump, snaffler, sharpfiles, credential-hunting-checklist]
alternatives: [manual-credential-triage, trufflehog, gitrob]
common_args: {}
last_updated: 2026-05-29
---

# Nemesis

SpecterOps' offensive data enrichment pipeline. Nemesis is not a tool you run on targets —
it's operator-side infrastructure that ingests files and artifacts collected from
compromised hosts and automatically enriches them: decrypting DPAPI blobs, scanning
for credentials in files, correlating collected data, and providing a searchable web UI
backed by Elasticsearch.

## What Nemesis Does

```
Input (collected data from targets):
  ├── Files (configs, scripts, binaries, documents)
  ├── DPAPI blobs (credential manager, Chrome state, cert stores)
  ├── Memory dumps (LSASS, process dumps)
  └── Raw collected artifacts

Processing (automatic):
  ├── DPAPI decryption (using submitted master keys)
  ├── Credential pattern extraction (regex + ML)
  ├── File content triage (known-sensitive file types)
  ├── LLM analysis (optional GPT-4 natural language queries)
  └── Attack path enrichment (connection to BloodHound)

Output (web UI):
  ├── Searchable credential database
  ├── File content indexed and searchable
  └── Triage queue with severity scoring
```

## Nemesis + Mythic Integration

Mythic can be configured to automatically forward all file downloads from agents to
Nemesis for processing. This creates a fully automated credential triage pipeline:

```
Apollo agent downloads a file → Mythic receives it → Nemesis ingests automatically →
DPAPI decryption attempted → Credentials extracted → Available in Nemesis UI
```

## Apollo-specific note
Operator infrastructure — no target-side component. The value is post-collection
automation that processes data gathered by Apollo/Athena agents via SharpDPAPI,
download commands, and Snaffler output.
