---
name: ADCS Scan BOF (adcs_enum)
category: adcs
subcategories: [adcs-enumeration, bof, in-process-adcs, certify-alternative]
tradecraft_tags: [adcs, certificate, bof, in-process, enumeration, esc, athena]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://github.com/trustedsec/CS-Situational-Awareness-BOF
  license: BSD-3-Clause
  maintained: true
binary_type: bof
binary_filename: adcs_enum.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  adcs_enum uses in-process LDAP queries against the PKI configuration container —
  same query pattern as Certify but executed inside the C2 agent process (no separate
  .NET AppDomain creation). The LDAP queries themselves are the detection signal;
  the in-process execution reduces the process-creation footprint.
usage_examples:
  - description: Enumerate ADCS Certificate Authorities and templates (BOF mode)
    args: "execute-bof adcs_enum.x64.o"
  - description: Enumerate with specific domain targeting
    args: "execute-bof adcs_enum_com.x64.o"
  - description: Certify equivalent (Apollo - .NET assembly)
    args: "Certify.exe find /vulnerable"
opsec_notes: |
  adcs_enum BOF provides the same enumeration as Certify's `find` command but runs
  entirely in-process via Athena's execute-bof — no new process creation, no AppDomain
  load. For Athena-based engagements, this is the preferred ADCS enumeration path.
  For Apollo: use Certify.exe via inline_assembly.
gotchas: |
  Apollo has no BOF runner — requires Athena. adcs_enum is part of TrustedSec's
  CS-Situational-Awareness-BOF collection. The output format differs slightly from
  Certify — it's more raw LDAP data vs Certify's structured vulnerability classification.
  For vulnerability classification, parse the adcs_enum output manually or follow up
  with Certify/Certipy for confirmation.
related_ttps: [certify, certipy, trustedsec-bofs, adcs-esc4, adcs-esc6, adcs-esc8]
alternatives: [certify, certipy]
common_args:
  adcs_enum:
    description: Enumerate CAs and certificate templates via in-process LDAP
    typical_values: [flag-only]
  adcs_enum_com:
    description: Alternative ADCS enumeration via COM interface
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# ADCS Scan BOF (adcs_enum)

The TrustedSec CS-Situational-Awareness-BOF's ADCS enumeration module. Provides the
same data as Certify `find` but executed entirely in-process via Athena's execute-bof —
eliminating the AppDomain creation event that Certify's inline_assembly execution generates.

## Certify vs adcs_enum BOF Comparison

| Method | Process created? | AppDomain? | Signal |
|--------|----------------|-----------|--------|
| Certify inline_assembly | No | YES | AppDomain creation |
| adcs_enum BOF (Athena) | No | No | In-process LDAP only |

## Enumeration Output

adcs_enum lists:
- Certificate Authorities (name, DNS, enrollment endpoint)
- Certificate templates (name, permissions, flags)
- Identifies high-level misconfiguration indicators

For vulnerability classification (ESC1-13), follow up with Certify or Certipy after
initial adcs_enum discovery.

## Apollo-specific note
BOF — requires Athena. For Apollo, use Certify.exe via inline_assembly.
