---
name: CredManBOF (Credential Manager BOF)
category: credential-access
subcategories: [credential-manager, bof, in-process-credman]
tradecraft_tags: [credential-manager, credman, bof, in-process, windows-vault, athena]
mitre_attack:
  - id: T1555.004
    name: Credentials from Password Stores — Windows Credential Manager
source:
  url: https://github.com/aas-n/craydentbof
  license: Unknown
  maintained: false
binary_type: bof
binary_filename: CredManBOF.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Windows Credential Manager API calls (CredEnumerate, CredRead) from the agent
  process are low-signal — identical to what many legitimate applications do to
  read stored credentials. No process creation, no disk writes. Behavioral EDR
  may flag credential enumeration from unusual processes.
usage_examples:
  - description: Enumerate all Windows Credential Manager entries in-process
    args: "execute-bof CredManBOF.x64.o"
  - description: Enumerate credential entries targeting a specific target name
    args: "execute-bof CredManBOF.x64.o <filter>"
opsec_notes: |
  BOF-based Credential Manager access is stealthier than Seatbelt's CredEnum check
  (which creates a child process via inline_assembly). The Credential Manager stores:
  - Windows Login credentials (stored via Credential Manager GUI)
  - Application passwords saved by apps via the Windows credential API
  - Network credentials (mapped drives, SharePoint, etc.)
  - Certificates and other secrets
  SharpDPAPI's `credentials` subcommand decrypts these; CredManBOF just enumerates
  them in-process without decryption.
gotchas: |
  Apollo has no BOF runner — requires Athena. For Apollo: use Seatbelt.exe CredEnum
  or SharpDPAPI credentials. CredManBOF enumerates but may not decrypt — full
  decryption requires DPAPI master keys (available to current user context by default,
  or domain backup key for other users).
related_ttps: [seatbelt, sharpdpapi, credential-hunting-checklist, trustedsec-bofs]
alternatives: [seatbelt-credenum, sharpdpapi-credentials]
common_args:
  filter:
    description: Optional filter for credential target name
    typical_values: ["", "<server_name>"]
last_updated: 2026-05-29
---

# CredManBOF (Credential Manager BOF)

A BOF for enumerating Windows Credential Manager entries in-process without spawning
a child process. Windows Credential Manager stores network passwords, application
credentials, and Windows Login credentials. The BOF enumerates all accessible entries
in the current user's credential store.

## Credential Manager Categories

| Type | What's stored |
|------|--------------|
| Windows Credentials | Network drive passwords, SharePoint, domain creds |
| Generic Credentials | Application-specific stored passwords |
| Certificate-based | Certificate private keys via credential API |

## Decryption Chain

CredManBOF enumerates → SharpDPAPI credentials decrypts:
```
# Apollo path:
Seatbelt.exe CredEnum WindowsCredentialFiles    (enumerate)
SharpDPAPI.exe credentials                       (decrypt all)
SharpDPAPI.exe triage                            (comprehensive vault+cred+master key dump)
```

## Apollo-specific note
BOF — requires Athena. For Apollo, Seatbelt's CredEnum and SharpDPAPI's credentials
subcommand provide equivalent functionality via inline_assembly.
