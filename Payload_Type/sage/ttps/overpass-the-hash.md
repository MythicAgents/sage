---
name: Overpass-the-Hash
category: kerberos
subcategories: [pass-the-key, tgt-request, ntlm-to-kerberos, credential-reuse]
tradecraft_tags: [overpass-the-hash, opth, kerberos, ntlm, tgt, rubeus, technique]
mitre_attack:
  - id: T1550.002
    name: Use Alternate Authentication Material — Pass the Hash
source:
  url: https://attack.mitre.org/techniques/T1550/002/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Overpass-the-Hash requests a TGT using an NT hash as the RC4 session key. This generates
  a Kerberos AS-REQ with RC4 encryption (etype 23), which is detectable in environments
  where AES-only Kerberos is enforced. AES keys produce etype 17/18 — more common and
  harder to distinguish. MDI detects unusual TGT requests (hash used from different source).
usage_examples:
  - description: Rubeus Overpass-the-Hash (request TGT with NT hash)
    args: "Rubeus.exe asktgt /user:administrator /rc4:<nthash> /domain:north.sevenkingdoms.local /ptt"
  - description: Rubeus with AES256 key (harder to detect than RC4)
    args: "Rubeus.exe asktgt /user:administrator /aes256:<aes256key> /domain:north.sevenkingdoms.local /ptt"
  - description: Mimikatz Overpass-the-Hash
    args: "sekurlsa::pth /user:administrator /domain:NORTH /ntlm:<nthash> /run:cmd.exe"
opsec_notes: |
  RC4-based TGT requests (Rubeus /rc4) generate etype-23 AS-REQ — detectable in
  environments with RC4 auditing. AES-based requests (/aes256) are harder to distinguish.
  Use /aes256 when AES keys are available (they're in LSASS alongside the NT hash, or
  obtainable via Mimikatz). Overpass-the-Hash → Kerberos TGT → all subsequent auth is
  normal Kerberos traffic (no NTLM detection). This is generally preferred over raw
  NTLM Pass-the-Hash for modern networks.
gotchas: |
  Rubeus /rc4 option specifically requests an RC4-encrypted TGT. Modern Kerberos
  environments may reject RC4 (etype 23) if RC4 is disabled in the domain. If RC4
  is disabled, use /aes256 or /aes128 with the corresponding key. AES keys are in
  LSASS (Mimikatz sekurlsa::ekeys shows them). The resulting TGT can be used for
  the full S4U chain (constrained delegation) or pass-the-ticket.
related_ttps: [rubeus, mimikatz, pass-the-hash, pass-the-ticket, constrained-delegation-abuse]
alternatives: [pass-the-hash, pass-the-key]
common_args: {}
last_updated: 2026-05-29
---

# Overpass-the-Hash

"Overpass-the-Hash" converts an NT hash into a Kerberos TGT by using the hash as the
RC4 session key in a Kerberos AS-REQ. The resulting TGT enables fully Kerberos-based
authentication — no raw NTLM pass-the-hash traffic. This is generally preferred over
NTLM PTH because subsequent auth uses normal Kerberos traffic.

## The Distinction

```
Pass-the-Hash (PTH):
  NT hash → NTLM challenge/response → Service access
  (all traffic is NTLM — detectable by NTLM monitoring)

Overpass-the-Hash (OPtH):
  NT hash → Kerberos AS-REQ (with hash as RC4 key) → TGT → TGS → Service access
  (after initial AS-REQ, all traffic is Kerberos — harder to detect)
```

## Rubeus Commands

```
# RC4 (NT hash) — may be detectable if RC4 auditing enabled:
Rubeus.exe asktgt /user:X /rc4:<nthash> /domain:X /ptt

# AES256 (preferred — harder to detect):
Rubeus.exe asktgt /user:X /aes256:<aes256key> /domain:X /ptt

# AES128:
Rubeus.exe asktgt /user:X /aes128:<aes128key> /domain:X /ptt
```

## Getting AES Keys

AES keys are stored in LSASS alongside NT hashes. To extract:
```
Mimikatz: sekurlsa::ekeys   (lists all Kerberos encryption keys from LSASS)
```
The aes256_cts_hmac_sha1 key is the one for /aes256.

## When to Use OPtH vs PTH

- Use OPtH when: Kerberos authentication is preferred (S4U chains, delegation abuse, PKINIT)
- Use PTH when: Kerberos fails or NTLM is the only available protocol (no DC reachable)
- Use neither when: password is known (just use it directly)
