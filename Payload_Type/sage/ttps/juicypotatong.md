---
name: JuicyPotatoNG
category: privilege-escalation
subcategories: [seimpersonate, token-impersonation, local-pe, com-abuse]
tradecraft_tags: [seimpersonate, com, token-impersonation, windows-server, local-pe]
mitre_attack:
  - id: T1134.002
    name: Access Token Manipulation — Create Process with Token
source:
  url: https://github.com/antonioCoco/JuicyPotatoNG
  license: MIT
  maintained: true
binary_type: native-exe
binary_filename: JuicyPotatoNG.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  DCOM activation events, COM impersonation, and token manipulation are the primary
  signals. Suspicious parent/child process relationships (DCOM server spawning cmd.exe).
  Behavioral EDR signatures for potato exploits. Sysmon event 10 for process access.
usage_examples:
  - description: Run command as SYSTEM (using CLSID enumeration)
    args: "-t * -p cmd.exe -a '/c whoami'"
  - description: Specify a CLSID explicitly
    args: "-t * -p cmd.exe -a '/c whoami' -c {CLSID}"
  - description: Spawn a reverse shell
    args: "-t * -p cmd.exe -a '/c powershell -enc <base64>'"
opsec_notes: |
  JuicyPotatoNG is a NATIVE EXE — Apollo cannot use inline_assembly for this.
  Use GodPotato (.NET assembly) instead for Apollo-friendly delivery. JuicyPotatoNG
  improved on the original JuicyPotato/RottenPotatoNG for newer Windows versions
  (Server 2019, 2022, Windows 10 1809+). COM-based impersonation events are detectable
  by modern EDR.
gotchas: |
  NATIVE EXE — use GodPotato (.NET assembly) for Apollo inline_assembly delivery.
  JuicyPotatoNG is the recommended variant for Windows Server 2019/2022 when GodPotato
  isn't available. Requires SeImpersonatePrivilege or SeAssignPrimaryTokenPrivilege.
  The `-t *` flag tries both CreateProcessWithTokenW and CreateProcessAsUser.
related_ttps: [godpotato, printspoofer, sharpup]
alternatives: [godpotato, printspoofer, sweetpotato]
common_args:
  -t:
    name: -t
    description: Type of token creation (*, t, u — use * to try both)
    typical_values: ["*"]
    required: true
  -p:
    name: -p
    description: Process to launch
    typical_values: ["cmd.exe", "powershell.exe"]
    required: true
  -a:
    name: -a
    description: Arguments for the spawned process
    typical_values: ["/c whoami", "/c net user ..."]
  -c:
    name: -c
    description: Specific COM CLSID to use for impersonation
    typical_values: ["{90f18417-f0f1-484e-9d3c-59dceee5dbd8}"]
last_updated: 2026-05-29
---

# JuicyPotatoNG

The "NG" (New Generation) variant of the JuicyPotato SeImpersonatePrivilege exploit by
antonioCoco. Addresses compatibility issues with Windows 10 1809+ and Server 2019/2022
that broke earlier potato variants. Uses DCOM server impersonation with a broader
set of working CLSIDs. For Apollo engagements, prefer GodPotato (.NET assembly) to
avoid the native-exe delivery challenge.

## Typical use cases
- SYSTEM escalation on Windows Server 2019/2022 when GodPotato isn't available
- Service account / IIS / MSSQL context with SeImpersonatePrivilege

## Apollo-specific note
Native EXE — Apollo cannot use inline_assembly for this. Prefer GodPotato (`.net-assembly`)
for Apollo engagements. If JuicyPotatoNG must be used, it requires a different execution
primitive (spawn, shellcode wrapper, or dropping to disk).
