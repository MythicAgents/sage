---
name: SharpLdapSearch
category: recon
subcategories: [ldap-enumeration, ad-query, targeted-ldap]
tradecraft_tags: [ldap, dotnet, ad-enumeration, apollo-runnable, sharphound-alternative, targeted]
mitre_attack:
  - id: T1087.002
    name: Account Discovery — Domain Account
source:
  url: https://github.com/CrowdStrike/SharpLdapSearch
  license: Apache-2.0
  maintained: true
binary_type: .net-assembly
binary_filename: SharpLdapSearch.exe
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  LDAP queries against DC — same detection profile as all LDAP-based enumeration.
  Individual queries are low-noise; bulk enumeration generates a query burst. No
  SAMR or SMB enumeration — quieter than SharpHound's full collection.
usage_examples:
  - description: Find all users with a specific attribute
    args: "SharpLdapSearch.exe -d north.sevenkingdoms.local -f '(&(objectClass=user)(servicePrincipalName=*))'"
  - description: Find computers with unconstrained delegation
    args: "SharpLdapSearch.exe -d north.sevenkingdoms.local -f '(&(objectClass=computer)(userAccountControl:1.2.840.113556.1.4.803:=524288))'"
  - description: Find all Domain Admins
    args: "SharpLdapSearch.exe -d north.sevenkingdoms.local -f '(memberOf=CN=Domain Admins,CN=Users,DC=north,DC=sevenkingdoms,DC=local)'"
  - description: Custom properties output
    args: "SharpLdapSearch.exe -d north.sevenkingdoms.local -f '(objectClass=user)' -p samaccountname,mail,description"
opsec_notes: |
  SharpLdapSearch is a targeted, single-query LDAP tool — useful when only specific
  data is needed (not the full SharpHound collection). The .NET assembly runs via
  Apollo inline_assembly. For comprehensive attack-path data, SharpHound+BloodHound
  is required; SharpLdapSearch fills the gap for specific targeted lookups.
gotchas: |
  LDAP filter syntax must be correct — errors return no results without explanation.
  Attribute names must match AD attribute names exactly (case-insensitive but exact spelling).
  For delegation UAC flags, the bitwise filter format (`:1.2.840.113556.1.4.803:=`) is
  required — incorrect formats return no results silently.
related_ttps: [sharphound, powerview, sharpview, pyldapsearch]
alternatives: [powerview, sharpview, sharpdir, pyldapsearch]
common_args:
  -d:
    description: Target domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
    required: true
  -f:
    description: LDAP filter string
    typical_values: ["(&(objectClass=user)(servicePrincipalName=*))", "(objectClass=computer)"]
    required: true
  -p:
    description: Comma-separated properties to return
    typical_values: ["samaccountname,mail,description", "name,dnshostname,operatingsystem"]
  -s:
    description: Search scope (base, onelevel, subtree)
    typical_values: [subtree]
last_updated: 2026-05-29
---

# SharpLdapSearch

CrowdStrike's .NET LDAP search utility — targeted LDAP queries against Active Directory
in a clean, Apollo-compatible assembly. Accepts raw LDAP filter syntax and specified
attribute lists, returning structured output. The key use case is targeted lookups that
don't warrant a full SharpHound collection pass.

## Typical use cases
- Find kerberoastable or AS-REP-roastable accounts in-process
- Find delegation-configured computers/users with specific filters
- Quick targeted attribute lookups (description, servicePrincipalName, etc.)
- ACL-targeted queries for specific groups without loading PowerView

## How Sage uses this
SharpLdapSearch is Sage's targeted LDAP query tool when a full SharpHound collection
is unnecessary. Sage uses it for specific questions: "what users have ServicePrincipalName
set?", "which computers have unconstrained delegation?", "what are the members of
this group?"

## Output
Structured text output per search result, one attribute per line with attribute name.
