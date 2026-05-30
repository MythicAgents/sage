---
name: PKINITtools
category: adcs
subcategories: [pkinit, unpac-the-hash, s4u2self, certificate-auth]
tradecraft_tags: [adcs, pkinit, unpac, nt-hash-recovery, kerberos, python, linux]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://github.com/dirkjanm/PKINITtools
  license: MIT
  maintained: true
binary_type: python-script
binary_filename: gettgtpkinit.py
supported_os: [linux, windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  PKINIT authentication generates Kerberos events with pre-auth type 16 (certificate)
  which is unusual for user accounts that don't normally use smart cards. The AS-REQ
  with PKINIT pre-auth is detectable. NT hash recovery via UnPAC (getnthash.py) generates
  a service ticket request with MS-PAC decryption.
usage_examples:
  - description: Get TGT via PKINIT (from Linux)
    args: "python3 gettgtpkinit.py -cert-pfx admin.pfx -pfx-pass P@ss north.sevenkingdoms.local/administrator admin.ccache"
  - description: Get NT hash from PKINIT TGT (UnPAC the hash)
    args: "python3 getnthash.py -key <session_key> north.sevenkingdoms.local/administrator"
  - description: Get TGT using certificate from Certify output
    args: "python3 gettgtpkinit.py -cert-pem cert.pem -key-pem key.pem domain.local/user user.ccache"
opsec_notes: |
  Python tools — run from attacker infrastructure (Linux). For Windows-side PKINIT,
  Rubeus is preferred (native .NET assembly, Apollo-compatible). PKINITtools are the
  Linux/impacket-compatible path for ADCS exploitation chains run entirely from the
  attacker's system without a Windows foothold.
gotchas: |
  Python-only — not runnable from Apollo. These are the Linux equivalents of Rubeus
  PKINIT commands. Use Rubeus when on Windows. PKINITtools require impacket. The
  session key from gettgtpkinit.py output is required as input to getnthash.py —
  save it from the output. DC must support PKINIT (Windows Server 2016+ functional level).
related_ttps: [certify, rubeus, forgecert, passthecert]
alternatives: [rubeus-pkinit, certipy]
common_args:
  -cert-pfx:
    description: PFX certificate file
    typical_values: ["admin.pfx"]
  -pfx-pass:
    description: PFX password
    typical_values: ["P@ss123", "''"]
  -cert-pem:
    description: PEM certificate file (alternative to PFX)
    typical_values: ["cert.pem"]
  -key-pem:
    description: PEM private key file
    typical_values: ["key.pem"]
  target:
    description: Domain/username in domain.local/username format
    typical_values: ["north.sevenkingdoms.local/administrator"]
    required: true
  ccache:
    description: Output ccache file for the resulting TGT
    typical_values: ["admin.ccache"]
    required: true
last_updated: 2026-05-29
---

# PKINITtools

Dirk-jan Mollema's Python toolkit for PKINIT-based certificate authentication and
UnPAC-the-hash. Provides the Linux-side equivalent of Rubeus PKINIT commands:
`gettgtpkinit.py` authenticates with a certificate to obtain a TGT (saved as a
ccache file), and `getnthash.py` recovers the NT hash from the PKINIT TGT's PAC.
The third tool `gets4uticket.py` performs S4U2self ticket acquisition for impersonation.

## Typical use cases
- Obtain a TGT from a certificate (from ESC1, Whisker, ForgeCert) on Linux
- UnPAC-the-hash: recover NT hash from PKINIT TGT without cracking
- S4U2self ticket impersonation from a machine account certificate
- All-Linux ADCS exploitation chain alongside impacket/certipy

## How Sage uses this
PKINITtools are infrastructure-side tools for Linux-based engagements. In Apollo-only
Windows engagements, Rubeus handles all PKINIT operations. Documented for completeness
and for engagements where C2 infrastructure includes Linux-side tooling.

## Apollo-specific note
Python-only — not runnable from Apollo. For Windows-side PKINIT authentication, use
Rubeus `asktgt /certificate:`.

## Output
- `gettgtpkinit.py`: prints session key, writes ccache file with TGT
- `getnthash.py`: prints recovered NT hash
- `gets4uticket.py`: writes ccache with service ticket
