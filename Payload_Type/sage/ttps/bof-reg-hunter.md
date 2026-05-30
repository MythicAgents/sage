---
name: BOF-RegHunter
category: credential-access
subcategories: [registry-credential-hunt, in-process-registry, bof]
tradecraft_tags: [bof, registry, credentials, autorun, wdigest, autologon, athena, in-process]
mitre_attack:
  - id: T1552.002
    name: Unsecured Credentials — Credentials in Registry
source:
  url: https://github.com/gtworek/PSBits/tree/master/BOF
  license: Unknown
  maintained: true
binary_type: bof
binary_filename: RegHunter.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  In-process registry reads generate no process creation events. Registry key reads
  are not audited by default unless registry object access auditing is configured.
  The reads themselves are indistinguishable from normal application registry access.
usage_examples:
  - description: Hunt for credential-related registry values in-process
    args: "execute-bof RegHunter.x64.o"
  - description: Check specific credential registry locations
    args: "execute-bof RegHunter.x64.o HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon"
opsec_notes: |
  In-process registry reads via a BOF are essentially silent — no child process, no
  file I/O, no network. The BOF reads directly from the registry hive via NtQueryValueKey
  syscall. This is the stealthiest way to check common credential registry locations
  (AutoLogon credentials, WDigest UseLogonCredential, saved VPN credentials).
gotchas: |
  Apollo has no BOF runner — requires Athena. For Apollo, Seatbelt's RegistryAutoLogon
  check covers AutoLogon credential discovery (not in-process, but functional). The
  registry locations worth checking are well-known and documented in the credential
  hunting checklist TTP.
related_ttps: [seatbelt, credential-hunting-checklist, trustedsec-bofs]
alternatives: [seatbelt-registry-checks, manual-reg-query]
common_args: {}
last_updated: 2026-05-29
---

# BOF-RegHunter

A BOF for hunting credential-related registry values entirely in-process — no child
process, no reg.exe, no reg query (a cmd-line tool that's anomalous from service accounts).
Checks common credential storage locations (AutoLogon, WDigest UseLogonCredential,
VPN credentials, saved passwords) using direct registry API calls inside the BOF.

## Registry locations covered

| Location | What it reveals |
|----------|----------------|
| `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon` | AutoLogon username/password |
| `HKLM\SYSTEM\CurrentControlSet\Control\SecurityProviders\WDigest\UseLogonCredential` | WDigest state |
| `HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings\ProxyUser/ProxyPass` | Proxy credentials |
| `HKLM\SOFTWARE\OpenSSH\AgentPath` | SSH agent path |
| `HKLM\SYSTEM\CurrentControlSet\Services\...` | Service account credentials |

## Typical use cases
- Check AutoLogon credentials silently (common on kiosk/service machines)
- Verify WDigest state before deciding if LSASS dump will yield cleartext
- Enumerate any saved credential registry values in-process

## How Sage uses this
With Athena, BOF-RegHunter is a stealthy pre-LSASS check. Before committing to a full
LSASS dump (nanodump), checking AutoLogon and WDigest state in-process answers:
- Is there an AutoLogon password (skip LSASS dump if yes)?
- Is WDigest enabled (dump will yield cleartext if yes)?

## Apollo-specific note
BOF — requires Athena. For Apollo, Seatbelt's RegistryAutoLogon check provides equivalent
AutoLogon discovery.
