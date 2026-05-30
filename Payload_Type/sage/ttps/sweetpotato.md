---
name: SweetPotato
category: privilege-escalation
subcategories: [seimpersonate, com-abuse, token-impersonation, local-pe]
tradecraft_tags: [seimpersonate, com, token-impersonation, local-pe, dotnet, multiplex]
mitre_attack:
  - id: T1134.002
    name: Access Token Manipulation — Create Process with Token
source:
  url: https://github.com/CCob/SweetPotato
  license: MIT
  maintained: true
binary_type: .net-assembly
binary_filename: SweetPotato.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  COM object instantiation, token impersonation, and SYSTEM process spawning from a
  service account process. Behavioral EDR signatures for potato-family exploits.
  SweetPotato uses multiple coercion techniques — each generates its own telemetry.
usage_examples:
  - description: Execute a command as SYSTEM using automatic technique selection
    args: "-p cmd.exe -a '/c whoami > C:\\Windows\\Temp\\out.txt'"
  - description: Execute using ImpersonateLoggedOnUser technique
    args: "-e ImpersonateLoggedOnUser -p cmd.exe"
  - description: Execute using EfsRpc coercion (PetitPotam-style local)
    args: "-e EfsRpc -p cmd.exe -a '/c whoami'"
opsec_notes: |
  SweetPotato combines multiple SeImpersonate exploit techniques in one tool — EfsRpc
  (PetitPotam local), ImpersonateLoggedOnUser, PrintSpoofer, and others. Good for
  environments where one technique is patched. The auto-select mode tries multiple
  methods. Use GodPotato first (broader Windows version support); SweetPotato as
  a fallback if GodPotato fails.
gotchas: |
  Requires SeImpersonatePrivilege or SeAssignPrimaryTokenPrivilege. Print Spooler
  technique requires spoolsv.exe running. EfsRpc technique requires EFS service.
  Auto-selection is convenient but may try noisy methods. If a specific technique
  is needed, specify `-e` explicitly. GodPotato is generally preferred for broad
  compatibility.
related_ttps: [godpotato, printspoofer, juicypotatong, sharpup]
alternatives: [godpotato, printspoofer]
common_args:
  -p:
    description: Process to execute
    typical_values: ["cmd.exe", "powershell.exe"]
    required: true
  -a:
    description: Arguments for the process
    typical_values: ["/c whoami", "/c net user ..."]
  -e:
    description: Exploit technique to use
    typical_values: [EfsRpc, PrintSpoofer, ImpersonateLoggedOnUser, JuicyPotato]
last_updated: 2026-05-29
---

# SweetPotato

A multi-technique SeImpersonatePrivilege exploitation tool that combines EfsRpc (local
PetitPotam), PrintSpoofer, ImpersonateLoggedOnUser, and JuicyPotato techniques in a
single .NET assembly. Useful when one specific technique is patched or unavailable —
SweetPotato tries alternatives automatically. GodPotato is generally the first choice
for broad Windows version compatibility; SweetPotato is a useful fallback.

## Typical use cases
- SeImpersonatePrivilege escalation when GodPotato fails
- Multi-technique fallback for environments with partially hardened spooler/EFS settings

## How Sage uses this
SweetPotato is Sage's backup SeImpersonate exploit when GodPotato doesn't work on the
target. Try GodPotato first; fall back to SweetPotato with explicit technique selection.

## Output
SYSTEM-level command execution. Process spawned or command output returned to Apollo.
