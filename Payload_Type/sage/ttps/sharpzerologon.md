---
name: SharpZeroLogon
category: privilege-escalation
subcategories: [cve-2020-1472, netlogon, dotnet-zerologon]
tradecraft_tags: [zerologon, cve-2020-1472, netlogon, dotnet, apollo-runnable]
mitre_attack:
  - id: T1068
    name: Exploitation for Privilege Escalation
source:
  url: https://github.com/nccgroup/nccfsas/tree/main/Tools/SharpZeroLogon
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpZeroLogon.exe
supported_os: [windows]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  Same as Zerologon — Netlogon failure flood before success, Event 4742 (computer account
  changed), MDI Zerologon detection signatures.
usage_examples:
  - description: Test for Zerologon vulnerability (no exploitation)
    args: "SharpZeroLogon.exe DC01 192.168.56.10 test"
  - description: Exploit (DESTRUCTIVE — requires restoration plan)
    args: "SharpZeroLogon.exe DC01 192.168.56.10"
opsec_notes: |
  .NET assembly version of Zerologon — Apollo-runnable via inline_assembly. Same
  operational cautions as the Python variant (destructive, detectable, requires
  restoration). Most DCs patched; primarily useful in lab/CTF contexts.
gotchas: |
  DESTRUCTIVE. Same as Python Zerologon variant. Use test mode first. Restoration
  required immediately after exploitation. Most production DCs are patched.
related_ttps: [zerologon, watson, impacket-secretsdump]
alternatives: [zerologon-python, dcsync-with-creds]
common_args:
  DC_NETBIOS:
    description: DC NetBIOS name
    typical_values: ["DC01"]
    required: true
  DC_IP:
    description: DC IP address
    typical_values: ["192.168.56.10"]
    required: true
  test:
    description: Test-only mode (no exploitation)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpZeroLogon

The .NET assembly version of Zerologon (CVE-2020-1472), making it Apollo inline_assembly
compatible. Same exploitation logic as the Python variant but runnable directly from
within an Apollo agent without Python infrastructure.

## How Sage uses this
If Watson identifies an unpatched Netlogon configuration and explicit operator approval
is given for the Zerologon chain, SharpZeroLogon can be executed via Apollo's
inline_assembly. Always use test mode first (`test` argument).

See `zerologon.md` for full operational context, restoration requirements, and cautions.
