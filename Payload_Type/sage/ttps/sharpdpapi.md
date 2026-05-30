---
name: SharpDPAPI
category: credential-access
subcategories: [dpapi, credential-decryption, certificate-extraction]
tradecraft_tags: [dpapi, credentials, certificates, masterkey, browser-creds, ghostpack]
mitre_attack:
  - id: T1555.003
    name: Credentials from Password Stores — Credentials from Web Browsers
  - id: T1552.002
    name: Unsecured Credentials — Credentials in Registry
source:
  url: https://github.com/GhostPack/SharpDPAPI
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: SharpDPAPI.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: user
network_required: false
detection_signal: |
  DPAPI calls to decrypt credentials generate minimal EDR signals — DPAPI is a legitimate
  Windows API used by many applications. LSASS is accessed for the masterkey decryption
  step (if using domain backup key path). Credential decryption from other users' profiles
  requires admin access and generates process-access events. SharpDPAPI-specific strings
  may be in some EDR rules.
usage_examples:
  - description: Decrypt all user credentials, vaults, and secrets (current user, no admin needed)
    args: "triage"
  - description: Decrypt credentials for all users using domain backup key
    args: "triage /pvk:domain_backup_key.pvk"
  - description: Extract Chromium-based browser credentials
    args: "credentials /target:chrome"
  - description: Dump certificates from user and machine stores
    args: "certificates /pvk:domain_backup_key.pvk"
  - description: Get the domain DPAPI backup key (requires DA)
    args: "backupkey /nowrap"
  - description: Decrypt a specific blob file
    args: "blob /target:C:\\Users\\user\\AppData\\...\\blob"
opsec_notes: |
  Most SharpDPAPI operations (triage at user level) use only DPAPI API calls — very
  low detection signal. The domain backup key (`backupkey`) command requires DA-level
  access and DCSync-equivalent RPC to the domain controller. Certificates and private keys
  extracted can be used for PKINIT (ForgeCert) or passed to other tools. The `/pvk:`
  path (domain backup key) decrypts ALL user DPAPI material across the domain.
gotchas: |
  The domain DPAPI backup key (`backupkey`) can decrypt any user's DPAPI-protected material
  across the entire domain — this is a very powerful persistence/access primitive. It's
  static until intentionally rotated (domains typically never rotate it). Certificate
  extraction (certificates subcommand) may yield CAs with private keys or user certificates
  for PKINIT. Browser credential decryption requires that the browser database files are
  accessible (user must be logged out or use volume shadow copy).
related_ttps: [mimikatz, sharpchromium, certify, forgecert]
alternatives: [mimikatz-dpapi, impacket-dpapi, pypykatz-dpapi]
common_args:
  triage:
    description: Decrypt all accessible DPAPI-protected secrets (user context)
    typical_values: [flag-only]
  certificates:
    description: Extract certificates and private keys from DPAPI-protected stores
    typical_values: [flag-only, "/pvk:backup.pvk"]
  backupkey:
    description: Retrieve the domain DPAPI backup private key (requires DA)
    typical_values: [flag-only, "/nowrap"]
  credentials:
    description: Decrypt Windows Credential Manager and browser credentials
    typical_values: [flag-only, "/target:chrome"]
  /pvk:
    description: Domain backup key PVK file (allows decrypting other users' DPAPI material)
    typical_values: ["domain_backup_key.pvk"]
  blob:
    description: Decrypt a specific DPAPI blob file
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpDPAPI

GhostPack's DPAPI (Data Protection API) decryption tool. DPAPI is Windows' built-in
credential protection API — it protects WiFi passwords, browser-saved credentials, RDP
credentials, private keys, certificates, and much more. SharpDPAPI can decrypt these
using either the current user's context (for secrets the current user owns) or the
domain DPAPI backup key (to decrypt any user's secrets across the domain). The domain
backup key is a highly-prized persistence primitive — it's static for the domain's lifetime.

## Typical use cases
- Extract browser-saved passwords and cookies (Chromium, Firefox, IE/Edge Legacy)
- Decrypt Windows Credential Manager entries and vaults
- Extract certificate private keys protected by DPAPI
- Retrieve the domain DPAPI backup key for persistent domain-wide credential access
- Decrypt DPAPI master keys after obtaining the raw master key file

## How Sage uses this
SharpDPAPI is used in post-escalation credential harvesting:
1. After gaining DA: `backupkey /nowrap` to extract the domain backup key (powerful persistence)
2. On user machines: `triage` to extract cached browser credentials and Windows vault entries
3. Certificate hunting: `certificates` to find any DPAPI-protected certs for PKINIT paths
4. The domain backup key, once extracted, enables decrypting any user's DPAPI material
   across the domain — feeds into ForgeCert workflow if CA key is DPAPI-protected

## Output
Text output listing decrypted DPAPI artifacts: credentials (username + password),
certificate subject/private key (PEM-format), or raw decrypted blob bytes.

## Full Reference

> Captured against SharpDPAPI v1.11.x, 2026-05-29. Source: https://github.com/GhostPack/SharpDPAPI README.

### Sub-commands

| Sub-command | Description |
|-------------|-------------|
| `triage` | Enumerate and decrypt accessible DPAPI credentials, vaults, and secrets |
| `backupkey` | Retrieve domain DPAPI backup key (RPC to DC; requires DA or backup-key read ACL) |
| `certificates` | Extract and decrypt DPAPI-protected certificates + private keys |
| `credentials` | Decrypt Windows Credential Manager entries |
| `vaults` | Decrypt Windows Vault entries |
| `rdg` | Decrypt Remote Desktop Gateway credentials |
| `keepass` | Decrypt KeePass 2.x database master password |
| `blob` | Decrypt a specific DPAPI blob |
| `ps` | Decrypt PowerShell credential objects |

### `/pvk:` flag (domain backup key path)

The domain DPAPI backup key allows decrypting master keys without the user's logon password.
Obtain with `backupkey /nowrap`:
```
SharpDPAPI.exe backupkey /nowrap
```
The printed PVK bytes can be saved to a file. Then use `/pvk:X.pvk` with any sub-command
to decrypt other users' DPAPI-protected material.

### Source for this reference

- https://github.com/GhostPack/SharpDPAPI (README)
- HarmJ0y blog: https://specterops.io/blog/2018/04/23/sharptools-sharpdpapi-and-sharpsniper/
- Version: v1.11.x as of 2026-05-29
