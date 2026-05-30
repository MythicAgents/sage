---
name: SpecterOps Blog Research Highlights
category: recon
subcategories: [research-reference, blog-archive, technique-discovery]
tradecraft_tags: [specterops, ghostpack, research, blog, posts.specterops.io, harmj0y, tifkin, reference]
mitre_attack: []
source:
  url: https://posts.specterops.io
  license: none
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [windows, linux, macos]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Reference document — no tools, no detection signal.
usage_examples:
  - description: Reference for understanding SpecterOps attack research provenance
    args: "(research reference)"
opsec_notes: |
  Sage's tradecraft library is heavily based on SpecterOps research. This document
  provides the canonical research references for attribution and deeper reading.
gotchas: |
  Blog posts are updated over time. Key papers should be saved locally as PDFs.
related_ttps: [certify-adcs-primer, bloodhound-enterprise-vs-ce, sharphound-enterprise,
               certify, certify-v2, adcs-esc7, adcs-esc9-esc10, koh, lockless]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# SpecterOps Blog Research Highlights

Authoritative research references for the techniques in Sage's TTP library.
Primary sources for Sage's AD, ADCS, and delegation tradecraft.

## Foundational Papers

| Paper | Authors | Published | Covers |
|-------|---------|-----------|--------|
| Certified Pre-Owned | Schroeder + Christensen | June 2022 | ESC1-ESC8, ADCS attack model |
| An Ace Up the Sleeve | Schroeder + Robbins | Feb 2017 | ACL abuse primitives, BloodHound ACE analysis |
| Abusing AD Delegation | Schroeder | 2017-2019 | Constrained/unconstrained/RBCD delegation chains |
| From Zero to Domain Admin | Schroeder | Various | Full AD compromise chains |
| Shadow Credentials | Shamir | Aug 2021 | msDS-KeyCredentialLink (Whisker technique basis) |

## Key Blog Posts by Technique Area

### ADCS
- "Certified Pre-Owned" (blog): https://posts.specterops.io/certified-pre-owned-d95910965cd2
- "Certipy 4.0: ESC9 & ESC10": https://research.ifcr.dk/certipy-4-0-...
- "Certificates and Pwnage and Patches": https://posts.specterops.io/certificates-and-pwnage-and-patches-oh-my-8ae0f4304c1d

### Kerberos / Delegation
- "S4U2Pwnage" (Schroeder): https://blog.harmj0y.net/activedirectory/s4u2pwnage/
- "Roasting AS-REPs" (Schroeder): https://blog.harmj0y.net/activedirectory/roasting-as-reps/
- "The Art of Asking Nicely" (Schroeder): S4U2self + unconstrained delegation

### BloodHound / ACL
- "An Ace Up the Sleeve": https://specterops.io/assets/resources/an_ace_up_the_sleeve.pdf
- "Abusing Active Directory Permissions with PowerView": https://blog.harmj0y.net/redteaming/abusing-active-directory-permissions-with-powerview/

### Credential Access
- "Koh: The Token Stealer" (Schroeder): https://posts.specterops.io/koh-the-token-stealer-41ca07a8a1a8
- "Hunting for Credentials in Process Memory" (Schroeder)

### SCCM
- "Exploring SCCM by Unintentionally Bypassing Network Access Accounts": https://posts.specterops.io/...
- "SCCM Exploitation: The Subtle Art of NAA Abuse"

### Shadow Credentials
- "Shadow Credentials: Abusing Key Trust Account Mapping": https://posts.specterops.io/shadow-credentials-abusing-key-trust-account-mapping-for-takeover-8ee1a53566ab

## Researcher Profiles

| Researcher | Handle | Primary blog / profile |
|-----------|--------|----------------------|
| Will Schroeder | @harmj0y | https://harmj0y.medium.com / https://blog.harmj0y.net |
| Lee Christensen | @tifkin_ | https://medium.com/@tifkin |
| Andy Robbins | @_wald0 | https://posts.specterops.io |
| Elad Shamir | @elad_shamir | https://eladshamir.com |
| Chris Thompson | @_Mayyhem | https://posts.specterops.io |
| Oliver Lyak | @ly4k_ | https://research.ifcr.dk |

## Staying Current

SpecterOps publishes new research at https://posts.specterops.io — subscribe to
their newsletter or follow @specterops on social media for new technique publications.
Most new GhostPack/Certipy tools are announced with accompanying blog posts that explain
the underlying technique.
