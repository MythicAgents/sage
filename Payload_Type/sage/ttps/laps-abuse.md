---
name: LAPS Credential Access
category: credential-access
subcategories: [laps, local-admin-password, computer-object-attribute]
tradecraft_tags: [laps, local-admin, ms-mcs-admpwd, computer-attribute, lateral-movement]
mitre_attack:
  - id: T1552
    name: Unsecured Credentials
source:
  url: https://docs.microsoft.com/en-us/windows-server/identity/laps/laps-overview
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Reading the `ms-Mcs-AdmPwd` attribute (legacy LAPS) or the `msLAPS-Password`/
  `msLAPS-EncryptedPassword` attribute (Windows LAPS, Server 2022) from a computer
  object generates Event 4662 (Object access: read property) if object access auditing
  is configured on computer objects. SharpHound collects LAPS readability data — showing
  which principals can read each computer's LAPS password.
usage_examples:
  - description: Read LAPS password for a specific computer (PowerView)
    args: "Get-DomainComputer WINTERFELL -Properties ms-Mcs-AdmPwd,ms-Mcs-AdmPwdExpirationTime"
  - description: Read LAPS password via native LDAP
    args: "([adsisearcher]\"(&(objectClass=computer)(cn=WINTERFELL))\").FindOne().Properties['ms-Mcs-AdmPwd']"
  - description: Find all computers where current user can read LAPS password
    args: "Get-DomainComputer | Where-Object {$_.ms-Mcs-AdmPwd -ne $null}"
  - description: SharpHound identifies LAPS-readable computers in BloodHound
    args: "(BloodHound query: MATCH (u)-[:ReadLAPSPassword]->(c:Computer) WHERE u.name='ATTACKER@DOMAIN' RETURN c)"
opsec_notes: |
  Reading the LAPS attribute is an LDAP read — one query to the DC, minimal footprint.
  The access is only detectable if Directory Service Object Access auditing is enabled on
  computer objects (not enabled by default). PowerView / native LDAP query approach is
  identical. BloodHound's ReadLAPSPassword edge shows which accounts can read LAPS.
gotchas: |
  LAPS must be deployed (not all environments use it). Legacy LAPS stores in ms-Mcs-AdmPwd
  attribute (cleartext). Windows LAPS (2022+) stores in msLAPS-EncryptedPassword or
  msLAPS-Password — the encrypted variant requires the computer's DPAPI master key to
  decrypt. Verify which variant is deployed before reading the attribute.
related_ttps: [sharphound, bloodhound-ingest, powerview, seatbelt]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# LAPS Credential Access

Reading the Local Administrator Password Solution (LAPS) stored password from the
`ms-Mcs-AdmPwd` (legacy LAPS) or `msLAPS-Password` (Windows LAPS) attribute of a
computer object in Active Directory. LAPS stores the local administrator password in AD
so domain admins can manage it centrally — but any principal with read access to that
attribute can retrieve the plaintext password for local admin on that machine.

## Typical use cases
- Retrieve local admin password for a machine (lateral movement without LSASS touch)
- Stealthy credential access — LDAP read is less detectable than LSASS dump
- Lateral movement from a low-privilege domain user who has LAPS read rights

## How Sage uses this
SharpHound collects LAPS readability data — BloodHound shows `ReadLAPSPassword` edges.
When such an edge exists from a controlled principal to a target computer, Sage reads
the LAPS attribute directly via LDAP (PowerView or native ADSI) to retrieve the local
admin password without touching LSASS on the target.

This is one of the most stealthy lateral movement paths: no LSASS access, no binary
upload to the target, just one LDAP query — PROVIDED the query runs as a principal that can read it.

## Execution context — the silent-failure trap (CRITICAL)

`ms-Mcs-AdmPwd` / `msLAPS-Password` are CONFIDENTIAL attributes. AD returns them ONLY to a caller that holds
`ReadLAPSPassword` (or GenericAll/equivalent) on that computer object. If the identity running your LDAP/ADSI
query lacks that right, the query SUCCEEDS and returns the computer object **with the non-confidential attrs
(e.g. `ms-mcs-admpwdexpirationtime`) but WITHOUT the password** — and no error.

> An object that comes back with `ms-mcs-admpwdexpirationtime` but NO `ms-mcs-admpwd` means your QUERY
> CONTEXT is wrong — NOT that LAPS is absent. Fix the identity. Do NOT keep re-issuing the same read or
> blindly permuting ticket-cache LUIDs.

Run the read AS a principal that holds the right:
1. Identify a principal with the edge (BloodHound `ReadLAPSPassword`). It is often a GROUP, and membership
   can be transitive / cross-forest (a foreign-group membership).
2. Obtain a ticket/token for that principal and APPLY it into the query's execution context (`make_token`
   for a password/hash; or forge → `ticket_cache_add`/PTT for a ticket), THEN run the LDAP read in that
   context (e.g. `execute_assembly`/`powerpick` once the ticket is in the calling thread's cache).

## Cross-forest LAPS via a foreign-group membership

When a principal in one forest is an authentic member of a foreign group that holds
`ReadLAPSPassword` in the trusting forest, authenticate as that principal while preserving its real PAC group
memberships. Do not infer the edge from a benchmark recipe; require current directory or graph evidence.

> **CRITICAL — use the member's REAL ticket, NOT a forged golden ticket.** A golden ticket forged for
> a chosen account carries only the SIDs you put in it; it does NOT carry the account's authentic
> source-forest group membership, and you CANNOT inject the foreign group as an ExtraSID / SID-history —
> SID filtering on the FOREST trust strips foreign ExtraSIDs (that filtering is exactly why the legit
> foreign-group membership is the only path). The cross-forest referral grants the foreign-group rights
> ONLY when the PAC carries the genuine source-forest membership. Forging a golden ticket here is a common
> way this class of hop silently fails.

1. Obtain the real key for the source-forest member through an authorized, independently verified capability.
2. Request the member's **real TGT**, which carries authentic group memberships from the source DC.
3. Request the **cross-forest referral** / LDAP service ticket to the trusting forest with that TGT.
4. Read the target computer's LAPS attribute in that ticket context (run the LDAP/ADSI
   query under the applied ticket — a sacrificial process / `make_token`-equivalent, not the default token).
   A successful read proves the LAPS edge without prescribing the next attack family.

## LAPS Variants

| Variant | Attribute | Storage | Decryption needed |
|---------|-----------|---------|------------------|
| Legacy LAPS | `ms-Mcs-AdmPwd` | Cleartext | No — read directly |
| Windows LAPS (2022+) | `msLAPS-Password` | Cleartext | No — read directly |
| Windows LAPS Encrypted | `msLAPS-EncryptedPassword` | Encrypted | Yes — DPAPI computer key |

## BloodHound Cypher for LAPS access

```cypher
-- Find computers where attacker can read LAPS password
MATCH p=(u:User {name:'ATTACKER@DOMAIN'})-[:ReadLAPSPassword]->(c:Computer)
RETURN c.name, c.laps
```
