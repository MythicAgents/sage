---
name: Red Team Infrastructure Design
category: command-and-control
subcategories: [infrastructure, redirectors, c2-opsec, apache-redirectors]
tradecraft_tags: [infrastructure, redirector, c2, apache, mod-rewrite, cdn, cloudfront, categorization]
mitre_attack:
  - id: T1090
    name: Proxy
  - id: T1102
    name: Web Service
source:
  url: https://github.com/bluscreenofjeff/Red-Team-Infrastructure-Wiki
  license: Unknown
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [linux]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Infrastructure design affects detection of the C2 channel. Well-designed infrastructure
  uses traffic malleable profiles, categorized domains, and redirectors to blend C2
  traffic with legitimate HTTPS traffic.
usage_examples:
  - description: Deploy Apache mod_rewrite redirector for C2 traffic
    args: "(see https://github.com/bluscreenofjeff/Red-Team-Infrastructure-Wiki)"
  - description: Categorize C2 domain for traffic blending
    args: "(domain categorization services: bluecoat, symantec, fortiguard)"
opsec_notes: |
  Jeff Dimmock (@bluscreenofjeff) published the authoritative Red Team Infrastructure
  Wiki — covering redirectors, domain fronting, CDN-based C2, domain categorization,
  and Apache mod_rewrite rulesets. This reference documents the infrastructure patterns
  for making Mythic/Apollo C2 traffic appear legitimate.
gotchas: |
  Domain fronting (using CDN host headers to route C2 through legitimate CDN infrastructure)
  has been largely addressed by major CDN providers (AWS CloudFront, Azure CDN blocked
  domain fronting in 2018-2019). Modern infrastructure relies on domain categorization,
  certificate legitimacy, and traffic malleable profiles (Cobalt Strike Malleable C2,
  Mythic's agent profile options).
related_ttps: [chisel, ligolo-ng, post-exploitation-playbook, opsec-checklist]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# Red Team Infrastructure Design

Reference for designing C2 infrastructure that evades network-level detection.
Based on Jeff Dimmock's (@bluscreenofjeff) Red Team Infrastructure Wiki and SpecterOps
infrastructure research.

## Core Components

```
Internet → [Phishing / Payload Delivery] → Initial Callback
                                                ↓
[C2 Server (Mythic)] ← [Redirector] ← Target machine
    (hidden)               (front)
```

## Redirector Patterns

### Apache mod_rewrite Redirector

```apache
# Redirect legitimate C2 traffic to Mythic backend, everything else to Google:
RewriteRule ^/api/v1/agent_check.*$ https://MYTHIC_BACKEND%{REQUEST_URI} [P,L]
RewriteRule ^.*$ https://www.google.com/ [R=302,L]
```

This ensures:
- Blue team scanning the redirector sees no C2 content
- Only properly-formatted C2 traffic reaches the Mythic server
- Unauthorized access redirects to benign destination

## Domain Selection

Good C2 domains:
- **Aged domains**: >1 year old, previously categorized as legitimate
- **Correct category**: "Information Technology" or "Business" category
- **Valid certificate**: Let's Encrypt or purchased certificate (not self-signed)
- **Passive DNS history**: Some legitimate-looking DNS history

## Mythic-Specific Infrastructure

Mythic (and Sage) relevant infrastructure:
- Apollo HTTPS profile: Configure callback host/user-agent/headers to match target environment
- C2 profile: Set to match network behavior expected for the implant's supposed purpose
- Kill date: Configure Apollo to stop calling back after engagement end

## Traffic Profile

For Apollo HTTPS C2:
- Callback interval: 60-300s (shorter looks more like malware; longer blends with update checks)
- User-Agent: Match target browser/OS
- Host header: Configured in C2 profile
- Certificate: Legitimate-looking (not self-signed)

## Resources

- Red Team Infrastructure Wiki: https://github.com/bluscreenofjeff/Red-Team-Infrastructure-Wiki
- Malleable C2 profiles: https://github.com/rsmudge/Malleable-C2-Profiles
- Cobalt Strike CDN redirect: https://bluescreenofjeff.com/2018-04-12-https-using-lets-encrypt.html
