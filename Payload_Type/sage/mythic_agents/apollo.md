---
name: Apollo
mythic_payload_type: apollo
supported_os: [windows]
description: .NET-based Windows Mythic agent with disposable-AppDomain execution
author: MythicAgents
source_url: https://github.com/MythicAgents/Apollo
version_tested: 2.x (confirmed 2026-05-29 via get_all_commands_for_payloadtype)
binary_type_execution:
  .net-assembly:
    command: inline_assembly
    upload_required: true
    parameters_template:
      assembly_name: "<ChooseOne registered assembly name, or new>"
      assembly_file: "<Mythic file UUID returned from upload>"
      assembly_arguments: "<args string, space-separated>"
    fallback: null
  .net-assembly-injected:
    command: assembly_inject
    upload_required: true
    parameters_template:
      pid: "<target PID>"
      assembly_name: "<name>"
      assembly_file: "<uuid>"
      assembly_arguments: "<args>"
    fallback: null
  powershell-script:
    command: powershell_import
    upload_required: true
    parameters_template:
      file: "<Mythic file UUID>"
      existingFile: "<ChooseOne previously-imported script name>"
    fallback: null
  shellcode:
    command: shinject
    upload_required: true
    parameters_template:
      pid: "<target PID>"
      shellcode: "<Mythic file UUID>"
      shellcode-file-id: "<automation field — leave blank>"
    fallback: null
  bof:
    command: null
    upload_required: false
    parameters_template: {}
    fallback: |
      Apollo does not currently ship a BOF runner. Options:
      (a) Use the native command that achieves the same tradecraft (e.g. `dcsync`,
          `mimikatz` for credential access; `make_token`, `steal_token` for tokens).
      (b) Port the BOF to a .NET assembly (e.g. via Inceptor) and use inline_assembly.
      (c) Use a different Mythic agent that does ship a BOF runner (e.g. Athena).
  native-exe:
    command: null
    upload_required: false
    parameters_template: {}
    fallback: |
      Apollo does not have a generic "run an EXE" command (inline_assembly only
      runs .NET). Options: spawn-and-inject via `spawn`/`inject` if the EXE
      can be expressed as shellcode; or use a different Mythic agent.
  python-script:
    command: null
    upload_required: false
    parameters_template: {}
    fallback: Apollo cannot execute Python directly; Python tools must be re-implemented in C#.
native_capabilities:
  dcsync:
    command: dcsync
    parameters:
      domain: "<domain to sync from>"
      user: "<username; defaults to 'all'>"
      dc: "<specific DC FQDN; optional>"
    notes: |
      Native DCSync — no need to upload SharpKatz or run mimikatz. Requires
      DCSync rights (typically krbtgt or DA-equivalent ACL on the domain).
  mimikatz:
    command: mimikatz
    parameters:
      commands: "<array of mimikatz commands, e.g. ['sekurlsa::logonpasswords']>"
    notes: |
      Embedded Mimikatz. Pass each mimikatz command as an array entry.
      Output comes back as the agent response.
  pass_the_hash:
    command: pth
    parameters:
      credential: "<CredentialJson from agent credential store>"
      domain: "<domain>"
      user: "<user>"
      ntlm: "<NT hash>"
      aes128: "<optional AES128 key>"
      aes256: "<optional AES256 key>"
      run: "<optional command to launch in the new logon session>"
    notes: Spawns a process under the supplied domain credential material.
  token_steal:
    command: steal_token
    parameters:
      pid: "<target PID; defaults to winlogon.exe if absent>"
    notes: Steals a primary token from another process.
  token_make:
    command: make_token
    parameters:
      username: "<user>"
      password: "<password>"
      netOnly: "<true|false; default true>"
    notes: Creates a new logon session and applies it to the agent.
  token_revert:
    command: rev2self
    parameters: {}
    notes: Reverts to the Mythic agent's primary token.
  kerberos_ticket_inject:
    command: ticket_cache_add
    parameters:
      base64ticket: "<base64-encoded ticket>"
      luid: "<optional target LUID>"
    notes: Injects a Kerberos ticket into the current or specified logon session.
  kerberos_ticket_list:
    command: ticket_cache_list
    parameters:
      luid: "<optional target LUID>"
      getSystemTickets: "<true|false; default false>"
    notes: Lists Kerberos tickets in current or elevated logon sessions.
  kerberos_ticket_purge:
    command: ticket_store_purge
    parameters:
      serviceName: "<optional specific service>"
      all: "<true|false; default false>"
    notes: Removes ticket from the Mythic agent ticket store.
  printspoofer:
    command: printspoofer
    parameters: {}
    notes: Local privilege escalation via SeImpersonate (PrintSpoofer family).
  domain_controller_list:
    command: net_dclist
    parameters: {}
    notes: Lists domain controllers for the current or specified domain.
  share_enumeration:
    command: net_shares
    parameters:
      computer: "<optional target computer>"
    notes: Lists remote shares and accessibility.
