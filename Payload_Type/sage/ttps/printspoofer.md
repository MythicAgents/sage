---
name: PrintSpoofer
category: privilege-escalation
subcategories: [seimpersonate, token-impersonation, local-pe]
tradecraft_tags: [seimpersonate, token-impersonation, spooler, printerbug, local-pe]
mitre_attack:
  - id: T1134.002
    name: Access Token Manipulation — Create Process with Token
source:
  url: https://github.com/itm4n/PrintSpoofer
  license: MIT
  maintained: false
binary_type: native-exe
binary_filename: PrintSpoofer64.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  Named pipe impersonation events, Sysmon event 10 for LSASS/service process access,
  and suspicious parent/child process relationships (spoolsv.exe spawning cmd.exe or
  PowerShell). EDR behavioral signatures for token impersonation from service accounts.
  Apollo native `printspoofer` command may have different detection characteristics.
usage_examples:
  - description: Get a SYSTEM shell
    args: "-i -c cmd"
  - description: Run a specific command as SYSTEM
    args: "-c 'net user backdoor P@ss123! /add'"
  - description: Spawn SYSTEM shell in new window
    args: "-i -c cmd -h"
opsec_notes: |
  PrintSpoofer is a native EXE — Apollo cannot use inline_assembly for this.
  Apollo ships a native `printspoofer` command that implements the same technique
  and is the preferred path. The technique requires Print Spooler to be running
  (spoolsv.exe). On hardened systems where spoolsv.exe is disabled, PrintSpoofer
  will fail; use GodPotato or JuicyPotatoNG as alternatives.
gotchas: |
  NATIVE EXE — Apollo's inline_assembly will NOT run this. Use Apollo's native
  `printspoofer` command instead. Print Spooler service (spoolsv.exe) must be running.
  Works best on Windows 10/Server 2016-2019; some newer builds have additional mitigations.
  GodPotato has broader Windows version compatibility and is generally preferred.
related_ttps: [godpotato, juicypotatong, sharpup, standin]
alternatives: [godpotato, juicypotatong, sweetpotato]
common_args:
  -c:
    name: -c
    description: Command to execute as SYSTEM
    typical_values: ["cmd", "powershell", "net user backdoor P@ss123! /add"]
    required: true
  -i:
    name: -i
    description: Interactive mode — spawn an interactive shell
    typical_values: [flag-only]
  -h:
    name: -h
    description: Open new window (hide from current console)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# PrintSpoofer

A classic SeImpersonatePrivilege exploit by itm4n. PrintSpoofer uses a named pipe
impersonation trick to capture a SYSTEM token from the Print Spooler service and
execute commands in a SYSTEM context. The technique requires the Print Spooler to be
running and SeImpersonatePrivilege in the current token. Apollo ships this as a native
command, making the standalone binary less useful in Apollo engagements.

## Typical use cases
- Escalate from service account / IIS app pool to SYSTEM on older Windows versions
- Quick SYSTEM when Print Spooler is running and SeImpersonatePrivilege is present

## How Sage uses this
Sage uses Apollo's native `printspoofer` command rather than uploading the binary.
GodPotato is preferred for broader compatibility; PrintSpoofer is the fallback for
specific environments where Apollo's native command is preferred.

## Apollo-specific note
Apollo ships a native `printspoofer` command. Use that instead of uploading the binary.
See `mythic_agents/apollo.md` for the native command parameters.
