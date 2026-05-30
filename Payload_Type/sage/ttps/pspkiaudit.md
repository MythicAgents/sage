---
name: PSPKIAudit
category: adcs
subcategories: [adcs-audit, certificate-template-review, pki-misconfiguration]
tradecraft_tags: [adcs, pki, audit, powershell, certificate-templates, ghostpack, complementary-to-certify]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://github.com/GhostPack/PSPKIAudit
  license: BSD-3-Clause
  maintained: true
binary_type: powershell-script
binary_filename: PSPKIAudit.ps1
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  PowerShell queries to the PKI configuration container via the PSPKI PowerShell module.
  LDAP queries to the Certificate Templates container — same detection as Certify find.
  Script block logging (Event 4104) captures the PSPKIAudit function calls if PS logging
  is enabled. AMSI must be bypassed before loading.
usage_examples:
  - description: Full AD CS audit (requires PSPKI module)
    args: "Import-Module PSPKIAudit.ps1; Invoke-PKIAudit"
  - description: Audit a specific CA
    args: "Invoke-PKIAudit -CAName 'NORTH-CA'"
  - description: Check certificate templates for ESC misconfigurations
    args: "Get-CertificateTemplateAcl | Find-CertificateTemplateVulnerability"
opsec_notes: |
  PSPKIAudit is a PowerShell-based ADCS auditor — it complements Certify by providing
  more detailed analysis of PKI configuration including CA-level settings that Certify
  may not surface (ESC7 CA ACLs, unusual template permissions). Requires AMSI bypass
  before loading. PowerShell logging (Event 4104) captures the analysis if logging is
  active. For Apollo, use powershell_import with prior AMSI bypass.
gotchas: |
  PSPKIAudit requires the PSPKI PowerShell module to be installed OR loaded alongside it.
  PSPKI is a third-party module (not built-in Windows) — it needs to be loaded separately
  if not available on the target: `Import-Module .\PSPKI.ps1`. Certify.exe is generally
  preferred for operational use (inline_assembly delivery, no AMSI concern, no module dependency).
  PSPKIAudit is most useful for comprehensive PKI auditing (defender perspective) and
  for ESC misconfigurations that Certify doesn't surface (ESC7, CA-level audit flag checks).
related_ttps: [certify, certipy, adcs-esc4, adcs-esc6, adcs-esc8]
alternatives: [certify, certipy]
common_args:
  Invoke-PKIAudit:
    description: Run the full PKI security audit
    typical_values: [flag-only]
  -CAName:
    description: Limit audit to a specific CA name
    typical_values: ["NORTH-CA", "SEVENKINGDOMS-CA"]
last_updated: 2026-05-29
---

# PSPKIAudit

GhostPack's PowerShell-based AD CS auditing tool. Uses the PSPKI PowerShell module
to perform a comprehensive security audit of Active Directory Certificate Services.
Provides deeper analysis than Certify for some ESC categories, particularly ESC7
(CA-level ACL abuse) and EDITF flag checks.

## PSPKIAudit vs Certify vs Certipy

| Tool | Language | Delivery | ESC Coverage | Depth |
|------|----------|---------|-------------|-------|
| Certify | .NET | inline_assembly | ESC1-8 | Good; structures findings |
| Certipy | Python | Infrastructure | ESC1-13 | Comprehensive; Linux-side |
| PSPKIAudit | PowerShell | powershell_import | ESC1-8 + CA ACLs | Deep CA-level; needs PSPKI module |

## When PSPKIAudit Adds Value

PSPKIAudit is most useful when:
- A comprehensive CA-level audit is needed (not just template misconfigs)
- ESC7 (CA ACL abuse) is suspected — PSPKIAudit surfaces CA DACL details
- A report-style output is needed (auditor perspective)
- The target environment already has PSPKI module installed (some enterprise environments do)

For operational use (quick ESC discovery), Certify is preferred.
For comprehensive auditing, PSPKIAudit provides additional CA-level detail.
