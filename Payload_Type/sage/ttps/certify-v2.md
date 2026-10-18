---
name: Certify v2 argument migration
category: adcs
subcategories: [certify-v2, parser-migration]
tradecraft_tags: [adcs, certify, ghostpack, v2]
source:
  url: https://github.com/GhostPack/Certify
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: Certify.exe
binary_sha256: 1383e8bca90e4be70ff2224b7e2af465a8f3d5bede1fde60f1ece71e3b02d346
binary_version: "2.0.0"
binary_contract: ttps/contracts/certify.json
binary_contract_evidence: compiled-parser-metadata
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
usage_examples:
  - description: Replace legacy vulnerable-template discovery
    args: "enum-templates --filter-vulnerable"
  - description: Replace legacy CA discovery
    args: "enum-cas --filter-vulnerable"
  - description: Replace legacy alternate-name request
    args: "request --ca SERVER\\CA-NAME --template TEMPLATE --upn USER@DOMAIN"
gotchas: |
  Version 2 reorganizes the parser. Do not reuse v1 verb names or slash options. This
  reference does not claim that every ESC classification is implemented by this build.
related_ttps: [certify, certipy, rubeus]
alternatives: [certify]
common_args:
  enum-templates:
    description: Current template-enumeration verb
    typical_values: [flag-only]
  enum-cas:
    description: Current CA-enumeration verb
    typical_values: [flag-only]
  --filter-vulnerable:
    description: Current vulnerability filter for the selected enumeration verb
    typical_values: [flag-only]
  --upn:
    description: Current UPN SAN request option; its compiled property is sequence-valued
    typical_values: ["user@domain.local"]
last_updated: 2026-10-07
---

# Certify v2 argument migration

For the pinned v2 binary, split legacy `find /vulnerable` into `enum-cas` and
`enum-templates`, and translate slash-style request options to double-dash options.
Use the `certify` entry for the full current reference. Parser metadata alone does not
prove complete ESC coverage, help compatibility, successful target execution, or Mythic transport.

## Full Reference

Current examples for the pinned hash:

```text
enum-cas --filter-vulnerable
enum-templates --filter-vulnerable
request --ca SERVER\CA-NAME --template TEMPLATE --upn USER@DOMAIN
```

Legacy `find`, `cas`, `download`, `/vulnerable`, `/ca:`, `/template:`, and `/altname:`
are not registered by this build.