file_upload_pattern: |
  1. Sage checks Mythic file store via get_all_uploaded_files for the binary
     (matched by binary_filename from the TTP file).
  2. If not in Mythic, Sage checks Payload_Type/sage/tools/<binary_filename>
     (operator's drop zone, mounted into the Sage container).
  3. If found locally, Sage uploads via register_file and receives a Mythic
     file UUID (see ensure_tool_uploaded).
  4. Subsequent issue_task_and_waitfor_task_output calls reference the UUID
     in the binary's parameter field (e.g. assembly_file for inline_assembly).
opsec_notes: |
  Apollo's inline_assembly uses a disposable AppDomain — the assembly is unloaded
  after execution. AMSI is bypassed by the loader. Assemblies are still subject to
  EDR signature scanning on .NET load; rename binaries before upload.
known_gaps:
  - bof (no BOF runner; use native commands or port BOF to assembly)
  - native-exe (no generic EXE runner)
  - python-script (no Python runtime)
last_updated: 2026-05-29
---

# Apollo

Apollo is the canonical .NET Mythic agent for Windows targets. Built around
.NET-assembly execution in a disposable AppDomain, Apollo is the easiest path
to running modern Windows post-exploitation tradecraft (GhostPack, Sliver-port
assemblies, etc.) through Mythic.

## Execution model
- .NET assemblies execute in a disposable AppDomain (AMSI bypassed at load)
- PowerShell scripts execute via `powershell_import` (cached for re-use)
- Shellcode injects via `shinject` (own process or remote)
- No BOF runner; native commands fill that gap for common BOF use cases

## Notable native commands
- `dcsync` — direct DCSync, no SharpKatz upload needed
- `mimikatz` — embedded Mimikatz; pass commands as array
- `pth` — pass-the-hash with full credential material
- `make_token` / `steal_token` / `rev2self` — token manipulation
- `ticket_cache_*` — Kerberos ticket inject/list/purge
- `printspoofer` — local SeImpersonate-based privesc
- `net_dclist`, `net_shares` — basic AD enumeration

## Upload workflow
For any .NET assembly Sage needs to execute (SharpHound, Rubeus, Certify,
etc.), the workflow is:
1. Sage calls `get_all_uploaded_files` to check if the binary is in Mythic's file store
2. If not, Sage checks the operator drop zone at `Payload_Type/sage/tools/<binary_filename>`
3. If found locally, Sage calls `register_file` to push the binary to Mythic
4. Sage receives the file UUID
5. Sage calls `issue_task_and_waitfor_task_output(command="inline_assembly", parameters={...}, callback_display_id=N)` with the UUID in `assembly_file`

## OPSEC considerations
- Rename binaries before upload (signature scanners catch literal tool names)
- inline_assembly is louder than native commands for the same outcome —
  prefer `dcsync`/`mimikatz`/`pth` over GhostPack alternatives when a native
  command covers the tradecraft
- AppDomain disposal helps but is not invisible to behavior-based EDR

## Known gaps
- **BOFs:** Apollo currently has no BOF runner. For tradecraft normally
  delivered as BOFs (nanodump, TrustedSec CS-Sit-Awareness BOFs), either use
  the closest native command (e.g. `mimikatz sekurlsa::minidump` instead of
  nanodump) or port the BOF to assembly.
- **Native EXE:** `inline_assembly` runs .NET only. Plain Windows PEs require
  a different execution primitive (typically `spawn` + shellcode wrapper).
- **Python:** Apollo cannot execute Python directly. Python tools must be
  re-implemented in C#.

## See also
- Athena (Mythic agent with BOF support)
- Poseidon (Linux/macOS sibling of Apollo)
- Merlin (Go-based cross-platform Mythic agent)
