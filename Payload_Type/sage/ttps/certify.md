---
name: Certify
category: adcs
subcategories: [certificate-authority-enumeration, certificate-template-enumeration, cert-request]
tradecraft_tags: [adcs, certificate, ghostpack, pki, enrollment]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
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
detection_signal: |
  Enumeration queries the PKI configuration container. Certificate enrollment is
  recorded by the CA and may generate certificate-service events.
usage_examples:
  - description: Enumerate vulnerable certificate authorities
    args: "enum-cas --filter-vulnerable"
  - description: Enumerate vulnerable certificate templates
    args: "enum-templates --filter-vulnerable"
  - description: Request a certificate with a supplied UPN
    args: "request --ca SERVER\\CA-NAME --template TEMPLATE --upn USER@DOMAIN --output-pem"
  - description: Download a prior certificate request
    args: "request-download --ca SERVER\\CA-NAME --id 1337 --output-pem"
opsec_notes: |
  Enumeration creates LDAP/PKI traffic; requests leave CA records. Prefer a focused CA,
  template, or vulnerability filter once evidence supplies the target.
gotchas: |
  This guidance is bound to the declared SHA-256. The parser uses verb names and
  double-dash options. `find`, `cas`, `download`, and slash-style options are legacy
  syntax and are not registered by this build.
related_ttps: [certify-v2, certipy, rubeus, whisker, forgecert, passthecert, pkinittools]
alternatives: [certipy, adcsscan-bof]
common_args:
  enum-cas:
    description: Enumerate certificate authorities
    typical_values: [flag-only]
    required: false
  enum-templates:
    description: Enumerate certificate templates
    typical_values: [flag-only]
    required: false
  request:
    description: Request a certificate
    typical_values: [flag-only]
    required: false
  request-download:
    description: Download a prior certificate request
    typical_values: [flag-only]
    required: false
  --filter-vulnerable:
    description: Restrict CA or template enumeration to entries classified as vulnerable
    typical_values: [flag-only]
  --ca:
    description: CA in SERVER\CA-NAME format
    typical_values: ["SERVER\\CA-NAME"]
  --template:
    description: Certificate template name
    typical_values: [User, VulnerableTemplate]
  --upn:
    description: UPN subject alternative name; the compiled property is sequence-valued
    typical_values: ["user@domain.local"]
  --output-pem:
    description: Emit the request or downloaded certificate in PEM form
    typical_values: [flag-only]
last_updated: 2026-10-07
---

# Certify

This entry describes the exact inspected Certify v2 artifact when its SHA-256 matches. Its compiled
parser registers separate verbs for CA enumeration, template enumeration, requests, and
request downloads. Establish the applicable CA, template, permissions, and certificate
mapping from observed evidence before selecting a request.

The parser schema is statically inspected. Off-target help compatibility and Mythic task
rendering are separate checks; neither is claimed by this reference.

## Typical use cases

- Use `enum-cas` to inspect certificate authorities and CA-wide vulnerability indicators.
- Use `enum-templates` to inspect enrollment and template conditions.
- Use `request` only after a specific CA and template are supported by evidence.
- Use `request-download` when a prior request ID and the required key context are known.

## Current argument form

The first token is a registered verb. Every option uses a double dash. `request` requires
`--ca` and `--template`; a bare request is parser-invalid. For example:

```text
enum-templates --filter-vulnerable --current-user
request --ca SERVER\CA-NAME --template TEMPLATE --upn USER@DOMAIN --output-pem
```

## Full Reference

> Binary-backed scope: Certify display version 2.0.0, SHA-256
> `1383e8bca90e4be70ff2224b7e2af465a8f3d5bede1fde60f1ece71e3b02d346`.
> Registration and argument shapes come from compiled parser metadata; help execution is provisional.

| Current verb | Required options | Selected options |
|---|---|---|
| `enum-cas` | — | `--ca`, `--domain`, `--ldap-server`, `--current-user`, `--target-user`, `--filter-vulnerable`, `--skip-web-checks` |
| `enum-templates` | — | `--ca`, `--template`, `--domain`, `--ldap-server`, `--current-user`, `--target-user`, `--filter-vulnerable`, `--filter-enabled` |
| `request` | `--ca`, `--template` | `--subject`, sequence-valued `--upn`/`--dns`/`--email`, `--sid-url`, `--sid`, sequence-valued `--application-policy`, `--key-size`, `--machine`, `--output-pem`, `--output-csr`, `--install` |
| `request-download` | `--ca`, `--id` | `--private-key`, `--output-pem`, mutually exclusive `--install-machine`/`--install-user` |

The compiled parser also registers `enum-pkiobjects`, `request-agent`, `request-renew`,
`forge`, `manage-ca`, `manage-template`, and `manage-self`. Their metadata is inventoried
in the adjacent static contract, but they are not enrolled in this initial structured subset.

### Legacy scope

Older Certify builds used `find`, `cas`, and `download` with slash-style options such as
`/vulnerable`, `/ca:`, `/template:`, and `/altname:`. Retain that syntax only when a
separately identified legacy executable requires it. It is invalid guidance for this hash.
