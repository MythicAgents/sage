---
name: GPP Password Discovery
category: credential-access
subcategories: [group-policy-preferences, sysvol-creds, cleartext-password]
tradecraft_tags: [gpp, group-policy-preferences, sysvol, cleartext-password, ms14-025, cpassword]
mitre_attack:
  - id: T1552.006
    name: Unsecured Credentials — Group Policy Preferences
source:
  url: https://adsecurity.org/?p=2288
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Reading GPP XML files from SYSVOL is standard domain behavior — all domain computers
  do this. No special detection signal. The decryption of cpassword is client-side.
  MS14-025 patched creation of new GPP passwords but did NOT remove existing ones.
usage_examples:
  - description: Find GPP cpassword entries in SYSVOL (PowerView)
    args: "Get-GPPPassword"
  - description: Native search for cpassword in SYSVOL
    args: "findstr /S /I cpassword \\\\<DOMAIN>\\sysvol\\<DOMAIN>\\Policies\\*.xml"
  - description: Decrypt a cpassword value (the AES key is publicly known)
    args: "(uses hardcoded AES key from MS14-025 disclosure: 4e9906e8fcb66cc9faf49310620ffee8f496e806cc057990209b09a433b66c1b)"
opsec_notes: |
  GPP password discovery requires only standard SYSVOL read access (every domain user
  has this). The SYSVOL is cached on all domain-joined machines. Reading XML files from
  SYSVOL generates minimal telemetry — standard file access events. This is one of the
  quietest credential access techniques.
gotchas: |
  MS14-025 (2014) patched Group Policy from CREATING new cpassword entries but did NOT
  force removal of existing ones. Many domains still have legacy GPP passwords dating
  from before 2014. Run this check early — it's low-risk, low-noise, and occasionally
  yields local admin credentials. The AES decryption key for cpassword is publicly
  documented (not a secret — Microsoft published it). Seatbelt's `CachedGPPPassword`
  check and SharpUp's `DomainGPPPassword` check both enumerate these.
related_ttps: [seatbelt, sharpup, grouper2, sharphound]
alternatives: [seatbelt-gpp, sharpup-gpp]
common_args: {}
last_updated: 2026-05-29
---

# GPP Password Discovery

Discovery and decryption of plaintext-equivalent passwords stored in Group Policy
Preferences (GPP) XML files on the SYSVOL share. Prior to MS14-025 (2014), Group Policy
Preferences could configure local accounts and scheduled tasks with passwords, which were
stored using AES encryption with a key that Microsoft later published. Any domain user can
read SYSVOL and decrypt these passwords.

## Typical use cases
- Find legacy local admin credentials from pre-2014 GPP configurations
- Quick low-noise credential check early in an engagement
- Find service account credentials configured in GPP scheduled tasks

## How Sage uses this
Sage checks for GPP passwords early via SharpUp's `DomainGPPPassword` or Seatbelt's
`CachedGPPPassword` checks. If found, the decrypted credentials are used for immediate
lateral movement or escalation. This check is free (no binary upload, standard SYSVOL read).

## Decryption

The cpassword value is AES-256-CBC encrypted with a key that Microsoft disclosed when
patching MS14-025. Any tool (PowerView `Get-GPPPassword`, Metasploit `post/windows/gather/credentials/gpp`,
or a Python one-liner) can decrypt it using the published key.

## Where to look

```
\\<DOMAIN>\SYSVOL\<DOMAIN>\Policies\{GPO-GUID}\Machine\Preferences\Groups\Groups.xml
\\<DOMAIN>\SYSVOL\<DOMAIN>\Policies\{GPO-GUID}\User\Preferences\Groups\Groups.xml
(also ScheduledTasks.xml, Services.xml, DataSources.xml, Printers.xml)
```
