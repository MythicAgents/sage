---
name: SharpPotato
category: privilege-escalation
subcategories: [seimpersonate, com-abuse, local-pe, potato-family]
tradecraft_tags: [seimpersonate, potato, com, token-impersonation, local-pe, dotnet, apollo-runnable]
mitre_attack:
  - id: T1134.002
    name: Access Token Manipulation — Create Process with Token
source:
  url: https://github.com/uknowsec/SharpPotato
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpPotato.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Same as other potato family exploits — COM activation, token impersonation from
  a service account context. Behavioral EDR signatures for potato exploits cover
  this technique.
usage_examples:
  - description: Escalate to SYSTEM from SeImpersonatePrivilege service account context
    args: "SharpPotato.exe 'cmd.exe /c whoami > C:\\Windows\\Temp\\out.txt'"
  - description: Spawn a SYSTEM shell
    args: "SharpPotato.exe 'cmd.exe'"
opsec_notes: |
  Another SeImpersonate exploit in the potato family. Prefer GodPotato for modern
  Windows (broader compatibility). SharpPotato is documented for completeness and as
  a fallback when GodPotato fails on specific configurations.
gotchas: |
  GodPotato is the preferred SeImpersonate exploit for Apollo operators due to broader
  Windows version compatibility. SharpPotato is an older variant. Always run SharpUp's
  TokenPrivileges check to confirm SeImpersonatePrivilege is present before attempting.
related_ttps: [godpotato, printspoofer, sweetpotato, juicypotatong, sharpup]
alternatives: [godpotato, printspoofer, sweetpotato]
common_args:
  command:
    description: Command to execute as SYSTEM
    typical_values: ["cmd.exe /c whoami > C:\\\\Windows\\\\Temp\\\\out.txt", "cmd.exe"]
    required: true
last_updated: 2026-05-29
---

# SharpPotato

Another SeImpersonatePrivilege exploitation tool in the Potato family. Uses COM-based
impersonation to escalate from a service account context to SYSTEM. GodPotato is
generally preferred for Apollo operations due to better Windows version compatibility.
SharpPotato is documented as a reference and fallback.

## When to Use vs GodPotato

| Tool | Best for |
|------|---------|
| GodPotato | Windows Server 2012-2022 and Windows 10/11 — first choice |
| SweetPotato | Multi-technique fallback when GodPotato fails |
| PrintSpoofer | When Print Spooler is running and no other potato works |
| SharpPotato | Older/specific configurations; generally superseded |
| JuicyPotatoNG | Server 2019/2022 when GodPotato unavailable |
