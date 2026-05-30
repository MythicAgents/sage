---
name: Zerologon
category: privilege-escalation
subcategories: [cve-2020-1472, netlogon, dc-takeover]
tradecraft_tags: [zerologon, cve-2020-1472, netlogon, dc-compromise, domain-takeover]
mitre_attack:
  - id: T1068
    name: Exploitation for Privilege Escalation
source:
  url: https://github.com/SecuraBV/CVE-2020-1472
  license: MIT
  maintained: false
binary_type: python-script
binary_filename: zerologon_tester.py
supported_os: [linux]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  Zerologon exploitation generates a flood of Netlogon authentication failures
  (Event 4742 — computer account changed, Event 4625 — logon failure pattern) before
  succeeding. Microsoft released detection guidance (Event ID 5805 — Netlogon secure
  channel set up denied). MDI has specific Zerologon detection. Modern patched systems
  are completely immune.
usage_examples:
  - description: Test if DC is vulnerable (no exploit)
    args: "python3 zerologon_tester.py DC01 192.168.56.10"
  - description: Exploit — set DC machine account password to empty (DESTRUCTIVE)
    args: "(DO NOT run in production — changes DC machine account password; requires immediate restoration)"
opsec_notes: |
  Zerologon (CVE-2020-1472) sets the DC machine account password to empty, allowing
  DCSync. CRITICAL: this breaks DC functionality and must be immediately reversed after
  exploitation (restore the original password from previous domain sync or the domain
  will degrade). Most DCs are patched as of 2021. Watson.exe will indicate vulnerable
  Netlogon configurations. Python-only — infrastructure side.
gotchas: |
  DESTRUCTIVE — resetting the DC machine account password breaks all Kerberos services
  on that DC until restored. This is a last-resort technique for engagements where
  restoration is planned. HIGHLY DETECTABLE — the exploitation flood of failed
  Netlogon requests generates massive event logs. Most environments are patched
  (August 2020 patch KB4570333). Use Watson to check Netlogon secure channel enforcement.
  Restoration requires: impacket restoreconfidentialattribute to restore the original
  machine account password hash from the NTDS.dit.
related_ttps: [impacket-secretsdump, watson, mimikatz, shadow-copy-ntds]
alternatives: [dcsync-with-da, rbcd-dc-compromise]
common_args:
  DC_NAME:
    description: NetBIOS name of the DC
    typical_values: ["DC01"]
    required: true
  DC_IP:
    description: IP address of the DC
    typical_values: ["192.168.56.10"]
    required: true
last_updated: 2026-05-29
---

# Zerologon

CVE-2020-1472 — a critical vulnerability in the Netlogon Remote Protocol that allows an
unauthenticated attacker to set the DC machine account password to empty, enabling
immediate DCSync. The vulnerability stems from a cryptographic flaw in Netlogon's
AES-CFB8 usage. Patched in August 2020 (KB4570333); most production DCs are patched.

## Exploitation Chain

```
1. Test vulnerability: zerologon_tester.py DC01 DC_IP (no authentication needed)
2. If vulnerable:
   a. Set DC machine account password to empty (zerologon_exploit.py)
   b. DCSync using empty DC machine account: 
      secretsdump.py -no-pass DOMAIN/DC01$@DC_IP
   c. CRITICAL: Immediately restore DC machine account password:
      restoreconfidentialattribute.py
```

## When to Use

Only in lab environments or engagements with explicit authorization for potentially
disruptive techniques. Production DCs should NOT be exploited with Zerologon unless:
1. A reliable restoration procedure is in place
2. A maintenance window is available
3. The scope explicitly allows destructive techniques

## Typical use cases
- Lab/CTF: quick domain compromise on unpatched DC
- Test whether a DC is patched (tester mode — no exploitation)

## How Sage uses this
Watson identifies vulnerable Netlogon configurations. Sage reports the vulnerability
to the operator but does NOT autonomously exploit Zerologon without explicit approval
due to the destructive nature (DC disruption). This is one of the few techniques
where Sage asks for operator confirmation regardless of autonomy level.

## Note
Most DCs are patched as of 2026. This is primarily a historical reference and lab
technique.
