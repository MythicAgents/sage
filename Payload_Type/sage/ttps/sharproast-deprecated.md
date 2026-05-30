---
name: SharpRoast (Deprecated)
category: kerberos
subcategories: [kerberoast-deprecated, superseded-by-rubeus]
tradecraft_tags: [kerberoast, kerberos, deprecated, ghostpack, historical-reference]
mitre_attack:
  - id: T1558.003
    name: Steal or Forge Kerberos Tickets — Kerberoasting
source:
  url: https://github.com/GhostPack/SharpRoast
  license: BSD-3-Clause
  maintained: false
binary_type: .net-assembly
binary_filename: SharpRoast.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  TGS requests for SPN-mapped accounts — identical to Rubeus kerberoast detection.
usage_examples:
  - description: This tool is deprecated — use Rubeus instead
    args: "Rubeus.exe kerberoast /ldaponly  (inventory without hash request — no cracking needed)"
opsec_notes: |
  SharpRoast is DEPRECATED and superseded by Rubeus. Use Rubeus for all kerberoasting
  operations. SharpRoast only does kerberoasting (requesting TGS hashes for offline
  cracking) — it has NO /ldaponly mode, NO AS-REP roasting, NO S4U capabilities,
  NO PKINIT, etc. Rubeus covers all of these.
gotchas: |
  DO NOT USE SharpRoast in new engagements. It is archived and Rubeus supersedes it
  completely. This entry is included only for historical reference — if SharpRoast
  appears in a target's binary blocklist or EDR signatures, it indicates historical
  exposure. For Sage operations, Rubeus is the only Kerberos tool needed.
related_ttps: [rubeus, rubeus-kerberoast-nocrack, asrep-roast-inventory]
alternatives: [rubeus-kerberoast]
common_args: {}
last_updated: 2026-05-29
---

# SharpRoast (Deprecated — Use Rubeus)

GhostPack's original C# kerberoasting tool. DEPRECATED — completely superseded by
Rubeus which provides:
- All of SharpRoast's kerberoasting functionality
- AS-REP roasting
- S4U2self / S4U2proxy chains
- PKINIT / shadow credential auth
- Golden/Diamond/Silver tickets
- /ldaponly mode (inventory without hash request)
- Pass-the-ticket injection
- And much more

**If you see SharpRoast referenced: use Rubeus instead.**

The only operational relevance of SharpRoast in 2026 is as historical context.
